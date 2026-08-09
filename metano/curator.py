"""Curator: auto-maintain skills and memory when idle.

Runs as a background process or MCP tool. Scans memory files and CLAUDE.md,
identifies stale/duplicate entries, and suggests or applies improvements.
"""

import json
import re
import time
from pathlib import Path
from typing import Optional

MEMORY_DIR = Path.home() / ".claude" / "projects" / "-home-dk" / "memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
CLAUDE_MD = Path.home() / "CLAUDE.md"
CURATOR_STATE = Path.home() / ".claude" / "metano" / "curator_state.json"


def load_state() -> dict:
    if CURATOR_STATE.exists():
        return json.loads(CURATOR_STATE.read_text())
    return {"last_run": None, "actions_taken": [], "total_runs": 0}


def save_state(state: dict):
    CURATOR_STATE.parent.mkdir(parents=True, exist_ok=True)
    CURATOR_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def scan_memory_files() -> list[dict]:
    """Scan all memory files and return metadata."""
    files = []
    if not MEMORY_DIR.exists():
        return files
    for f in MEMORY_DIR.glob("*.md"):
        if f.name == "MEMORY.md":
            continue
        content = f.read_text()
        # Parse frontmatter
        name = f.stem
        description = ""
        mem_type = ""
        lines = content.split("\n")
        in_frontmatter = False
        for line in lines:
            if line.strip() == "---":
                if in_frontmatter:
                    break
                in_frontmatter = True
                continue
            if in_frontmatter:
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip()
                elif line.startswith("description:"):
                    description = line.split(":", 1)[1].strip()
                elif line.startswith("type:"):
                    mem_type = line.split(":", 1)[1].strip()

        files.append({
            "filename": f.name,
            "name": name,
            "description": description,
            "type": mem_type,
            "size": len(content),
            "modified": f.stat().st_mtime,
            "path": str(f),
        })
    return files


def find_issues(memory_files: list[dict]) -> list[dict]:
    """Identify issues in memory files."""
    issues = []

    # Check for duplicate names
    seen_names = {}
    for f in memory_files:
        n = f["name"]
        if n in seen_names:
            issues.append({
                "type": "duplicate_name",
                "severity": "warning",
                "message": f"Duplicate memory name '{n}' in {f['filename']} and {seen_names[n]}",
                "files": [f["filename"], seen_names[n]],
            })
        seen_names[n] = f["filename"]

    # Check for empty descriptions
    for f in memory_files:
        if not f["description"]:
            issues.append({
                "type": "empty_description",
                "severity": "info",
                "message": f"Memory '{f['name']}' has no description",
                "file": f["filename"],
            })

    # Check for very large files (>5KB might need splitting)
    for f in memory_files:
        if f["size"] > 5000:
            issues.append({
                "type": "large_file",
                "severity": "info",
                "message": f"Memory '{f['name']}' is {f['size']} bytes, consider splitting",
                "file": f["filename"],
            })

    # Check MEMORY.md index consistency
    if MEMORY_INDEX.exists():
        index_content = MEMORY_INDEX.read_text()
        indexed_files = set()
        for line in index_content.split("\n"):
            m = re.search(r'\[(.*?)\]\((.*?)\)', line)
            if m:
                indexed_files.add(m.group(2))

        for f in memory_files:
            if f["filename"] not in indexed_files:
                issues.append({
                    "type": "missing_from_index",
                    "severity": "warning",
                    "message": f"Memory '{f['name']}' exists but is not in MEMORY.md index",
                    "file": f["filename"],
                })

    # Check for stale files (>30 days since last modified)
    now = time.time()
    for f in memory_files:
        age_days = (now - f["modified"]) / 86400
        if age_days > 30:
            issues.append({
                "type": "stale",
                "severity": "info",
                "message": f"Memory '{f['name']}' hasn't been updated in {age_days:.0f} days",
                "file": f["filename"],
                "age_days": age_days,
            })

    return issues


def generate_report(issues: list[dict]) -> str:
    """Generate a human-readable curator report."""
    if not issues:
        return "All memory files are in good shape. No issues found."

    by_severity = {"warning": [], "info": []}
    for issue in issues:
        by_severity.get(issue["severity"], by_severity["info"]).append(issue)

    lines = ["=== Curator Report ===", ""]

    if by_severity["warning"]:
        lines.append(f"Warnings ({len(by_severity['warning'])}):")
        for i in by_severity["warning"]:
            lines.append(f"  - {i['message']}")
        lines.append("")

    if by_severity["info"]:
        lines.append(f"Suggestions ({len(by_severity['info'])}):")
        for i in by_severity["info"]:
            lines.append(f"  - {i['message']}")
        lines.append("")

    return "\n".join(lines)


def auto_fix(issues: list[dict], dry_run: bool = True) -> list[dict]:
    """Auto-fix safe issues. Returns list of actions taken."""
    actions = []

    for issue in issues:
        if issue["type"] == "missing_from_index":
            filename = issue["file"]
            # Find the memory file to get its name and description
            mem_file = MEMORY_DIR / filename
            if mem_file.exists():
                content = mem_file.read_text()
                name = filename.replace(".md", "")
                desc = ""
                for line in content.split("\n"):
                    if line.startswith("description:"):
                        desc = line.split(":", 1)[1].strip()
                        break

                entry = f"- [{name}]({filename}) — {desc}\n"
                if not dry_run:
                    with open(MEMORY_INDEX, "a") as f:
                        f.write(entry)
                actions.append({
                    "action": "add_to_index",
                    "file": filename,
                    "entry": entry.strip(),
                    "dry_run": dry_run,
                })

    return actions


def run_curator(dry_run: bool = True) -> dict:
    """Run a full curator cycle."""
    memory_files = scan_memory_files()
    issues = find_issues(memory_files)
    actions = auto_fix(issues, dry_run=dry_run)
    report = generate_report(issues)

    state = load_state()
    state["last_run"] = time.time()
    state["total_runs"] = state.get("total_runs", 0) + 1
    state["actions_taken"].extend(actions)
    # Keep only last 50 actions
    state["actions_taken"] = state["actions_taken"][-50:]
    save_state(state)

    return {
        "files_scanned": len(memory_files),
        "issues_found": len(issues),
        "actions_taken": len(actions),
        "report": report,
        "details": {"issues": issues, "actions": actions},
    }