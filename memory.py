import json
import os
from pathlib import Path
from datetime import datetime
import obsidian

MEMORY_FILE = os.getenv("MEMORY_FILE", "/app/data/memory.json")


def _load() -> dict:
    path = Path(MEMORY_FILE)
    if not path.exists():
        return {"facts": [], "updated_at": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"facts": [], "updated_at": None}


def _save(data: dict):
    path = Path(MEMORY_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.utcnow().isoformat()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


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
    _save({"facts": []})


# Re-export for backwards compat
def search_obsidian(query: str, max_results: int = 5) -> list[dict]:
    return obsidian.search_notes(query, max_results)


def build_system_prompt(obsidian_context: str = "") -> str:
    facts = get_facts()
    base = (
        "Você é o Hermes, um assistente pessoal inteligente. "
        "Responda sempre em português, de forma direta e útil. "
        "Você pode ler e escrever nas notas pessoais do usuário no Obsidian."
    )

    sections = [base]

    if facts:
        facts_text = "\n".join(f"- {f}" for f in facts)
        sections.append(f"O que você já sabe sobre o usuário:\n{facts_text}")

    if obsidian_context:
        sections.append(f"Contexto das notas pessoais do usuário (Obsidian):\n{obsidian_context}")

    index = obsidian.get_note_index()
    if index:
        sections.append(index)

    return "\n\n".join(sections)
