"""
Semantic index for the Obsidian vault.
Uses nomic-embed-text via Ollama to generate embeddings and cosine similarity for search.
Falls back gracefully if the embedding model is unavailable.
"""
import json
import logging
import os
import threading
from pathlib import Path

import ollama

log = logging.getLogger(__name__)

VAULT_INDEX_FILE = os.getenv("VAULT_INDEX_FILE", "/app/data/vault_index.json")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
OLLAMA_HOST = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
SIMILARITY_THRESHOLD = float(os.getenv("EMBED_SIMILARITY_THRESHOLD", "0.3"))

_client: ollama.Client | None = None
_index: dict[str, dict] = {}
_lock = threading.Lock()
_available: bool | None = None  # None = not yet checked


def _get_client() -> ollama.Client:
    global _client
    if _client is None:
        _client = ollama.Client(host=OLLAMA_HOST)
    return _client


def _check_available() -> bool:
    """Check once whether the embedding model is reachable."""
    global _available
    if _available is not None:
        return _available
    try:
        _get_client().embeddings(model=EMBED_MODEL, prompt="test")
        _available = True
        log.info(f"Embedding model '{EMBED_MODEL}' is available.")
    except Exception as e:
        _available = False
        log.warning(f"Embedding model '{EMBED_MODEL}' not available — semantic search disabled. ({e})")
    return _available


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(x * x for x in b) ** 0.5
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def _embed(text: str) -> list[float] | None:
    try:
        resp = _get_client().embeddings(model=EMBED_MODEL, prompt=text[:4000])
        return resp["embedding"]
    except Exception as e:
        log.debug(f"Embedding call failed: {e}")
        return None


def _load() -> dict:
    path = Path(VAULT_INDEX_FILE)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save():
    path = Path(VAULT_INDEX_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        snapshot = dict(_index)
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")


# ── Public API ────────────────────────────────────────────────────────────────

def build_index(vault: Path):
    """Build/refresh full semantic index. Safe to call in a background thread."""
    if not _check_available():
        return

    global _index
    log.info("Building vault semantic index…")
    with _lock:
        _index = _load()

    notes = [p for p in vault.rglob("*.md") if not any(part.startswith(".") for part in p.parts)]
    existing_rels = {str(p.relative_to(vault)) for p in notes}

    updated = 0
    for note_path in notes:
        rel = str(note_path.relative_to(vault))
        try:
            mtime = note_path.stat().st_mtime
            if _index.get(rel, {}).get("mtime") == mtime:
                continue
            content = note_path.read_text(encoding="utf-8", errors="ignore")
            embedding = _embed(f"{note_path.stem}\n{content}")
            if embedding is None:
                continue
            with _lock:
                _index[rel] = {
                    "embedding": embedding,
                    "mtime": mtime,
                    "excerpt": content[:400].strip(),
                }
            updated += 1
        except Exception as e:
            log.warning(f"Failed to index {rel}: {e}")

    # Remove stale entries for deleted notes
    with _lock:
        stale = [k for k in _index if k not in existing_rels]
        for k in stale:
            del _index[k]

    _save()
    log.info(f"Vault index ready: {len(_index)} notes ({updated} updated, {len(stale)} removed).")


def update_note(vault: Path, note_path: Path):
    """Update a single note's embedding after a write. Non-blocking."""
    if not _check_available():
        return

    def _run():
        rel = str(note_path.relative_to(vault))
        try:
            content = note_path.read_text(encoding="utf-8", errors="ignore")
            embedding = _embed(f"{note_path.stem}\n{content}")
            if embedding is None:
                return
            with _lock:
                _index[rel] = {
                    "embedding": embedding,
                    "mtime": note_path.stat().st_mtime,
                    "excerpt": content[:400].strip(),
                }
            _save()
            log.debug(f"Index updated for {rel}")
        except Exception as e:
            log.warning(f"Index update failed for {rel}: {e}")

    threading.Thread(target=_run, daemon=True).start()


def remove_note(vault: Path, note_path: Path):
    rel = str(note_path.relative_to(vault))
    with _lock:
        _index.pop(rel, None)
    _save()


def search_similar(vault: Path, query: str, top_k: int = 3) -> list[dict]:
    """
    Return top_k semantically similar notes.
    Returns empty list if embedding model is unavailable (caller should fall back to keyword search).
    """
    if not _check_available():
        return []

    with _lock:
        if not _index:
            return []

    query_emb = _embed(query)
    if query_emb is None:
        return []

    results = []
    with _lock:
        items = list(_index.items())

    for rel, data in items:
        emb = data.get("embedding")
        if not emb:
            continue
        score = _cosine(query_emb, emb)
        if score >= SIMILARITY_THRESHOLD:
            results.append({
                "file": rel,
                "path": str(vault / rel),
                "score": round(score, 4),
                "excerpt": data.get("excerpt", ""),
            })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]


def get_stats() -> dict:
    with _lock:
        return {
            "indexed_notes": len(_index),
            "model": EMBED_MODEL,
            "available": _available,
        }
