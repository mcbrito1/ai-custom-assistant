"""
Hermes learning module — passive behavior tracking, smart fact extraction,
semantic deduplication, and profile auto-population.
"""
import json
import logging
import os
import threading
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

BEHAVIOR_FILE = os.getenv("BEHAVIOR_FILE", "/app/data/behavior.json")
FACT_STALE_DAYS = int(os.getenv("FACT_STALE_DAYS", "30"))

_lock = threading.Lock()

# ── Behavior data ─────────────────────────────────────────────────────────────
# Schema:
# {
#   "intent_counts": {"answer": 12, "search": 4, ...},
#   "hourly_activity": {"8": 3, "14": 7, ...},
#   "weekday_intents": {"0": {"answer": 2}, ...},   # 0=Monday
#   "notes_accessed": {"projetos.md": 5, ...},
#   "action_outcomes": {"delegate_claude": {"ok": 3, "error": 1}, ...},
#   "feedback": [{"ts","context","rating","action"}, ...],
#   "corrections": [{"ts","original","correction"}, ...],
#   "implicit_repeats": [{"ts","topic"}, ...],
#   "briefing_interactions": {"task_count": 0, "vault_count": 0, "total": 0},
#   "last_updated": "..."
# }


def _load() -> dict:
    path = Path(BEHAVIOR_FILE)
    if not path.exists():
        return _empty()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _empty()


def _empty() -> dict:
    return {
        "intent_counts": {},
        "hourly_activity": {},
        "weekday_intents": {},
        "notes_accessed": {},
        "action_outcomes": {},
        "feedback": [],
        "corrections": [],
        "implicit_repeats": [],
        "briefing_interactions": {"task_count": 0, "vault_count": 0, "total": 0},
        "last_updated": None,
    }


def _save(data: dict):
    path = Path(BEHAVIOR_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    data["last_updated"] = datetime.now().isoformat(timespec="seconds")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Public API ────────────────────────────────────────────────────────────────

def record_behavior(intent: str, notes_used: list[str] = None, action_status: str = "ok"):
    """Called after every /chat to track usage patterns."""
    now = datetime.now()
    hour = str(now.hour)
    weekday = str(now.weekday())

    with _lock:
        data = _load()

        # Intent frequency
        data["intent_counts"][intent] = data["intent_counts"].get(intent, 0) + 1

        # Hourly activity
        data["hourly_activity"][hour] = data["hourly_activity"].get(hour, 0) + 1

        # Intent by weekday
        if weekday not in data["weekday_intents"]:
            data["weekday_intents"][weekday] = {}
        wd = data["weekday_intents"][weekday]
        wd[intent] = wd.get(intent, 0) + 1

        # Notes accessed
        for note in (notes_used or []):
            data["notes_accessed"][note] = data["notes_accessed"].get(note, 0) + 1

        # Action outcomes by intent
        if intent not in data["action_outcomes"]:
            data["action_outcomes"][intent] = {"ok": 0, "error": 0}
        key = "ok" if action_status == "ok" else "error"
        data["action_outcomes"][intent][key] = data["action_outcomes"][intent].get(key, 0) + 1

        _save(data)


def record_feedback(context: str, rating: str, action: str):
    """Record explicit 👍/👎 feedback from Telegram inline buttons."""
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "context": context[:200],
        "rating": rating,  # "up" or "down"
        "action": action,
    }
    with _lock:
        data = _load()
        data["feedback"].append(entry)
        # Keep last 100 feedback entries
        data["feedback"] = data["feedback"][-100:]
        _save(data)
    log.info(f"Feedback recorded: {rating} for {action}")


def record_correction(original: str, correction: str):
    """Record when user implicitly corrects Hermes' response."""
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "original": original[:200],
        "correction": correction[:200],
    }
    with _lock:
        data = _load()
        data["corrections"].append(entry)
        data["corrections"] = data["corrections"][-50:]
        _save(data)
    log.info("Implicit correction recorded.")


def record_briefing_interaction(section: str):
    """Track which briefing sections the user interacts with."""
    with _lock:
        data = _load()
        bi = data.setdefault("briefing_interactions", {"task_count": 0, "vault_count": 0, "total": 0})
        bi["total"] = bi.get("total", 0) + 1
        bi[f"{section}_count"] = bi.get(f"{section}_count", 0) + 1
        _save(data)


