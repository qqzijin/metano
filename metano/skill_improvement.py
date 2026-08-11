"""Skill improvement proposal generator.

Mirrors Hermes' class-level skill update priority (patch in-play skill →
extend existing umbrella → add a references/ support file → create a new
class-level skill), but every write is routed through the approval-gated
proposal pipeline so nothing is auto-applied.

Corrections are the first-class signal: a user correction is both a memory
signal ("who the user is") and a skill signal ("how to do this class of task").
This module turns recent corrections into proposals to patch the relevant
skill's knowledge — adding a pitfall or a references/ file — so the next
session starts already knowing the lesson.
"""
import json
import re
from .skills.loader import SkillLoader
from .skills.manager import SkillManager
from .evo_models import get_proposals, add_proposal

# Only pinned skills are exempt from proposals: proposals are approval-gated
# (the user consents before anything is applied), so bundled/user skills are
# fair game as long as the user approves. A pinned skill means the user said
# "never touch this" — it is skipped entirely.
PROTECTED_SOURCES = set()


def _tokenize(text: str) -> set[str]:
    """Tokenize a mixed zh/en string, keeping meaningful terms."""
    text = (text or '').lower()
    zh = re.findall(r'[一-鿿]{2,}', text)
    en = [t for t in re.split(r'[^a-z0-9]+', text) if len(t) >= 3]
    return set(zh) | set(en)


# Domain → skill keywords mapping. Chinese correction domains are hard to
# match against English skill names by token overlap alone, so a small
# curated map bridges the two languages for the most common correction
# signals (mirrors the keywords already used in behavior_analyzer).
_DOMAIN_SKILL_KEYWORDS = {
    '验证': ['verif', 'test', 'curl', 'debug', 'code-review'],
    'curl': ['verif', 'test', 'debug', 'code-review'],
    '测试': ['test', 'tdd', 'verif', 'debug'],
    '前端': ['design', 'web', 'claude-design', 'sketch'],
    '界面': ['design', 'web', 'claude-design', 'sketch', 'ui'],
    'UI': ['design', 'web', 'claude-design', 'sketch', 'ui'],
    '页面': ['design', 'web', 'claude-design', 'sketch'],
    '后端': ['code-review', 'debug', 'plan', 'spike'],
    'API': ['code-review', 'github', 'api', 'debug'],
    '接口': ['api', 'code-review', 'github', 'debug'],
    '数据库': ['sql', 'data', 'code-review'],
    '字段': ['sql', 'data', 'code-review'],
    '类型': ['code-review', 'type', 'typescript'],
    'TS': ['type', 'typescript', 'code-review'],
    'hooks': ['type', 'typescript', 'code-review'],
    '文档': ['writing-plans', 'plan', 'obsidian'],
    '注释': ['writing-plans', 'code-review'],
    '重复': ['code-review', 'debug'],
    '依赖': ['plan', 'spike', 'debug'],
    '性能': ['debug', 'code-review', 'plan'],
    '架构': ['architecture', 'plan', 'excalidraw'],
    '设计': ['design', 'claude-design', 'sketch', 'architecture-diagram'],
}


def _domain_match(correction: str, rec) -> int:
    """Score overlap between correction domains and a skill's identity."""
    score = 0
    for kw, skill_keywords in _DOMAIN_SKILL_KEYWORDS.items():
        if kw in correction:
            target = f"{rec.name} {rec.description} {rec.category}".lower()
            for sk in skill_keywords:
                if sk in target:
                    score += 2
    return score


def find_relevant_skills(correction_text: str, limit: int = 3) -> list:
    """Find unprotected skills whose identity overlaps the correction.

    Returns up to ``limit`` skills ranked by combined lexical + domain score.
    """
    loader = SkillLoader()
    skills = loader.discover_all()
    if not skills:
        return []
    kw = _tokenize(correction_text)
    if not kw:
        return []
    manager = SkillManager()
    scored = []
    for rec in skills:
        if manager.is_pinned(rec.name):
            continue
        target = f"{rec.name} {rec.description} {rec.category} {rec.trigger}".lower()
        overlap = len(kw & _tokenize(target))
        for k in kw:
            if k in target:
                overlap += 3
        domain = _domain_match(correction_text, rec)
        total = overlap + domain
        if total > 0:
            scored.append((total, rec))
    scored.sort(key=lambda x: -x[0])
    return [rec for _, rec in scored[:limit]]


