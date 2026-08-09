"""Tests for knowledge module — ingestion, chunking, search, deletion."""

import hashlib
import time
import json
from pathlib import Path


def test_chunk_text_short():
    from metano.knowledge import _chunk_text
    text = "Hello world"
    chunks = _chunk_text(text, size=100, overlap=20)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_chunk_text_long():
    from metano.knowledge import _chunk_text
    text = "A" * 500 + "\n\n" + "B" * 500
    chunks = _chunk_text(text, size=200, overlap=50)
    assert len(chunks) >= 2
    assert all(isinstance(c, str) and len(c) <= 200 for c in chunks)


def test_chunk_text_paragraph_boundary():
    from metano.knowledge import _chunk_text
    para1 = "Word " * 100
    para2 = "Test " * 100
    text = para1 + "\n\n" + para2
    chunks = _chunk_text(text, size=300, overlap=50)
    assert len(chunks) >= 2


def test_validate_ingest_path_allowed():
    from metano.knowledge import _validate_ingest_path
    import os
    home = os.path.expanduser("~")
    result = _validate_ingest_path(f"{home}/.claude/metano/test.txt")
    assert result is None


def test_validate_ingest_path_blocked():
    from metano.knowledge import _validate_ingest_path
    result = _validate_ingest_path("/etc/passwd")
    assert result is not None
    assert "outside" in result


def test_keyword_search_basic(tmp_path, monkeypatch):
    from metano.knowledge import knowledge_ingest, _keyword_search, KB_DIR
    monkeypatch.setattr("metano.knowledge.KB_DIR", tmp_path)
    monkeypatch.setattr("metano.knowledge.KB_DB", tmp_path / "knowledge.db")
    monkeypatch.setattr("metano.knowledge.ALLOWED_INGEST_PREFIXES", [tmp_path])

    # Ingest a document
    doc = tmp_path / "test_doc.md"
    doc.write_text("Python is a programming language. Python is used for web development.")

    result = knowledge_ingest(str(doc), title="Python Guide")
    assert result["status"] == "ingested"

    # Search
    search_result = _keyword_search("Python programming", limit=5)
    assert search_result["query"] == "Python programming"
    assert len(search_result["results"]) >= 1
    assert "Python" in search_result["results"][0]["content"]


def test_keyword_search_no_match(tmp_path, monkeypatch):
    from metano.knowledge import _keyword_search
    from metano.knowledge import KB_DIR, KB_DB
    monkeypatch.setattr("metano.knowledge.KB_DIR", tmp_path)
    monkeypatch.setattr("metano.knowledge.KB_DB", tmp_path / "knowledge.db")

    search_result = _keyword_search("nonexistent_unique_term_xyz", limit=5)
    assert search_result["query"] == "nonexistent_unique_term_xyz"
    assert len(search_result["results"]) == 0


def test_knowledge_ingest_file(tmp_path, monkeypatch):
    from metano.knowledge import knowledge_ingest, knowledge_list, knowledge_delete
    monkeypatch.setattr("metano.knowledge.KB_DIR", tmp_path)
    monkeypatch.setattr("metano.knowledge.KB_DB", tmp_path / "knowledge.db")
    monkeypatch.setattr("metano.knowledge.ALLOWED_INGEST_PREFIXES", [tmp_path])

    doc = tmp_path / "test.md"
    doc.write_text("# Test\n\nHello world.")

    result = knowledge_ingest(str(doc), title="Test Doc")
    assert result["status"] == "ingested"
    assert result["chunks"] >= 1

    # List should include it
    listed = knowledge_list()
    titles = [d["title"] for d in listed.get("documents", [])]
    assert "Test Doc" in titles

    # Delete
    delete_result = knowledge_delete(result["doc_id"])
    assert delete_result["status"] == "deleted"


def test_knowledge_ingest_directory(tmp_path, monkeypatch):
    from metano.knowledge import knowledge_ingest
    monkeypatch.setattr("metano.knowledge.KB_DIR", tmp_path)
    monkeypatch.setattr("metano.knowledge.KB_DB", tmp_path / "knowledge.db")
    monkeypatch.setattr("metano.knowledge.ALLOWED_INGEST_PREFIXES", [tmp_path])

    sub = tmp_path / "docs"
    sub.mkdir()
    (sub / "a.md").write_text("# Doc A")
    (sub / "b.py").write_text("x = 1")

    result = knowledge_ingest(str(sub))
    assert result["status"] == "batch"
    # Note: batch will fail validation since tmp_path isn't in ALLOWED_INGEST_PREFIXES
    # But the individual files should still work
    assert result["count"] > 0


def test_knowledge_ingest_binary(tmp_path, monkeypatch):
    from metano.knowledge import knowledge_ingest
    monkeypatch.setattr("metano.knowledge.KB_DIR", tmp_path)
    monkeypatch.setattr("metano.knowledge.KB_DB", tmp_path / "knowledge.db")

    binary = tmp_path / "test.bin"
    binary.write_bytes(b"\x00\x01\x02\x03")

    result = knowledge_ingest(str(binary))
    assert "error" in result


def test_knowledge_list_empty(tmp_path, monkeypatch):
    from metano.knowledge import knowledge_list
    from metano.knowledge import KB_DIR as orig_dir
    monkeypatch.setattr("metano.knowledge.KB_DIR", tmp_path)
    monkeypatch.setattr("metano.knowledge.KB_DB", tmp_path / "knowledge.db")

    result = knowledge_list()
    assert "documents" in result
    assert len(result["documents"]) == 0


def test_chunk_size_respected():
    from metano.knowledge import _chunk_text
    text = "X" * 5000
    chunks = _chunk_text(text, size=1000, overlap=200)
    for c in chunks:
        assert len(c) <= 1000
    assert len(chunks) >= 5
