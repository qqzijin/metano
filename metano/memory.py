"""Semantic memory system with compression — inspired by claude-mem.

Stores cross-session observations, auto-compresses old entries,
and provides semantic search over accumulated knowledge.
"""
import contextlib
import json
import os
import sqlite3
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from metano.log import logger

DB_PATH = os.environ.get('MEMORY_DB', str(Path(__file__).resolve().parent.parent / 'memory.db'))

_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL,
        category TEXT DEFAULT 'general',
        importance REAL DEFAULT 0.5,
        compressed_from INTEGER,
        created_at TEXT DEFAULT (datetime('now')),
        last_accessed TEXT DEFAULT (datetime('now')),
        access_count INTEGER DEFAULT 0,
        hash TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);
    CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance DESC);
    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(content, category, tokenize='trigram');
"""


def _fts_sync(conn, row_id: int, content: str, category: str):
    """Sync a row into FTS5 index (insert or update)."""
    conn.execute(
        "INSERT OR REPLACE INTO memories_fts(rowid, content, category) VALUES (?, ?, ?)",
        (row_id, content, category),
    )


def _fts_remove(conn, row_id: int):
    """Remove a row from FTS5 index."""
    conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (row_id,))


@contextlib.contextmanager
def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA_SQL)
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def add_memory(content: str, category: str='general', importance: float=0.5) -> dict:
    with _get_conn() as conn:
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
        existing = conn.execute('SELECT id FROM memories WHERE hash = ?', (content_hash,)).fetchone()
        if existing:
            conn.execute("UPDATE memories SET access_count = access_count + 1, last_accessed = datetime('now') WHERE id = ?", (existing['id'],))
            conn.commit()
            return {'status': 'duplicate', 'id': existing['id']}
        conn.execute('INSERT INTO memories (content, category, importance, hash) VALUES (?, ?, ?, ?)', (content, category, importance, content_hash))
        conn.commit()
        mid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        _fts_sync(conn, mid, content, category)
        conn.commit()
        return {'status': 'added', 'id': mid}


def search_memories(query: str, limit: int=10) -> dict:
    with _get_conn() as conn:
        try:
            rows = conn.execute(
                "SELECT m.id, m.content, m.category, m.importance, m.created_at "
                "FROM memories m JOIN memories_fts fts ON m.id = fts.rowid "
                "WHERE memories_fts MATCH ? "
                "ORDER BY m.importance DESC, m.last_accessed DESC LIMIT ?",
                (query, limit)
            ).fetchall()
            if rows:
                for r in rows:
                    conn.execute("UPDATE memories SET last_accessed = datetime('now') WHERE id = ?", (r['id'],))
                conn.commit()
                return {'query': query, 'results': [dict(r) for r in rows], 'count': len(rows), 'method': 'fts5'}
        except Exception:
            logger.debug("search_memories: FTS5 query failed, falling back to LIKE")
        words = query.lower().split()
        if not words:
            return {'query': query, 'results': [], 'count': 0, 'method': 'none'}
        conditions = ' AND '.join(['LOWER(content) LIKE ?' for _ in words])
        params = [f'%{w}%' for w in words] + [limit]
        rows = conn.execute(f'SELECT id, content, category, importance, created_at FROM memories WHERE {conditions} ORDER BY importance DESC, last_accessed DESC LIMIT ?', params).fetchall()
        return {'query': query, 'results': [dict(r) for r in rows], 'count': len(rows), 'method': 'like'}


def get_memory_stats() -> dict:
    with _get_conn() as conn:
        total = conn.execute('SELECT COUNT(*) FROM memories').fetchone()[0]
        by_category = conn.execute('SELECT category, COUNT(*) as cnt FROM memories GROUP BY category').fetchall()
        avg_importance = conn.execute('SELECT AVG(importance) FROM memories').fetchone()[0] or 0
        oldest = conn.execute('SELECT MIN(created_at) FROM memories').fetchone()[0]
        return {'total_memories': total, 'by_category': {r['category']: r['cnt'] for r in by_category}, 'avg_importance': round(avg_importance, 3), 'oldest_memory': oldest}


def compress_memories() -> dict:
    """Compress old, low-importance memories by merging similar ones."""
    with _get_conn() as conn:
        cutoff = (datetime.now() - timedelta(days=30)).isoformat()
        old = conn.execute('SELECT id, content, category, importance FROM memories WHERE created_at < ? AND importance < 0.3 AND compressed_from IS NULL', (cutoff,)).fetchall()
        if not old:
            return {'status': 'nothing_to_compress', 'compressed': 0}
        by_category: dict[str, list] = {}
        for row in old:
            cat = row['category']
            by_category.setdefault(cat, []).append(row)
        compressed_count = 0
        for cat, items in by_category.items():
            if len(items) < 2:
                continue
            merged = f'[压缩] {cat}: ' + '; '.join((r['content'][:80] for r in items[:10]))
            content_hash = hashlib.sha256(merged.encode()).hexdigest()[:16]
            existing = conn.execute('SELECT id FROM memories WHERE hash = ?', (content_hash,)).fetchone()
            if existing:
                continue
            conn.execute('INSERT INTO memories (content, category, importance, hash, compressed_from) VALUES (?, ?, ?, ?, ?)',
                         (merged, f'compressed_{cat}', 0.6, content_hash, items[0]['id']))
            new_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            _fts_sync(conn, new_id, merged, f'compressed_{cat}')
            for item in items:
                _fts_remove(conn, item['id'])
            placeholders = ','.join(['?'] * len(items))
            conn.execute(f'DELETE FROM memories WHERE id IN ({placeholders})', [r['id'] for r in items])
            compressed_count += len(items)
        conn.commit()
        return {'status': 'compressed', 'compressed': compressed_count}


def export_memories(format: str='json') -> dict:
    """Export all memories to JSON for migration."""
    with _get_conn() as conn:
        rows = conn.execute('SELECT id, content, category, importance, created_at, last_accessed, access_count FROM memories ORDER BY importance DESC').fetchall()
        memories = [dict(r) for r in rows]
        return {'format': format, 'count': len(memories), 'memories': memories, 'exported_at': datetime.now().isoformat()}


def import_memories(data: dict, merge: bool=True) -> dict:
    """Import memories from JSON export. merge=True skips duplicates."""
    with _get_conn() as conn:
        imported = 0
        skipped = 0
        for m in data.get('memories', []):
            content = m.get('content', '')
            if not content:
                continue
            content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
            if merge:
                existing = conn.execute('SELECT id FROM memories WHERE hash = ?', (content_hash,)).fetchone()
                if existing:
                    skipped += 1
                    continue
            conn.execute('INSERT INTO memories (content, category, importance, hash) VALUES (?, ?, ?, ?)', (content, m.get('category', 'general'), m.get('importance', 0.5), content_hash))
            mid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            _fts_sync(conn, mid, content, m.get('category', 'general'))
            imported += 1
        conn.commit()
        return {'imported': imported, 'skipped': skipped, 'total': imported + skipped}


def seed_from_claude_memory() -> dict:
    """Import seed data from Claude Code's own memory system."""
    claude_memory_dir = Path.home() / '.claude' / 'projects' / '-home-dk' / 'memory'
    claude_memory_index = claude_memory_dir / 'MEMORY.md'
    if not claude_memory_index.exists():
        return {'status': 'no_memory_index', 'imported': 0}
    content = claude_memory_index.read_text()
    entries = [line.strip() for line in content.split('\n') if line.strip() and not line.startswith('#')]
    imported = 0
    for entry in entries:
        result = add_memory(entry, category='seed', importance=0.3)
        if result.get('status') == 'added':
            imported += 1
    return {'status': 'seeded', 'imported': imported}
