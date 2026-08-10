"""Persistent local text-embedding helper for the knowledge base.

Runs under the cocoindex pipx venv python (the interpreter that has
sentence-transformers + torch). It loads the cached snowflake-arctic-embed-xs
model once, then serves embedding requests over a JSON-lines protocol on
stdin/stdout:

  request:  {"texts": [...], "prompt_name": "query"|null, "batch_size": 64}
  response: {"embeddings": [[...float...], ...]}    # unit-normalized
            {"error": "..."}

The process lives as long as its parent keeps stdin open; when stdin hits EOF
the helper exits, so a dead parent never leaves a stray process behind.
"""

from __future__ import annotations

import json
import os
import sys

MODEL_NAME = os.environ.get("METANO_EMBED_MODEL", "Snowflake/snowflake-arctic-embed-xs")


def main() -> None:
    os.environ["HF_HUB_OFFLINE"] = "1"
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(MODEL_NAME)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            texts = req.get("texts") or []
            prompt_name = req.get("prompt_name")
            batch_size = int(req.get("batch_size") or 32)
            kwargs = {"batch_size": batch_size, "normalize_embeddings": True}
            if prompt_name:
                kwargs["prompt_name"] = prompt_name
            emb = model.encode(texts, **kwargs)
            out = {"embeddings": emb.tolist()}
        except Exception as e:  # noqa: BLE001 - report any failure back to caller
            out = {"error": f"{type(e).__name__}: {e}"}
        try:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
        except BrokenPipeError:
            break


if __name__ == "__main__":
    main()
