import os
import logging
import threading
import requests
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (ApplicationBuilder, MessageHandler, CommandHandler,
                           CallbackQueryHandler, filters, ContextTypes)
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


def _feedback_keyboard(action: str, context_snippet: str = "") -> InlineKeyboardMarkup:
    ctx = context_snippet[:80].replace(":", "")
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("👍", callback_data=f"fb:up:{action}:{ctx}"),
        InlineKeyboardButton("👎", callback_data=f"fb:down:{action}:{ctx}"),
    ]])


async def handle_feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":", 3)
    if len(parts) < 3 or parts[0] != "fb":
        return
    _, rating, action = parts[0], parts[1], parts[2]
    ctx = parts[3] if len(parts) > 3 else ""
    try:
        requests.post(
            f"{HERMES_BASE}/feedback",
            json={"action": action, "context": ctx, "rating": rating},
            timeout=5,
        )
    except Exception:
        pass
    label = "Obrigado! 👍" if rating == "up" else "Registrado. Vou melhorar! 👎"
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(label)


async def handle_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    try:
        resp = requests.get(f"{HERMES_BASE}/memory", timeout=10)
        facts = resp.json().get("facts", [])
        profile_resp = requests.get(f"{HERMES_BASE}/memory/profile", timeout=10)
        profile = profile_resp.json().get("profile", {}) if profile_resp.ok else {}
    except Exception as e:
        await update.message.reply_text(f"Erro: {e}")
        return

    lines = []
    if facts:
        lines.append("*O que eu sei sobre você:*\n")
        lines += [f"{i+1}. {f}" for i, f in enumerate(facts)]
    else:
        lines.append("Ainda não aprendi fatos sobre você.")

    labels = {"preferences": "Preferências", "projects": "Projetos",
               "people": "Pessoas", "current_context": "Contexto atual"}
    has_profile = any(profile.get(cat) for cat in labels)
    if has_profile:
        lines.append("\n*Perfil estruturado:*")
        for cat, label in labels.items():
            items = profile.get(cat, {})
            if items:
                lines.append(f"\n_{label}_")
                lines += [f"  • {v}" for v in items.values()]

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


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
        text += f"`{r['file']}`\n{r['excerpt'][:200]}...\n\n"
    if len(text) > 4000:
        text = text[:4000] + "\n...(truncado)"
    await update.message.reply_text(text, parse_mode="Markdown")


