"""Skill validator: frontmatter parsing and security checks."""

import re
import yaml

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_SKILL_CONTENT_CHARS = 100_000
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
CATEGORY_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")


def validate_skill_ident(name: str, category: str) -> str | None:
    """Validate skill ``name`` and ``category`` as path-safe identifiers.

    Returns an error string, or None when both are valid. Used before any
    path construction so a caller can never smuggle ``/``, ``..``, absolute
    paths or other separators into the on-disk location (M-04).
    """
    if not name or len(name) > MAX_NAME_LENGTH:
        return f"Invalid skill name: {name!r}"
    if not NAME_PATTERN.match(name):
        return f"Invalid skill name (must match {NAME_PATTERN.pattern}): {name!r}"
    if not category or not CATEGORY_PATTERN.match(category):
        return f"Invalid skill category (must match {CATEGORY_PATTERN.pattern}): {category!r}"
    return None

DANGEROUS_PATTERNS = [
    r"rm\s+-rf\s+/",
    r"sudo\s+",
    r"\b__import__\b",
]


def validate_frontmatter(raw: str) -> tuple[dict | None, list[str]]:
    """Parse and validate YAML frontmatter from a SKILL.md file.

    Returns (parsed_dict, warnings). parsed_dict is None on hard errors.
    """
    warnings = []

    if not raw.startswith("---"):
        warnings.append("SKILL.md must start with ---")
        return (None, warnings)

    # Find closing ---
    second = raw.find("---", 3)
    if second < 0:
        warnings.append("SKILL.md missing closing ---")
        return (None, warnings)

    fm_text = raw[3:second].strip()

    try:
        fm = yaml.safe_load(fm_text)
    except yaml.YAMLError as e:
        warnings.append(f"YAML parse error: {e}")
        return (None, warnings)

    if not isinstance(fm, dict):
        warnings.append("Frontmatter must be a YAML mapping")
        return (None, warnings)

    # Required fields
    if "name" not in fm:
        warnings.append("Missing required field: name")
        return (None, warnings)

    if "description" not in fm:
        warnings.append("Missing required field: description")
        return (None, warnings)

    # Validate name
    name = fm["name"]
    if len(name) > MAX_NAME_LENGTH:
        warnings.append(f"Name exceeds {MAX_NAME_LENGTH} chars")
        return (None, warnings)

    if not NAME_PATTERN.match(name):
        warnings.append(f"Name '{name}' must match pattern {NAME_PATTERN.pattern}")
        return (None, warnings)

    # Validate description
    desc = fm["description"]
    if len(desc) > MAX_DESCRIPTION_LENGTH:
        warnings.append(f"Description exceeds {MAX_DESCRIPTION_LENGTH} chars")
        return (None, warnings)

    # Validate total content size
    if len(raw) > MAX_SKILL_CONTENT_CHARS:
        warnings.append(f"Content exceeds {MAX_SKILL_CONTENT_CHARS} chars")
        return (None, warnings)

    return (fm, warnings)


def validate_content(body: str) -> list[str]:
    """Scan body for dangerous patterns. Returns list of warnings."""
    warnings = []
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, body):
            warnings.append(f"Potentially dangerous pattern detected")
    return warnings