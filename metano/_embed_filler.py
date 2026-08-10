"""One-shot batch embedder: fills chunks.embedding in knowledge.db.

Idempotent — chunks whose embedding column is already non-null are skipped.

Usage:  python _embed_filler.py <knowledge.db> [batch_size]

Run under the cocoindex pipx venv python (has sentence-transformers + torch).
The model (snowflake-arctic-embed-xs) is read from the local HF cache with
HF_HUB_OFFLINE=1, so no network access is needed.

Embeddings are stored as raw little-endian float32 blobs (384 floats each) so
knowledge.py can read them back with struct.unpack without numpy.
"""

from __future__ import annotations

import json
import os
import sqlite3
import struct
import sys
import time

MODEL_NAME = os.environ.get("METANO_EMBED_MODEL", "Snowflake/snowflake-arctic-embed-xs")


def _to_blob(vec) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def main() -> None:
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: _embed_filler.py <knowledge.db> [batch_size]"}))
        sys.exit(2)
    db_path = sys.argv[1]
    batch_size = int(sys.argv[2]) if len(sys.argv) > 2 else 64

    os.environ["HF_HUB_OFFLINE"] = "1"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT chunk_id, content FROM chunks WHERE embedding IS NULL"
    ).fetchall()
    total = len(rows)
    print(json.dumps({"to_fill": total}), flush=True)

    # Nothing to do — skip loading the model entirely (idempotent re-run).
    if total == 0:
        conn.close()
        print(json.dumps({"filled": 0, "skipped": 0, "elapsed": 0}), flush=True)
        return

    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(MODEL_NAME)

    filled = 0
    t0 = time.time()
    for start in range(0, total, batch_size):
        batch = rows[start:start + batch_size]
        texts = [r["content"] for r in batch]
        emb = model.encode(texts, batch_size=batch_size, normalize_embeddings=True)
        for i, row in enumerate(batch):
            conn.execute(
                "UPDATE chunks SET embedding=? WHERE chunk_id=?",
                (_to_blob(emb[i]), row["chunk_id"]),
            )
        conn.commit()
        filled += len(batch)
        print(json.dumps({"filled": filled, "of": total,
                          "elapsed": round(time.time() - t0, 1)}), flush=True)

    conn.close()
    print(json.dumps({"filled": filled, "skipped": 0,
                      "elapsed": round(time.time() - t0, 1)}), flush=True)


if __name__ == "__main__":
    main()
