"""Semantic memory system with compression — inspired by claude-mem.

Stores cross-session observations, auto-compresses old entries,
and provides semantic search over accumulated knowledge.
"""
import contextlib
import json
import os
import re
import sqlite3
import hashlib
from datetime import datetime, timedelta
from typing import Optional
from pathlib import Path
from metano.log import logger
from .paths import home_dir

DB_PATH = os.environ.get('MEMORY_DB') or str(home_dir() / 'memory.db')

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
        hash TEXT,
        tags TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);
    CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance DESC);
    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(content, category, tokenize='trigram');
    -- B5: keep the FTS index in lockstep with the main table for ANY delete /
    -- update executed through a connection that runs this schema, so old text
    -- can no longer hit while the main row shows new content (37 dead rows
    -- accumulated because the 8/11 cleanup only deleted the main table).
    CREATE TRIGGER IF NOT EXISTS trg_memories_ai AFTER INSERT ON memories BEGIN
        INSERT INTO memories_fts(rowid, content, category) VALUES (new.id, new.content, new.category);
    END;
    CREATE TRIGGER IF NOT EXISTS trg_memories_ad AFTER DELETE ON memories BEGIN
        DELETE FROM memories_fts WHERE rowid = old.id;
    END;
    CREATE TRIGGER IF NOT EXISTS trg_memories_au AFTER UPDATE ON memories BEGIN
        DELETE FROM memories_fts WHERE rowid = old.id;
        INSERT INTO memories_fts(rowid, content, category) VALUES (new.id, new.content, new.category);
    END;
