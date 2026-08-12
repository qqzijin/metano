"""Skill manager: CRUD operations for skills."""

import yaml
from pathlib import Path
from .loader import SKILLS_DIR, SkillLoader
from .validator import validate_frontmatter, validate_content, validate_skill_ident


class SkillManager:
    def __init__(self):
        self.loader = SkillLoader()

    def is_protected(self, name: str) -> bool:
        """A skill is protected when it must not be modified by autonomous
        background processes (the evolution system) without explicit consent.

        Mirrors Hermes' protected-skill rules: bundled skills (shipped with the
        system), hub-installed skills, pinned skills, and user-owned skills are
        off-limits to autonomous writes. Only the user, in a foreground session,
        can change a protected skill.
        """
        rec = self.loader.find_by_name(name)
        if not rec:
            return False
        return self._is_protected(rec)

    def is_pinned(self, name: str) -> bool:
        """Pinned skills are exempt even from approval-gated proposals:
        the user explicitly declared them off-limits."""
        rec = self.loader.find_by_name(name)
        if not rec:
            return False
        return self._is_pinned(rec)

    def _is_pinned(self, rec) -> bool:
        # Pinned via frontmatter metadata.pinned: true (or hermes-style metadata)
        metadata = rec.metadata or {}
        if metadata.get('pinned'):
            return True
        hermes = metadata.get('hermes') if isinstance(metadata, dict) else None
        if isinstance(hermes, dict) and hermes.get('pinned'):
            return True
        return False

    def _is_protected(self, rec) -> bool:
        if rec.source == 'bundled':
            return True
        return self._is_pinned(rec)

    def create(self, name: str, category: str, description: str, content: str,
               version: str = "1.0.0", author: str = "") -> dict:
        """Create a new skill. Returns result dict."""
        # Security (M-04): validate name + category before touching the
        # filesystem, then enforce directory containment and reject symlinks.
        ident_err = validate_skill_ident(name, category)
        if ident_err:
            return {"error": ident_err}
        skill_dir = (SKILLS_DIR / category / name).resolve()
        if not skill_dir.is_relative_to(SKILLS_DIR.resolve()):
            return {"error": f"Skill path escapes skills directory: {skill_dir!s}"}
        if skill_dir.is_symlink() or any(
            p.is_symlink() for p in skill_dir.parents if p != SKILLS_DIR.resolve() and SKILLS_DIR.resolve() in p.parents
        ):
            return {"error": "Skill path resolves through a symlink; refusing to write"}
        # Build SKILL.md content
        fm = {
            "name": name,
            "description": description,
            "version": version,
            "author": author,
            "trigger": f"/{name}",
            "category": category,
        }
        fm_text = yaml.dump(fm, default_flow_style=False, allow_unicode=True)
        skill_md = f"---\n{fm_text}---\n\n{content}"

        # Validate
        parsed, warnings = validate_frontmatter(skill_md)
        if parsed is None:
            return {"error": "Validation failed", "warnings": warnings}
        # C6: surface dangerous-command warnings from the skill body too
        # (validate_content was dead code; now it feeds the write path).
        content_warnings = validate_content(content or '')
        if content_warnings:
            warnings = (warnings or []) + content_warnings

        # Write file (skill_dir was validated above).
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_path = skill_dir / "SKILL.md"
        skill_path.write_text(skill_md)

        # Invalidate cache
        self.loader.discover_all(force=True)

        return {"status": "created", "name": name, "path": str(skill_path), "warnings": warnings}

    def edit(self, name: str, content: str, force: bool = False) -> dict:
        """Replace the body content of an existing skill.

        Protected skills (bundled / pinned) are refused unless ``force=True``.
        ``force`` is reserved for approved proposals (the user consented to
        the specific change), never for autonomous background writes.
        """
        rec = self.loader.find_by_name(name)
        if not rec:
            return {"error": f"Skill '{name}' not found"}
        if self._is_protected(rec) and not force:
            return {"error": f"Skill '{name}' is protected (source={rec.source}); autonomous modification refused"}

        # Rebuild SKILL.md with new body
        fm_text = yaml.dump(rec.frontmatter, default_flow_style=False, allow_unicode=True)
        skill_md = f"---\n{fm_text}---\n\n{content}"

        parsed, warnings = validate_frontmatter(skill_md)
        if parsed is None:
            return {"error": "Validation failed", "warnings": warnings}
        # C6: dangerous-command check on the edited body.
        content_warnings = validate_content(content or '')
        if content_warnings:
            warnings = (warnings or []) + content_warnings

        rec.path.write_text(skill_md)
        self.loader.discover_all(force=True)

        return {"status": "edited", "name": name, "warnings": warnings}

    def patch(self, name: str, old_string: str, new_string: str, force: bool = False) -> dict:
        """Find and replace a string in the skill body.

        ``force=True`` is reserved for approved proposals (user consent).
        """
        rec = self.loader.find_by_name(name)
        if not rec:
            return {"error": f"Skill '{name}' not found"}

        if old_string not in rec.body:
            return {"error": f"old_string not found in skill body"}

        new_body = rec.body.replace(old_string, new_string)
        return self.edit(name, new_body, force=force)

    def delete(self, name: str) -> dict:
        """Delete a skill (only unprotected user skills)."""
        rec = self.loader.find_by_name(name)
        if not rec:
            return {"error": f"Skill '{name}' not found"}

        if self._is_protected(rec):
            return {"error": f"Cannot delete protected skill '{name}' (source={rec.source})"}

        # Remove the skill directory
        skill_dir = rec.path.parent
        if skill_dir.exists():
            import shutil
            shutil.rmtree(skill_dir)

        self.loader.discover_all(force=True)
        return {"status": "deleted", "name": name}

    def info(self, name: str) -> dict:
        """Get detailed info about a skill."""
        rec = self.loader.find_by_name(name)
        if not rec:
            return {"error": f"Skill '{name}' not found"}

        return {
            "name": rec.name,
            "description": rec.description,
            "version": rec.version,
            "author": rec.author,
            "trigger": rec.trigger,
            "category": rec.category,
            "source": rec.source,
            "path": str(rec.path),
            "body_length": len(rec.body),
            "warnings": rec.warnings,
        }