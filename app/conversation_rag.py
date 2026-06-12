"""
Conversation RAG — semantic retrieval of past messages for long-context memory.
Embeds every assistant/user message pair and retrieves relevant history beyond
the rolling 20-message window.
"""
import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

CONV_INDEX_FILE = os.getenv("CONV_INDEX_FILE", "/app/data/conversation_index.json")
MAX_CONV_ENTRIES = int(os.getenv("CONV_INDEX_MAX", "500"))

_lock = threading.Lock()
_index: list[dict] = []  # [{user_id, ts, text, embedding}]
_dirty = False


def _load():
    global _index
    path = Path(CONV_INDEX_FILE)
    if not path.exists():
        return
    try:
        with _lock:
            _index = json.loads(path.read_text(encoding="utf-8"))
        log.debug(f"Conversation RAG: loaded {len(_index)} entries.")
    except Exception as e:
        log.warning(f"ConvRAG load failed: {e}")


def _save():
    path = Path(CONV_INDEX_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        data = _index[-MAX_CONV_ENTRIES:]
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def init():
    """Load existing index from disk. Call once on startup."""
    _load()


def index_exchange(user_id: str, user_msg: str, assistant_msg: str):
    """Embed a user↔assistant exchange and add to the rolling index."""
    try:
        import vault_index as vi
        text = f"Usuário: {user_msg}\nHermes: {assistant_msg}"
        emb = vi._embed(text[:2000])
        if emb is None:
            return
        entry = {
            "user_id": user_id,
            "ts": time.time(),
            "text": text[:1000],
            "embedding": emb,
        }
        with _lock:
            _index.append(entry)
            # Trim to cap
            if len(_index) > MAX_CONV_ENTRIES:
                del _index[:len(_index) - MAX_CONV_ENTRIES]
        threading.Thread(target=_save, daemon=True).start()
    except Exception as e:
        log.debug(f"ConvRAG index failed: {e}")


def retrieve(user_id: str, query: str, top_k: int = 3, min_score: float = 0.5) -> list[str]:
    """
    Return top_k past exchanges relevant to the query.
    Excludes entries from the most recent 20 (already in the live context window).
    """
    try:
        import vault_index as vi
        query_emb = vi._embed(query[:500])
        if query_emb is None:
            return []

        with _lock:
            # Skip the most recent 20 entries (already in history window)
            candidates = [e for e in _index if e.get("user_id") == user_id][:-20]

        if not candidates:
            return []

        scored = []
        for entry in candidates:
            emb = entry.get("embedding")
            if not emb:
                continue
            score = vi._cosine(query_emb, emb)
            if score >= min_score:
                scored.append((score, entry["text"]))

        scored.sort(reverse=True)
        return [text for _, text in scored[:top_k]]
    except Exception as e:
        log.debug(f"ConvRAG retrieve failed: {e}")
        return []