def propose_skill_improvements(corrections: list[dict], source: str = 'immediate_learn') -> dict:
    """Generate approval-gated proposals to improve skills from corrections.

    For each correction, find the relevant (unprotected) skills and create a
    pending proposal that will add a references/ file capturing the lesson
    (Hermes' preference-order option 3: extend an existing umbrella with a
    support file). Deduplicates against existing pending proposals.
    """
    existing = get_proposals()
    existing_keys = {(p['proposal_type'], p['content']) for p in existing}
    created = []
    for corr in corrections:
        content = (corr.get('content') or '').strip()
        if not content:
            continue
        matches = find_relevant_skills(content)
        if not matches:
            # Root-cause gap: a correction in a domain with NO matching skill
            # used to be silently dropped — the system could never grow a new
            # skill. Surface it as a "new skill" suggestion so the loop closes:
            # discover missing capability → propose → approve → manager.create.
            ptype = 'new_skill_suggestion'
            pcontent = f"建议新建技能: {content[:60]}"
            if (ptype, pcontent) in existing_keys:
                continue
            detail = json.dumps({
                'correction': content[:300],
                'reason': 'no existing skill matches this correction',
            }, ensure_ascii=False)
            add_proposal(ptype, pcontent, detail, source=source)
            existing_keys.add((ptype, pcontent))
            created.append({'skill': '(new)', 'proposal': pcontent})
            continue
        for rec in matches:
            ptype = 'skill_improvement'
            pcontent = f"为技能 '{rec.name}' 补充经验: {content[:60]}"
            if (ptype, pcontent) in existing_keys:
                continue
            detail = json.dumps({
                'skill': rec.name,
                'category': rec.category,
                'source': rec.source,
                'correction': content[:300],
            }, ensure_ascii=False)
            add_proposal(ptype, pcontent, detail, source=source)
            existing_keys.add((ptype, pcontent))
            created.append({'skill': rec.name, 'proposal': pcontent})
    return {'proposals_created': len(created), 'proposals': created}


def apply_skill_improvement(content: str, detail: str) -> dict:
    """Execute an approved skill_improvement proposal.

    Adds a references/<topic>.md file under the target skill capturing the
    correction-derived lesson, and adds a one-line pointer in the skill's
    SKILL.md body (Hermes' 'add a support file under an existing umbrella').
    Refuses to touch protected skills.
    """
    try:
        detail_dict = json.loads(detail) if detail else {}
    except (json.JSONDecodeError, ValueError):
        detail_dict = {}
    skill_name = detail_dict.get('skill', '')
    correction = detail_dict.get('correction', content)
    if not skill_name:
        return {'status': 'skipped', 'reason': 'no skill name in detail'}

    manager = SkillManager()
    rec = manager.loader.find_by_name(skill_name)
    if not rec:
        return {'status': 'skipped', 'reason': f"skill '{skill_name}' not found"}
    # Protected (bundled/pinned) skills must not be mutated by the autonomous
    # evolution system. is_pinned alone missed bundled skills (0 pinned), so an
    # approved proposal could write references/ + SKILL.md pointers into the
    # git-tracked source tree — use is_protected instead.
    if manager.is_protected(skill_name):
        return {'status': 'skipped', 'reason': f"skill '{skill_name}' is protected"}

    skill_dir = rec.path.parent
    refs_dir = skill_dir / 'references'
    refs_dir.mkdir(parents=True, exist_ok=True)
    import time
    fname = f'lesson-{int(time.time())}.md'
    ref_path = refs_dir / fname
    ref_path.write_text(
        f"# 学习到的经验 (auto-learned)\n\n"
        f"来源: 会话纠正\n\n"
        f"{correction}\n",
        encoding='utf-8',
    )

    # Add a one-line pointer in SKILL.md body (idempotent per file).
    # force=False: protected skills were already rejected above; a non-protected
    # skill still needs the normal protection check honoured.
    body = rec.body
    if f"references/{fname}" not in body:
        pointer = f"- 经验补充: `references/{fname}`（自动学习）"
        anchor = '## Pitfalls'
        if anchor in body:
            manager.patch(skill_name, anchor, f'{anchor}\n{pointer}')
        else:
            manager.edit(skill_name, body.rstrip() + f'\n\n{pointer}\n')

    return {'status': 'applied', 'skill': skill_name, 'references_file': str(ref_path)}
