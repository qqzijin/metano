"""RAG knowledge base: document ingestion, chunking, embedding, and retrieval."""

import hashlib
import json
import logging
import math
import sqlite3
import time
from pathlib import Path
from typing import Optional

KB_DIR = Path.home() / ".claude" / "metano" / "knowledge"
KB_DB = KB_DIR / "knowledge.db"
PROJECT_ROOT = Path.home() / ".claude" / "metano"
ALLOWED_INGEST_PREFIXES = [PROJECT_ROOT, Path.home() / "scrapling-project", Path.home() / "DailyHotApi"]

CHUNK_SIZE = 1000
CHUNK_OVERLAP = 200


def _validate_ingest_path(path: str) -> str | None:
    """Return error message if path is outside allowed directories, else None."""
    real = Path(path).resolve()
    for prefix in ALLOWED_INGEST_PREFIXES:
        try:
            real.relative_to(prefix.resolve())
            return None
        except ValueError:
            continue
    return f"Path outside allowed directories: {path}"


def _get_kb_conn() -> sqlite3.Connection:
    KB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(KB_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            doc_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            source TEXT NOT NULL,
            content TEXT NOT NULL,
            doc_type TEXT DEFAULT 'text',
            chunk_count INTEGER DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            chunk_id TEXT PRIMARY KEY,
            doc_id TEXT NOT NULL,
            content TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            embedding BLOB,
            created_at REAL NOT NULL,
            FOREIGN KEY (doc_id) REFERENCES documents(doc_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entities (
            entity_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT 'concept',
            source_doc_id TEXT,
            source_chunk_id TEXT,
            confidence REAL NOT NULL DEFAULT 0.5,
            created_at REAL NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS relationships (
            rel_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            rel_type TEXT NOT NULL DEFAULT 'related_to',
            confidence REAL NOT NULL DEFAULT 0.5,
            evidence TEXT,
            created_at REAL NOT NULL,
            FOREIGN KEY (source_id) REFERENCES entities(entity_id),
            FOREIGN KEY (target_id) REFERENCES entities(entity_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rels_source ON relationships(source_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rels_target ON relationships(target_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rels_type ON relationships(rel_type)")
    conn.commit()
    return conn


def _chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping chunks."""
    if len(text) <= size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + size
        chunk = text[start:end]
        # Try to break at sentence/paragraph boundary
        if end < len(text):
            for sep in ["\n\n", "\n", "。", ".", " "]:
                last = chunk.rfind(sep)
                if last > size // 2:
                    chunk = text[start:start + last + len(sep)]
                    end = start + last + len(sep)
                    break
        chunks.append(chunk.strip())
        start = end - overlap
        if start >= len(text):
            break
    return [c for c in chunks if c]


def knowledge_ingest(path: str, title: str = "", doc_type: str = "auto") -> dict:
    """Ingest a document into the knowledge base.

    path: file path or directory
    title: document title (optional, uses filename)
    doc_type: text, markdown, code, json, auto
    """
    p = Path(path)
    err = _validate_ingest_path(path)
    if err:
        return {"error": err}
    if not p.exists():
        return {"error": f"Path not found: {path}"}

    # Handle directory
    if p.is_dir():
        results = []
        for f in p.rglob("*"):
            if f.is_file() and f.suffix in (".txt", ".md", ".py", ".js", ".json", ".yaml", ".yml", ".csv"):
                r = knowledge_ingest(str(f), doc_type=doc_type)
                results.append(r)
        return {"status": "batch", "count": len(results), "results": results}

    # Read file
    try:
        content = p.read_text()
    except UnicodeDecodeError:
        return {"error": f"Cannot read binary file: {path}"}

    if not title:
        title = p.name

    # Auto-detect type
    if doc_type == "auto":
        ext = p.suffix.lower()
        type_map = {".md": "markdown", ".py": "code", ".js": "code", ".json": "json",
                    ".yaml": "code", ".yml": "code", ".csv": "csv", ".txt": "text"}
        doc_type = type_map.get(ext, "text")

    # Generate doc ID
    doc_id = hashlib.sha256(f"{p}:{title}".encode()).hexdigest()[:16]

    # Chunk
    chunks = _chunk_text(content)

    conn = _get_kb_conn()
    now = time.time()

    # Upsert document
    conn.execute("""
        INSERT OR REPLACE INTO documents (doc_id, title, source, content, doc_type, chunk_count, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (doc_id, title, str(p), content, doc_type, len(chunks),
          now if not conn.execute("SELECT 1 FROM documents WHERE doc_id=?", (doc_id,)).fetchone() else
          conn.execute("SELECT created_at FROM documents WHERE doc_id=?", (doc_id,)).fetchone()[0],
          now))

    # Delete old chunks and insert new ones
    conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
    for i, chunk in enumerate(chunks):
        chunk_id = hashlib.sha256(f"{doc_id}:{i}".encode()).hexdigest()[:16]
        conn.execute("""
            INSERT INTO chunks (chunk_id, doc_id, content, chunk_index, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (chunk_id, doc_id, chunk, i, now))

    conn.commit()
    conn.close()

    return {
        "status": "ingested",
        "doc_id": doc_id,
        "title": title,
        "chunks": len(chunks),
        "doc_type": doc_type,
        "content_length": len(content),
    }


def _keyword_search(query: str, limit: int = 5) -> dict:
    """Keyword-based search with TF-IDF scoring."""
    conn = _get_kb_conn()

    keywords = query.lower().split()
    if not keywords:
        conn.close()
        return {"results": []}

    # Compute IDF for each keyword across the corpus
    total_chunks = max(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 1)
    idf_weights = {}
    for kw in keywords:
        df = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE LOWER(content) LIKE ?",
            (f"%{kw}%",)
        ).fetchone()[0]
        idf_weights[kw] = math.log((total_chunks + 1) / (df + 1)) + 1

    conditions = " AND ".join(["LOWER(c.content) LIKE ?" for _ in keywords])
    params = [f"%{kw}%" for kw in keywords]

    rows = conn.execute(f"""
        SELECT c.chunk_id, c.doc_id, c.content, c.chunk_index, d.title, d.source, d.doc_type
        FROM chunks c JOIN documents d ON c.doc_id = d.doc_id
        WHERE {conditions}
        ORDER BY c.chunk_index
        LIMIT ?
    """, params + [limit * 3]).fetchall()

    scored = []
    for row in rows:
        content_lower = row["content"].lower()
        score = sum(content_lower.count(kw) * idf_weights[kw] for kw in keywords)
        scored.append({
            "chunk_id": row["chunk_id"],
            "doc_id": row["doc_id"],
            "title": row["title"],
            "content": row["content"][:500],
            "score": score,
            "chunk_index": row["chunk_index"],
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    conn.close()
    return {"query": query, "results": scored[:limit]}


def knowledge_semantic_search(query: str, project: str = "", limit: int = 5) -> dict:
    """Search indexed codebases via CocoIndex semantic search.

    Returns structured results with file paths and content snippets.
    """
    from .knowledge_explorer import semantic_search
    return semantic_search(query, project=project)


def knowledge_search(query: str, limit: int = 5) -> dict:
    """Search the knowledge base for relevant chunks.

    Uses semantic search (CocoIndex) first, falls back to keyword matching.
    """
    # Try semantic search first
    semantic = knowledge_semantic_search(query, limit=limit)
    if semantic.get("results") and semantic.get("source") == "cocoindex":
        local = _keyword_search(query, limit=limit)
        combined = []
        seen = set()
        for r in semantic["results"]:
            key = r.get("file", "")
            if key not in seen:
                combined.append({
                    "title": r.get("file", ""),
                    "content": r.get("content", ""),
                    "score": r.get("score", 0),
                    "source": "cocoindex",
                })
                seen.add(key)
        for r in local.get("results", []):
            key = f"{r.get('doc_id')}/{r.get('chunk_index')}"
            if key not in seen:
                combined.append({
                    "title": r.get("title", ""),
                    "content": r.get("content", ""),
                    "score": r.get("score", 0),
                    "source": "local_kb",
                })
                seen.add(key)
        return {"query": query, "results": combined[:limit], "source": "merged"}

    # Fallback to keyword search
    return _keyword_search(query, limit=limit)


def knowledge_list(doc_type: str = "") -> dict:
    """List all documents in the knowledge base."""
    conn = _get_kb_conn()

    if doc_type:
        rows = conn.execute(
            "SELECT doc_id, title, source, doc_type, chunk_count, created_at, updated_at FROM documents WHERE doc_type=? ORDER BY updated_at DESC",
            (doc_type,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT doc_id, title, source, doc_type, chunk_count, created_at, updated_at FROM documents ORDER BY updated_at DESC"
        ).fetchall()

    conn.close()

    return {"documents": [{
        "doc_id": r["doc_id"],
        "title": r["title"],
        "source": r["source"],
        "doc_type": r["doc_type"],
        "chunk_count": r["chunk_count"],
        "updated_at": r["updated_at"],
    } for r in rows]}


def knowledge_delete(doc_id: str) -> dict:
    """Delete a document and its chunks from the knowledge base."""
    conn = _get_kb_conn()
    conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
    conn.execute("DELETE FROM documents WHERE doc_id=?", (doc_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted", "doc_id": doc_id}


# ── Knowledge Graph: Entity Extraction & Relationship Building ──

def _extract_entities(text: str, chunk_id: str = "", doc_id: str = "") -> list[dict]:
    """Extract entities from text using simple heuristics.

    Uses keyword matching and pattern recognition to identify:
    - Technologies (Python, React, Docker, etc.)
    - Concepts (API, database, authentication, etc.)
    - Files (paths, module names)
    - People/Organizations
    """
    import re
    entities = []
    seen = set()

    # Technology patterns
    tech_patterns = [
        (r'\b(Python|JavaScript|TypeScript|React|Vue|Angular|Node\.?js|Django|Flask|FastAPI|Docker|Kubernetes|AWS|GCP|Azure|SQLite|PostgreSQL|MySQL|MongoDB|Redis|Elasticsearch|Nginx|Apache|Linux|Ubuntu|Debian|macOS|Windows)\b', 'technology'),
        (r'\b(API|REST|GraphQL|gRPC|WebSocket|HTTP|HTTPS|TCP|UDP|JSON|XML|YAML|CSV|SQL|NoSQL|ORM|JWT|OAuth|SSO|LDAP|CI/CD|CI/CD|DevOps|MLOps)\b', 'concept'),
        (r'\b(Claude|OpenAI|Anthropic|GPT|LLM|AI|ML|NLP|RAG|embedding|vector|semantic|search)\b', 'technology'),
    ]

    for pattern, entity_type in tech_patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            name = match.group(1)
            key = (name.lower(), entity_type)
            if key not in seen:
                seen.add(key)
                entities.append({
                    'name': name,
                    'type': entity_type,
                    'confidence': 0.7,
                })

    # File path patterns
    file_pattern = r'([\w\-/]+\.(?:py|js|ts|jsx|tsx|json|yaml|yml|md|txt|sql|sh|dockerfile))\b'
    for match in re.finditer(file_pattern, text, re.IGNORECASE):
        name = match.group(1)
        key = (name.lower(), 'file')
        if key not in seen:
            seen.add(key)
            entities.append({
                'name': name,
                'type': 'file',
                'confidence': 0.8,
            })

    # Module/Package patterns (import statements).
    # Anchored to the start of a line so prose or SQL keywords ("in", "IS",
    # "INTEGER") are not mistaken for modules, and so "from typing import
    # Optional" only captures the real module ("typing"), not the imported name.
    module_pattern = re.compile(
        r'^\s*(?:from\s+([\w\.]+)\s+import|import\s+([\w\.]+))',
        re.MULTILINE | re.IGNORECASE
    )
    for match in module_pattern.finditer(text):
        name = (match.group(1) or match.group(2) or '').strip('.')
        key = (name.lower(), 'module')
        if name and key not in seen:
            seen.add(key)
            entities.append({
                'name': name,
                'type': 'module',
                'confidence': 0.75,
            })

    return entities


def _build_relationships(entities: list[dict], chunk_id: str = "", text: str = "") -> list[dict]:
    """Build relationships between extracted entities.

    Simple co-occurrence based relationship building.
    """
    relationships = []
    if len(entities) < 2:
        return relationships

    # Co-occurrence: if two entities appear in same chunk, they're related
    for i in range(len(entities)):
        for j in range(i + 1, len(entities)):
            e1, e2 = entities[i], entities[j]
            # Determine relationship type based on entity types
            rel_type = _infer_relationship_type(e1, e2, text)
            relationships.append({
                'source': e1['name'],
                'target': e2['name'],
                'type': rel_type,
                'confidence': (e1.get('confidence', 0.5) + e2.get('confidence', 0.5)) / 2,
            })

    return relationships


def _infer_relationship_type(e1: dict, e2: dict, text: str) -> str:
    """Infer relationship type between two entities."""
    t1, t2 = e1.get('type', ''), e2.get('type', '')
    text_lower = text.lower()

    # File imports module
    if (t1 == 'file' and t2 == 'module') or (t1 == 'module' and t2 == 'file'):
        return 'imports'
    # Technology uses/concept
    if t1 == 'technology' and t2 == 'concept':
        return 'implements'
    if t1 == 'concept' and t2 == 'technology':
        return 'implemented_by'
    # File related to file
    if t1 == 'file' and t2 == 'file':
        return 'co_occurs_with'
    # Technology related to technology
    if t1 == 'technology' and t2 == 'technology':
        return 'related_to'

    return 'related_to'


def knowledge_extract_graph(doc_id: str = "", limit: int = 100, replace: bool = False) -> dict:
    """Extract knowledge graph from documents.

    Processes chunks and builds entity-relationship graph.

    limit: max chunks to process; 0 or negative means all chunks.
    replace: if True, wipe the existing graph (entities/relationships)
             before building, giving a clean rebuild.
    """
    conn = _get_kb_conn()

    if replace:
        conn.execute("DELETE FROM relationships")
        conn.execute("DELETE FROM entities")

    # Get chunks to process
    rows = []
    if doc_id:
        rows = conn.execute(
            "SELECT chunk_id, doc_id, content FROM chunks WHERE doc_id=?",
            (doc_id,)
        ).fetchall()
    elif limit and limit > 0:
        rows = conn.execute(
            "SELECT chunk_id, doc_id, content FROM chunks LIMIT ?",
            (limit,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT chunk_id, doc_id, content FROM chunks"
        ).fetchall()

    total_entities = 0
    total_relationships = 0

    for row in rows:
        chunk_id = row['chunk_id']
        doc_id_val = row['doc_id']
        content = row['content']

        # Extract entities
        entities = _extract_entities(content, chunk_id=chunk_id, doc_id=doc_id_val)

        # Store entities
        entity_ids = {}
        for entity in entities:
            entity_id = hashlib.sha256(f"{entity['name']}:{entity['type']}".encode()).hexdigest()[:16]
            entity_ids[entity['name']] = entity_id

            conn.execute("""
                INSERT OR REPLACE INTO entities
                (entity_id, name, entity_type, source_doc_id, source_chunk_id, confidence, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (entity_id, entity['name'], entity['type'], doc_id_val, chunk_id,
                  entity.get('confidence', 0.5), time.time()))

        total_entities += len(entities)

        # Build and store relationships
        relationships = _build_relationships(entities, chunk_id=chunk_id, text=content)
        for rel in relationships:
            source_id = entity_ids.get(rel['source'])
            target_id = entity_ids.get(rel['target'])
            if not source_id or not target_id:
                continue

            rel_id = hashlib.sha256(f"{source_id}:{target_id}:{rel['type']}".encode()).hexdigest()[:16]
            conn.execute("""
                INSERT OR REPLACE INTO relationships
                (rel_id, source_id, target_id, rel_type, confidence, evidence, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (rel_id, source_id, target_id, rel['type'],
                  rel.get('confidence', 0.5), content[:200], time.time()))

        total_relationships += len(relationships)

    conn.commit()
    conn.close()

    return {
        "status": "extracted",
        "chunks_processed": len(rows),
        "entities_extracted": total_entities,
        "relationships_built": total_relationships,
    }


def _ensure_graph_built() -> bool:
    """Lazily build the knowledge graph on first access.

    If the entities table is empty but chunks exist, run a full clean rebuild.
    Best-effort: never raise — callers should keep working even if the build fails.
    Returns True if a build was triggered.
    """
    try:
        conn = _get_kb_conn()
        entity_count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        conn.close()
        if entity_count == 0 and chunk_count > 0:
            knowledge_extract_graph(limit=0, replace=True)
            return True
    except Exception:
        logging.getLogger(__name__).exception('lazy graph build failed')
    return False


def knowledge_graph_query(entity_name: str = "", entity_type: str = "", limit: int = 20) -> dict:
    """Query the knowledge graph for entities and their relationships."""
    _ensure_graph_built()
    conn = _get_kb_conn()

    results = {"entities": [], "relationships": []}

    # Query entities
    if entity_name:
        rows = conn.execute("""
            SELECT entity_id, name, entity_type, confidence
            FROM entities WHERE name LIKE ? LIMIT ?
        """, (f"%{entity_name}%", limit)).fetchall()
    elif entity_type:
        rows = conn.execute("""
            SELECT entity_id, name, entity_type, confidence
            FROM entities WHERE entity_type = ? LIMIT ?
        """, (entity_type, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT entity_id, name, entity_type, confidence
            FROM entities LIMIT ?
        """, (limit,)).fetchall()

    results["entities"] = [dict(r) for r in rows]

    # Query relationships for found entities
    entity_ids = [r["entity_id"] for r in results["entities"]]
    if entity_ids:
        placeholders = ','.join('?' * len(entity_ids))
        rel_rows = conn.execute(f"""
            SELECT r.rel_id, r.source_id, r.target_id, r.rel_type, r.confidence,
                   s.name as source_name, t.name as target_name
            FROM relationships r
            JOIN entities s ON r.source_id = s.entity_id
            JOIN entities t ON r.target_id = t.entity_id
            WHERE r.source_id IN ({placeholders}) OR r.target_id IN ({placeholders})
            LIMIT ?
        """, entity_ids + entity_ids + [limit]).fetchall()

        results["relationships"] = [dict(r) for r in rel_rows]

    conn.close()
    return results


def knowledge_graph_stats() -> dict:
    """Return statistics about the knowledge graph."""
    _ensure_graph_built()
    conn = _get_kb_conn()

    entity_count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    rel_count = conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]

    # Entity type distribution
    type_rows = conn.execute("""
        SELECT entity_type, COUNT(*) as count FROM entities GROUP BY entity_type
    """).fetchall()

    # Relationship type distribution
    rel_type_rows = conn.execute("""
        SELECT rel_type, COUNT(*) as count FROM relationships GROUP BY rel_type
    """).fetchall()

    conn.close()

    return {
        "entities": entity_count,
        "relationships": rel_count,
        "entity_types": {r["entity_type"]: r["count"] for r in type_rows},
        "relationship_types": {r["rel_type"]: r["count"] for r in rel_type_rows},
    }