async def handle_obsidian(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all notes or show a specific note. Usage: /obsidian [nome]"""
    if not is_owner(update):
        return
    query = " ".join(context.args).strip()

    if not query:
        resp = requests.get(f"{HERMES_BASE}/obsidian/notes", timeout=10)
        notes = resp.json().get("notes", [])
        if not notes:
            await update.message.reply_text("Vault vazio ou não montado.")
            return
        text = f"*{len(notes)} notas no vault:*\n\n" + "\n".join(f"- `{n}`" for n in notes[:50])
        if len(notes) > 50:
            text += f"\n... e mais {len(notes) - 50}"
        await update.message.reply_text(text, parse_mode="Markdown")
        return

    # Search
    resp = requests.get(f"{HERMES_BASE}/obsidian/search", params={"q": query}, timeout=10)
    results = resp.json().get("results", [])
    if not results:
        await update.message.reply_text(f"Nenhuma nota encontrada para: {query}")
        return
    text = f"*Resultados para '{query}':*\n\n"
    for r in results[:3]:
        text += f"📄 `{r['file']}` (score: {r['score']})\n{r['excerpt'][:300]}\n\n"
    await update.message.reply_text(text, parse_mode="Markdown")


async def handle_hermes_tags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show pending #hermes tags in the vault."""
    if not is_owner(update):
        return
    resp = requests.get(f"{HERMES_BASE}/obsidian/hermes-tags", timeout=10)
    pending = resp.json().get("pending", [])
    if not pending:
        await update.message.reply_text("Nenhuma tag #hermes pendente no vault.")
        return
    text = f"*{len(pending)} tag(s) #hermes pendente(s):*\n\n"
    for t in pending:
        text += f"`{t['file']}` linha {t['line_number'] + 1}:\n_{t['line_text']}_\n\n"
    await update.message.reply_text(text, parse_mode="Markdown")


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


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show overall agent status."""
    if not is_owner(update):
        return
    try:
        resp = requests.get(f"{HERMES_BASE}/status", timeout=10)
        data = resp.json()
        vi = data.get("vault_index", {})
        models = data.get("models", {})
        model_lines = "\n".join(f"  · {k}: `{v}`" for k, v in models.items())
        uptime = data.get("uptime_seconds", 0)
        uptime_str = f"{uptime // 3600}h {(uptime % 3600) // 60}m"
        text = (
            f"*Status do Hermes*\n\n"
            f"🧠 Modelos:\n{model_lines}\n\n"
            f"📚 Notas indexadas: `{vi.get('indexed_notes','?')}` "
            f"({'semântico' if vi.get('available') else 'keyword fallback'})\n"
            f"📅 Tarefas agendadas: `{data.get('scheduled_tasks','?')}`\n"
            f"🧬 Fatos na memória: `{data.get('memory_facts','?')}`\n"
            f"⏱ Uptime: `{uptime_str}`"
        )
    except Exception as e:
        text = f"Erro ao obter status: {e}"
    await update.message.reply_text(text, parse_mode="Markdown")


async def handle_reflect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Trigger vault reflection and send insights."""
    if not is_owner(update):
        return
    await update.message.reply_text("🔍 Analisando seu vault… aguarde.")
    try:
        resp = requests.get(f"{HERMES_BASE}/reflect", timeout=600)
        data = resp.json()
        if "error" in data:
            await update.message.reply_text(f"Erro: {data['error']}")
            return
        insight = data.get("insight", "Sem insights.")
        if len(insight) > 4000:
            insight = insight[:4000] + "\n...(truncado)"
        await update.message.reply_text(
            insight,
            reply_markup=_feedback_keyboard("reflect", insight[:80]),
        )
    except Exception as e:
        await update.message.reply_text(f"Erro: {e}")


async def handle_projects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List project notes with open items."""
    if not is_owner(update):
        return
    try:
        resp = requests.get(f"{HERMES_BASE}/projects", timeout=15)
        projects = resp.json().get("projects", [])
    except Exception as e:
        await update.message.reply_text(f"Erro: {e}")
        return
    if not projects:
        await update.message.reply_text("Nenhuma nota com #projeto encontrada no vault.")
        return
    lines = [f"*{len(projects)} projeto(s) encontrado(s):*\n"]
    for p in projects:
        status = "🔴" if p["age_days"] >= 7 and p["open_items"] > 0 else "🟢"
        lines.append(
            f"{status} `{p['file']}`\n"
            f"   {p['open_items']} item(s) aberto(s) · última mod. há {p['age_days']}d"
        )
    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")


async def handle_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show recent activity log."""
    if not is_owner(update):
        return
    try:
        n = int(context.args[0]) if context.args else 10
        resp = requests.get(f"{HERMES_BASE}/activity", params={"n": n}, timeout=10)
        entries = resp.json().get("entries", [])
    except Exception as e:
        await update.message.reply_text(f"Erro: {e}")
        return
    if not entries:
        await update.message.reply_text("Nenhuma atividade registrada ainda.")
        return
    lines = [f"*Últimas {len(entries)} atividades:*\n"]
    for e in reversed(entries):
        icon = "✅" if e["status"] == "ok" else ("⚠️" if e["status"] == "alert" else "❌")
        lines.append(f"{icon} `{e['ts']}` *{e['action']}*\n   _{e['prompt'][:80]}_")
    text = "\n\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n...(truncado)"
    await update.message.reply_text(text, parse_mode="Markdown")


async def handle_delegate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Directly delegate a task to Claude Code. Usage: /delegate <task>"""
    if not is_owner(update):
        return
    task = " ".join(context.args).strip()
    if not task:
        await update.message.reply_text("Uso: /delegate <descrição da tarefa>")
        return
    chat_id = str(update.effective_chat.id)
    try:
        requests.post(
            HERMES_URL,
            json={"message": task, "user_id": str(update.effective_user.id), "chat_id": chat_id},
            timeout=10,
        )
    except Exception:
        pass
    await update.message.reply_text(
        f"🤖 Delegando ao Claude Code:\n`{task[:200]}`\n\nTe aviso quando terminar.",
        parse_mode="Markdown",
    )


async def handle_projeto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show project note details. Usage: /projeto <nome>"""
    if not is_owner(update):
        return
    note = " ".join(context.args).strip()
    if not note:
        await update.message.reply_text("Uso: /projeto <nome da nota>")
        return
    try:
        resp = requests.get(f"{HERMES_BASE}/projects/detail", params={"note": note}, timeout=15)
        data = resp.json()
    except Exception as e:
        await update.message.reply_text(f"Erro: {e}")
        return
    if "error" in data:
        await update.message.reply_text(f"❌ {data['error']}")
        return

    open_items = data.get("open_items", [])
    done_items = data.get("done_items", [])
    recent_act = data.get("recent_activity", [])

    lines = [
        f"📋 *{data['file']}*",
        f"Última modificação: há {data['age_days']} dias",
        f"✅ {len(done_items)} concluído(s) · ⏳ {len(open_items)} em aberto",
    ]
    if open_items:
        lines.append("\n*Itens em aberto:*")
        for item in open_items[:10]:
            lines.append(f"  {item}")
    if recent_act:
        lines.append("\n*Última atividade Hermes:*")
        for a in recent_act[-2:]:
            lines.append(f"  `{a['ts']}` {a['action']} [{a['status']}]")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n...(truncado)"
    await update.message.reply_text(text, parse_mode="Markdown")


async def handle_aprimorar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Trigger project improvement pipeline. Usage: /aprimorar <nome da nota>"""
    if not is_owner(update):
        return
    note = " ".join(context.args).strip()
    if not note:
        await update.message.reply_text(
            "Uso: /aprimorar <nome da nota>\n"
            "Ex: `/aprimorar Projeto Hermes`",
            parse_mode="Markdown",
        )
        return
    chat_id = str(update.effective_chat.id)
    try:
        resp = requests.post(
            f"{HERMES_BASE}/projects/improve",
            json={"note": note, "chat_id": chat_id},
            timeout=15,
        )
        data = resp.json()
    except Exception as e:
        await update.message.reply_text(f"Erro: {e}")
        return
    if not data.get("ok"):
        await update.message.reply_text(f"❌ {data.get('error', 'Falha desconhecida.')}")
        return
    await update.message.reply_text(
        f"🔧 Melhoria iniciada para `{data['note']}`\n"
        f"Iterações planejadas: *{data['iterations']}*\n\n"
        f"Te aviso a cada iteração quando concluir.",
        parse_mode="Markdown",
        reply_markup=_feedback_keyboard("aprimorar", data["note"]),
    )


async def handle_agenda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List upcoming calendar events. Usage: /agenda [days]"""
    if not is_owner(update):
        return
    days = int(context.args[0]) if context.args else 7
    try:
        resp = requests.get(f"{HERMES_BASE}/calendar/events", params={"days": days}, timeout=15)
        data = resp.json()
    except Exception as e:
        await update.message.reply_text(f"Erro: {e}")
        return
    if "error" in data:
        await update.message.reply_text(
            f"📅 {data['error']}\n\n"
            "_Para configurar: adicione `google_credentials.json` em `data/` e defina `GOOGLE_CREDS_PATH` no `.env`._",
            parse_mode="Markdown",
        )
        return
    events = data.get("events", [])
    if not events:
        await update.message.reply_text(f"Nenhum evento nos próximos {days} dias.")
        return
    lines = [f"*📅 Agenda — próximos {days} dias:*\n"]
    for e in events:
        start = e["start"].replace("T", " ")[:16] if "T" in e["start"] else e["start"]
        line = f"• `{start}` — {e['summary']}"
        if e.get("location"):
            line += f" 📍 {e['location']}"
        lines.append(line)
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def handle_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Health check on all Hermes circuits."""
    if not is_owner(update):
        return
    await update.message.reply_text("🔎 Verificando circuitos…")

    results = {}

    # 1. Hermes agent API
    try:
        r = requests.get(f"{HERMES_BASE}/health", timeout=5)
        results["hermes_api"] = ("✅", "online") if r.status_code == 200 else ("⚠️", f"status {r.status_code}")
    except Exception as e:
        results["hermes_api"] = ("❌", str(e)[:60])

    # 2. Ollama / LLM models
    try:
        r = requests.get(f"{HERMES_BASE}/status", timeout=10)
        data = r.json()
        models = data.get("models", {})
        model_lines = " | ".join(f"{k}:`{v}`" for k, v in models.items())
        results["ollama"] = ("✅", model_lines)
    except Exception as e:
        results["ollama"] = ("❌", str(e)[:60])

    # 3. Vault index
    try:
        r = requests.get(f"{HERMES_BASE}/status", timeout=10)
        vi = r.json().get("vault_index", {})
        n = vi.get("indexed_notes", 0)
        mode = "semântico" if vi.get("available") else "keyword fallback"
        results["vault_index"] = ("✅" if n > 0 else "⚠️", f"{n} notas indexadas ({mode})")
    except Exception as e:
        results["vault_index"] = ("❌", str(e)[:60])

    # 4. Obsidian vault (note listing)
    try:
        r = requests.get(f"{HERMES_BASE}/obsidian/notes", timeout=10)
        notes = r.json().get("notes", [])
        results["obsidian_vault"] = ("✅", f"{len(notes)} notas no vault") if notes else ("⚠️", "vault vazio ou não montado")
    except Exception as e:
        results["obsidian_vault"] = ("❌", str(e)[:60])

    # 5. Host agent (Windows PowerShell bridge)
    try:
        r = requests.post(
            HOST_AGENT_URL,
            json={"command": "echo hermes-check"},
            headers={"X-Agent-Secret": HOST_AGENT_SECRET},
            timeout=10,
        )
        out = r.json().get("output", "")
        results["host_agent"] = ("✅", "PowerShell bridge online") if "hermes-check" in out else ("⚠️", f"resposta inesperada: {out[:40]}")
    except Exception as e:
        results["host_agent"] = ("❌", str(e)[:60])

    # 6. Scheduler / tasks
    try:
        r = requests.get(f"{HERMES_BASE}/tasks", timeout=5)
        n = len(r.json().get("tasks", []))
        results["scheduler"] = ("✅", f"{n} tarefa(s) agendada(s)")
    except Exception as e:
        results["scheduler"] = ("❌", str(e)[:60])

    # 7. Google Calendar
    try:
        r = requests.get(f"{HERMES_BASE}/status", timeout=10)
        cal_ok = r.json().get("calendar_available", False)
        results["google_calendar"] = ("✅", "configurado") if cal_ok else ("⚠️", "não configurado (opcional)")
    except Exception as e:
        results["google_calendar"] = ("❌", str(e)[:60])

    lines = ["*🩺 Diagnóstico do Hermes*\n"]
    all_ok = True
    for name, (icon, detail) in results.items():
        lines.append(f"{icon} *{name}*: {detail}")
        if icon == "❌":
            all_ok = False

    lines.append(f"\n{'✅ Todos os circuitos operacionais.' if all_ok else '⚠️ Alguns circuitos com problema.'}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    chat_id = str(update.effective_chat.id)
    user_text = update.message.text
    await update.message.reply_text("⏳ Processando…")
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

    _bot_instance = application.bot

    def _register_callback():
        import time
        time.sleep(5)
        try:
            import scheduler as sched
            sched.set_notify_callback(send_reminder)
        except Exception as e:
            logging.warning(f"Could not register scheduler callback: {e}")

    threading.Thread(target=_register_callback, daemon=True).start()

    application.add_handler(CommandHandler("help", handle_help))
    application.add_handler(CommandHandler("run", handle_run))
    application.add_handler(CommandHandler("memory", handle_memory))
    application.add_handler(CommandHandler("remember", handle_remember))
    application.add_handler(CommandHandler("notes", handle_notes))
    application.add_handler(CommandHandler("obsidian", handle_obsidian))
    application.add_handler(CommandHandler("hermestags", handle_hermes_tags))
    application.add_handler(CommandHandler("tasks", handle_tasks))
    application.add_handler(CommandHandler("cancel", handle_cancel))
    application.add_handler(CommandHandler("status", handle_status))
    application.add_handler(CommandHandler("reflect", handle_reflect))
    application.add_handler(CommandHandler("projects", handle_projects))
    application.add_handler(CommandHandler("activity", handle_activity))
    application.add_handler(CommandHandler("delegate", handle_delegate))
    application.add_handler(CommandHandler("projeto", handle_projeto))
    application.add_handler(CommandHandler("aprimorar", handle_aprimorar))
    application.add_handler(CommandHandler("agenda", handle_agenda))
    application.add_handler(CommandHandler("check", handle_check))
    application.add_handler(CallbackQueryHandler(handle_feedback_callback, pattern=r"^fb:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.run_polling()


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 *Hermes — Assistente Virtual*\n\n"
        "Fale comigo em português! Entendo linguagem natural para agendar lembretes, "
        "buscar informações, gerenciar suas notas no Obsidian e muito mais.\n\n"

        "*📋 Comandos disponíveis*\n\n"

        "*Diagnóstico*\n"
        "/check — Verifica todos os circuitos \\(Ollama, vault, calendário…\\)\n"
        "/status — Painel: modelos ativos, índice, tarefas, uptime\n\n"

        "*Memória e Aprendizado*\n"
        "/memory — Fatos que aprendi sobre você \\+ perfil estruturado\n"
        "/remember \\<fato\\> — Ensina um fato novo manualmente\n\n"

        "*Vault Obsidian*\n"
        "/notes \\<busca\\> — Busca semântica nas suas notas\n"
        "/obsidian — Lista todas as notas\n"
        "/hermestags — Tags `#hermes` pendentes de processamento\n"
        "/reflect — Análise do vault: projetos parados, sugestões\n\n"

        "*Projetos*\n"
        "/projects — Lista notas `#projeto` com status\n"
        "/projeto \\<nome\\> — Detalhe de um projeto\n"
        "/aprimorar \\<nota\\> — Melhora o projeto via Claude Code\n\n"

        "*Delegação e Execução*\n"
        "/delegate \\<tarefa\\> — Delega ao Claude Code\n"
        "/run \\<cmd\\> — Executa PowerShell no Windows\n"
        "/activity — Últimas ações autônomas\n\n"

        "*Agendamento e Calendário*\n"
        "/tasks — Lembretes agendados\n"
        "/cancel \\<id\\> — Cancela um lembrete\n"
        "/agenda \\[dias\\] — Eventos do Google Calendar\n\n"

        "*💬 Exemplos de uso direto*\n"
        "• _Adicione leite à lista de compras_\n"
        "• _Me lembre amanhã às 9h de ligar para o médico_\n"
        "• _Pesquise o primeiro jogo do Brasil na copa_\n"
        "• _Crie uma nota chamada Ideias de Projeto_\n"
        "• _Refatora o arquivo main\\.py_ → delega ao Claude Code"
    )
    await update.message.reply_text(text, parse_mode="MarkdownV2")
