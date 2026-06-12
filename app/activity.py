"""
Activity log for Claude Code delegations and autonomous actions.
Stored in data/activity_log.json as an append-only list (capped at MAX_ENTRIES).
"""
import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

ACTIVITY_FILE = os.getenv("ACTIVITY_FILE", "/app/data/activity_log.json")
MAX_ENTRIES = int(os.getenv("ACTIVITY_LOG_MAX", "200"))

log = logging.getLogger(__name__)
_lock = threading.Lock()


def _load() -> list:
    path = Path(ACTIVITY_FILE)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(entries: list):
    path = Path(ACTIVITY_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def record(action: str, prompt: str, result: str = "", status: str = "ok", source: str = "hermes"):
    """Append one entry. Trims to MAX_ENTRIES."""
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "action": action,
        "prompt": prompt[:500],
        "result": result[:1000],
        "status": status,
    }
    with _lock:
        entries = _load()
        entries.append(entry)
        if len(entries) > MAX_ENTRIES:
            entries = entries[-MAX_ENTRIES:]
        _save(entries)
    log.debug(f"Activity recorded: {action} [{status}]")


def get_recent(n: int = 10) -> list[dict]:
    with _lock:
        entries = _load()
    return entries[-n:]
