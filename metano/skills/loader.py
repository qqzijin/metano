"""Skill loader: discovers, parses, and caches skills from filesystem."""
import hashlib
import re
import time
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from .validator import validate_frontmatter, validate_content
from ._bundled_hashes import BUNDLED_SKILL_HASHES
from metano.log import logger
from ..paths import SKILLS_DIR

BUNDLED_SKILLS_DIR = Path(__file__).parent.parent / 'skills_data'

# ---------------------------------------------------------------------------
# Hermes / Claude-Code -> metano tool contract mapping.
#
# Many bundled SKILL.md bodies were authored for Hermes Agent / Claude Code and
# reference tool names metano does not expose (terminal, read_file, web_extract,
# delegate_task, browser_vision, ...). Rather than editing every skill body, we
# detect those tool names at load time and prepend a short mapping note so the
# model reaches for metano's real tools. Kept in this module so both the loader
# and the gateway router get the same behaviour.
# ---------------------------------------------------------------------------

HERMES_TOOL_MAPPING = """## 工具映射：本技能部分指令基于 Hermes/Claude-Code 工具名，metano 中请使用以下替代

| 技能正文中的工具 | metano 中的替代 |
|---|---|
| `terminal` / `terminal(...)` | `code_run(language="shell", code=...)` |
| `execute_code` | `code_run(language="python", code=...)` |
| `read_file` / `write_file` / `patch` / `search_files` | 无直接等价工具；用 `code_run(language="shell", code=...)` 执行 `cat` / `printf` / `sed` / `grep` / `find` 完成文件读写与搜索 |
| `web_extract(urls=[...])` | `web_search_tavily(query=...)`（搜索）或 `browser_get_content(url=...)`（抓取指定 URL 正文） |
| `delegate_task(...)` | `agent_spawn(task=..., model=..., timeout=...)`（异步子任务；用 `agent_status(task_id=...)` / `agent_result(task_id=...)` 获取结果） |
| `browser_vision` | 无直接等价；用 `browser_screenshot(...)` 截图后 `image_describe(...)` 描述，或用 `browser_get_content(url=...)` 读取页面文本 |

注意：`code_run` 运行在沙箱中（禁网络代理、禁危险命令），shell 模式禁止 `rm -rf /`、`curl ... | sh` 等操作。
"""

# Tool-name -> detection regex. `patch` is matched only in call form `patch(`
# so common prose ("apply a patch") does not trigger the note.
_HERMES_TOOL_PATTERNS = {
    'terminal': re.compile(r'\bterminal\b'),
    'execute_code': re.compile(r'\bexecute_code\b'),
    'read_file': re.compile(r'\bread_file\b'),
    'write_file': re.compile(r'\bwrite_file\b'),
    'patch': re.compile(r'\bpatch\s*\('),
    'search_files': re.compile(r'\bsearch_files\b'),
    'web_extract': re.compile(r'\bweb_extract\b'),
    'delegate_task': re.compile(r'\bdelegate_task\b'),
    'browser_vision': re.compile(r'\bbrowser_vision\b'),
}


def tool_mapping_note(body: str) -> str:
    """Return the metano tool-mapping note if the body references Hermes tools.

    Empty string when the body is already metano-native — the mapping note is
    only prepended to keep prompts lean for skills that need it.
    """
    if not body:
        return ''
    for pattern in _HERMES_TOOL_PATTERNS.values():
        if pattern.search(body):
            return HERMES_TOOL_MAPPING
    return ''

@dataclass
class SkillRecord:
    name: str
    description: str
    version: str = '1.0.0'
    author: str = ''
    trigger: str = ''
    category: str = ''
    source: str = ''
    path: Path = field(default_factory=lambda: Path())
    frontmatter: dict = field(default_factory=dict)
    body: str = ''
    metadata: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

