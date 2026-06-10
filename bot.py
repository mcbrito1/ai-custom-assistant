import os
import logging
import threading
import requests
from telegram import Bot, Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
from telegram.request import HTTPXRequest

logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
HERMES_URL = os.getenv("HERMES_URL", "http://hermes:8000/chat")
HERMES_BASE = HERMES_URL.replace("/chat", "")
OWNER_ID = int(os.getenv("TELEGRAM_OWNER_ID", "0"))
HOST_AGENT_URL = os.getenv("HOST_AGENT_URL", "http://host.docker.internal:9000/exec")
HOST_AGENT_SECRET = os.getenv("HOST_AGENT_SECRET", "")

_bot_instance: Bot | None = None


def is_owner(update: Update) -> bool:
    return update.effective_user.id == OWNER_ID


# ── Scheduler notification ────────────────────────────────────────────────────

def send_reminder(chat_id: str, text: str, task_id: str):
    """Called by the scheduler inside hermes container — posts via Telegram Bot API."""
    if _bot_instance is None:
        return
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            _bot_instance.send_message(chat_id=int(chat_id), text=text, parse_mode="Markdown")
        )
    finally:
        loop.close()
    # Clean up one-time task from persistence
    try:
        requests.delete(f"{HERMES_BASE}/tasks/{task_id}", timeout=5)
    except Exception:
        pass


# ── Handlers ──────────────────────────────────────────────────────────────────

async def handle_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.message.reply_text("Sem permissao.")
        return
    command = " ".join(context.args)
    if not command:
        await update.message.reply_text("Uso: /run <comando PowerShell>")
        return
    await update.message.reply_text(f"Executando: `{command}`", parse_mode="Markdown")
    try:
        resp = requests.post(
            HOST_AGENT_URL,
            json={"command": command},
            headers={"X-Agent-Secret": HOST_AGENT_SECRET},
            timeout=35,
        )
        output = resp.json().get("output", "(sem saida)")
    except Exception as e:
        output = f"Erro ao contatar host-agent: {e}"
    if len(output) > 4000:
        output = output[:4000] + "\n... (truncado)"
    await update.message.reply_text(f"```\n{output}\n```", parse_mode="Markdown")


async def handle_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    try:
        resp = requests.get(f"{HERMES_BASE}/memory", timeout=10)
        facts = resp.json().get("facts", [])
    except Exception as e:
        await update.message.reply_text(f"Erro: {e}")
        return
    if not facts:
        await update.message.reply_text("Ainda não aprendi nada sobre você.")
        return
    text = "O que eu sei sobre você:\n\n" + "\n".join(f"{i+1}. {f}" for i, f in enumerate(facts))
    await update.message.reply_text(text)