"""


def _migrate(conn):
    """Add new columns to pre-existing databases (idempotent)."""
    cols = {r['name'] for r in conn.execute('PRAGMA table_info(memories)')}
    if 'tags' not in cols:
        conn.execute("ALTER TABLE memories ADD COLUMN tags TEXT")


def _normalize_tags(tags) -> str:
    """Normalize tags into a canonical comma-separated lowercase string.

    Accepts a list/tuple of tag strings, a single tag string, or a comma/space
    (incl. full-width comma) separated string. Empty/None -> ''.
    """
    if not tags:
        return ''
    if isinstance(tags, str):
        items = re.split(r'[,，\s]+', tags)
    else:
        items = tags
    seen: list[str] = []
    for t in items:
        t = str(t).strip().lower()
        if t and t not in seen:
            seen.append(t)
    return ','.join(seen)


def _tag_filter_clause(tag):
    """Build a SQL WHERE fragment that matches memories whose tags contain the
    given tag(s). Multiple tags (comma/space separated) are AND-ed — a row must
    carry every tag. Returns ('', []) when no tag is given.

    Exact-tag matching uses `instr(',' || m.tags || ',', ',' || ? || ',')` so
    that 'front' never matches a tag 'frontend'.
    """
    if not tag:
        return '', []
    tags = [t.strip().lower() for t in re.split(r'[,，\s]+', tag) if t.strip()]
    if not tags:
        return '', []
    conds = ["instr(',' || m.tags || ',', ',' || ? || ',') > 0" for _ in tags]
    return 'AND ' + ' AND '.join(conds), tags


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
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA_SQL)
    _migrate(conn)
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def add_memory(content: str, category: str='general', importance: float=0.5, tags=None) -> dict:
    """Add a memory observation.

    tags: list of scenario keywords (or comma/space separated string) that gate
    when this memory is relevant, e.g. ['backend', 'frontend', 'sync']. Stored
    normalized as a comma-separated lowercase string. Empty -> no tags.
    """
    tags_str = _normalize_tags(tags)
    with _get_conn() as conn:
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
        existing = conn.execute('SELECT id FROM memories WHERE hash = ?', (content_hash,)).fetchone()
        if existing:
            conn.execute("UPDATE memories SET access_count = access_count + 1, last_accessed = datetime('now') WHERE id = ?", (existing['id'],))
            conn.commit()
            return {'status': 'duplicate', 'id': existing['id']}
        conn.execute('INSERT INTO memories (content, category, importance, hash, tags) VALUES (?, ?, ?, ?, ?)',
                     (content, category, importance, content_hash, tags_str or None))
        conn.commit()
        mid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        _fts_sync(conn, mid, content, category)
        conn.commit()
        return {'status': 'added', 'id': mid, 'tags': tags_str}


def update_memory(memory_id: int, content: Optional[str] = None,
                  category: Optional[str] = None, importance: Optional[float] = None,
                  tags=None) -> dict:
    """Update a memory row (FTS is kept in sync via trg_memories_au).

    B5: previously UPDATEs on memories never rewrote the FTS index, so old
    content could still be matched and return new text. The AFTER UPDATE
    trigger keeps the two tables consistent for this and any other writer.
    """
    tags_str = _normalize_tags(tags) if tags is not None else None
    with _get_conn() as conn:
        row = conn.execute('SELECT * FROM memories WHERE id=?', (memory_id,)).fetchone()
        if not row:
            return {'status': 'not_found', 'id': memory_id}
        conn.execute(
            'UPDATE memories SET content=?, category=?, importance=?, tags=? WHERE id=?',
            (row['content'] if content is None else content,
             row['category'] if category is None else category,
             row['importance'] if importance is None else importance,
             row['tags'] if tags_str is None else tags_str,
             memory_id),
        )
        conn.commit()
        return {'status': 'updated', 'id': memory_id}


def delete_memory(memory_id: int) -> dict:
    """Delete a memory row (FTS is removed via trg_memories_ad)."""
    with _get_conn() as conn:
        cur = conn.execute('DELETE FROM memories WHERE id=?', (memory_id,))
        conn.commit()
        return {'status': 'deleted' if cur.rowcount else 'not_found', 'id': memory_id}


def rebuild_fts() -> dict:
    """Repair the FTS index from the main table (fixes existing desync).

    B5: the historical FTS desync (37 dead rows) predates the triggers — run
    this once to resync every row, or call it opportunistically after any
    external SQL cleanup that bypassed memory.py.
    """
    with _get_conn() as conn:
        conn.execute('DELETE FROM memories_fts')
        rows = conn.execute('SELECT id, content, category FROM memories').fetchall()
        for r in rows:
            _fts_sync(conn, r['id'], r['content'], r['category'])
        conn.commit()
        return {'status': 'rebuilt', 'rows': len(rows)}


def search_memories(query: str, limit: int=10, tag: Optional[str]=None) -> dict:
    """Search memories by content (FTS5 with LIKE fallback).

    tag: optional scenario keyword to filter by. Pass one tag, or a comma/space
    separated list for AND semantics (row must carry every tag). When omitted
    behaviour is identical to before (backward compatible). When query is empty
    but tag is given, returns all rows carrying that tag (scenario browsing).
    """
    tag_clause, tag_params = _tag_filter_clause(tag)
    with _get_conn() as conn:
        if query:
            try:
                rows = conn.execute(
                    "SELECT m.id, m.content, m.category, m.importance, m.created_at, m.tags "
                    "FROM memories m JOIN memories_fts fts ON m.id = fts.rowid "
                    "WHERE memories_fts MATCH ? " + tag_clause + " "
                    "ORDER BY m.importance DESC, m.last_accessed DESC LIMIT ?",
                    (query, *tag_params, limit)
                ).fetchall()
                if rows:
                    for r in rows:
                        conn.execute("UPDATE memories SET last_accessed = datetime('now') WHERE id = ?", (r['id'],))
                    conn.commit()
                    return {'query': query, 'tag': tag, 'results': [dict(r) for r in rows], 'count': len(rows), 'method': 'fts5'}
            except Exception:
                logger.debug("search_memories: FTS5 query failed, falling back to LIKE")
            words = query.lower().split()
            if not words:
                return {'query': query, 'tag': tag, 'results': [], 'count': 0, 'method': 'none'}
            conditions = ' AND '.join(['LOWER(m.content) LIKE ?' for _ in words])
            params = [f'%{w}%' for w in words]
            rows = conn.execute(
                f'SELECT m.id, m.content, m.category, m.importance, m.created_at, m.tags '
                f'FROM memories m WHERE {conditions} {tag_clause} '
                f'ORDER BY m.importance DESC, m.last_accessed DESC LIMIT ?',
                (*params, *tag_params, limit)
            ).fetchall()
            return {'query': query, 'tag': tag, 'results': [dict(r) for r in rows], 'count': len(rows), 'method': 'like'}
        # No query: tag-only browsing or empty result (original behaviour)
        if not tag_params:
            return {'query': query, 'tag': tag, 'results': [], 'count': 0, 'method': 'none'}
        rows = conn.execute(
            f'SELECT m.id, m.content, m.category, m.importance, m.created_at, m.tags '
            f'FROM memories m WHERE {tag_clause[4:]} '
            f'ORDER BY m.importance DESC, m.last_accessed DESC LIMIT ?',
            (*tag_params, limit)
        ).fetchall()
        return {'query': query, 'tag': tag, 'results': [dict(r) for r in rows], 'count': len(rows), 'method': 'tag'}


def get_memory_stats() -> dict:
    with _get_conn() as conn:
        total = conn.execute('SELECT COUNT(*) FROM memories').fetchone()[0]
        by_category = conn.execute('SELECT category, COUNT(*) as cnt FROM memories GROUP BY category').fetchall()
        avg_importance = conn.execute('SELECT AVG(importance) FROM memories').fetchone()[0] or 0
        oldest = conn.execute('SELECT MIN(created_at) FROM memories').fetchone()[0]
        tagged = conn.execute("SELECT COUNT(*) FROM memories WHERE tags IS NOT NULL AND tags != ''").fetchone()[0]
        tag_rows = conn.execute("SELECT tags FROM memories WHERE tags IS NOT NULL AND tags != ''").fetchall()
        tag_counts: dict[str, int] = {}
        for r in tag_rows:
            for t in r['tags'].split(','):
                t = t.strip()
                if t:
                    tag_counts[t] = tag_counts.get(t, 0) + 1
        return {'total_memories': total,
                'by_category': {r['category']: r['cnt'] for r in by_category},
                'avg_importance': round(avg_importance, 3),
                'oldest_memory': oldest,
                'tagged_memories': tagged,
                'tag_counts': dict(sorted(tag_counts.items(), key=lambda kv: kv[1], reverse=True))}


def compress_memories() -> dict:
    """Compress old, low-importance memories by merging similar ones."""
    with _get_conn() as conn:
        cutoff = (datetime.now() - timedelta(days=30)).isoformat()
        old = conn.execute('SELECT id, content, category, importance, tags FROM memories WHERE created_at < ? AND importance < 0.3 AND compressed_from IS NULL', (cutoff,)).fetchall()
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
            merged_tags = _normalize_tags(','.join([r['tags'] or '' for r in items[:5]]))
            conn.execute('INSERT INTO memories (content, category, importance, hash, compressed_from, tags) VALUES (?, ?, ?, ?, ?, ?)',
                         (merged, f'compressed_{cat}', 0.6, content_hash, items[0]['id'], merged_tags or None))
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
        rows = conn.execute('SELECT id, content, category, importance, created_at, last_accessed, access_count, tags FROM memories ORDER BY importance DESC').fetchall()
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
            conn.execute('INSERT INTO memories (content, category, importance, hash, tags) VALUES (?, ?, ?, ?, ?)',
                         (content, m.get('category', 'general'), m.get('importance', 0.5), content_hash,
                          _normalize_tags(m.get('tags')) or None))
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


def search_memories_for_context(query: str = '', tags=None, limit: int = 5) -> list[str]:
    """Return prompt-ready context lines from memory.db relevant to ``query``/``tags``.

    F-06: gateway/router's ``_build_system_context`` calls this so memories
    written to memory.db actually reach the next chat prompt (closing the
    write-inject loop). When both query and tags are empty, falls back to the
    most important recent memories so the context is never empty if memories
    exist.
    """
    tag_str = _normalize_tags(tags) if tags else ''
    try:
        if query or tag_str:
            res = search_memories(query, limit=limit, tag=tag_str or None)
            if res.get('results'):
                return [f"- [{r['category']}] {r['content']}" for r in res['results']]
    except Exception:
        logger.exception("search_memories_for_context: search failed")
    # Fallback: top by importance (or empty when nothing is stored yet).
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                'SELECT content, category FROM memories '
                'ORDER BY importance DESC, last_accessed DESC LIMIT ?',
                (limit,)).fetchall()
            return [f"- [{r['category']}] {r['content']}" for r in rows]
    except Exception:
        logger.exception("search_memories_for_context: fallback failed")
    return []


def rebuild_index_from_db(index_path=None) -> dict:
    """Rebuild a Claude-readable markdown memory index from the memories table.

    F-06: the adapter calls this when its ``MEMORY.md`` index file is missing,
    so auto-learned pointers are not silently dropped. ``index_path`` may be a
    ``str``/``Path``; when omitted only the generated text count is returned.
    """
    with _get_conn() as conn:
        rows = conn.execute(
            'SELECT content, category, tags FROM memories '
            'ORDER BY importance DESC, last_accessed DESC LIMIT 100'
        ).fetchall()
    lines = ['# Memory Index (rebuilt from memory.db)', '']
    for r in rows:
        content = (r['content'] or '').strip().replace('\n', ' ')[:200]
        if not content:
            continue
        tag = f" (tags: {r['tags']})" if r['tags'] else ''
        lines.append(f"- [{r['category']}] {content}{tag}")
    text = '\n'.join(lines) + '\n'
    written = ''
    if index_path:
        p = Path(index_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        written = str(p)
    return {'status': 'rebuilt', 'entries': len(rows), 'path': written}