def get_behavior_summary() -> dict:
    """Return a snapshot of behavior data for use in prompts and reflection."""
    with _lock:
        data = _load()

    # Top 3 most frequent intents
    top_intents = sorted(data["intent_counts"].items(), key=lambda x: x[1], reverse=True)[:3]

    # Peak hour
    hourly = data["hourly_activity"]
    peak_hour = max(hourly, key=hourly.get) if hourly else None

    # Most accessed notes
    top_notes = sorted(data["notes_accessed"].items(), key=lambda x: x[1], reverse=True)[:3]

    return {
        "top_intents": top_intents,
        "peak_hour": peak_hour,
        "top_notes": top_notes,
        "total_interactions": sum(data["intent_counts"].values()),
        "recent_feedback": data["feedback"][-5:],
        "correction_count": len(data["corrections"]),
    }


def is_implicit_correction(message: str) -> bool:
    """Detect if the user is implicitly correcting the last response."""
    correction_signals = [
        "na verdade", "não é isso", "errado", "incorreto",
        "não foi isso", "você errou", "não entendeu",
        "quero dizer", "me refiro a", "corrija",
    ]
    msg_lower = message.lower()
    return any(s in msg_lower for s in correction_signals)


# ── Smart fact extraction ────────────────────────────────────────────────────

PROFILE_CLASSIFY_SYSTEM = """Classifique fatos em categorias e retorne APENAS JSON.
Schema: {"preferences": [...], "projects": [...], "people": [...], "current_context": [...]}
- preferences: gostos, hábitos, preferências pessoais
- projects: projetos em andamento ou planejados
- people: pessoas mencionadas
- current_context: situação atual, emprego, localização, fase de vida"""

PROFILE_CLASSIFY_PROMPT = "Fatos:\n{facts}"

FACT_STALE_CHECK_PROMPT = """Analise estes fatos e identifique quais provavelmente ainda são verdadeiros.
Considere que fatos antigos sobre projetos, emprego e contexto mudam; gostos e características pessoais são mais estáveis.

Fatos com data de extração:
{facts_with_dates}

Retorne APENAS JSON com os fatos que ainda parecem relevantes:
{{"valid_facts": ["fato1", "fato2"]}}"""


def classify_facts_into_profile(facts: list[str], llm_fn) -> dict:
    """
    Use the reasoning model to classify flat facts into profile categories.
    llm_fn(model, messages) → str
    """
    if not facts:
        return {}
    try:
        import re
        from main import REASONING_MODEL, extract_json
        prompt = PROFILE_CLASSIFY_PROMPT.format(facts="\n".join(f"- {f}" for f in facts))
        raw = llm_fn(
            REASONING_MODEL,
            [{"role": "user", "content": prompt}],
            system=PROFILE_CLASSIFY_SYSTEM,
        )
        return extract_json(raw)
    except Exception as e:
        log.warning(f"Profile classification failed: {e}")
        return {}


def deduplicate_facts_semantic(facts: list[str]) -> list[str]:
    """
    Remove semantically duplicate facts using embeddings.
    Falls back to string dedup if embedding model unavailable.
    """
    if len(facts) <= 1:
        return facts
    try:
        import vault_index as vi
        from obsidian import VAULT

        # Embed each fact and remove those with cosine > 0.92 to an existing one
        embeddings = []
        for fact in facts:
            try:
                emb = vi._embed(fact)
                embeddings.append(emb)
            except Exception:
                embeddings.append(None)

        kept = []
        kept_embs = []
        for i, (fact, emb) in enumerate(zip(facts, embeddings)):
            if emb is None:
                # No embedding — keep if not exact duplicate
                if fact not in kept:
                    kept.append(fact)
                continue
            # Check against already-kept embeddings
            is_dup = any(
                ke is not None and vi._cosine(emb, ke) > 0.92
                for ke in kept_embs
            )
            if not is_dup:
                kept.append(fact)
                kept_embs.append(emb)

        removed = len(facts) - len(kept)
        if removed:
            log.info(f"Semantic dedup: removed {removed} duplicate fact(s).")
        return kept
    except Exception as e:
        log.debug(f"Semantic dedup unavailable, using string dedup: {e}")
        return list(dict.fromkeys(facts))  # preserve order, remove exact dupes
