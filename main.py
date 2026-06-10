import os
import re
import json
import logging
import uvicorn
from collections import deque
from datetime import datetime
from fastapi import FastAPI
from contextlib import asynccontextmanager
from pydantic import BaseModel
import ollama
from duckduckgo_search import DDGS
from memory import build_system_prompt, add_fact, get_facts, search_obsidian
import scheduler as sched
import whatsapp as wa

OLLAMA_HOST = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
MODEL = os.getenv("OLLAMA_MODEL", "llama3")
HISTORY_SIZE = int(os.getenv("HISTORY_SIZE", "20"))

client = ollama.Client(host=OLLAMA_HOST)

# Per-user conversation history
_histories: dict[str, deque] = {}

# Telegram notify callback — injected by bot via /internal/set-callback or polling
_telegram_send = None


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_OWNER_ID = os.getenv("TELEGRAM_OWNER_ID", "0")


def _send_telegram(chat_id: str, text: str, task_id: str):
    """Send a Telegram message directly from the hermes service."""
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
    # Clean up one-time task
    sched.remove_completed(task_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    sched.set_notify_callback(_send_telegram)
    sched.start()
    yield

app = FastAPI(title="Hermes Agent", lifespan=lifespan)


def get_history(user_id: str) -> deque:
    if user_id not in _histories:
        _histories[user_id] = deque(maxlen=HISTORY_SIZE)
    return _histories[user_id]


DECISION_PROMPT = """Analise a mensagem abaixo e classifique a intenção. Responda APENAS com JSON:

Mensagem: {question}
Data/hora atual: {now}

Opções:
- Busca na internet: {{"action": "search", "query": "<termo>"}}
- Agendar lembrete único: {{"action": "schedule_once", "message": "<o que lembrar>", "run_at": "<ISO8601 datetime>"}}
- Agendar recorrente: {{"action": "schedule_recurring", "message": "<o que lembrar>", "cron": "<cron expr 5 campos>"}}
- Responder normalmente: {{"action": "answer"}}

Exemplos de cron: "0 8 * * 1-5" = dias úteis às 8h, "0 9 * * 1" = segunda às 9h, "0 */2 * * *" = a cada 2h.
Use o fuso horário America/Sao_Paulo para calcular os horários."""

EXTRACT_FACTS_PROMPT = """Analise a conversa e extraia fatos importantes e duradouros sobre o usuário \
(nome, profissão, projetos, preferências, hábitos, localização, etc).

Retorne APENAS JSON: {{"facts": ["fato 1", "fato 2"]}}
Se não houver fatos novos: {{"facts": []}}

Conversa:
{conversation}"""


def web_search(query: str, max_results: int = 5) -> str:
    with DDGS() as ddgs:
        results = list(ddgs.text(query, max_results=max_results))
    if not results:
        return "Nenhum resultado encontrado."
    return "\n\n".join(
        f"Título: {r['title']}\nResumo: {r['body']}\nFonte: {r['href']}" for r in results
    )


def extract_json(text: str) -> dict:
    match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except Exception:
            pass
    return {}


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


# ── Endpoints ─────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    user_id: str = "default"
    chat_id: str = "default"


class ScheduleRequest(BaseModel):
    message: str
    chat_id: str
    run_at: str | None = None   # ISO8601 for once
    cron: str | None = None     # cron expr for recurring


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
    hits = search_obsidian(q) if q else []
    return {"results": hits}


@app.get("/whatsapp/status")
def whatsapp_status():
    return wa.check_status()


@app.post("/whatsapp/send")
def whatsapp_send(body: dict):
    to = body.get("to", "")
    text = body.get("text", "")
    if not text:
        return {"ok": False, "error": "text is required"}
    if to:
        return wa.send_text(to, text)
    return wa.send_to_owner(text)


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

    # Classify intent
    decision_resp = client.chat(
        model=MODEL,
        messages=[{"role": "user", "content": DECISION_PROMPT.format(
            question=req.message, now=now_str
        )}],
    )
    decision = extract_json(decision_resp["message"]["content"])
    action = decision.get("action", "answer")

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
    obsidian_hits = search_obsidian(req.message)
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
