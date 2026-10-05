"""A complete search plugin: a vector index the hub keeps in step and queries for candidates.

Run it as its own service. The hub sends it the records an instance is allowed to see; it keeps its own index and
answers `search` with record ids and scores. It never reads the hub's database and never receives a token.

    INDEX_PLUGIN_SECRET=<32+ random characters> uvicorn app:app --port 9100

    MEMORY_HUB_REMOTE_PLUGINS='[{"key":"embeddings","url":"http://127.0.0.1:9100","secret_env":"INDEX_PLUGIN_SECRET"}]'

Embedding backends (EMBEDDER):
  hash    (default) hashed word + bigram vectors with cosine similarity. Dependency-free and fully offline, but it is
          *lexical*, not truly semantic: "car" will not match "automobile". Good for a zero-setup start and for tests.
  ollama  POST {OLLAMA_URL}/api/embeddings  {"model": EMBED_MODEL, "prompt": text}  -> {"embedding": [...]}
  openai  POST {EMBED_URL}/v1/embeddings    {"model": EMBED_MODEL, "input": text}   -> {"data": [{"embedding": [...]}]}
          (any OpenAI-compatible server; EMBED_API_KEY is sent as a bearer token if set)

The model-backed paths are written against those documented shapes and tested against local stand-ins, not against
a real Ollama or OpenAI server.

The index is a SQLite file (INDEX_DB, default ./index.sqlite3), partitioned by hub instance id so two instances never
see each other's vectors. Vectors are stored as JSON; cosine similarity is computed in pure Python, which is fine for
thousands of records (use a real vector store behind the same protocol if you have far more).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import urllib.request
from collections import Counter

from acm_hub.plugins.remote_sdk import create_app

DIM = 512
WORD = re.compile(r"[a-z0-9]{2,}")
LOCK = threading.Lock()

MANIFEST = {
    "key": "embeddings",
    "name": "Embedding index",
    "description": "Adds semantic-style search to task focus by keeping its own vector index of the records this instance may see.",
    "kind": "search",
    "config": [],
}


# --- embedders ----------------------------------------------------------------------------------------------------------


def embed_hash(text: str) -> list[float]:
    words = WORD.findall(text.lower())
    feats = Counter(words) + Counter(f"{a}_{b}" for a, b in zip(words, words[1:], strict=False))
    vec = [0.0] * DIM
    for token, n in feats.items():
        h = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big")
        vec[h % DIM] += (1 if (h >> 32) & 1 else -1) * (
            1 + math.log(n)
        )  # signed hashing: collisions cancel on average
    return vec


def _post_json(url: str, payload: dict, headers: dict | None = None) -> dict:
    req = urllib.request.Request(
        url, json.dumps(payload).encode(), {"Content-Type": "application/json", **(headers or {})}
    )
    with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 — operator-configured embedding endpoint
        return json.loads(r.read(5_000_000))


def embed_ollama(text: str) -> list[float]:
    out = _post_json(
        os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/") + "/api/embeddings",
        {"model": os.environ.get("EMBED_MODEL", "nomic-embed-text"), "prompt": text},
    )
    return [float(x) for x in out["embedding"]]


def embed_openai(text: str) -> list[float]:
    headers = (
        {"Authorization": f"Bearer {os.environ['EMBED_API_KEY']}"} if os.environ.get("EMBED_API_KEY") else {}
    )
    out = _post_json(
        os.environ["EMBED_URL"].rstrip("/") + "/v1/embeddings",
        {"model": os.environ.get("EMBED_MODEL", "text-embedding-3-small"), "input": text},
        headers,
    )
    return [float(x) for x in out["data"][0]["embedding"]]


def embed(text: str) -> list[float]:
    kind = os.environ.get("EMBEDDER", "hash")
    vec = {"hash": embed_hash, "ollama": embed_ollama, "openai": embed_openai}[kind](text[:8000])
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


# --- the index ------------------------------------------------------------------------------------------------------------


def db() -> sqlite3.Connection:
    con = sqlite3.connect(os.environ.get("INDEX_DB", "./index.sqlite3"))
    con.execute(
        "CREATE TABLE IF NOT EXISTS vec (instance TEXT, id TEXT, name TEXT, vector TEXT, PRIMARY KEY (instance, id))"
    )
    return con


def doc_text(rec: dict) -> str:
    return "\n".join(
        filter(
            None,
            [
                rec.get("name", ""),
                rec.get("description", ""),
                " ".join(rec.get("topics") or []),
                rec.get("body", ""),
            ],
        )
    )


def index(instance: dict, records: list[dict]) -> dict:
    rows = [(instance["id"], r["id"], r.get("name", ""), json.dumps(embed(doc_text(r)))) for r in records]
    with LOCK, db() as con:
        con.executemany("INSERT OR REPLACE INTO vec VALUES (?,?,?,?)", rows)
    return {"ok": True, "indexed": len(rows)}


def remove(instance: dict, ids: list[str]) -> dict:
    with LOCK, db() as con:
        con.executemany("DELETE FROM vec WHERE instance=? AND id=?", [(instance["id"], i) for i in ids])
    return {"ok": True}


def reset(instance: dict) -> dict:
    with LOCK, db() as con:
        con.execute("DELETE FROM vec WHERE instance=?", (instance["id"],))
    return {"ok": True}


def search(instance: dict, query: str, limit: int) -> dict:
    q = embed(query)
    with db() as con:
        rows = con.execute("SELECT id, vector FROM vec WHERE instance=?", (instance["id"],)).fetchall()
    scored = []
    for rid, raw in rows:
        v = json.loads(raw)
        s = sum(a * b for a, b in zip(q, v, strict=False))
        if s > 0.05:  # nothing in common is not a match
            scored.append({"id": rid, "score": round(s, 4)})
    scored.sort(key=lambda r: -r["score"])
    return {"results": scored[: max(1, min(limit, 50))]}


app = create_app(
    secret=os.environ.get("INDEX_PLUGIN_SECRET", ""),
    manifest=MANIFEST,
    index=index,
    remove=remove,
    reset=reset,
    search=search,
)
