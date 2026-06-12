import json
import os
from pathlib import Path
from datetime import datetime
import obsidian

MEMORY_FILE = os.getenv("MEMORY_FILE", "/app/data/memory.json")
FACT_STALE_DAYS = int(os.getenv("FACT_STALE_DAYS", "30"))

# Schema:
# {
#   "facts": [{"text": str, "added_at": iso, "last_used_at": iso, "use_count": int, "stale": bool}],
#   "profile": {preferences: {}, projects: {}, people: {}, current_context: {}},
#   "updated_at": iso
# }


def _load() -> dict:
    path = Path(MEMORY_FILE)
    if not path.exists():
        return {"facts": [], "profile": {}, "updated_at": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if "profile" not in data:
            data["profile"] = {}
        # Migrate flat string facts to structured format
        migrated = []
        for f in data.get("facts", []):
            if isinstance(f, str):
                migrated.append({"text": f, "added_at": data.get("updated_at"), "last_used_at": None, "use_count": 0, "stale": False})
            else:
                migrated.append(f)
        data["facts"] = migrated
        return data
    except Exception:
        return {"facts": [], "profile": {}, "updated_at": None}


def _save(data: dict):
    path = Path(MEMORY_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.utcnow().isoformat()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    # Persist profile to vault asynchronously so it survives container rebuilds
    import threading
    threading.Thread(target=_sync_to_vault, args=(data,), daemon=True).start()


def _sync_to_vault(data: dict):
    """Write a human-readable Hermes/Perfil.md note to the Obsidian vault."""
    try:
        import obsidian as obs
        vault = obs.VAULT
        hermes_dir = vault / "Hermes"
        hermes_dir.mkdir(exist_ok=True)
        profile_path = hermes_dir / "Perfil.md"

        facts = [f["text"] if isinstance(f, dict) else f for f in data.get("facts", [])]
        profile = data.get("profile", {})
        updated = data.get("updated_at", "")[:19]

        lines = [
            "# Perfil do Usuário — Hermes",
            f"_Atualizado em: {updated}_",
            "",
            "## Fatos aprendidos",
        ]
        for f in facts:
            lines.append(f"- {f}")

        labels = {"preferences": "Preferências", "projects": "Projetos",
                  "people": "Pessoas", "current_context": "Contexto atual"}
        for cat, label in labels.items():
            items = profile.get(cat, {})
            if items:
                lines.append(f"\n## {label}")
                for v in items.values():
                    lines.append(f"- {v}")

        content = "\n".join(lines) + "\n"
        profile_path.write_text(content, encoding="utf-8")
        obs.git_push("hermes: sync profile to vault")
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug(f"Vault profile sync failed: {e}")


# ── Flat facts API (backwards compat — returns text strings) ──────────────────

def get_facts(include_stale: bool = False) -> list[str]:
    facts = _load().get("facts", [])
    if include_stale:
        return [f["text"] for f in facts]
    return [f["text"] for f in facts if not f.get("stale")]


def get_facts_rich() -> list[dict]:
    """Return full fact objects with metadata."""
    return _load().get("facts", [])


def add_fact(fact: str):
    """Add a fact if not already present (exact match). Use add_facts_smart for dedup."""
    data = _load()
    existing_texts = [f["text"] for f in data["facts"]]
    if fact not in existing_texts:
        data["facts"].append({
            "text": fact,
            "added_at": datetime.utcnow().isoformat(),
            "last_used_at": None,
            "use_count": 0,
            "stale": False,
        })
        _save(data)


def add_facts_bulk(new_facts: list[str]):
    """Add multiple facts at once, skipping exact duplicates."""
    data = _load()
    existing = {f["text"] for f in data["facts"]}
    added = 0
    for fact in new_facts:
        if fact and len(fact) > 5 and fact not in existing:
            data["facts"].append({
                "text": fact,
                "added_at": datetime.utcnow().isoformat(),
                "last_used_at": None,
                "use_count": 0,
                "stale": False,
            })
            existing.add(fact)
            added += 1
    if added:
        _save(data)
    return added


def replace_facts(new_fact_texts: list[str]):
    """Replace all facts with a new deduplicated list (after semantic dedup)."""
    data = _load()
    now = datetime.utcnow().isoformat()
    # Preserve metadata for facts that still exist
    old_map = {f["text"]: f for f in data["facts"]}
    data["facts"] = []
    for text in new_fact_texts:
        if text in old_map:
            data["facts"].append(old_map[text])
        else:
            data["facts"].append({"text": text, "added_at": now, "last_used_at": None, "use_count": 0, "stale": False})
    _save(data)


def mark_fact_used(fact_text: str):
    data = _load()
    for f in data["facts"]:
        if f["text"] == fact_text:
            f["last_used_at"] = datetime.utcnow().isoformat()
            f["use_count"] = f.get("use_count", 0) + 1
            f["stale"] = False
            break
    _save(data)


def mark_stale_facts():
    """Mark facts not used in FACT_STALE_DAYS as stale."""
    data = _load()
    cutoff = datetime.utcnow().timestamp() - FACT_STALE_DAYS * 86400
    for f in data["facts"]:
        last = f.get("last_used_at") or f.get("added_at")
        if last:
            try:
                ts = datetime.fromisoformat(last.replace("Z", "")).timestamp()
                f["stale"] = ts < cutoff
            except Exception:
                pass
    _save(data)


def remove_fact(index: int):
    data = _load()
    if 0 <= index < len(data["facts"]):
        data["facts"].pop(index)
        _save(data)


def clear_facts():
    data = _load()
    data["facts"] = []
    _save(data)


# ── Structured profile ────────────────────────────────────────────────────────

PROFILE_CATEGORIES = ("preferences", "projects", "people", "current_context")


def get_profile() -> dict:
    return _load().get("profile", {})


def update_profile(category: str, key: str, value: str):
    if category not in PROFILE_CATEGORIES:
        return
    data = _load()
    if category not in data["profile"]:
        data["profile"][category] = {}
    data["profile"][category][key] = value
    _save(data)


def update_profile_from_classified(classified: dict):
    """
    Populate profile from learner.classify_facts_into_profile() output.
    classified = {preferences: [...], projects: [...], people: [...], current_context: [...]}
    """
    data = _load()
    for cat in PROFILE_CATEGORIES:
        items = classified.get(cat, [])
        if not items:
            continue
        if cat not in data["profile"]:
            data["profile"][cat] = {}
        for i, item in enumerate(items):
            key = f"item_{i+1}"
            data["profile"][cat][key] = item
    _save(data)


def get_profile_summary() -> str:
    profile = get_profile()
    if not profile:
        return ""
    lines = []
    labels = {
        "preferences": "Preferências",
        "projects": "Projetos",
        "people": "Pessoas",
        "current_context": "Contexto atual",
    }
    for cat, label in labels.items():
        items = profile.get(cat, {})
        if items:
            lines.append(f"{label}:")
            for v in items.values():
                lines.append(f"  - {v}")
    return "\n".join(lines)


# ── Re-export for backwards compat ────────────────────────────────────────────

def search_obsidian(query: str, max_results: int = 5) -> list[dict]:
    return obsidian.search_notes(query, max_results)


def build_system_prompt(obsidian_context: str = "") -> str:
    facts = get_facts()
    profile_summary = get_profile_summary()

    base = (
        "Você é o Hermes, um assistente pessoal inteligente e autônomo. "
        "Responda sempre em português, de forma direta e útil. "
        "Você pode ler e escrever nas notas pessoais do usuário no Obsidian. "
        "Para tarefas técnicas complexas (código, refatoração, análise de projetos), "
        "você delega automaticamente ao Claude Code."
    )

    sections = [base]

    if facts:
        facts_text = "\n".join(f"- {f}" for f in facts)
        sections.append(f"O que você já sabe sobre o usuário:\n{facts_text}")

    if profile_summary:
        sections.append(f"Perfil estruturado do usuário:\n{profile_summary}")

    if obsidian_context:
        sections.append(f"Contexto das notas pessoais (Obsidian):\n{obsidian_context}")

    index = obsidian.get_note_index()
    if index:
        sections.append(index)

    return "\n\n".join(sections)
