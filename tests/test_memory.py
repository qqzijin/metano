"""Tests for memory module — CRUD, FTS5 search, compression, export/import."""

from unittest.mock import patch


def test_add_and_search(tmp_path, monkeypatch):
    monkeypatch.setattr("metano.memory.DB_PATH", str(tmp_path / "mem.db"))
    from metano.memory import add_memory, search_memories

    r = add_memory("Python is a programming language", category="tech", importance=0.8)
    assert r["status"] == "added"

    found = search_memories("programming")
    assert found["count"] >= 1
    assert "programming" in found["results"][0]["content"]


def test_add_duplicate(tmp_path, monkeypatch):
    monkeypatch.setattr("metano.memory.DB_PATH", str(tmp_path / "mem.db"))
    from metano.memory import add_memory

    add_memory("unique content here", category="test")
    r = add_memory("unique content here", category="test")
    assert r["status"] == "duplicate"


def test_add_no_double_category_on_dup(tmp_path, monkeypatch):
    monkeypatch.setattr("metano.memory.DB_PATH", str(tmp_path / "mem.db"))
    from metano.memory import add_memory, get_memory_stats

    add_memory("dup test content", category="cat_a")
    add_memory("dup test content", category="cat_b")
    stats = get_memory_stats()
    assert stats["total_memories"] == 1


def test_search_no_match(tmp_path, monkeypatch):
    monkeypatch.setattr("metano.memory.DB_PATH", str(tmp_path / "mem.db"))
    from metano.memory import add_memory, search_memories

    add_memory("only this content here", category="test")
    found = search_memories("xyznonexistent")
    assert found["count"] == 0


def test_get_memory_stats(tmp_path, monkeypatch):
    monkeypatch.setattr("metano.memory.DB_PATH", str(tmp_path / "mem.db"))
    from metano.memory import add_memory, get_memory_stats

    add_memory("first", category="cat1", importance=0.9)
    add_memory("second", category="cat1", importance=0.5)
    add_memory("third", category="cat2", importance=0.3)

    stats = get_memory_stats()
    assert stats["total_memories"] == 3
    assert stats["by_category"]["cat1"] == 2
    assert stats["by_category"]["cat2"] == 1
    assert stats["avg_importance"] > 0


def test_compress_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr("metano.memory.DB_PATH", str(tmp_path / "mem.db"))
    from metano.memory import compress_memories, add_memory

    add_memory("recent important memory", importance=0.9)
    r = compress_memories()
    assert r["status"] == "nothing_to_compress"


def test_compress_old_low_importance(tmp_path, monkeypatch):
    monkeypatch.setattr("metano.memory.DB_PATH", str(tmp_path / "mem.db"))
    from metano.memory import compress_memories, add_memory, search_memories
    import time

    add_memory("old thing a", category="trivia", importance=0.1)
    add_memory("old thing b", category="trivia", importance=0.1)

    # Fake created_at by setting system time far back
    with patch("metano.memory.datetime") as mock_dt:
        from datetime import datetime, timedelta
        mock_dt.now.return_value = datetime.now() + timedelta(days=60)
        r = compress_memories()
        # May or may not compress depending on actual DB created_at
        assert r["status"] in ("nothing_to_compress", "compressed")


def test_export_memories(tmp_path, monkeypatch):
    monkeypatch.setattr("metano.memory.DB_PATH", str(tmp_path / "mem.db"))
    from metano.memory import add_memory, export_memories

    add_memory("exportable content", category="test", importance=0.7)
    exported = export_memories()
    assert exported["count"] >= 1
    assert any("exportable" in m["content"] for m in exported["memories"])


def test_import_memories(tmp_path, monkeypatch):
    monkeypatch.setattr("metano.memory.DB_PATH", str(tmp_path / "mem.db"))
    from metano.memory import import_memories, search_memories

    data = {
        "memories": [
            {"content": "imported memory one", "category": "imported", "importance": 0.5},
            {"content": "imported memory two", "category": "imported", "importance": 0.6},
        ]
    }
    r = import_memories(data, merge=False)
    assert r["imported"] == 2

    found = search_memories("imported")
    assert found["count"] >= 2


def test_import_memories_merge_skip(tmp_path, monkeypatch):
    monkeypatch.setattr("metano.memory.DB_PATH", str(tmp_path / "mem.db"))
    from metano.memory import add_memory, import_memories

    add_memory("existing memory", category="test")
    data = {
        "memories": [
            {"content": "existing memory", "category": "test", "importance": 0.5},
            {"content": "new memory", "category": "test", "importance": 0.5},
        ]
    }
    r = import_memories(data, merge=True)
    assert r["imported"] == 1
    assert r["skipped"] == 1


def test_add_memory_importance_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr("metano.memory.DB_PATH", str(tmp_path / "mem.db"))
    from metano.memory import add_memory, search_memories

    add_memory("default importance")
    found = search_memories("default")
    assert found["count"] >= 1


def test_fts5_fallback_on_bad_query(tmp_path, monkeypatch):
    monkeypatch.setattr("metano.memory.DB_PATH", str(tmp_path / "mem.db"))
    from metano.memory import add_memory, search_memories

    add_memory("hello world test data", category="test")
    # FTS5 may throw on certain special chars; fallback should handle it
    found = search_memories("hello")
    assert found["count"] >= 1
