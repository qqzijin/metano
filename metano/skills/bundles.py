"""Skill bundles: multi-skill aliases for common workflows."""

import yaml
from .loader import SkillLoader
from ..paths import BUNDLES_FILE


class BundleLoader:
    def __init__(self):
        self.loader = SkillLoader()

    def _load_bundles(self) -> dict:
        if not BUNDLES_FILE.exists():
            return {"bundles": {}}
        try:
            return yaml.safe_load(BUNDLES_FILE.read_text()) or {"bundles": {}}
        except yaml.YAMLError:
            return {"bundles": {}}

    def _save_bundles(self, data: dict):
        BUNDLES_FILE.parent.mkdir(parents=True, exist_ok=True)
        BUNDLES_FILE.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True))

    def list_bundles(self) -> list[dict]:
        data = self._load_bundles()
        bundles = data.get("bundles", {})
        result = []
        for name, cfg in bundles.items():
            result.append({
                "name": name,
                "description": cfg.get("description", ""),
                "skills": cfg.get("skills", []),
            })
        return result

    def resolve_bundle(self, name: str) -> str:
        """Load all skills in a bundle and concatenate their content."""
        data = self._load_bundles()
        bundles = data.get("bundles", {})
        if name not in bundles:
            return ""
        cfg = bundles[name]
        skill_names = cfg.get("skills", [])
        parts = []
        for skill_name in skill_names:
            content = self.loader.get_content(skill_name)
            if content:
                parts.append(f"[Skill: {skill_name}]\n{content}")
        return "\n\n---\n\n".join(parts) if parts else ""

