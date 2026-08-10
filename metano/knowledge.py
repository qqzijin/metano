"""RAG knowledge base: document ingestion, chunking, embedding, and retrieval."""

import hashlib
import json
import logging
import math
import os
import sqlite3
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

KB_DIR = Path.home() / ".claude" / "metano" / "knowledge"
KB_DB = KB_DIR / "knowledge.db"
PROJECT_ROOT = Path.home() / ".claude" / "metano"
ALLOWED_INGEST_PREFIXES = [PROJECT_ROOT, Path.home() / "scrapling-project", Path.home() / "DailyHotApi"]

CHUNK_SIZE = 1000
CHUNK_OVERLAP = 200

# ── Local embeddings (chunks.embedding) ────────────────────────────────────
# The embedding model (snowflake-arctic-embed-xs) is served from the local HF
# cache by a python that has sentence-transformers + torch — the cocoindex
# pipx venv. Query embeddings use the model's "query" prompt; passage/chunk
# embeddings use no prompt. Both are unit-normalized, so cosine similarity is a
# plain dot product. Embeddings are stored in chunks.embedding as raw
# little-endian float32 blobs (384 floats).
EMBED_MODEL = "Snowflake/snowflake-arctic-embed-xs"
_EMBED_HELPER = Path(__file__).resolve().parent / "_embed_helper.py"
_EMBED_FILLER = Path(__file__).resolve().parent / "_embed_filler.py"
_embed_proc: Optional[subprocess.Popen] = None
_embed_lock = threading.Lock()


def _find_embed_python() -> Optional[str]:
    """Locate a python interpreter that can import sentence-transformers.

    Prefers the METANO_EMBED_PYTHON env var, else the cocoindex pipx venv.
    Returns None when no candidate exists (callers should then fall back to
    CocoIndex or report an error).
    """
    forced = os.environ.get("METANO_EMBED_PYTHON")
    if forced and Path(forced).exists():
        return forced
    for cand in (
        str(Path.home() / ".local/share/pipx/venvs/cocoindex-code/bin/python"),
        "/home/dk/.local/share/pipx/venvs/cocoindex-code/bin/python",
    ):
        if Path(cand).exists():
            return cand
    return None


def _reset_embed_proc() -> None:
    """Close/terminate the persistent embed helper, if any."""
    global _embed_proc
    if _embed_proc is not None:
        try:
            _embed_proc.stdin.close()
        except Exception:
            pass
        try:
            _embed_proc.terminate()
        except Exception:
            pass
        _embed_proc = None


