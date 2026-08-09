"""Skill loader: discovers, parses, and caches skills from filesystem."""
import time
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from .validator import validate_frontmatter, validate_content
from metano.log import logger
SKILLS_DIR = Path.home() / '.claude' / 'metano' / 'skills'
BUNDLED_SKILLS_DIR = Path(__file__).parent.parent / 'skills_data'

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
            return None
        body = self._extract_body(raw)
        rel = path.relative_to(path.parent.parent.parent)
        parts = rel.parts
        category = parts[0] if len(parts) >= 3 else ''
        name = fm.get('name', path.parent.name)
        trigger = fm.get('trigger', f'/{name}')
        metadata = fm.get('metadata', {})
        return SkillRecord(name=name, description=fm.get('description', ''), version=fm.get('version', '1.0.0'), author=fm.get('author', ''), trigger=trigger, category=category, source=source, path=path, frontmatter=fm, body=body, metadata=metadata, warnings=warnings)

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
        return substitute(rec.body, vars_)

    def list_by_category(self) -> dict[str, list[SkillRecord]]:
        self.discover_all()
        by_cat: dict[str, list[SkillRecord]] = {}
        for rec in self._cache.values():
            cat = rec.category or 'uncategorized'
            by_cat.setdefault(cat, []).append(rec)
        return by_cat