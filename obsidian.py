"""
Obsidian vault read/write operations and #hermes tag processing.
Git pull is performed before any vault access; push after any write.
"""
import os
import re
import time
import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

VAULT = Path(os.getenv("OBSIDIAN_VAULT", "/obsidian"))
HERMES_TAG = "#hermes"
HERMES_DONE_TAG = "#hermes/done"

# Avoid pulling more than once per minute across rapid successive reads
_last_pull_time: float = 0
_PULL_COOLDOWN = 60  # seconds


# ── Git helpers ───────────────────────────────────────────────────────────────

def _run_git(*args) -> tuple[int, str]:
    """Run a git command inside VAULT. Returns (returncode, combined output)."""
    try:
        result = subprocess.run(
            ["git"] + list(args),
            cwd=str(VAULT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result.returncode, (result.stdout + result.stderr).strip()
    except Exception as e:
        return 1, str(e)


def git_pull() -> str:
    """
    Pull latest changes from remote.
    Rate-limited to once per PULL_COOLDOWN seconds to avoid hammering the remote.
    Returns a human-readable status string.
    """
    global _last_pull_time
    now = time.time()
    if now - _last_pull_time < _PULL_COOLDOWN:
        return "pull skipped (cooldown)"
    _last_pull_time = now
    code, out = _run_git("pull", "--rebase", "--autostash")
    if code == 0:
        log.info(f"git pull: {out}")
        return out or "Already up to date."
    log.warning(f"git pull failed (code {code}): {out}")
    return f"pull failed: {out}"


def git_push(commit_message: str = "hermes: update vault") -> str:
    """
    Stage all changes, commit, and push. Returns human-readable status.
    """
    # Nothing staged? Check if there are changes at all.
    _, status = _run_git("status", "--porcelain")
    if not status.strip():
        return "nothing to commit"

    _run_git("add", "-A")
    code, out = _run_git("commit", "-m", commit_message)
    if code != 0 and "nothing to commit" not in out:
        log.warning(f"git commit failed: {out}")
        return f"commit failed: {out}"

    code, out = _run_git("push")
    if code == 0:
        log.info(f"git push: {out}")
        return out or "pushed"
    log.warning(f"git push failed: {out}")
    return f"push failed: {out}"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_hidden(path: Path) -> bool:
    return any(part.startswith(".") for part in path.parts)


def _all_notes() -> list[Path]:
    if not VAULT.exists():
        return []
    return [p for p in VAULT.rglob("*.md") if not _is_hidden(p)]


# ── Search / Read (pull before) ───────────────────────────────────────────────

def search_notes(query: str, max_results: int = 5) -> list[dict]:
    """Score-based keyword search. Pulls latest vault state first."""
    git_pull()
    terms = query.lower().split()
    results = []
    for path in _all_notes():
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
            content_lower = content.lower()
            score = sum(content_lower.count(t) for t in terms)
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
    """Find a note by approximate name match (case-insensitive). Pulls first."""
    git_pull()
    name_lower = name.lower().replace(".md", "")
    best: tuple[int, Path | None] = (0, None)
    for path in _all_notes():
        stem = path.stem.lower()
        if stem == name_lower:
            return path
        score = sum(1 for w in name_lower.split() if w in stem)
        if score > best[0]:
            best = (score, path)
    return best[1] if best[0] > 0 else None


def read_note(path: Path) -> str:
    git_pull()
    return path.read_text(encoding="utf-8", errors="ignore")


def get_note_index() -> str:
    """Return compact index of all note titles. Pulls first."""
    git_pull()
    notes = [str(p.relative_to(VAULT)) for p in sorted(_all_notes())]
    if not notes:
        return ""
    return "Notas no vault Obsidian:\n" + "\n".join(f"- {n}" for n in notes)


# ── Write (push after) ────────────────────────────────────────────────────────

def append_to_note(path: Path, content: str) -> bool:
    """Append content to an existing note, then push."""
    try:
        existing = path.read_text(encoding="utf-8", newline="")
        separator = "\n" if existing.endswith("\n") else "\n\n"
        path.write_text(existing + separator + content, encoding="utf-8", newline="")
        log.info(f"Appended to {path.name}: {content[:60]}")
        git_push(f"hermes: append to {path.stem}")
        return True
    except Exception as e:
        log.error(f"append_to_note failed: {e}")
        return False


def append_list_item(path: Path, item: str) -> bool:
    """Append a markdown list item to a note, then push."""
    return append_to_note(path, f"- {item}")


def create_note(relative_path: str, content: str) -> Path | None:
    """Create a new note at VAULT/relative_path, then push."""
    path = VAULT / relative_path
    if not relative_path.endswith(".md"):
        path = Path(str(path) + ".md")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
        log.info(f"Created note: {path.name}")
        git_push(f"hermes: create {path.stem}")
        return path
    except Exception as e:
        log.error(f"create_note failed: {e}")
        return None


def update_note_content(path: Path, new_content: str) -> bool:
    """Overwrite the full content of a note, then push."""
    try:
        path.write_text(new_content, encoding="utf-8")
        git_push(f"hermes: update {path.stem}")
        return True
    except Exception as e:
        log.error(f"update_note_content failed: {e}")
        return False


# ── #hermes tag scanner ───────────────────────────────────────────────────────

def scan_hermes_tags() -> list[dict]:
    """
    Find all lines containing #hermes (not #hermes/done) across the vault.
    Pulls first.
    """
    git_pull()
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
    """Replace #hermes with #hermes/done on a specific line, then push."""
    path = Path(note_path_str)
    try:
        lines = path.read_text(encoding="utf-8", newline="").splitlines(keepends=True)
        if 0 <= line_number < len(lines):
            lines[line_number] = lines[line_number].replace(HERMES_TAG, HERMES_DONE_TAG, 1)
            path.write_text("".join(lines), encoding="utf-8", newline="")
        git_push(f"hermes: mark done in {path.stem}")
    except Exception as e:
        log.error(f"mark_hermes_done failed: {e}")
