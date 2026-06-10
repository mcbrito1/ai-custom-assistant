"""
Obsidian vault read/write operations and #hermes tag processing.
"""
import os
import re
import logging
from pathlib import Path
from datetime import datetime

log = logging.getLogger(__name__)

VAULT = Path(os.getenv("OBSIDIAN_VAULT", "/obsidian"))
HERMES_TAG = "#hermes"
HERMES_DONE_TAG = "#hermes/done"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_hidden(path: Path) -> bool:
    return any(part.startswith(".") for part in path.parts)


def _all_notes() -> list[Path]:
    if not VAULT.exists():
        return []
    return [p for p in VAULT.rglob("*.md") if not _is_hidden(p)]


# ── Search ────────────────────────────────────────────────────────────────────

def search_notes(query: str, max_results: int = 5) -> list[dict]:
    """Score-based keyword search across all vault notes."""
    terms = query.lower().split()
    results = []
    for path in _all_notes():
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
            content_lower = content.lower()
            score = sum(content_lower.count(t) for t in terms)
            # Bonus: term in filename
            score += sum(path.stem.lower().count(t) * 3 for t in terms)
            if score > 0:
                results.append({
                    "file": str(path.relative_to(VAULT)),
                    "path": str(path),
                    "score": score,
                    "excerpt": content[:500].strip(),
                })
        except Exception:
            continue
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:max_results]


def find_note(name: str) -> Path | None:
    """Find a note by approximate name match (case-insensitive, ignores path)."""
    name_lower = name.lower().replace(".md", "")
    best: tuple[int, Path | None] = (0, None)
    for path in _all_notes():
        stem = path.stem.lower()
        if stem == name_lower:
            return path  # exact match
        # partial match scored by overlap
        score = sum(1 for w in name_lower.split() if w in stem)
        if score > best[0]:
            best = (score, path)
    return best[1] if best[0] > 0 else None


def read_note(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def get_note_index() -> str:
    notes = [str(p.relative_to(VAULT)) for p in sorted(_all_notes())]
    if not notes:
        return ""
    return "Notas no vault Obsidian:\n" + "\n".join(f"- {n}" for n in notes)


# ── Write ─────────────────────────────────────────────────────────────────────

def append_to_note(path: Path, content: str) -> bool:
    """Append content (a line or block) to an existing note."""
    try:
        existing = path.read_text(encoding="utf-8", errors="ignore")
        separator = "\n" if existing.endswith("\n") else "\n\n"
        path.write_text(existing + separator + content, encoding="utf-8")
        log.info(f"Appended to {path.name}: {content[:60]}")
        return True
    except Exception as e:
        log.error(f"append_to_note failed: {e}")
        return False


def append_list_item(path: Path, item: str) -> bool:
    """Append a markdown list item to a note."""
    return append_to_note(path, f"- {item}")


def create_note(relative_path: str, content: str) -> Path | None:
    """Create a new note at VAULT/relative_path."""
    path = VAULT / relative_path
    if not relative_path.endswith(".md"):
        path = Path(str(path) + ".md")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        log.info(f"Created note: {path.name}")
        return path
    except Exception as e:
        log.error(f"create_note failed: {e}")
        return None


def update_note_content(path: Path, new_content: str) -> bool:
    """Overwrite the full content of a note."""
    try:
        path.write_text(new_content, encoding="utf-8")
        return True
    except Exception as e:
        log.error(f"update_note_content failed: {e}")
        return False


# ── #hermes tag scanner ───────────────────────────────────────────────────────

def scan_hermes_tags() -> list[dict]:
    """
    Find all lines containing #hermes (but not #hermes/done) across the vault.
    Returns list of {file, path, line_number, line_text}.
    """
    pending = []
    for note_path in _all_notes():
        try:
            lines = note_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            for i, line in enumerate(lines):
                if HERMES_TAG in line and HERMES_DONE_TAG not in line:
                    pending.append({
                        "file": str(note_path.relative_to(VAULT)),
                        "path": str(note_path),
                        "line_number": i,
                        "line_text": line.strip(),
                    })
        except Exception:
            continue
    return pending


def mark_hermes_done(note_path_str: str, line_number: int):
    """Replace #hermes with #hermes/done on a specific line."""
    path = Path(note_path_str)
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
        if 0 <= line_number < len(lines):
            lines[line_number] = lines[line_number].replace(HERMES_TAG, HERMES_DONE_TAG, 1)
            path.write_text("".join(lines), encoding="utf-8")
    except Exception as e:
        log.error(f"mark_hermes_done failed: {e}")
