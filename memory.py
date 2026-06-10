import json
import os
from pathlib import Path
from datetime import datetime

MEMORY_FILE = os.getenv("MEMORY_FILE", "/app/data/memory.json")
OBSIDIAN_VAULT = os.getenv("OBSIDIAN_VAULT", "/obsidian")
OBSIDIAN_MAX_CHARS = 6000  # limit injected into prompt


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


def search_obsidian(query: str, max_results: int = 5) -> list[dict]:
    """Search Obsidian vault for notes containing query terms."""
    vault = Path(OBSIDIAN_VAULT)
    if not vault.exists():
        return []

    terms = query.lower().split()
    results = []

    for md_file in vault.rglob("*.md"):
        # Skip hidden dirs like .obsidian, .trash
        if any(part.startswith(".") for part in md_file.parts):
            continue
        try:
            content = md_file.read_text(encoding="utf-8", errors="ignore")
            content_lower = content.lower()
            score = sum(content_lower.count(term) for term in terms)
            if score > 0:
                results.append({
                    "file": str(md_file.relative_to(vault)),
                    "score": score,
                    "excerpt": content[:500].strip(),
                })
        except Exception:
            continue

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:max_results]


def get_obsidian_index() -> str:
    """Return a compact index of all note titles in the vault."""
    vault = Path(OBSIDIAN_VAULT)
    if not vault.exists():
        return ""

    notes = []
    for md_file in sorted(vault.rglob("*.md")):
        if any(part.startswith(".") for part in md_file.parts):
            continue
        notes.append(str(md_file.relative_to(vault)))

    if not notes:
        return ""

    return "Notas no vault Obsidian:\n" + "\n".join(f"- {n}" for n in notes)


def build_system_prompt(obsidian_context: str = "") -> str:
    facts = get_facts()
    base = (
        "Você é o Hermes, um assistente pessoal inteligente. "
        "Responda sempre em português, de forma direta e útil."
    )

    sections = [base]

    if facts:
        facts_text = "\n".join(f"- {f}" for f in facts)
        sections.append(f"O que você já sabe sobre o usuário:\n{facts_text}")

    if obsidian_context:
        sections.append(f"Contexto das notas pessoais do usuário (Obsidian):\n{obsidian_context}")

    index = get_obsidian_index()
    if index:
        sections.append(index)

    return "\n\n".join(sections)
