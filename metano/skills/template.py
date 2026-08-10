"""Template variable substitution for skill content."""

from string import Template
from pathlib import Path
from ..paths import home_dir

DEFAULT_VARIABLES = {
    "HOME": str(Path.home()),
    "BRIDGE_DIR": str(home_dir()),
}


def substitute(content: str, variables: dict | None = None) -> str:
    """Substitute ${VAR} placeholders in skill content.

    Uses safe_substitute so unknown ${...} patterns are left intact.
    """
    all_vars = {**DEFAULT_VARIABLES, **(variables or {})}
    return Template(content).safe_substitute(all_vars)