async def handle_remember(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    fact = " ".join(context.args).strip()
    if not fact:
        await update.message.reply_text("Uso: /remember <fato>")
        return
    requests.post(f"{HERMES_BASE}/memory/add", json={"fact": fact}, timeout=10)
    await update.message.reply_text(f"Memorizei: _{fact}_", parse_mode="Markdown")


async def handle_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Uso: /notes <termo>")
        return
    resp = requests.get(f"{HERMES_BASE}/obsidian/search", params={"q": query}, timeout=10)
    results = resp.json().get("results", [])
    if not results:
        await update.message.reply_text("Nenhuma nota encontrada.")
        return
    text = f"Notas para *{query}*:\n\n"
    for r in results:
        text += f"📄 `{r['file']}`\n{r['excerpt'][:200]}...\n\n"
    if len(text) > 4000:
        text = text[:4000] + "\n...(truncado)"
    await update.message.reply_text(text, parse_mode="Markdown")


async def handle_wa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a WhatsApp message. Usage: /wa <mensagem> | /wa <numero> <mensagem>"""
    if not is_owner(update):
        return
    args = context.args
    if not args:
        await update.message.reply_text("Uso:\n/wa <mensagem>  → envia para você\n/wa <numero> <mensagem>  → envia para outro número")
        return

    # If first arg looks like a phone number, use it as destination
    if args[0].lstrip("+").isdigit() and len(args[0]) >= 8:
        to = args[0]
        text = " ".join(args[1:])
    else:
        to = ""
        text = " ".join(args)

    if not text:
        await update.message.reply_text("Informe uma mensagem.")
        return

    payload = {"text": text}
    if to:
        payload["to"] = to

    try:
        resp = requests.post(f"{HERMES_BASE}/whatsapp/send", json=payload, timeout=15)
        result = resp.json()
        if result.get("ok"):
            dest = result.get("chat_id", to or "você")
            await update.message.reply_text(f"✅ WhatsApp enviado para `{dest}`", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"❌ Erro: {result.get('error')}")
    except Exception as e:
        await update.message.reply_text(f"Erro: {e}")


async def handle_wa_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    try:
        resp = requests.get(f"{HERMES_BASE}/whatsapp/status", timeout=10)
        data = resp.json()
        if data.get("ok"):
            sessions = data.get("sessions", [])
            text = f"✅ WAHA conectado\n{len(sessions)} sessão(ões) ativa(s)"
        else:
            text = f"❌ WAHA offline: {data.get('error')}"
    except Exception as e:
        text = f"Erro ao verificar WAHA: {e}"
    await update.message.reply_text(text)


async def handle_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    resp = requests.get(f"{HERMES_BASE}/tasks", timeout=10)
    tasks = resp.json().get("tasks", [])
    if not tasks:
        await update.message.reply_text("Nenhuma tarefa agendada.")
        return
    lines = ["*Tarefas agendadas:*\n"]
    for t in tasks:
        if t["type"] == "once":
            lines.append(f"📅 `{t['id']}` — {t['message']}\n_{t.get('run_at','')}_")
        else:
            lines.append(f"🔁 `{t['id']}` — {t['message']}\ncron: `{t.get('cron','')}`")
    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")


async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    task_id = " ".join(context.args).strip()
    if not task_id:
        await update.message.reply_text("Uso: /cancel <id>")
        return
    resp = requests.delete(f"{HERMES_BASE}/tasks/{task_id}", timeout=10)
    ok = resp.json().get("cancelled", False)
    await update.message.reply_text(
        f"✅ Tarefa `{task_id}` cancelada." if ok else f"❌ Tarefa `{task_id}` não encontrada.",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    chat_id = str(update.effective_chat.id)
    user_text = update.message.text
    try:
        resp = requests.post(
            HERMES_URL,
            json={"message": user_text, "user_id": user_id, "chat_id": chat_id},
            timeout=120,
        )
        reply = resp.json().get("reply", "Sem resposta.")
    except Exception as e:
        reply = f"Erro: {e}"
    await update.message.reply_text(reply)


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    req_kwargs = {"verify": False, "http2": False}
    request = HTTPXRequest(http_version="1.1", httpx_kwargs=req_kwargs)
    get_updates_req = HTTPXRequest(http_version="1.1", httpx_kwargs=req_kwargs)

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .request(request)
        .get_updates_request(get_updates_req)
        .build()
    )

    # Store bot reference for scheduler callbacks
    _bot_instance = application.bot

    # Register scheduler callback in hermes service
    def _register_callback():
        import time
        time.sleep(5)
        try:
            import scheduler as sched
            sched.set_notify_callback(send_reminder)
        except Exception as e:
            logging.warning(f"Could not register scheduler callback: {e}")

    threading.Thread(target=_register_callback, daemon=True).start()

    application.add_handler(CommandHandler("run", handle_run))
    application.add_handler(CommandHandler("memory", handle_memory))
    application.add_handler(CommandHandler("remember", handle_remember))
    application.add_handler(CommandHandler("notes", handle_notes))
    application.add_handler(CommandHandler("tasks", handle_tasks))
    application.add_handler(CommandHandler("cancel", handle_cancel))
    application.add_handler(CommandHandler("wa", handle_wa))
    application.add_handler(CommandHandler("wastatus", handle_wa_status))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.run_polling()