def _ensure_embed_proc() -> subprocess.Popen:
    """Return the live persistent embed helper, spawning it if needed."""
    global _embed_proc
    if _embed_proc is not None and _embed_proc.poll() is None:
        return _embed_proc
    py = _find_embed_python()
    if py is None:
        raise RuntimeError(
            "No python with sentence-transformers found; set METANO_EMBED_PYTHON"
        )
    _embed_proc = subprocess.Popen(
        [py, str(_EMBED_HELPER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env={**os.environ, "HF_HUB_OFFLINE": "1"},
    )
    return _embed_proc


def _embed_texts(texts: list[str], prompt_name: Optional[str] = None) -> list[list[float]]:
    """Embed a list of texts with the local model via the persistent helper.

    prompt_name="query" applies the model's query prompt (asymmetric search).
    Returns a list of unit-normalized float vectors.
    """
    if not texts:
        return []
    with _embed_lock:
        req = {"texts": texts, "prompt_name": prompt_name}
        for attempt in range(2):
            proc = _ensure_embed_proc()
            try:
                proc.stdin.write(json.dumps(req) + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
            except (BrokenPipeError, ValueError, OSError):
                _reset_embed_proc()
                if attempt == 1:
                    raise
                continue
            if not line:
                _reset_embed_proc()
                if attempt == 1:
                    raise RuntimeError("embed helper closed unexpectedly")
                continue
            resp = json.loads(line)
            if "error" in resp:
                raise RuntimeError(f"embed helper error: {resp['error']}")
            return resp["embeddings"]
    raise RuntimeError("embed helper unavailable")


def _blob_to_vec(blob: bytes) -> tuple[float, ...] | None:
    """Decode a stored float32 BLOB into a tuple of floats."""
    if not blob:
        return None
    return struct.unpack(f"<{len(blob) // 4}f", blob)


def _vec_to_blob(vec) -> bytes:
    """Encode a float vector into the raw little-endian float32 BLOB format."""
    return struct.pack(f"<{len(vec)}f", *vec)


def _embedding_count() -> int:
    """Number of chunks that have an embedding stored."""
    conn = _get_kb_conn()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL"
        ).fetchone()[0]
    finally:
        conn.close()


def populate_embeddings(batch_size: int = 64) -> dict:
    """Fill chunks.embedding for every chunk that lacks one. Idempotent.

    Runs the one-shot filler (_embed_filler.py) under the interpreter that has
    sentence-transformers (the cocoindex pipx venv). The model is read from
    the local HF cache with HF_HUB_OFFLINE=1, so no network access is needed.
    Chunks with a non-null embedding are skipped, so re-running only processes
    chunks that were added after the last run.
    """
    py = _find_embed_python()
    if py is None:
        return {
            "error": "No python with sentence-transformers found. "
                     "Install it or set METANO_EMBED_PYTHON.",
        }
    try:
        result = subprocess.run(
            [py, str(_EMBED_FILLER), str(KB_DB), str(batch_size)],
            capture_output=True,
            text=True,
            timeout=3600,
            env={**os.environ, "HF_HUB_OFFLINE": "1"},
        )
    except subprocess.TimeoutExpired:
        return {"error": "embedding fill timed out"}
    if result.returncode != 0:
        return {"error": (result.stderr or result.stdout)[-500:]}
    summary: dict = {"raw": result.stdout.strip()}
    for line in result.stdout.strip().splitlines():
        if line.startswith("{"):
            try:
                summary = json.loads(line)
            except json.JSONDecodeError:
                continue
    summary["batch_size"] = batch_size
    return summary


def _vector_search(query: str, limit: int = 5) -> dict:
    """Local vector search over chunk embeddings (no CocoIndex dependency).

    Embeds the query with the local cached model and scores every chunk by
    cosine similarity. Embeddings are unit-normalized, so cosine == dot.
    Returns results shaped like _keyword_search with real cosine scores.
    """
    conn = _get_kb_conn()
    try:
        total = _embedding_count()
        if total == 0:
            return {"query": query, "results": [], "source": "local_vector",
                    "error": "no_embeddings"}
        try:
            qvec = _embed_texts([query], prompt_name="query")[0]
        except Exception:
            logging.getLogger(__name__).exception("query embedding failed")
            return {"query": query, "results": [], "source": "local_vector",
                    "error": "embed_failed"}
        rows = conn.execute(
            "SELECT c.chunk_id, c.doc_id, c.content, c.chunk_index, c.embedding,"
            " d.title, d.source, d.doc_type"
            " FROM chunks c JOIN documents d ON c.doc_id = d.doc_id"
            " WHERE c.embedding IS NOT NULL"
        ).fetchall()
        scored = []
        for r in rows:
            ev = _blob_to_vec(r["embedding"])
            if ev is None:
                continue
            sim = sum(a * b for a, b in zip(qvec, ev))
            scored.append({
                "chunk_id": r["chunk_id"],
                "doc_id": r["doc_id"],
                "title": r["title"],
                "content": r["content"][:500],
                "score": round(sim, 6),
                "chunk_index": r["chunk_index"],
                "source": "local_vector",
            })
        scored.sort(key=lambda x: x["score"], reverse=True)
        return {"query": query, "results": scored[:limit], "source": "local_vector"}
    finally:
        conn.close()


def knowledge_vector_search(query: str, limit: int = 5) -> dict:
    """Local vector (semantic) search over the knowledge base chunks.

    Uses the cached snowflake-arctic-embed-xs model for the query and the
    per-chunk embeddings stored in chunks.embedding. No CocoIndex dependency
    and no CocoIndex CLI cold start.
    """
    return _vector_search(query, limit=limit)


def _merge_results(query: str, semantic_results: list, semantic_source: str,
                   keyword_results: list, limit: int) -> dict:
    """Merge semantic (score < 1.0) results with real TF-IDF keyword hits.

    Semantic results carry a score below 1.0 — either the CocoIndex rank
    discount or a cosine similarity — so a genuine TF-IDF keyword match
    (>= ~1.0) always outranks them, while relative semantic order among
    semantic hits is preserved. This keeps the documented ranking invariant:
    real keyword matches are never pushed out by weak semantic noise.
    """
    combined = []
    seen = set()
    n_sem = len(semantic_results)
    for i, r in enumerate(semantic_results):
        if semantic_source == "cocoindex":
            key = r.get("file", "")
            score = round((n_sem - i) / (n_sem + 1), 4) if n_sem else 0.0
            title = r.get("file", "")
        else:  # local_vector
            key = f"{r.get('doc_id')}/{r.get('chunk_index')}"
            score = r.get("score", 0.0)
            title = r.get("title", "")
        if key not in seen:
            combined.append({
                "title": title,
                "content": r.get("content", ""),
                "score": score,
                "source": semantic_source,
            })
            seen.add(key)
    for r in keyword_results or []:
        key = f"{r.get('doc_id')}/{r.get('chunk_index')}"
        if key not in seen:
            combined.append({
                "title": r.get("title", ""),
                "content": r.get("content", ""),
                "score": r.get("score", 0),
                "source": "local_kb",
            })
            seen.add(key)
    combined.sort(key=lambda x: x["score"], reverse=True)
    return {"query": query, "results": combined[:limit], "source": "merged"}


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

    When chunk embeddings are populated, semantic results come from a fast
    local vector search over the KB itself (no CocoIndex CLI cold start),
    merged with TF-IDF keyword hits. When embeddings are absent, falls back to
    the legacy CocoIndex semantic search merged with keyword hits.

    Ranking invariant (unchanged): real TF-IDF keyword scores (>= ~1.0)
    always outrank semantic scores (CocoIndex rank discount or cosine
    similarity, both < 1.0), so genuine keyword matches are never pushed out
    by weak semantic noise. The merged list is sorted by score desc and only
    then truncated to `limit`.
    """
    # Local vector search first — fast, no external CocoIndex dependency.
    if _embedding_count() > 0:
        vec = _vector_search(query, limit=limit)
        if vec.get("results") and vec.get("source") == "local_vector":
            local = _keyword_search(query, limit=limit)
            return _merge_results(
                query, vec["results"], "local_vector", local.get("results", []), limit
            )

    # Fallback: CocoIndex semantic search merged with keyword hits.
    semantic = knowledge_semantic_search(query, limit=limit)
    if semantic.get("results") and semantic.get("source") == "cocoindex":
        local = _keyword_search(query, limit=limit)
        return _merge_results(
            query, semantic["results"], "cocoindex", local.get("results", []), limit
        )

    # Last resort: keyword-only search.
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


def _load_graph_adjacency(conn, beta=0.0):
    """Build an undirected, confidence-weighted adjacency list from relationships.

    Co-occurrence relationships are semantically undirected, so every row is
    added in both directions; self-loops are dropped. Edge weight is the
    relationship confidence (floored at 0.1) so stronger evidence flows more
    probability.

    `beta` is the degree-correction exponent: with beta=0 the random-walk
    transition weight from i to j is w_ij / out_weight_i (plain PPR); with
    beta>0 the target's degree is factored in as w_ij / (deg_i^beta * deg_j^beta)
    (renormalized per node). Degree correction suppresses the hub-dominance of
    dense co-occurrence graphs, so topically specific low-degree entities
    (e.g. fastapi.middleware.cors, starlette) rank above generic high-degree
    hubs (json, Python) for a "fastapi" query.

    Returns {entity_id: [(neighbor_id, transition_prob), ...]}.
    """
    adj_weight = {}
    for src, tgt, conf in conn.execute(
        "SELECT source_id, target_id, confidence FROM relationships"
    ):
        if src == tgt:
            continue
        w = max(float(conf or 0.5), 0.1)
        d = adj_weight.setdefault(src, {})
        d[tgt] = d.get(tgt, 0.0) + w
        d = adj_weight.setdefault(tgt, {})
        d[src] = d.get(src, 0.0) + w

    if beta:
        deg = {node: sum(nbrs.values()) for node, nbrs in adj_weight.items()}
        adj = {}
        for node, nbrs in adj_weight.items():
            raw = [(nbr, w / (deg[node] ** beta * deg[nbr] ** beta))
                   for nbr, w in nbrs.items()]
            total = sum(w for _, w in raw)
            adj[node] = [(nbr, w / total) for nbr, w in raw]
    else:
        adj = {}
        for node, nbrs in adj_weight.items():
            total = sum(nbrs.values())
            adj[node] = [(nbr, w / total) for nbr, w in nbrs.items()]
    return adj


def _ppr_related(seed_ids, conn, alpha=0.15, beta=0.8, max_iter=80, tol=1e-7):
    """Personalized PageRank over the knowledge graph (HippoRAG-inspired).

    Seeds start with equal probability mass; each iteration diffuses mass along
    graph edges and teleports a fraction `alpha` (0.15) back to the seed set.
    The result is a ranked distribution where the seed entities score highest,
    direct neighbors next, and multi-hop (indirectly related) entities still
    carry non-zero mass — so a query like "fastapi" surfaces starlette and
    web_server even without a direct relationship.

    `beta` (0.8) applies the degree correction described in
    _load_graph_adjacency: it counters hub dominance and surfaces topically
    specific low-degree entities above generic high-degree hubs.

    Returns {entity_id: score} for every reachable graph node. Purely in
    memory over the ~500-node / ~5800-edge graph: converges in a few dozen
    iterations, well under 1s, with no external dependencies.
    """
    if not seed_ids:
        return {}
    adj = _load_graph_adjacency(conn, beta=beta)

    seeds = [s for s in seed_ids if s in adj]
    if not seeds:
        # Seeds have no edges at all: nothing to propagate, return uniform mass.
        return {s: 1.0 / len(seed_ids) for s in seed_ids}

    nodes = list(adj.keys())
    index = {n: i for i, n in enumerate(nodes)}
    n = len(nodes)

    p = [0.0] * n
    seed_mass = 1.0 / len(seeds)
    for s in seeds:
        p[index[s]] = seed_mass
    restart = list(p)

    neighbors = [[(index[nbr], w) for nbr, w in adj[node]] for node in nodes]

    one_minus_alpha = 1.0 - alpha
    for _ in range(max_iter):
        p_new = [alpha * restart[i] for i in range(n)]
        for i in range(n):
            pi = p[i]
            if pi == 0.0:
                continue
            spread = one_minus_alpha * pi
            for j, w in neighbors[i]:
                p_new[j] += spread * w
        diff = 0.0
        for i in range(n):
            diff += abs(p_new[i] - p[i])
        p = p_new
        if diff < tol:
            break

    return {nodes[i]: p[i] for i in range(n)}


def _relationships_between(conn, ids, cap, both=True):
    """Return relationship edges touching `ids`.

    both=True: edges whose *both* endpoints are in `ids` (used by the PPR
    path — relationships are the edges between the returned related entities).
    both=False: edges where *either* endpoint is in `ids` (used by type/browse
    listings, preserving the original wide relationship view).
    """
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    if both:
        where = f"r.source_id IN ({placeholders}) AND r.target_id IN ({placeholders})"
    else:
        where = f"r.source_id IN ({placeholders}) OR r.target_id IN ({placeholders})"
    rows = conn.execute(
        f"""
        SELECT r.rel_id, r.source_id, r.target_id, r.rel_type, r.confidence,
               s.name AS source_name, t.name AS target_name
        FROM relationships r
        JOIN entities s ON r.source_id = s.entity_id
        JOIN entities t ON r.target_id = t.entity_id
        WHERE {where}
        LIMIT ?
        """,
        ids + ids + [cap],
    ).fetchall()
    return [dict(r) for r in rows]


def _query_graph_by_name(conn, name, limit):
    """Entity/keyword query: exact-name seeds + Personalized PageRank diffusion.

    Seeds are exact (case-insensitive) name matches, falling back to substring
    matches for partial/fuzzy keywords. PPR then ranks the most relevant
    entities — both direct neighbors and multi-hop related ones. Seed entities
    are returned first (relatedness 1.0, is_seed=True), followed by the top
    `limit` related entities with their `relatedness` score. Entities sharing
    a relationship edge with any seed are flagged is_direct=True.
    """
    rows = conn.execute(
        "SELECT entity_id, name, entity_type, confidence FROM entities"
        " WHERE LOWER(name) = LOWER(?) ORDER BY confidence DESC LIMIT ?",
        (name, max(limit, 10)),
    ).fetchall()
    if not rows:
        rows = conn.execute(
            "SELECT entity_id, name, entity_type, confidence FROM entities"
            " WHERE name LIKE ? ORDER BY confidence DESC LIMIT ?",
            (f"%{name}%", max(limit, 10)),
        ).fetchall()
    if not rows:
        return {"query": name, "entities": [], "relationships": [],
                "message": "no matching entities"}

    seed_rows = rows[:10]
    seed_ids = [r["entity_id"] for r in seed_rows]
    ppr = _ppr_related(seed_ids, conn)

    # Direct neighbors of the seeds (share at least one relationship edge) —
    # the "直接相关" entities. The PPR tail below covers multi-hop / indirect
    # ("PPR 相关") entities.
    direct_ids = set()
    if seed_ids:
        ph = ",".join("?" * len(seed_ids))
        for r in conn.execute(
            f"SELECT source_id, target_id FROM relationships"
            f" WHERE source_id IN ({ph}) OR target_id IN ({ph})",
            seed_ids + seed_ids,
        ):
            if r["source_id"] != r["target_id"]:
                direct_ids.add(r["source_id"])
                direct_ids.add(r["target_id"])

    entities = []
    seen = set()
    for r in seed_rows:
        if len(entities) >= limit:
            break
        d = dict(r)
        d["relatedness"] = 1.0
        d["is_seed"] = True
        d["is_direct"] = True
        entities.append(d)
        seen.add(d["entity_id"])

    for eid, score in sorted(ppr.items(), key=lambda kv: kv[1], reverse=True):
        if len(entities) >= limit:
            break
        if eid in seen or score <= 0:
            continue
        row = conn.execute(
            "SELECT entity_id, name, entity_type, confidence FROM entities"
            " WHERE entity_id = ?",
            (eid,),
        ).fetchone()
        if row is None:
            continue
        d = dict(row)
        d["relatedness"] = round(score, 6)
        d["is_seed"] = False
        d["is_direct"] = eid in direct_ids
        entities.append(d)
        seen.add(eid)

    ids = [e["entity_id"] for e in entities]
    return {
        "query": name,
        "entities": entities,
        "relationships": _relationships_between(conn, ids, max(limit * 2, 200)),
    }


def _query_graph_by_type(conn, entity_type, limit):
    """Type-filtered listing (no PPR): all entities of a type, with relations."""
    rows = conn.execute(
        "SELECT entity_id, name, entity_type, confidence FROM entities"
        " WHERE entity_type = ? ORDER BY confidence DESC LIMIT ?",
        (entity_type, limit),
    ).fetchall()
    entities = []
    for r in rows:
        d = dict(r)
        d["relatedness"] = 1.0
        d["is_seed"] = True
        entities.append(d)
    ids = [d["entity_id"] for d in entities]
    return {
        "entity_type": entity_type,
        "entities": entities,
        "relationships": _relationships_between(conn, ids, max(limit * 2, 200), both=False),
    }


def _query_graph_all(conn, limit):
    """Browse mode (no query / no type): top entities, with their relations."""
    rows = conn.execute(
        "SELECT entity_id, name, entity_type, confidence FROM entities"
        " ORDER BY confidence DESC LIMIT ?",
        (limit,),
    ).fetchall()
    entities = []
    for r in rows:
        d = dict(r)
        d["relatedness"] = 1.0
        d["is_seed"] = True
        entities.append(d)
    ids = [d["entity_id"] for d in entities]
    return {
        "entities": entities,
        "relationships": _relationships_between(conn, ids, max(limit * 2, 200), both=False),
    }


def knowledge_graph_query(entity_name: str = "", entity_type: str = "", limit: int = 50) -> dict:
    """Query the knowledge graph for entities and their relationships.

    When an entity name / keyword is given, a Personalized PageRank (PPR)
    diffusion (HippoRAG-style) is run over the relationship graph so that not
    only direct neighbors but also indirectly related entities (via multi-hop
    propagation) are returned, ranked by their `relatedness` score.

    Return shape (backwards compatible with the frontend KnowledgeGraphPage):
      entities: [{entity_id, name, entity_type, confidence, relatedness, is_seed}]
      relationships: [{rel_id, source_id, target_id, rel_type, confidence,
                       source_name, target_name}]
    """
    _ensure_graph_built()
    conn = _get_kb_conn()
    try:
        if entity_name:
            return _query_graph_by_name(conn, entity_name, max(limit, 1))
        if entity_type:
            return _query_graph_by_type(conn, entity_type, max(limit, 1))
        return _query_graph_all(conn, max(limit, 1))
    finally:
        conn.close()


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