class SkillLoader:

    def __init__(self):
        self._cache: dict[str, SkillRecord] = {}
        self._cache_ts: float = 0
        self._cache_ttl: float = 60.0

    def discover_all(self, force: bool=False) -> list[SkillRecord]:
        if not force and self._cache and (time.time() - self._cache_ts < self._cache_ttl):
            return list(self._cache.values())
        skills: dict[str, SkillRecord] = {}
        if BUNDLED_SKILLS_DIR.exists():
            for rec in self._scan_tree(BUNDLED_SKILLS_DIR, source='bundled'):
                skills[rec.name] = rec
        SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        for rec in self._scan_tree(SKILLS_DIR, source='user'):
            skills[rec.name] = rec
        self._cache = skills
        self._cache_ts = time.time()
        return list(skills.values())

    def _scan_tree(self, root: Path, source: str) -> list[SkillRecord]:
        records = []
        for skill_md in root.rglob('SKILL.md'):
            rec = self._parse_skill(skill_md, source)
            if rec:
                records.append(rec)
        return records

    def _parse_skill(self, path: Path, source: str) -> SkillRecord | None:
        try:
            raw = path.read_text()
        except Exception:
            logger.exception()
            return None
        fm, warnings = validate_frontmatter(raw)
        if fm is None:
            # Log instead of silently dropping the skill — a broken SKILL.md used
            # to disappear with no trace (e.g. research-paper-writing exceeded
            # the size limit and vanished from skills_list).
            from metano.log import logger as _lg
            _lg.warning('[skills] 技能加载失败(跳过): %s — %s', path, warnings or 'frontmatter 无效')
            return None
        # Trust enforcement (audit P2-2): a `trust: bundled` skill must match the
        # shipped SHA-256 whitelist. Tampered or unknown bundled skills are dropped
        # fail-closed with a HASH_MISMATCH warning; `user` / `verified` trust levels
        # keep the previous permissive behaviour.
        if (fm.get('trust') or '').lower() == 'bundled' and not self._verify_bundled_hash(path, raw):
            return None
        body = self._extract_body(raw)
        rel = path.relative_to(path.parent.parent.parent)
        parts = rel.parts
        category = parts[0] if len(parts) >= 3 else ''
        name = fm.get('name', path.parent.name)
        trigger = fm.get('trigger', f'/{name}')
        metadata = fm.get('metadata', {})
        return SkillRecord(name=name, description=fm.get('description', ''), version=fm.get('version', '1.0.0'), author=fm.get('author', ''), trigger=trigger, category=category, source=source, path=path, frontmatter=fm, body=body, metadata=metadata, warnings=warnings)

    @staticmethod
    def _verify_bundled_hash(path: Path, raw: str) -> bool:
        """Verify a ``trust: bundled`` skill against the SHA-256 whitelist.

        Returns True when the on-disk content matches the pristine hash shipped
        with the whitelist. Logs a ``HASH_MISMATCH`` warning and returns False
        otherwise — bundled skills are fail-closed: any bundled skill that is
        tampered with or absent from the whitelist is refused at load time.
        """
        try:
            rel_key = path.relative_to(BUNDLED_SKILLS_DIR).as_posix()
        except ValueError:
            rel_key = None
        if rel_key is None:
            logger.warning('[skills] HASH_MISMATCH: bundled 技能在 skills_data 之外(拒绝加载): %s', path)
            return False
        expected = BUNDLED_SKILL_HASHES.get(rel_key)
        if expected is None:
            logger.warning('[skills] HASH_MISMATCH: bundled 技能不在白名单(拒绝加载): %s (rel=%s)', path, rel_key)
            return False
        digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()
        if digest != expected:
            logger.warning('[skills] HASH_MISMATCH: bundled 技能哈希不符(拒绝加载): %s (rel=%s) — 内容可能被篡改', path, rel_key)
            return False
        return True

    def _extract_body(self, raw: str) -> str:
        first = raw.find('---')
        if first < 0:
            return raw
        second = raw.find('---', first + 3)
        if second < 0:
            return raw
        return raw[second + 3:].strip()

    def find_by_name(self, name: str) -> SkillRecord | None:
        self.discover_all()
        return self._cache.get(name)

    def find_by_trigger(self, trigger: str) -> SkillRecord | None:
        self.discover_all()
        t = trigger if trigger.startswith('/') else f'/{trigger}'
        for rec in self._cache.values():
            if rec.trigger == t or rec.trigger == trigger or f'/{rec.name}' == t:
                return rec
        return None

    def get_content(self, name: str, variables: dict | None=None) -> str:
        rec = self.find_by_name(name)
        if not rec:
            return ''
        from .template import substitute
        vars_ = {'SKILL_DIR': str(rec.path.parent)}
        if variables:
            vars_.update(variables)
        content = substitute(rec.body, vars_)
        note = tool_mapping_note(content)
        if note:
            content = f'{note}\n\n{content}'
        return content
