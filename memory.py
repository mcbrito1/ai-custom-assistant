import json
import os
from pathlib import Path
from datetime import datetime
import obsidian

MEMORY_FILE = os.getenv("MEMORY_FILE", "/app/data/memory.json")

# Schema: {facts: [...], profile: {preferences, projects, people, current_context}, updated_at}


def _load() -> dict:
    path = Path(MEMORY_FILE)
    if not path.exists():
        return {"facts": [], "profile": {}, "updated_at": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if "profile" not in data:
            data["profile"] = {}
        return data
    except Exception:
        return {"facts": [], "profile": {}, "updated_at": None}


def _save(data: dict):
    path = Path(MEMORY_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.utcnow().isoformat()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Flat facts (backwards compat) ─────────────────────────────────────────────

def get_facts() -> list[str]:
    return _load().get("facts", [])


def add_fact(fact: str):
    data = _load()
    if fact not in data["facts"]:
        data["facts"].append(fact)
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
    """Set a structured profile field. category must be one of PROFILE_CATEGORIES."""
    if category not in PROFILE_CATEGORIES:
        return
    data = _load()
    if category not in data["profile"]:
        data["profile"][category] = {}
    data["profile"][category][key] = value
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
            for k, v in items.items():
                lines.append(f"  - {k}: {v}")
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
