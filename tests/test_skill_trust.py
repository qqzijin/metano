"""Skill trust mechanism tests (audit P2-2).

Covers the bundled SKILL.md SHA-256 whitelist enforcement in ``SkillLoader``:

  (a) a pristine bundled skill loads normally;
  (b) a tampered bundled skill (content changed) is refused with a
      ``HASH_MISMATCH`` warning;
  (b2) a bundled skill that is not in the whitelist at all is refused
      (fail-closed);
  (c) a ``trust: user`` skill is not restricted by the bundled hash whitelist.
"""
import logging
from pathlib import Path

import metano.skills.loader as loader_mod


def _write_skill(root: Path, rel_dir: str, name: str, trust: str, body: str = "plain body\n") -> Path:
    d = root / rel_dir / name
    d.mkdir(parents=True, exist_ok=True)
    p = d / "SKILL.md"
    p.write_text(
        f"---\ntrust: {trust}\nname: {name}\ndescription: test skill\n---\n\n{body}",
        encoding="utf-8",
    )
    return p


def test_pristine_bundled_skill_loads():
    """(a) An unmodified bundled SKILL.md passes the hash whitelist."""
    loader = loader_mod.SkillLoader()
    rec = loader.find_by_name("notion")
    assert rec is not None
    assert rec.source == "bundled"

    # Every shipped bundled skill must clear the whitelist, none dropped.
    recs = loader.discover_all(force=True)
    bundled = [r for r in recs if r.source == "bundled"]
    assert len(bundled) == len(loader_mod.BUNDLED_SKILL_HASHES)


def test_tampered_bundled_skill_rejected_with_hash_mismatch(tmp_path, monkeypatch, caplog):
    """(b) A bundled SKILL.md with modified content is refused + HASH_MISMATCH."""
    src = loader_mod.BUNDLED_SKILLS_DIR / "productivity" / "notion" / "SKILL.md"
    assert src.exists()

    # Copy the pristine file and append a comment — content no longer matches
    # the shipped hash, while the frontmatter (incl. trust: bundled) stays valid.
    dst = tmp_path / "skills_data" / "productivity" / "notion" / "SKILL.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(src.read_text(encoding="utf-8") + "\n# tampered: injected line\n", encoding="utf-8")

    monkeypatch.setattr(loader_mod, "BUNDLED_SKILLS_DIR", tmp_path / "skills_data")
    loader = loader_mod.SkillLoader()

    with caplog.at_level(logging.WARNING, logger="metano"):
        recs = loader.discover_all(force=True)
    assert "notion" not in [r.name for r in recs]
    assert "HASH_MISMATCH" in caplog.text


def test_bundled_skill_not_in_whitelist_rejected(tmp_path, monkeypatch, caplog):
    """(b2) A bundled skill absent from the whitelist is refused (fail-closed)."""
    p = _write_skill(tmp_path / "skills_data", "productivity", "brand-new", trust="bundled")
    assert p.exists()
    monkeypatch.setattr(loader_mod, "BUNDLED_SKILLS_DIR", tmp_path / "skills_data")
    loader = loader_mod.SkillLoader()

    with caplog.at_level(logging.WARNING, logger="metano"):
        recs = loader.discover_all(force=True)
    assert "brand-new" not in [r.name for r in recs]
    assert "HASH_MISMATCH" in caplog.text


def test_user_skill_not_hash_restricted(tmp_path, monkeypatch):
    """(c) A `trust: user` skill loads regardless of the bundled whitelist."""
    user_root = tmp_path / "skills"
    monkeypatch.setattr(loader_mod, "SKILLS_DIR", user_root)
    p = _write_skill(user_root, "productivity", "trusted-user-skill", trust="user")
    assert p.exists()

    loader = loader_mod.SkillLoader()
    rec = loader.find_by_name("trusted-user-skill")
    assert rec is not None
    assert rec.source == "user"
