"""Kanban/project management: task boards for multi-agent coordination."""

import json
import sqlite3
import time

from .paths import KANBAN_DIR
KANBAN_DB = KANBAN_DIR / "kanban.db"

# Default columns
DEFAULT_COLUMNS = ["backlog", "todo", "in-progress", "review", "done"]


def _get_conn() -> sqlite3.Connection:
    KANBAN_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(KANBAN_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS boards (
            board_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            columns TEXT DEFAULT '[]',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            board_id TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            column TEXT DEFAULT 'todo',
            assignee TEXT DEFAULT '',
            priority TEXT DEFAULT 'medium',
            tags TEXT DEFAULT '[]',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            completed_at REAL,
            FOREIGN KEY (board_id) REFERENCES boards(board_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_board ON tasks(board_id)")
    conn.commit()
    return conn


def kanban_create_board(name: str, description: str = "", columns: list[str] = None) -> dict:
    """Create a new kanban board."""
    import hashlib
    board_id = hashlib.sha256(f"board:{name}:{time.time()}".encode()).hexdigest()[:12]
    cols = columns or DEFAULT_COLUMNS

    conn = _get_conn()
    now = time.time()
    conn.execute("""
        INSERT INTO boards (board_id, name, description, columns, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (board_id, name, description, json.dumps(cols), now, now))
    conn.commit()
    conn.close()

    return {"board_id": board_id, "name": name, "columns": cols}


def kanban_add_task(board_id: str, title: str, description: str = "",
                    column: str = "todo", assignee: str = "",
                    priority: str = "medium", tags: list[str] = None) -> dict:
    """Add a task to a kanban board."""
    import hashlib
    task_id = hashlib.sha256(f"task:{title}:{time.time()}".encode()).hexdigest()[:12]

    conn = _get_conn()
    now = time.time()

    # Verify board exists
    board = conn.execute("SELECT columns FROM boards WHERE board_id=?", (board_id,)).fetchone()
    if not board:
        conn.close()
        return {"error": f"Board {board_id} not found"}

    # Verify column
    valid_cols = json.loads(board["columns"])
    if column not in valid_cols:
        column = valid_cols[0] if valid_cols else "todo"

    conn.execute("""
        INSERT INTO tasks (task_id, board_id, title, description, column, assignee, priority, tags, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (task_id, board_id, title, description, column, assignee, priority,
          json.dumps(tags or []), now, now))
    conn.commit()
    conn.close()

    return {"task_id": task_id, "title": title, "column": column, "board_id": board_id}


def kanban_move_task(task_id: str, column: str) -> dict:
    """Move a task to a different column."""
    conn = _get_conn()
    now = time.time()

    task = conn.execute("SELECT board_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if not task:
        conn.close()
        return {"error": f"Task {task_id} not found"}

    # Verify column
    board = conn.execute("SELECT columns FROM boards WHERE board_id=?", (task["board_id"],)).fetchone()
    valid_cols = json.loads(board["columns"])
    if column not in valid_cols:
        conn.close()
        return {"error": f"Invalid column: {column}. Valid: {valid_cols}"}

    completed_at = now if column in ("done", "completed") else None
    conn.execute("""
        UPDATE tasks SET column=?, updated_at=?, completed_at=COALESCE(?, completed_at) WHERE task_id=?
    """, (column, now, completed_at, task_id))
    conn.commit()
    conn.close()

    return {"task_id": task_id, "column": column, "status": "moved"}


def kanban_list(board_id: str = "", column: str = "") -> dict:
    """List boards or tasks in a board."""
    conn = _get_conn()

    if not board_id:
        boards = conn.execute("SELECT * FROM boards ORDER BY updated_at DESC").fetchall()
        conn.close()
        return {"boards": [{
            "board_id": b["board_id"],
            "name": b["name"],
            "description": b["description"],
            "columns": json.loads(b["columns"]),
            "updated_at": b["updated_at"],
        } for b in boards]}

    if column:
        tasks = conn.execute(
            "SELECT * FROM tasks WHERE board_id=? AND column=? ORDER BY priority, created_at",
            (board_id, column)
        ).fetchall()
    else:
        tasks = conn.execute(
            "SELECT * FROM tasks WHERE board_id=? ORDER BY column, priority, created_at",
            (board_id,)
        ).fetchall()

    board = conn.execute("SELECT name, columns FROM boards WHERE board_id=?", (board_id,)).fetchone()
    conn.close()

    if not board:
        return {"error": f"Board {board_id} not found"}

    # Group by column
    by_column = {}
    for t in tasks:
        col = t["column"]
        by_column.setdefault(col, []).append({
            "task_id": t["task_id"],
            "title": t["title"],
            "description": t["description"],
            "assignee": t["assignee"],
            "priority": t["priority"],
            "tags": json.loads(t["tags"]),
        })

    return {
        "board_id": board_id,
        "name": board["name"],
        "columns": json.loads(board["columns"]),
        "tasks": by_column,
    }


def kanban_delete_task(task_id: str) -> dict:
    """Delete a task."""
    conn = _get_conn()
    conn.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted", "task_id": task_id}