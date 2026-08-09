"""Tests for kanban module — board and task CRUD."""

import json


def test_create_board(tmp_path, monkeypatch):
    from metano.kanban import kanban_create_board, kanban_list
    monkeypatch.setattr("metano.kanban.KANBAN_DIR", tmp_path)
    monkeypatch.setattr("metano.kanban.KANBAN_DB", tmp_path / "kanban.db")

    result = kanban_create_board("Test Board", "A board for testing")
    assert "board_id" in result
    assert result["name"] == "Test Board"


def test_create_board_default_columns(tmp_path, monkeypatch):
    from metano.kanban import kanban_create_board
    monkeypatch.setattr("metano.kanban.KANBAN_DIR", tmp_path)
    monkeypatch.setattr("metano.kanban.KANBAN_DB", tmp_path / "kanban.db")

    result = kanban_create_board("Default")
    assert result["columns"] == ["backlog", "todo", "in-progress", "review", "done"]


def test_list_boards(tmp_path, monkeypatch):
    from metano.kanban import kanban_create_board, kanban_list
    monkeypatch.setattr("metano.kanban.KANBAN_DIR", tmp_path)
    monkeypatch.setattr("metano.kanban.KANBAN_DB", tmp_path / "kanban.db")

    kanban_create_board("Board A")
    kanban_create_board("Board B")
    result = kanban_list()
    assert len(result["boards"]) >= 2
    names = [b["name"] for b in result["boards"]]
    assert "Board A" in names
    assert "Board B" in names


def test_add_task(tmp_path, monkeypatch):
    from metano.kanban import kanban_create_board, kanban_add_task, kanban_list
    monkeypatch.setattr("metano.kanban.KANBAN_DIR", tmp_path)
    monkeypatch.setattr("metano.kanban.KANBAN_DB", tmp_path / "kanban.db")

    board = kanban_create_board("Board")
    task = kanban_add_task(board["board_id"], "My Task", "Description",
                           column="todo", priority="high")
    assert task["task_id"]
    assert task["title"] == "My Task"
    assert task["column"] == "todo"

    # Verify in board listing
    listing = kanban_list(board["board_id"])
    assert "My Task" in str(listing)


def test_add_task_invalid_board(tmp_path, monkeypatch):
    from metano.kanban import kanban_add_task
    monkeypatch.setattr("metano.kanban.KANBAN_DIR", tmp_path)
    monkeypatch.setattr("metano.kanban.KANBAN_DB", tmp_path / "kanban.db")

    result = kanban_add_task("nonexistent", "Task")
    assert "error" in result


def test_move_task(tmp_path, monkeypatch):
    from metano.kanban import kanban_create_board, kanban_add_task, kanban_move_task
    monkeypatch.setattr("metano.kanban.KANBAN_DIR", tmp_path)
    monkeypatch.setattr("metano.kanban.KANBAN_DB", tmp_path / "kanban.db")

    board = kanban_create_board("Board")
    task = kanban_add_task(board["board_id"], "Task")
    result = kanban_move_task(task["task_id"], "done")
    assert result["column"] == "done"


def test_move_task_invalid_column(tmp_path, monkeypatch):
    from metano.kanban import kanban_create_board, kanban_add_task, kanban_move_task
    monkeypatch.setattr("metano.kanban.KANBAN_DIR", tmp_path)
    monkeypatch.setattr("metano.kanban.KANBAN_DB", tmp_path / "kanban.db")

    board = kanban_create_board("Board")
    task = kanban_add_task(board["board_id"], "Task")
    result = kanban_move_task(task["task_id"], "invalid_column")
    assert "error" in result


def test_move_task_not_found(tmp_path, monkeypatch):
    from metano.kanban import kanban_move_task
    monkeypatch.setattr("metano.kanban.KANBAN_DIR", tmp_path)
    monkeypatch.setattr("metano.kanban.KANBAN_DB", tmp_path / "kanban.db")

    result = kanban_move_task("nonexistent", "done")
    assert "error" in result


def test_delete_task(tmp_path, monkeypatch):
    from metano.kanban import kanban_create_board, kanban_add_task, kanban_delete_task, kanban_list
    monkeypatch.setattr("metano.kanban.KANBAN_DIR", tmp_path)
    monkeypatch.setattr("metano.kanban.KANBAN_DB", tmp_path / "kanban.db")

    board = kanban_create_board("Board")
    task = kanban_add_task(board["board_id"], "Task")
    delete_result = kanban_delete_task(task["task_id"])
    assert delete_result["status"] == "deleted"


def test_list_tasks_by_column(tmp_path, monkeypatch):
    from metano.kanban import kanban_create_board, kanban_add_task, kanban_list
    monkeypatch.setattr("metano.kanban.KANBAN_DIR", tmp_path)
    monkeypatch.setattr("metano.kanban.KANBAN_DB", tmp_path / "kanban.db")

    board = kanban_create_board("Board")
    kanban_add_task(board["board_id"], "Task A", column="todo")
    kanban_add_task(board["board_id"], "Task B", column="done")

    result = kanban_list(board["board_id"], "todo")
    assert "Task A" in str(result)
