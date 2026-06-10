import os
import re
import json
import logging
import threading
import uvicorn
from collections import deque
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI
from contextlib import asynccontextmanager
from pydantic import BaseModel
import ollama
from duckduckgo_search import DDGS
from memory import build_system_prompt, add_fact, get_facts, search_obsidian
import obsidian
import scheduler as sched

OLLAMA_HOST = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
MODEL = os.getenv("OLLAMA_MODEL", "llama3")
HISTORY_SIZE = int(os.getenv("HISTORY_SIZE", "20"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_OWNER_ID = os.getenv("TELEGRAM_OWNER_ID", "0")
DEFAULT_CHAT_ID = os.getenv("TELEGRAM_OWNER_ID", "0")
HERMES_TAG_SCAN_INTERVAL = int(os.getenv("HERMES_TAG_SCAN_INTERVAL", "60"))  # seconds

client = ollama.Client(host=OLLAMA_HOST)
_histories: dict[str, deque] = {}


def get_history(user_id: str) -> deque:
    if user_id not in _histories:
        _histories[user_id] = deque(maxlen=HISTORY_SIZE)
    return _histories[user_id]


# ── Telegram send (from hermes container) ────────────────────────────────────

def _send_telegram(chat_id: str, text: str, task_id: str = ""):
    import httpx
    try:
        httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": int(chat_id), "text": text, "parse_mode": "Markdown"},
            verify=False,
            timeout=10,
        )
    except Exception as e:
        logging.warning(f"Failed to send Telegram notification: {e}")
    if task_id:
        sched.remove_completed(task_id)


# ── Prompts ───────────────────────────────────────────────────────────────────

DECISION_PROMPT = """Classifique a intenção em JSON. Responda APENAS com o JSON, nada mais.

Mensagem: {question}
Data/hora: {now}

REGRAS:
1. Se começa com "Adicione", "Coloque", "Acrescente" → obsidian_append
   EXEMPLOS: "Adicione tomate à lista de compras", "Coloque reunião em tarefas"
   JSON: {{"action": "obsidian_append", "note": "nome_da_nota", "content": "item", "is_list_item": true}}

2. Se começa com "Crie", "Cria uma nota" → obsidian_create
   EXEMPLOS: "Crie uma nota sobre projetos", "Crie nota de Python"
   JSON: {{"action": "obsidian_create", "note": "Nome.md", "content": "conteúdo"}}

3. Se contém "lembr" + "amanhã" ou "segunda" ou "às" → schedule_once
   EXEMPLOS: "Me lembre amanhã às 9h", "Lembrete segunda de manhã"
   JSON: {{"action": "schedule_once", "message": "conteúdo", "run_at": "2026-06-11T09:00"}}

4. Se contém "todo dia", "diáriamente", "cada dia" → schedule_recurring
   EXEMPLOS: "Todo dia às 8h me lembre", "Me lembrar diariamente de exercício"
   JSON: {{"action": "schedule_recurring", "message": "conteúdo", "cron": "0 8 * * *"}}

5. Se é pergunta com "o que", "como", "qual", "quantos", "quando", "por que" → search
   EXEMPLOS: "O que é Python?", "Como fazer backup?"
   JSON: {{"action": "search", "query": "termo"}}

6. Tudo mais → answer
   JSON: {{"action": "answer"}}

Responda APENAS com o JSON, nada mais."""

EXTRACT_FACTS_PROMPT = """Analise a conversa e extraia fatos importantes e duradouros sobre o usuário \
(nome, profissão, projetos, preferências, hábitos, localização, etc).

Retorne APENAS JSON: {{"facts": ["fato 1", "fato 2"]}}
Se não houver fatos novos: {{"facts": []}}

Conversa:
{conversation}"""

HERMES_TAG_PROMPT = """O usuário marcou o seguinte texto com #hermes no seu vault Obsidian, \
indicando que quer que você tome alguma ação.

Arquivo: {file}
Texto: {line}

Interprete o que o usuário quer e execute a ação. Responda em JSON com o que deve ser feito:
{{"action": "answer", "reply": "<resposta/confirmação para enviar ao usuário>"}}
ou
{{"action": "obsidian_append", "note": "<nota>", "content": "<conteúdo>", "is_list_item": true|false, "reply": "<confirmação>"}}
ou
{{"action": "obsidian_create", "note": "<caminho>", "content": "<conteúdo>", "reply": "<confirmação>"}}
ou
{{"action": "schedule_once", "message": "<lembrete>", "run_at": "<ISO8601>", "reply": "<confirmação>"}}

Data/hora atual: {now}
Notas disponíveis: {note_index}"""


# ── LLM helpers ───────────────────────────────────────────────────────────────

def extract_json(text: str) -> dict:
    match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except Exception:
            pass
    return {}


def web_search(query: str, max_results: int = 5) -> str:
    with DDGS() as ddgs:
        results = list(ddgs.text(query, max_results=max_results))
    if not results:
        return "Nenhum resultado encontrado."
    return "\n\n".join(
        f"Título: {r['title']}\nResumo: {r['body']}\nFonte: {r['href']}" for r in results
    )


def extract_and_save_facts(user_id: str):
    history = get_history(user_id)
    if len(history) < 2:
        return
    conversation = "\n".join(
        f"{'Usuário' if m['role'] == 'user' else 'Hermes'}: {m['content']}"
        for m in history
    )
    try:
        resp = client.chat(
            model=MODEL,
            messages=[{"role": "user", "content": EXTRACT_FACTS_PROMPT.format(conversation=conversation)}],
        )
        data = extract_json(resp["message"]["content"])
        for fact in data.get("facts", []):
            if fact and len(fact) > 5:
                add_fact(fact)
    except Exception:
        pass


# ── Obsidian write actions ────────────────────────────────────────────────────

def execute_obsidian_action(decision: dict) -> tuple[bool, str]:
    """
    Execute an obsidian_append or obsidian_create action.
    Returns (success, reply_text).
    """
    action = decision.get("action", "")
    note_name = decision.get("note", "")
    content = decision.get("content", "")

    if not note_name or not content:
        return False, "Não entendi qual nota ou conteúdo modificar."

    if action == "obsidian_append":
        # Remover .md do final se existir
        search_name = note_name.replace(".md", "").strip()
        path = obsidian.find_note(search_name)
        if not path:
            return False, f"Nota '{note_name}' não encontrada no vault."
        is_list = decision.get("is_list_item", False)
        if is_list:
            ok = obsidian.append_list_item(path, content)
        else:
            ok = obsidian.append_to_note(path, content)
        if ok:
            return True, f"✅ Adicionado à nota *{path.stem}*:\n`{content}`"
        return False, "Falha ao escrever na nota."

    elif action == "obsidian_create":
        # Garantir que tenha .md se não tiver
        create_path = note_name if note_name.endswith(".md") else f"{note_name}.md"
        path = obsidian.create_note(create_path, content)
        if path:
            return True, f"✅ Nota criada: *{path.stem}*"
        return False, "Falha ao criar nota."

    return False, "Ação desconhecida."


# ── #hermes tag scanner ───────────────────────────────────────────────────────

def _process_hermes_tags():
    """Background thread: scan vault for #hermes tags and process them."""
    import time
    while True:
        time.sleep(HERMES_TAG_SCAN_INTERVAL)
        try:
            pending = obsidian.scan_hermes_tags()
            if not pending:
                continue
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
            note_index = obsidian.get_note_index()
            for tag in pending:
                try:
                    prompt = HERMES_TAG_PROMPT.format(
                        file=tag["file"],
                        line=tag["line_text"],
                        now=now_str,
                        note_index=note_index,
                    )
                    resp = client.chat(
                        model=MODEL,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    decision = extract_json(resp["message"]["content"])
                    reply = decision.pop("reply", f"Processado: {tag['line_text'][:60]}")

                    if decision.get("action") in ("obsidian_append", "obsidian_create"):
                        execute_obsidian_action(decision)
                    elif decision.get("action") == "schedule_once":
                        try:
                            run_at = datetime.fromisoformat(decision["run_at"])
                            sched.add_once(decision["message"], DEFAULT_CHAT_ID, run_at)
                        except Exception as e:
                            reply = f"Erro ao agendar: {e}"

                    # Mark as done in the note
                    obsidian.mark_hermes_done(tag["path"], tag["line_number"])

                    # Notify user via Telegram
                    _send_telegram(
                        DEFAULT_CHAT_ID,
                        f"📝 *#hermes* em `{tag['file']}`:\n{reply}",
                    )
                except Exception as e:
                    logging.warning(f"hermes tag processing failed for {tag['file']}: {e}")
        except Exception as e:
            logging.warning(f"hermes tag scan error: {e}")


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    sched.set_notify_callback(_send_telegram)
    sched.start()
    threading.Thread(target=_process_hermes_tags, daemon=True).start()
    yield


app = FastAPI(title="Hermes Agent", lifespan=lifespan)


# ── Request models ────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    user_id: str = "default"
    chat_id: str = "default"


class ScheduleRequest(BaseModel):
    message: str
    chat_id: str
    run_at: str | None = None
    cron: str | None = None


class ObsidianWriteRequest(BaseModel):
    note: str
    content: str
    is_list_item: bool = False


class ObsidianCreateRequest(BaseModel):
    path: str
    content: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/memory")
def memory():
    return {"facts": get_facts()}


@app.post("/memory/add")
def memory_add(body: dict):
    fact = body.get("fact", "").strip()
    if fact:
        add_fact(fact)
    return {"facts": get_facts()}


@app.get("/obsidian/search")
def obsidian_search(q: str = ""):
    hits = obsidian.search_notes(q) if q else []
    return {"results": hits}


@app.get("/obsidian/notes")
def obsidian_notes():
    notes = [str(p.relative_to(obsidian.VAULT)) for p in obsidian._all_notes()]
    return {"notes": sorted(notes)}


@app.post("/obsidian/append")
def obsidian_append(req: ObsidianWriteRequest):
    path = obsidian.find_note(req.note)
    if not path:
        return {"ok": False, "error": f"Nota '{req.note}' não encontrada."}
    if req.is_list_item:
        ok = obsidian.append_list_item(path, req.content)
    else:
        ok = obsidian.append_to_note(path, req.content)
    return {"ok": ok, "file": str(path.relative_to(obsidian.VAULT))}


@app.post("/obsidian/create")
def obsidian_create(req: ObsidianCreateRequest):
    path = obsidian.create_note(req.path, req.content)
    return {"ok": path is not None, "file": str(path.relative_to(obsidian.VAULT)) if path else None}


@app.get("/obsidian/hermes-tags")
def obsidian_hermes_tags():
    return {"pending": obsidian.scan_hermes_tags()}


@app.get("/tasks")
def tasks_list():
    return {"tasks": sched.list_tasks()}


@app.post("/tasks/add")
def tasks_add(req: ScheduleRequest):
    if req.cron:
        task_id = sched.add_recurring(req.message, req.chat_id, req.cron)
        return {"task_id": task_id, "type": "recurring"}
    elif req.run_at:
        run_at = datetime.fromisoformat(req.run_at)
        task_id = sched.add_once(req.message, req.chat_id, run_at)
        return {"task_id": task_id, "type": "once"}
    return {"error": "Informe run_at ou cron"}


@app.delete("/tasks/{task_id}")
def tasks_cancel(task_id: str):
    ok = sched.cancel_task(task_id)
    return {"cancelled": ok}


@app.post("/chat")
def chat(req: ChatRequest):
    history = get_history(req.user_id)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
    note_index_short = "\n".join(
        str(p.relative_to(obsidian.VAULT)) for p in obsidian._all_notes()
    )[:1500]

    # Classify intent
    decision_resp = client.chat(
        model=MODEL,
        messages=[{"role": "user", "content": DECISION_PROMPT.format(
            question=req.message,
            now=now_str,
            note_index=note_index_short,
        )}],
    )
    decision_raw = decision_resp["message"]["content"]
    decision = extract_json(decision_raw)
    action = decision.get("action", "answer")

    # ── Obsidian write actions ─────────────────────────────────────────────
    if action in ("obsidian_append", "obsidian_create"):
        ok, reply = execute_obsidian_action(decision)
        history.append({"role": "user", "content": req.message})
        history.append({"role": "assistant", "content": reply})
        return {"reply": reply, "action": action, "ok": ok}

    # ── Schedule once ──────────────────────────────────────────────────────
    if action == "schedule_once" and decision.get("run_at"):
        try:
            run_at = datetime.fromisoformat(decision["run_at"])
            task_id = sched.add_once(decision["message"], req.chat_id, run_at)
            reply = f"✅ Agendado para {run_at.strftime('%d/%m/%Y às %H:%M')}:\n_{decision['message']}_\n\nID: `{task_id}`"
        except Exception as e:
            reply = f"Não consegui agendar: {e}"
        history.append({"role": "user", "content": req.message})
        history.append({"role": "assistant", "content": reply})
        return {"reply": reply, "action": "schedule_once"}

    # ── Schedule recurring ─────────────────────────────────────────────────
    if action == "schedule_recurring" and decision.get("cron"):
        try:
            task_id = sched.add_recurring(decision["message"], req.chat_id, decision["cron"])
            reply = f"✅ Lembrete recorrente criado (`{decision['cron']}`):\n_{decision['message']}_\n\nID: `{task_id}`"
        except Exception as e:
            reply = f"Não consegui agendar: {e}"
        history.append({"role": "user", "content": req.message})
        history.append({"role": "assistant", "content": reply})
        return {"reply": reply, "action": "schedule_recurring"}

    # ── Web search ─────────────────────────────────────────────────────────
    search_context = ""
    searched = False
    if action == "search" and decision.get("query"):
        results = web_search(decision["query"])
        search_context = f"\n\nResultados de busca:\n{results}"
        searched = True

    # ── Normal answer ──────────────────────────────────────────────────────
    obsidian_hits = obsidian.search_notes(req.message)
    obsidian_context = "\n\n".join(
        f"[{h['file']}]\n{h['excerpt']}" for h in obsidian_hits
    ) if obsidian_hits else ""

    system = build_system_prompt(obsidian_context)
    messages = [{"role": "system", "content": system}]
    messages.extend(list(history))
    messages.append({"role": "user", "content": req.message + search_context})

    response = client.chat(model=MODEL, messages=messages)
    reply = response["message"]["content"]

    history.append({"role": "user", "content": req.message})
    history.append({"role": "assistant", "content": reply})

    if len(history) % 8 == 0:
        extract_and_save_facts(req.user_id)

    return {"reply": reply, "searched": searched}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
