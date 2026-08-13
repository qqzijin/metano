#!/usr/bin/env python3
"""Generate the bundled SKILL.md SHA-256 trust whitelist.

Scans every ``SKILL.md`` under ``metano/skills_data`` and writes
``metano/skills/_bundled_hashes.py`` — a module mapping skills_data-relative
paths to the SHA-256 of the pristine bundled file. ``SkillLoader`` refuses to
load any ``trust: bundled`` skill whose hash is missing or does not match
(audit P2-2): tampered or unknown bundled skills are fail-closed.

Usage (from the repo root):

    python3 scripts/generate_skill_hashes.py

Run this whenever a bundled SKILL.md is added, removed, or edited, then sync
the generated module to the runtime (``bash sync_runtime.sh``).
"""
import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DATA = REPO_ROOT / 'metano' / 'skills_data'
OUT = REPO_ROOT / 'metano' / 'skills' / '_bundled_hashes.py'

HEADER = '''"""Bundled SKILL.md SHA-256 trust whitelist (AUTO-GENERATED).

Maps skills_data-relative paths to the SHA-256 of the pristine bundled
SKILL.md. ``SkillLoader`` refuses to load any ``trust: bundled`` skill whose
hash is missing from this map or does not match the on-disk content — tampered
or unknown bundled skills are rejected at load time (audit P2-2, fail-closed).

Do not edit by hand. Regenerate with:

    python3 scripts/generate_skill_hashes.py

Then sync the runtime copy: ``bash sync_runtime.sh``.
"""

BUNDLED_SKILL_HASHES = {
'''

FOOTER = '''}
'''


def main() -> int:
    if not SKILLS_DATA.is_dir():
        print(f'ERROR: skills_data not found: {SKILLS_DATA}', file=sys.stderr)
        return 1

    entries = sorted(
        (p.relative_to(SKILLS_DATA).as_posix(), p)
        for p in SKILLS_DATA.rglob('SKILL.md')
    )
    if not entries:
        print('ERROR: no SKILL.md found under skills_data', file=sys.stderr)
        return 1

    lines = [HEADER]
    for rel, p in entries:
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        lines.append(f'    {rel!r}: {digest!r},\n')
    lines.append(FOOTER)

    OUT.write_text(''.join(lines))
    print(f'wrote {OUT} with {len(entries)} bundled SKILL.md hashes')
    print(f'source tree: {SKILLS_DATA}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
