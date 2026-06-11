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
from memory import build_system_prompt, add_fact, get_facts, search_obsidian, update_profile
import obsidian
import vault_index
import scheduler as sched
import activity

OLLAMA_HOST = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
MODEL = os.getenv("OLLAMA_MODEL", "gemma2:2b")
HISTORY_SIZE = int(os.getenv("HISTORY_SIZE", "20"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_OWNER_ID = os.getenv("TELEGRAM_OWNER_ID", "0")
DEFAULT_CHAT_ID = os.getenv("TELEGRAM_OWNER_ID", "0")
HERMES_TAG_SCAN_INTERVAL = int(os.getenv("HERMES_TAG_SCAN_INTERVAL", "60"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
HISTORY_FILE = os.getenv("HISTORY_FILE", "/app/data/history.json")
MORNING_BRIEFING_CRON = os.getenv("MORNING_BRIEFING_CRON", "0 8 * * *")
BACKUP_CRON = os.getenv("BACKUP_CRON", "0 3 * * 0")  # Sundays at 3am
PROJECT_SCAN_INTERVAL = int(os.getenv("PROJECT_SCAN_INTERVAL", "3600"))  # seconds
PROJECT_STALE_DAYS = int(os.getenv("PROJECT_STALE_DAYS", "7"))
MAX_IMPROVE_ITERATIONS = int(os.getenv("MAX_IMPROVE_ITERATIONS", "2"))
RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", "3"))
RETRY_BACKOFF = float(os.getenv("RETRY_BACKOFF", "2.0"))

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("hermes")

client = ollama.Client(host=OLLAMA_HOST)
_histories: dict[str, deque] = {}
_HISTORY_LOCK = threading.Lock()


# ── Conversation history persistence ─────────────────────────────────────────

def _load_histories():
    path = Path(HISTORY_FILE)
    if not path.exists():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        with _HISTORY_LOCK:
            for uid, msgs in raw.items():
                d = deque(maxlen=HISTORY_SIZE)
                d.extend(msgs[-HISTORY_SIZE:])
                _histories[uid] = d
        log.info(f"Loaded conversation history for {len(raw)} user(s).")
    except Exception as e:
        log.warning(f"Could not load history: {e}")


def _save_histories():
    path = Path(HISTORY_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _HISTORY_LOCK:
            data = {uid: list(msgs) for uid, msgs in _histories.items()}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning(f"Could not save history: {e}")


def get_history(user_id: str) -> deque:
    with _HISTORY_LOCK:
        if user_id not in _histories:
            _histories[user_id] = deque(maxlen=HISTORY_SIZE)
        return _histories[user_id]


# ── Telegram send ─────────────────────────────────────────────────────────────

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
        log.warning(f"Failed to send Telegram notification: {e}")
    if task_id:
        sched.remove_completed(task_id)


# ── Prompts ───────────────────────────────────────────────────────────────────

# Kept short and directive for gemma2:2b. Few-shot examples drive reliable JSON output.
DECISION_PROMPT = """Classifique a mensagem e retorne APENAS um JSON válido, sem explicações.

Mensagem: "{question}"
Data/hora: {now}

EXEMPLOS:
"Adicione leite à lista de compras" → {{"action":"obsidian_append","note":"lista de compras","content":"leite","is_list_item":true}}
"Crie uma nota de ideias" → {{"action":"obsidian_create","note":"Ideias.md","content":"# Ideias\n"}}
"O que está na minha nota de projetos?" → {{"action":"obsidian_read","note":"projetos"}}
"Atualize minha nota de reunião para incluir as decisões de hoje" → {{"action":"obsidian_update","note":"reunião","content":"## Decisões\n- ..."}}
"Me lembre amanhã às 9h de ligar para o médico" → {{"action":"schedule_once","message":"Ligar para o médico","run_at":"2026-06-12T09:00"}}
"Me lembre todo dia às 7h de tomar água" → {{"action":"schedule_recurring","message":"Tomar água","cron":"0 7 * * *"}}
"Refatora o arquivo main.py" → {{"action":"delegate_claude","task":"Refatora o arquivo main.py"}}
"Cria um script Python para renomear arquivos" → {{"action":"delegate_claude","task":"Cria um script Python para renomear arquivos"}}
"O que é Docker?" → {{"action":"search","query":"O que é Docker"}}
"Olá, como vai?" → {{"action":"answer"}}

REGRAS:
- obsidian_append: adicionar item/texto a nota existente
- obsidian_create: criar nota nova
- obsidian_read: ler/mostrar conteúdo de uma nota
- obsidian_update: sobrescrever/editar conteúdo de nota existente
- schedule_once: lembrete com data/hora específica
- schedule_recurring: lembrete que se repete (todo dia, toda semana)
- delegate_claude: qualquer tarefa técnica (código, scripts, análise de projetos, refatoração)
- search: perguntas factuais sobre o mundo
- answer: conversa geral

Retorne APENAS o JSON."""

EXTRACT_FACTS_PROMPT = """Extraia fatos duradouros sobre o usuário desta conversa (nome, profissão, projetos, hábitos).
Retorne APENAS JSON: {{"facts": ["fato 1", "fato 2"]}}
Se não houver fatos novos: {{"facts": []}}

Conversa:
{conversation}"""

MORNING_BRIEFING_PROMPT = """Você é o Hermes. Prepare um briefing matinal conciso para o usuário.

Data/hora: {now}
Tarefas agendadas para hoje: {tasks}
Resumo do vault Obsidian: {vault_summary}

Escreva um briefing em português com:
1. Saudação breve
2. Tarefas do dia (se houver)
3. Destaques do vault relevantes para hoje
4. Uma sugestão proativa (se houver algo parado ou importante)

Seja direto e útil. Máximo 300 palavras."""

REFLECT_PROMPT = """Você é o Hermes. Analise o vault Obsidian do usuário e gere insights.

Índice do vault:
{vault_index}

Projetos identificados (notas com #projeto):
{projects}

Tarefas agendadas: {tasks}

Gere um relatório de reflexão em português com:
1. Resumo geral do vault (quantas notas, temas principais)
2. Projetos em andamento vs parados (sem progresso recente)
3. Sugestões de próximas ações
4. Itens que poderiam ser delegados ao Claude Code

Seja analítico e objetivo. Máximo 400 palavras."""

IMPROVE_PROJECT_PROMPT = """Você é um assistente de desenvolvimento. Analise esta nota de projeto e execute melhorias.

Nome do projeto: {note_name}
Conteúdo atual:
{content}

Iteração {iteration} de {max_iterations}.
Resultados anteriores: {previous_results}

Sua tarefa:
1. Identifique os requisitos e itens em aberto (- [ ])
2. Para cada item em aberto, implemente ou elabore a solução
3. Se envolver código, escreva o código completo e funcional
4. Marque os itens implementados como concluídos (- [x])
5. Adicione uma seção "## Implementação {date}" com o que foi feito

Responda com o resultado completo da implementação."""

HERMES_TAG_PROMPT = """Texto marcado com #hermes no Obsidian do usuário. Execute a ação indicada.

Arquivo: {file}
Texto: {line}
Data/hora: {now}
Notas disponíveis: {note_index}

Retorne APENAS JSON com a ação e confirmação:
{{"action":"answer","reply":"mensagem ao usuário"}}
{{"action":"obsidian_append","note":"nome","content":"texto","is_list_item":true,"reply":"confirmação"}}
{{"action":"obsidian_create","note":"caminho.md","content":"conteúdo","reply":"confirmação"}}
{{"action":"schedule_once","message":"lembrete","run_at":"ISO8601","reply":"confirmação"}}"""


# ── LLM helpers ───────────────────────────────────────────────────────────────

def extract_json(text: str) -> dict:
    match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except Exception:
            pass
    return {}


def _validate_cron(expr: str) -> bool:
    parts = expr.strip().split()
    if len(parts) != 5:
        return False
    return all(re.match(r'^[\d\*\/\-,]+$', p) for p in parts)


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
    except Exception as e:
        log.warning(f"Fact extraction failed: {e}")


# ── Obsidian write actions ────────────────────────────────────────────────────

def execute_obsidian_action(decision: dict) -> tuple[bool, str]:
    action = decision.get("action", "")
    note_name = decision.get("note", "")
    content = decision.get("content", "")

    if not note_name or not content:
        return False, "Não entendi qual nota ou conteúdo modificar."

    if action == "obsidian_append":
        search_name = note_name.replace(".md", "").strip()
        path = obsidian.find_note(search_name)
        if not path:
            return False, f"Nota '{note_name}' não encontrada no vault."
        is_list = decision.get("is_list_item", False)
        ok = obsidian.append_list_item(path, content) if is_list else obsidian.append_to_note(path, content)
        if ok:
            return True, f"✅ Adicionado à nota *{path.stem}*:\n`{content}`"
        return False, "Falha ao escrever na nota."

    elif action == "obsidian_create":
        create_path = note_name if note_name.endswith(".md") else f"{note_name}.md"
        path = obsidian.create_note(create_path, content)
        if path:
            return True, f"✅ Nota criada: *{path.stem}*"
        return False, "Falha ao criar nota."

    return False, "Ação desconhecida."


# ── Claude delegation ─────────────────────────────────────────────────────────

def delegate_to_claude(task: str, chat_id: str, context: str = ""):
    """Run a task in Claude Code via host_agent and send result to Telegram."""
    import httpx
    host_agent_url = os.getenv("HOST_AGENT_URL", "http://host.docker.internal:9000/exec")
    host_agent_secret = os.getenv("HOST_AGENT_SECRET", "hermes-secret-mude-isso")

    full_prompt = task
    if context:
        full_prompt = f"{task}\n\nContexto adicional:\n{context}"

    _send_telegram(chat_id, f"🤖 Delegando para o Claude Code:\n`{task[:200]}`")
    activity.record("delegate_claude", task, status="started")

    def _run():
        try:
            log.info(f"Delegating to Claude: {task[:100]}")
            safe_prompt = full_prompt.replace('"', "'")
            resp = httpx.post(
                host_agent_url,
                json={"command": f'claude --print "{safe_prompt}"'},
                headers={"X-Agent-Secret": host_agent_secret},
                timeout=300,
                verify=False,
            )
            output = resp.json().get("output", "(sem saída)")[:3500]
            activity.record("delegate_claude", task, result=output, status="ok")
            _send_telegram(chat_id, f"✅ *Claude Code concluiu:*\n```\n{output}\n```")
        except Exception as e:
            log.error(f"Claude delegation failed: {e}")
            activity.record("delegate_claude", task, result=str(e), status="error")
            _send_telegram(chat_id, f"❌ Erro ao executar no Claude Code: {e}")

    threading.Thread(target=_run, daemon=True).start()


# ── Retry helper ─────────────────────────────────────────────────────────────

def _with_retry(fn, *args, attempts: int = RETRY_ATTEMPTS, backoff: float = RETRY_BACKOFF, **kwargs):
    """Call fn(*args, **kwargs) with exponential-backoff retry on exception."""
    import time
    last_exc = None
    for attempt in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            if attempt < attempts - 1:
                wait = backoff ** attempt
                log.warning(f"Retry {attempt + 1}/{attempts} for {fn.__name__} in {wait:.0f}s: {e}")
                time.sleep(wait)
    raise last_exc


# ── Project improvement pipeline ─────────────────────────────────────────────

def _build_improve_prompt(note_name: str, content: str, iteration: int, previous: list[str]) -> str:
    prev_text = "\n---\n".join(previous) if previous else "Nenhum resultado anterior."
    return IMPROVE_PROJECT_PROMPT.format(
        note_name=note_name,
        content=content[:3000],
        iteration=iteration,
        max_iterations=MAX_IMPROVE_ITERATIONS,
        previous_results=prev_text[:1000],
        date=datetime.now().strftime("%Y-%m-%d"),
    )


def improve_project(note_name: str, chat_id: str):
    """
    Multi-iteration improvement pipeline: read note → delegate to Claude →
    append result to note → repeat up to MAX_IMPROVE_ITERATIONS.
    Runs in a background thread.
    """
    import httpx
    import time

    def _run():
        host_agent_url = os.getenv("HOST_AGENT_URL", "http://host.docker.internal:9000/exec")
        host_agent_secret = os.getenv("HOST_AGENT_SECRET", "hermes-secret-mude-isso")

        path = obsidian.find_note(note_name)
        if not path:
            _send_telegram(chat_id, f"❌ Nota `{note_name}` não encontrada no vault.")
            return

        _send_telegram(chat_id, f"🔧 Iniciando melhoria de `{path.stem}` ({MAX_IMPROVE_ITERATIONS} iteração/ões)…")
        activity.record("improve_project", note_name, status="started")

        previous_results: list[str] = []
        for iteration in range(1, MAX_IMPROVE_ITERATIONS + 1):
            try:
                content = obsidian.read_note(path)
                prompt = _build_improve_prompt(path.stem, content, iteration, previous_results)
                safe_prompt = prompt.replace('"', "'")

                log.info(f"Improve iteration {iteration}/{MAX_IMPROVE_ITERATIONS} for {path.stem}")
                resp = _with_retry(
                    httpx.post,
                    host_agent_url,
                    json={"command": f'claude --print "{safe_prompt}"'},
                    headers={"X-Agent-Secret": host_agent_secret},
                    timeout=300,
                    verify=False,
                )
                output = resp.json().get("output", "(sem saída)")
                previous_results.append(output)

                # Append iteration result to note
                section = (
                    f"\n\n---\n## Hermes/Claude — Iteração {iteration} "
                    f"({datetime.now().strftime('%Y-%m-%d %H:%M')})\n\n{output[:2000]}"
                )
                obsidian.append_to_note(path, section)

                _send_telegram(
                    chat_id,
                    f"✅ *Iteração {iteration}/{MAX_IMPROVE_ITERATIONS}* concluída para `{path.stem}`\n"
                    f"```\n{output[:800]}\n```"
                )

                if iteration < MAX_IMPROVE_ITERATIONS:
                    time.sleep(3)  # brief pause before next iteration

            except Exception as e:
                log.error(f"Improve iteration {iteration} failed: {e}")
                _send_telegram(chat_id, f"❌ Iteração {iteration} falhou: {e}")
                activity.record("improve_project", note_name, result=str(e), status="error")
                return

        activity.record("improve_project", note_name,
                        result=f"{MAX_IMPROVE_ITERATIONS} iterations done", status="ok")
        _send_telegram(chat_id, f"🎉 Melhoria de `{path.stem}` concluída em {MAX_IMPROVE_ITERATIONS} iteração/ões.")

    threading.Thread(target=_run, daemon=True).start()


# ── Weekly data backup ────────────────────────────────────────────────────────

def backup_data():
    """Create a weekly snapshot of all data files. Keeps last 4 backups."""
    import json as _json
    data_dir = Path(os.getenv("MEMORY_FILE", "/app/data/memory.json")).parent
    backup_dir = data_dir / "backups"
    backup_dir.mkdir(exist_ok=True)

    try:
        snapshot = {
            "ts": datetime.now().isoformat(),
            "memory": json.loads((data_dir / "memory.json").read_text()) if (data_dir / "memory.json").exists() else {},
            "tasks": json.loads((data_dir / "tasks.json").read_text()) if (data_dir / "tasks.json").exists() else {},
            "activity_recent": activity.get_recent(50),
        }
        fname = backup_dir / f"backup_{datetime.now().strftime('%Y%m%d')}.json"
        fname.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info(f"Data backup saved: {fname.name}")

        # Keep only the 4 most recent backups
        backups = sorted(backup_dir.glob("backup_*.json"))
        for old in backups[:-4]:
            old.unlink()
            log.info(f"Removed old backup: {old.name}")

        activity.record("backup", f"backup_{datetime.now().strftime('%Y%m%d')}.json", status="ok")
    except Exception as e:
        log.error(f"Backup failed: {e}")


# ── Morning briefing ─────────────────────────────────────────────────────────

def morning_briefing():
    """Generate and send a morning briefing via Telegram. Called by APScheduler."""
    log.info("Generating morning briefing…")
    try:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        tasks = sched.list_tasks()
        tasks_text = "\n".join(
            f"- {t['message']} ({t.get('run_at','') or t.get('cron','')})" for t in tasks
        ) or "Nenhuma tarefa agendada."

        # Top 5 recently modified notes as vault summary
        notes = sorted(obsidian._all_notes(), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
        vault_summary = "\n".join(f"- {p.stem}" for p in notes) or "Vault vazio."

        prompt = MORNING_BRIEFING_PROMPT.format(
            now=now_str,
            tasks=tasks_text,
            vault_summary=vault_summary,
        )
        resp = client.chat(model=MODEL, messages=[{"role": "user", "content": prompt}])
        briefing = resp["message"]["content"]
        _send_telegram(DEFAULT_CHAT_ID, f"☀️ *Briefing matinal*\n\n{briefing}")
        activity.record("morning_briefing", "daily briefing", result=briefing[:200], status="ok")
    except Exception as e:
        log.error(f"Morning briefing failed: {e}")


# ── Project notes scanner ────────────────────────────────────────────────────

def _find_project_notes() -> list[dict]:
    """Find notes tagged with #projeto and assess staleness."""
    results = []
    for path in obsidian._all_notes():
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
            if "#projeto" not in content.lower():
                continue
            mtime = path.stat().st_mtime
            age_days = (datetime.now().timestamp() - mtime) / 86400
            open_items = len([
                l for l in content.splitlines()
                if l.strip().startswith("- [ ]")
            ])
            results.append({
                "file": str(path.relative_to(obsidian.VAULT)),
                "path": str(path),
                "age_days": round(age_days, 1),
                "open_items": open_items,
                "excerpt": content[:300].strip(),
            })
        except Exception:
            continue
    return results


def _scan_project_notes():
    """Background thread: alert on stale projects and suggest Claude delegation."""
    import time
    while True:
        time.sleep(PROJECT_SCAN_INTERVAL)
        try:
            projects = _find_project_notes()
            stale = [p for p in projects if p["age_days"] >= PROJECT_STALE_DAYS and p["open_items"] > 0]
            if not stale:
                continue

            lines = [f"📋 *{len(stale)} projeto(s) com itens abertos há +{PROJECT_STALE_DAYS} dias:*\n"]
            for p in stale[:5]:
                lines.append(
                    f"• `{p['file']}` — {p['open_items']} item(s) aberto(s), "
                    f"última modificação há {p['age_days']} dias\n"
                    f"  _Quer delegar ao Claude Code? Envie:_ "
                    f"`delegar projeto {p['file']}`"
                )
            _send_telegram(DEFAULT_CHAT_ID, "\n".join(lines))
            activity.record("project_scan", f"{len(stale)} stale projects", status="alert")
        except Exception as e:
            log.warning(f"Project scan error: {e}")


# ── #hermes tag scanner ───────────────────────────────────────────────────────

def _process_hermes_tags():
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

                    obsidian.mark_hermes_done(tag["path"], tag["line_number"])
                    _send_telegram(DEFAULT_CHAT_ID, f"📝 *#hermes* em `{tag['file']}`:\n{reply}")
                except Exception as e:
                    log.warning(f"hermes tag processing failed for {tag['file']}: {e}")
        except Exception as e:
            log.warning(f"hermes tag scan error: {e}")


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_histories()
    sched.set_notify_callback(_send_telegram)
    sched.start()
    sched.add_internal_cron(morning_briefing, MORNING_BRIEFING_CRON, "morning_briefing")
    sched.add_internal_cron(backup_data, BACKUP_CRON, "weekly_backup")
    threading.Thread(target=_process_hermes_tags, daemon=True).start()
    threading.Thread(target=_scan_project_notes, daemon=True).start()
    threading.Thread(
        target=vault_index.build_index, args=(obsidian.VAULT,), daemon=True
    ).start()
    yield
    _save_histories()


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


_START_TIME = datetime.now()


@app.get("/status")
def status():
    recent = activity.get_recent(1)
    last_action = recent[0] if recent else None
    return {
        "status": "ok",
        "model": MODEL,
        "uptime_seconds": int((datetime.now() - _START_TIME).total_seconds()),
        "vault_index": vault_index.get_stats(),
        "scheduled_tasks": len(sched.list_tasks()),
        "memory_facts": len(get_facts()),
        "last_activity": last_action,
    }


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


@app.get("/reflect")
def reflect():
    """Trigger vault reflection and return insights."""
    try:
        vault_idx = obsidian.get_note_index()
        projects = _find_project_notes()
        projects_text = "\n".join(
            f"- {p['file']} ({p['open_items']} abertos, {p['age_days']}d atrás)" for p in projects
        ) or "Nenhum projeto encontrado."
        tasks_text = "\n".join(
            f"- {t['message']}" for t in sched.list_tasks()
        ) or "Nenhuma tarefa."

        prompt = REFLECT_PROMPT.format(
            vault_index=vault_idx[:2000],
            projects=projects_text,
            tasks=tasks_text,
        )
        resp = client.chat(model=MODEL, messages=[{"role": "user", "content": prompt}])
        insight = resp["message"]["content"]
        activity.record("reflect", "vault reflection", result=insight[:200], status="ok")
        return {"insight": insight, "projects": projects}
    except Exception as e:
        log.error(f"Reflect failed: {e}")
        return {"error": str(e)}


@app.get("/activity")
def activity_log(n: int = 10):
    return {"entries": activity.get_recent(n)}


@app.get("/projects")
def projects_list():
    return {"projects": _find_project_notes()}


class ImproveRequest(BaseModel):
    note: str
    chat_id: str


@app.post("/projects/improve")
def projects_improve(req: ImproveRequest):
    path = obsidian.find_note(req.note)
    if not path:
        return {"ok": False, "error": f"Nota '{req.note}' não encontrada."}
    improve_project(req.note, req.chat_id)
    return {"ok": True, "note": str(path.relative_to(obsidian.VAULT)), "iterations": MAX_IMPROVE_ITERATIONS}


@app.get("/projects/detail")
def project_detail(note: str):
    path = obsidian.find_note(note)
    if not path:
        return {"error": f"Nota '{note}' não encontrada."}
    content = obsidian.read_note(path)
    lines = content.splitlines()
    open_items = [l.strip() for l in lines if l.strip().startswith("- [ ]")]
    done_items = [l.strip() for l in lines if l.strip().startswith("- [x]")]
    recent = activity.get_recent(20)
    note_activity = [e for e in recent if note.lower() in e.get("prompt", "").lower()]
    return {
        "file": str(path.relative_to(obsidian.VAULT)),
        "content": content[:3000],
        "open_items": open_items,
        "done_items": done_items,
        "age_days": round((datetime.now().timestamp() - path.stat().st_mtime) / 86400, 1),
        "recent_activity": note_activity[-3:],
    }


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
    log.info(f"Chat from user={req.user_id}: {req.message[:80]}")
    history = get_history(req.user_id)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")

    # Classify intent with gemma2:2b (with retry)
    try:
        decision_resp = _with_retry(
            client.chat,
            model=MODEL,
            messages=[{"role": "user", "content": DECISION_PROMPT.format(
                question=req.message,
                now=now_str,
            )}],
        )
        decision_raw = decision_resp["message"]["content"]
        decision = extract_json(decision_raw)
        action = decision.get("action", "answer")
        log.debug(f"Intent: {action} | raw: {decision_raw[:120]}")
    except Exception as e:
        log.error(f"Intent classification failed: {e}")
        action = "answer"
        decision = {}

    # ── Obsidian read ──────────────────────────────────────────────────────
    if action == "obsidian_read":
        note_name = decision.get("note", "")
        path = obsidian.find_note(note_name) if note_name else None
        if path:
            content = obsidian.read_note(path)
            reply = f"📄 *{path.stem}*\n\n```\n{content[:3000]}\n```"
        else:
            reply = f"Não encontrei a nota '{note_name}' no vault."
        history.append({"role": "user", "content": req.message})
        history.append({"role": "assistant", "content": reply})
        _save_histories()
        return {"reply": reply, "action": "obsidian_read"}

    # ── Obsidian update ────────────────────────────────────────────────────
    if action == "obsidian_update":
        note_name = decision.get("note", "")
        new_content = decision.get("content", "")
        path = obsidian.find_note(note_name) if note_name else None
        if not path:
            reply = f"Não encontrei a nota '{note_name}' para atualizar."
        elif not new_content:
            reply = "Não entendi o conteúdo novo para a nota."
        else:
            ok = obsidian.update_note_content(path, new_content)
            reply = f"✅ Nota *{path.stem}* atualizada." if ok else "Falha ao atualizar a nota."
        history.append({"role": "user", "content": req.message})
        history.append({"role": "assistant", "content": reply})
        _save_histories()
        return {"reply": reply, "action": "obsidian_update"}

    # ── Obsidian write ─────────────────────────────────────────────────────
    if action in ("obsidian_append", "obsidian_create"):
        ok, reply = execute_obsidian_action(decision)
        history.append({"role": "user", "content": req.message})
        history.append({"role": "assistant", "content": reply})
        _save_histories()
        return {"reply": reply, "action": action, "ok": ok}

    # ── Schedule once ──────────────────────────────────────────────────────
    if action == "schedule_once" and decision.get("run_at"):
        try:
            run_at = datetime.fromisoformat(decision["run_at"])
            task_id = sched.add_once(decision["message"], req.chat_id, run_at)
            reply = f"✅ Agendado para {run_at.strftime('%d/%m/%Y às %H:%M')}:\n_{decision['message']}_\n\nID: `{task_id}`"
        except Exception as e:
            log.warning(f"schedule_once failed: {e}")
            reply = f"Não consegui agendar: {e}"
        history.append({"role": "user", "content": req.message})
        history.append({"role": "assistant", "content": reply})
        _save_histories()
        return {"reply": reply, "action": "schedule_once"}

    # ── Schedule recurring ─────────────────────────────────────────────────
    if action == "schedule_recurring" and decision.get("cron"):
        cron_expr = decision["cron"]
        if not _validate_cron(cron_expr):
            log.warning(f"Invalid cron '{cron_expr}', falling back to schedule_once")
            # Fall through to answer so user knows what happened
            reply = f"Não consegui criar o lembrete recorrente (cron inválido: `{cron_expr}`). Tente especificar melhor o horário."
            history.append({"role": "user", "content": req.message})
            history.append({"role": "assistant", "content": reply})
            _save_histories()
            return {"reply": reply, "action": "schedule_recurring_failed"}
        try:
            task_id = sched.add_recurring(decision["message"], req.chat_id, cron_expr)
            reply = f"✅ Lembrete recorrente criado (`{cron_expr}`):\n_{decision['message']}_\n\nID: `{task_id}`"
        except Exception as e:
            log.warning(f"schedule_recurring failed: {e}")
            reply = f"Não consegui agendar: {e}"
        history.append({"role": "user", "content": req.message})
        history.append({"role": "assistant", "content": reply})
        _save_histories()
        return {"reply": reply, "action": "schedule_recurring"}

    # ── Delegate to Claude Code ────────────────────────────────────────────
    if action == "delegate_claude" and decision.get("task"):
        # Attach top relevant vault notes as context
        vault_hits = vault_index.search_similar(obsidian.VAULT, decision["task"], top_k=2)
        if not vault_hits:
            vault_hits = obsidian.search_notes(decision["task"], max_results=2)
        vault_ctx = "\n\n".join(f"[{h['file']}]\n{h['excerpt']}" for h in vault_hits)
        delegate_to_claude(decision["task"], req.chat_id, context=vault_ctx)
        reply = "🤖 Tarefa enviada ao Claude Code! Te aviso quando terminar."
        history.append({"role": "user", "content": req.message})
        history.append({"role": "assistant", "content": reply})
        _save_histories()
        return {"reply": reply, "action": "delegate_claude"}

    # ── Web search ─────────────────────────────────────────────────────────
    search_context = ""
    searched = False
    if action == "search" and decision.get("query"):
        try:
            results = web_search(decision["query"])
            search_context = f"\n\nResultados de busca:\n{results}"
            searched = True
        except Exception as e:
            log.warning(f"Web search failed: {e}")

    # ── Normal answer ──────────────────────────────────────────────────────
    # Prefer semantic search; fall back to keyword search if model unavailable
    obsidian_hits = vault_index.search_similar(obsidian.VAULT, req.message, top_k=3)
    if not obsidian_hits:
        obsidian_hits = obsidian.search_notes(req.message, max_results=3)
    obsidian_context = "\n\n".join(
        f"[{h['file']}]\n{h['excerpt']}" for h in obsidian_hits
    ) if obsidian_hits else ""

    system = build_system_prompt(obsidian_context)
    messages = [{"role": "system", "content": system}]
    messages.extend(list(history))
    messages.append({"role": "user", "content": req.message + search_context})

    try:
        response = _with_retry(client.chat, model=MODEL, messages=messages)
        reply = response["message"]["content"]
    except Exception as e:
        log.error(f"LLM chat failed: {e}")
        reply = "Desculpe, tive um problema ao processar sua mensagem. Tente novamente."

    history.append({"role": "user", "content": req.message})
    history.append({"role": "assistant", "content": reply})

    if len(history) % 8 == 0:
        extract_and_save_facts(req.user_id)

    _save_histories()
    log.info(f"Reply to user={req.user_id}: {reply[:80]}")
    return {"reply": reply, "searched": searched}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
