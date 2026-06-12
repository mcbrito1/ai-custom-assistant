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
from memory import (build_system_prompt, add_fact, get_facts, search_obsidian,
                     update_profile, add_facts_bulk, replace_facts,
                     update_profile_from_classified, mark_stale_facts)
import obsidian
import vault_index
import scheduler as sched
import activity
import learner
import conversation_rag
import calendar_integration

OLLAMA_HOST = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")

# ── Model routing ─────────────────────────────────────────────────────────────
# INTENT_MODEL   : JSON classification — hermes3:3b (strong instruction follower, ChatML)
# CHAT_MODEL     : conversational answers — gemma2:2b (fluid Portuguese)
# REASONING_MODEL: analysis, reflection, briefing, fact extraction — hermes3:3b
# CODE_MODEL     : code context prep for /aprimorar pipeline — qwen2.5-coder:1.5b
# Falls back to CHAT_MODEL if a specialized model is unavailable.
CHAT_MODEL      = os.getenv("OLLAMA_MODEL",      "gemma2:2b")
INTENT_MODEL    = os.getenv("INTENT_MODEL",      "hermes3:3b")
REASONING_MODEL = os.getenv("REASONING_MODEL",   "hermes3:3b")
CODE_MODEL      = os.getenv("CODE_MODEL",        "qwen2.5-coder:1.5b")

# Legacy alias so existing callers that used MODEL still work
MODEL = CHAT_MODEL

HISTORY_SIZE = int(os.getenv("HISTORY_SIZE", "20"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_OWNER_ID = os.getenv("TELEGRAM_OWNER_ID", "0")
DEFAULT_CHAT_ID = os.getenv("TELEGRAM_OWNER_ID", "0")
HERMES_TAG_SCAN_INTERVAL = int(os.getenv("HERMES_TAG_SCAN_INTERVAL", "60"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
HISTORY_FILE = os.getenv("HISTORY_FILE", "/app/data/history.json")
MORNING_BRIEFING_CRON = os.getenv("MORNING_BRIEFING_CRON", "0 8 * * *")
BACKUP_CRON = os.getenv("BACKUP_CRON", "0 3 * * 0")
WEEKLY_REFLECTION_CRON = os.getenv("WEEKLY_REFLECTION_CRON", "0 22 * * 6")
PROJECT_SCAN_INTERVAL = int(os.getenv("PROJECT_SCAN_INTERVAL", "3600"))
AGENT_MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "5"))
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

# Pending delegations awaiting user confirmation — keyed by short ID
import uuid as _uuid
_pending_delegations: dict[str, dict] = {}


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

# ── Prompts (hermes3:3b uses ChatML system messages; gemma2:2b gets inline prompts) ──

# Intent classifier — system message defines schema+rules; user message is the input only.
DECISION_SYSTEM = """Você é o classificador de intenções do Hermes. Retorne APENAS JSON válido, sem texto extra.

Schema de saída (escolha exatamente um):
{"action":"obsidian_append","note":"<nome>","content":"<texto>","is_list_item":true|false}
{"action":"obsidian_create","note":"<nome>.md","content":"<conteúdo>"}
{"action":"obsidian_read","note":"<nome>"}
{"action":"obsidian_update","note":"<nome>","content":"<novo conteúdo>"}
{"action":"schedule_once","message":"<lembrete>","run_at":"<ISO8601>"}
{"action":"schedule_recurring","message":"<lembrete>","cron":"<5 campos cron>"}
{"action":"delegate_claude","task":"<descrição completa>"}
{"action":"agent_plan","task":"<descrição completa>"}
{"action":"calendar_create","summary":"<título>","start":"<ISO8601>","duration_minutes":<int>}
{"action":"calendar_list","days":<int>}
{"action":"search","query":"<consulta>"}
{"action":"answer"}

Regras:
- obsidian_append: adicionar a nota existente | obsidian_create: criar nota nova
- obsidian_read: ler/mostrar nota | obsidian_update: sobrescrever nota existente
- schedule_once: lembrete pontual | schedule_recurring: lembrete periódico (todo dia/semana)
- delegate_claude: código, scripts, refatoração, análise técnica
- agent_plan: múltiplos passos heterogêneos (pesquisar E criar E agendar)
- calendar_create/list: eventos no Google Calendar
- search: QUALQUER pergunta sobre fatos do mundo, notícias, datas de eventos, pessoas, lugares, preços, previsão do tempo. Use sempre que o usuário pedir para "pesquisar", "buscar", "procurar" ou fizer uma pergunta factual.
- answer: APENAS saudações e perguntas sobre o próprio Hermes

Exemplos:
"Pesquise o primeiro jogo do Brasil na copa" → {"action":"search","query":"primeiro jogo do Brasil na copa do mundo 2026"}
"Qual a previsão do tempo amanhã?" → {"action":"search","query":"previsão do tempo amanhã"}
"O que é machine learning?" → {"action":"search","query":"o que é machine learning"}
"Adicione leite à lista de compras" → {"action":"obsidian_append","note":"lista de compras","content":"leite","is_list_item":true}
"Me lembre amanhã às 9h" → {"action":"schedule_once","message":"lembrete","run_at":"<ISO8601>"}
"Olá, como vai?" → {"action":"answer"}"""

DECISION_PROMPT = "Data/hora: {now}\nMensagem: {question}"

EXTRACT_FACTS_SYSTEM = """Extraia fatos duradouros sobre o usuário (nome, profissão, projetos, hábitos, preferências).
Retorne APENAS JSON: {"facts": ["fato 1", "fato 2"]}
Se não houver fatos: {"facts": []}"""

EXTRACT_FACTS_PROMPT = "Conversa:\n{conversation}"

AGENT_PLAN_SYSTEM = """Você é o Hermes. Quebre a tarefa em passos independentes e retorne APENAS JSON.
Schema: {"steps": [{"action": "search"|"obsidian_create"|"obsidian_append"|"schedule_once"|"calendar_create"|"delegate_claude"|"answer", ...campos específicos da ação...}]}
Máximo {max_steps} passos. Cada passo deve ser executável isoladamente."""

AGENT_PLAN_PROMPT = """Tarefa: {task}
Data/hora: {now}
Notas disponíveis: {note_index}
Google Calendar disponível: {calendar_available}"""

WEEKLY_REFLECTION_SYSTEM = """Você é o Hermes fazendo reflexão semanal. Responda em português, de forma direta e acionável. Máximo 300 palavras.
Estruture em: O que funcionou | O que falhou | Padrões detectados | Próxima semana | Sugestão proativa"""

WEEKLY_REFLECTION_PROMPT = """Atividades dos últimos 7 dias:
{activity_summary}

Padrões de comportamento:
{behavior_summary}"""

MORNING_BRIEFING_SYSTEM = """Você é o Hermes. Escreva um briefing matinal em português. Máximo 300 palavras.
Estruture em: Saudação | Tarefas do dia | Destaques do vault | Sugestão proativa"""

MORNING_BRIEFING_PROMPT = """Data/hora: {now}
Tarefas agendadas para hoje: {tasks}
Resumo do vault: {vault_summary}"""

REFLECT_SYSTEM = """Você é o Hermes. Analise o vault e gere insights em português. Máximo 400 palavras.
Estruture em: Resumo do vault | Projetos em andamento vs parados | Próximas ações sugeridas | Itens para delegar ao Claude Code"""

REFLECT_PROMPT = """Índice do vault:
{vault_index}

Projetos (#projeto):
{projects}

Tarefas agendadas: {tasks}"""

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

HERMES_TAG_SYSTEM = """Processe a linha marcada com #hermes no Obsidian. Retorne APENAS JSON com a ação e campo "reply".
Opções:
{"action":"answer","reply":"<msg>"}
{"action":"obsidian_append","note":"<nome>","content":"<texto>","is_list_item":true,"reply":"<confirmação>"}
{"action":"obsidian_create","note":"<nome>.md","content":"<conteúdo>","reply":"<confirmação>"}
{"action":"schedule_once","message":"<lembrete>","run_at":"<ISO8601>","reply":"<confirmação>"}"""

HERMES_TAG_PROMPT = """Arquivo: {file}
Texto: {line}
Data/hora: {now}
Notas disponíveis: {note_index}"""


# ── LLM helpers ───────────────────────────────────────────────────────────────

def extract_json(text: str) -> dict:
    """Extract the first valid JSON object from text, handling nested structures."""
    # Try to parse the whole text first (model returned pure JSON)
    stripped = text.strip()
    try:
        result = json.loads(stripped)
        if isinstance(result, dict):
            return result
    except Exception:
        pass

    # Walk through text finding '{' and try to parse from that position
    for i, ch in enumerate(text):
        if ch != '{':
            continue
        # Track brace depth to find the matching closing brace
        depth = 0
        in_string = False
        escape = False
        for j, c in enumerate(text[i:], start=i):
            if escape:
                escape = False
                continue
            if c == '\\' and in_string:
                escape = True
                continue
            if c == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    candidate = text[i:j + 1]
                    try:
                        result = json.loads(candidate)
                        if isinstance(result, dict):
                            return result
                    except Exception:
                        break
    return {}


def _validate_cron(expr: str) -> bool:
    parts = expr.strip().split()
    if len(parts) != 5:
        return False
    return all(re.match(r'^[\d\*\/\-,]+$', p) for p in parts)


def web_search(query: str, max_results: int = 5) -> str:
    # verify=False for corporate SSL proxy environments
    with DDGS(verify=False) as ddgs:
        results = list(ddgs.text(query, max_results=max_results))
    if not results:
        return "Nenhum resultado encontrado."
    return "\n\n".join(
        f"Título: {r['title']}\nResumo: {r['body']}\nFonte: {r['href']}" for r in results
    )


def extract_and_save_facts(user_id: str):
    """Extract facts every 4 messages, deduplicate semantically, classify into profile."""
    history = get_history(user_id)
    if len(history) < 2 or len(history) % 4 != 0:
        return

    conversation = "\n".join(
        f"{'Usuário' if m['role'] == 'user' else 'Hermes'}: {m['content']}"
        for m in list(history)[-12:]
    )

    def _run():
        try:
            raw = _llm(
                REASONING_MODEL,
                [{"role": "user", "content": EXTRACT_FACTS_PROMPT.format(conversation=conversation)}],
                system=EXTRACT_FACTS_SYSTEM,
            )
            new_facts = [f for f in extract_json(raw).get("facts", []) if f and len(f) > 5]
            if not new_facts:
                return

            added = add_facts_bulk(new_facts)
            if added:
                log.info(f"Extracted {added} new fact(s).")

            # Semantic dedup across all stored facts
            from memory import get_facts_rich
            all_texts = [f["text"] for f in get_facts_rich()]
            deduped = learner.deduplicate_facts_semantic(all_texts)
            if len(deduped) < len(all_texts):
                replace_facts(deduped)

            # Auto-populate profile categories
            classified = learner.classify_facts_into_profile(deduped, _llm)
            if classified:
                update_profile_from_classified(classified)

            mark_stale_facts()
        except Exception as e:
            log.warning(f"Fact extraction pipeline failed: {e}")

    threading.Thread(target=_run, daemon=True).start()


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


# ── Model availability cache ──────────────────────────────────────────────────
_model_available: dict[str, bool] = {}


def _is_model_available(model: str) -> bool:
    if model in _model_available:
        return _model_available[model]
    try:
        models = client.list()
        names = [m.get("name", m.get("model", "")) for m in models.get("models", [])]
        available = any(model in name for name in names)
        _model_available[model] = available
        if not available:
            log.warning(f"Model '{model}' not found in Ollama — will fall back to {CHAT_MODEL}")
        return available
    except Exception as e:
        log.warning(f"Could not check model availability: {e}")
        _model_available[model] = False
        return False


def _llm(model: str, messages: list, fallback: str = CHAT_MODEL, system: str = None) -> str:
    """
    Call Ollama with the given model, falling back to `fallback` if unavailable.
    Pass system= to prepend a system message (hermes3 supports ChatML system role natively).
    Strips <think>…</think> blocks from output (DeepSeek-R1 compatibility).
    """
    target = model if _is_model_available(model) else fallback
    if system:
        messages = [{"role": "system", "content": system}] + list(messages)
    resp = _with_retry(client.chat, model=target, messages=messages)
    text = resp["message"]["content"]
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    return text


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
                raw_prompt = _build_improve_prompt(path.stem, content, iteration, previous_results)

                # qwen2.5-coder pre-processes the task into a precise technical brief
                # before handing off to Claude Code for actual execution
                code_brief = _llm(
                    CODE_MODEL,
                    [{"role": "user", "content":
                      f"Analise esta nota de projeto e crie um brief técnico preciso "
                      f"para um desenvolvedor executar. Liste: arquivos a modificar, "
                      f"mudanças específicas, e ordem de execução. Máximo 300 palavras.\n\n{raw_prompt}"}],
                )
                safe_prompt = (raw_prompt + f"\n\nBrief técnico:\n{code_brief}").replace('"', "'")

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


# ── Weekly self-reflection ────────────────────────────────────────────────────

def weekly_reflection():
    """Analyze last 7 days of activity and behavior. Send insights to Telegram."""
    log.info("Running weekly self-reflection…")
    try:
        recent = activity.get_recent(50)
        activity_lines = "\n".join(
            f"- {e['ts']} | {e['action']} | {e['status']} | {e['prompt'][:60]}"
            for e in recent
        ) or "Nenhuma atividade registrada."

        behavior = learner.get_behavior_summary()
        top_intents = ", ".join(f"{k}({v})" for k, v in behavior.get("top_intents", []))
        behavior_text = (
            f"Interações totais: {behavior.get('total_interactions', 0)}\n"
            f"Intents mais frequentes: {top_intents or 'N/A'}\n"
            f"Hora de pico: {behavior.get('peak_hour', 'N/A')}h\n"
            f"Correções registradas: {behavior.get('correction_count', 0)}"
        )

        prompt = WEEKLY_REFLECTION_PROMPT.format(
            activity_summary=activity_lines[:2000],
            behavior_summary=behavior_text,
        )
        reflection = _llm(REASONING_MODEL, [{"role": "user", "content": prompt}], system=WEEKLY_REFLECTION_SYSTEM)

        # Save conclusions to profile current_context
        from memory import update_profile
        update_profile("current_context", "weekly_reflection", reflection[:500])

        _send_telegram(DEFAULT_CHAT_ID, f"🧠 *Reflexão semanal do Hermes*\n\n{reflection}")
        activity.record("weekly_reflection", "self-analysis", result=reflection[:200], status="ok")
    except Exception as e:
        log.error(f"Weekly reflection failed: {e}")


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
        briefing = _llm(REASONING_MODEL, [{"role": "user", "content": prompt}], system=MORNING_BRIEFING_SYSTEM)
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


# ── Multi-step agent ─────────────────────────────────────────────────────────

def _execute_agent_plan(task: str, chat_id: str, user_id: str) -> str:
    """
    Plan and execute a multi-step task synchronously.
    Returns a summary of what was done.
    """
    try:
        note_index = obsidian.get_note_index()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        prompt = AGENT_PLAN_PROMPT.format(
            task=task,
            now=now_str,
            note_index=note_index[:1000],
            calendar_available=calendar_integration.is_available(),
        )
        raw = _llm(
            REASONING_MODEL,
            [{"role": "user", "content": prompt}],
            system=AGENT_PLAN_SYSTEM.format(max_steps=AGENT_MAX_STEPS),
        )
        plan = extract_json(raw)
        steps = plan.get("steps", [])
        if not steps:
            return "Não consegui planejar os passos para essa tarefa."

        results = []
        _send_telegram(chat_id, f"🤖 *Plano de {len(steps)} passo(s) para:* _{task}_")

        for i, step in enumerate(steps[:AGENT_MAX_STEPS], 1):
            act = step.get("action", "answer")
            try:
                if act == "answer":
                    results.append(f"Passo {i}: {step.get('reply', '')}")
                elif act == "search":
                    sr = web_search(step["query"])
                    results.append(f"Passo {i} (busca): {sr[:300]}")
                elif act in ("obsidian_append", "obsidian_create", "obsidian_update", "obsidian_read"):
                    ok, msg = execute_obsidian_action(step)
                    results.append(f"Passo {i} ({act}): {msg}")
                elif act == "schedule_once":
                    run_at = datetime.fromisoformat(step["run_at"])
                    sched.add_once(step["message"], chat_id, run_at)
                    results.append(f"Passo {i}: lembrete agendado para {step['run_at'][:16]}")
                elif act == "calendar_create" and calendar_integration.is_available():
                    start_dt = datetime.fromisoformat(step["start"])
                    calendar_integration.create_event(step["summary"], start_dt)
                    results.append(f"Passo {i}: evento criado — {step['summary']}")
                elif act == "delegate_claude":
                    delegate_to_claude(step["task"], chat_id)
                    results.append(f"Passo {i}: delegado ao Claude Code")
                else:
                    results.append(f"Passo {i} ({act}): ignorado")
            except Exception as e:
                results.append(f"Passo {i} ({act}): erro — {e}")

        summary = "\n".join(results)
        activity.record("agent_plan", task, result=summary[:500], status="ok")
        return f"✅ *Tarefa concluída em {len(steps)} passo(s):*\n\n{summary}"
    except Exception as e:
        log.error(f"Agent plan failed: {e}")
        return f"❌ Erro ao executar plano: {e}"


# ── Contextual project alert ──────────────────────────────────────────────────

def _contextual_project_alert(query: str, chat_id: str):
    """
    If the user's message relates to a stale project topic, proactively alert.
    Runs in background to not delay the chat response.
    """
    def _run():
        try:
            hits = vault_index.search_similar(obsidian.VAULT, query, top_k=3)
            if not hits:
                return
            projects = _find_project_notes()
            stale = [p for p in projects if p["age_days"] >= PROJECT_STALE_DAYS and p["open_items"] > 0]
            for hit in hits:
                for proj in stale:
                    if hit["file"] == proj["file"]:
                        _send_telegram(
                            chat_id,
                            f"💡 *Alerta contextual:* você está perguntando sobre `{hit['file']}`, "
                            f"que tem {proj['open_items']} item(s) aberto(s) há {proj['age_days']} dias.\n"
                            f"Quer que eu delegue ao Claude Code? Envie `/aprimorar {proj['file']}`"
                        )
                        return  # one alert per message is enough
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


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
                    raw = _llm(REASONING_MODEL, [{"role": "user", "content": prompt}], system=HERMES_TAG_SYSTEM)
                    decision = extract_json(raw)
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
    conversation_rag.init()
    sched.set_notify_callback(_send_telegram)
    sched.start()
    sched.add_internal_cron(morning_briefing, MORNING_BRIEFING_CRON, "morning_briefing")
    sched.add_internal_cron(backup_data, BACKUP_CRON, "weekly_backup")
    sched.add_internal_cron(weekly_reflection, WEEKLY_REFLECTION_CRON, "weekly_reflection")
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
        "calendar_available": calendar_integration.is_available(),
        "models": {
            "chat":      CHAT_MODEL,
            "intent":    INTENT_MODEL,
            "reasoning": REASONING_MODEL,
            "code":      CODE_MODEL,
            "embed":     os.getenv("EMBED_MODEL", "nomic-embed-text"),
        },
        "uptime_seconds": int((datetime.now() - _START_TIME).total_seconds()),
        "vault_index": vault_index.get_stats(),
        "scheduled_tasks": len(sched.list_tasks()),
        "memory_facts": len(get_facts()),
        "last_activity": last_action,
    }


@app.get("/memory")
def memory():
    return {"facts": get_facts()}


@app.get("/memory/profile")
def memory_profile():
    from memory import get_profile
    return {"profile": get_profile()}


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
        insight = _llm(REASONING_MODEL, [{"role": "user", "content": prompt}], system=REFLECT_SYSTEM)
        activity.record("reflect", "vault reflection", result=insight[:200], status="ok")
        return {"insight": insight, "projects": projects}
    except Exception as e:
        log.error(f"Reflect failed: {e}")
        return {"error": str(e)}


@app.get("/activity")
def activity_log(n: int = 10):
    return {"entries": activity.get_recent(n)}


class FeedbackRequest(BaseModel):
    action: str
    context: str = ""
    rating: str  # "up" or "down"


@app.post("/feedback")
def feedback(req: FeedbackRequest):
    learner.record_feedback(req.context, req.rating, req.action)
    return {"ok": True}


class DelegationConfirmRequest(BaseModel):
    delegation_id: str


@app.post("/delegate/confirm")
def delegate_confirm(req: DelegationConfirmRequest):
    pending = _pending_delegations.pop(req.delegation_id, None)
    if not pending:
        return {"ok": False, "error": "Delegação não encontrada ou já processada."}
    delegate_to_claude(pending["task"], pending["chat_id"], context=pending.get("vault_ctx", ""))
    return {"ok": True}


@app.post("/delegate/cancel")
def delegate_cancel(req: DelegationConfirmRequest):
    _pending_delegations.pop(req.delegation_id, None)
    return {"ok": True}


@app.get("/behavior")
def behavior_summary():
    return learner.get_behavior_summary()


@app.get("/calendar/events")
def calendar_events(days: int = 7):
    if not calendar_integration.is_available():
        return {"error": "Google Calendar not configured", "events": []}
    return {"events": calendar_integration.list_events(days_ahead=days)}


@app.post("/calendar/event")
def calendar_create_event(body: dict):
    if not calendar_integration.is_available():
        return {"ok": False, "error": "Google Calendar not configured"}
    try:
        start = datetime.fromisoformat(body["start"])
        end_dt = start + __import__("datetime").timedelta(minutes=body.get("duration_minutes", 60))
        event = calendar_integration.create_event(
            summary=body["summary"], start=start, end=end_dt,
            description=body.get("description", ""),
        )
        return {"ok": event is not None, "event": event}
    except Exception as e:
        return {"ok": False, "error": str(e)}


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

    # Classify intent — hermes3:3b with system message for reliable JSON output
    try:
        decision_raw = _llm(
            INTENT_MODEL,
            [{"role": "user", "content": DECISION_PROMPT.format(
                question=req.message,
                now=now_str,
            )}],
            system=DECISION_SYSTEM,
        )
        decision = extract_json(decision_raw)
        action = decision.get("action", "answer")
        log.debug(f"Intent ({INTENT_MODEL}): {action} | raw: {decision_raw[:120]}")
    except Exception as e:
        log.error(f"Intent classification failed: {e}")
        action = "answer"
        decision = {}

    # Keyword-based search override — catches misclassifications when model falls back to gemma2
    _SEARCH_SIGNALS = ("pesquise", "pesquisar", "busque", "buscar", "procure", "procurar",
                       "o que é", "o que são", "quem é", "quando é", "quando foi",
                       "qual é", "quais são", "como funciona", "onde fica", "notícia",
                       "previsão do tempo", "cotação", "preço de")
    if action == "answer" and any(s in req.message.lower() for s in _SEARCH_SIGNALS):
        action = "search"
        decision = {"action": "search", "query": req.message}
        log.debug("Search override triggered by keyword match.")

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

    # ── Delegate to Claude Code (requires user confirmation) ──────────────
    if action == "delegate_claude" and decision.get("task"):
        vault_hits = vault_index.search_similar(obsidian.VAULT, decision["task"], top_k=2)
        if not vault_hits:
            vault_hits = obsidian.search_notes(decision["task"], max_results=2)
        vault_ctx = "\n\n".join(f"[{h['file']}]\n{h['excerpt']}" for h in vault_hits)
        del_id = _uuid.uuid4().hex[:12]
        _pending_delegations[del_id] = {"task": decision["task"], "chat_id": req.chat_id, "vault_ctx": vault_ctx}
        reply = f"🤖 *Delegar ao Claude Code:*\n```\n{decision['task'][:400]}\n```\nConfirmar?"
        history.append({"role": "user", "content": req.message})
        history.append({"role": "assistant", "content": reply})
        _save_histories()
        return {"reply": reply, "action": "delegate_claude", "delegation_id": del_id}

    # ── Calendar create ────────────────────────────────────────────────────
    if action == "calendar_create":
        if not calendar_integration.is_available():
            reply = "📅 Google Calendar não está configurado. Veja as instruções em `calendar_integration.py`."
        else:
            try:
                start_dt = datetime.fromisoformat(decision.get("start", ""))
                duration = decision.get("duration_minutes", 60)
                end_dt = start_dt + __import__("datetime").timedelta(minutes=duration)
                event = calendar_integration.create_event(
                    summary=decision.get("summary", req.message),
                    start=start_dt, end=end_dt,
                    description=decision.get("description", ""),
                )
                reply = f"📅 Evento criado: *{decision.get('summary')}* em {decision.get('start', '')[:16]}" if event else "❌ Falha ao criar evento no calendário."
            except Exception as e:
                reply = f"❌ Erro ao criar evento: {e}"
        history.append({"role": "user", "content": req.message})
        history.append({"role": "assistant", "content": reply})
        _save_histories()
        return {"reply": reply, "action": "calendar_create"}

    # ── Calendar list ──────────────────────────────────────────────────────
    if action == "calendar_list":
        if not calendar_integration.is_available():
            reply = "📅 Google Calendar não está configurado."
        else:
            days = decision.get("days", 7)
            events = calendar_integration.list_events(days_ahead=days)
            events_text = calendar_integration.format_events_text(events)
            reply = f"📅 *Próximos eventos ({days} dias):*\n\n{events_text}"
        history.append({"role": "user", "content": req.message})
        history.append({"role": "assistant", "content": reply})
        _save_histories()
        return {"reply": reply, "action": "calendar_list"}

    # ── Multi-step agent plan ──────────────────────────────────────────────
    if action == "agent_plan" and decision.get("task"):
        reply = _execute_agent_plan(decision["task"], req.chat_id, req.user_id)
        history.append({"role": "user", "content": req.message})
        history.append({"role": "assistant", "content": reply})
        _save_histories()
        return {"reply": reply, "action": "agent_plan"}

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

    # ── Long-context RAG (past conversations beyond the 20-msg window) ─────
    past_exchanges = conversation_rag.retrieve(req.user_id, req.message, top_k=2)
    rag_context = ("\n\nConversas anteriores relevantes:\n" +
                   "\n---\n".join(past_exchanges)) if past_exchanges else ""

    system = build_system_prompt(obsidian_context + rag_context)
    messages = [{"role": "system", "content": system}]
    messages.extend(list(history))
    messages.append({"role": "user", "content": req.message + search_context})

    try:
        reply = _llm(CHAT_MODEL, messages)
    except Exception as e:
        log.error(f"LLM chat failed: {e}")
        reply = "Desculpe, tive um problema ao processar sua mensagem. Tente novamente."

    # Index this exchange for long-context RAG
    conversation_rag.index_exchange(req.user_id, req.message, reply)

    # Contextual project alert if vault note related to query is stale
    if action in ("answer", "search") and obsidian_hits:
        _contextual_project_alert(req.message, req.chat_id)

    # Detect implicit correction before appending new response
    if len(history) >= 2 and learner.is_implicit_correction(req.message):
        last_response = history[-1]["content"] if history else ""
        learner.record_correction(last_response, req.message)
        log.info("Implicit correction detected and recorded.")

    history.append({"role": "user", "content": req.message})
    history.append({"role": "assistant", "content": reply})

    # Record behavior pattern for this interaction
    notes_used = [h["file"] for h in obsidian_hits] if obsidian_hits else []
    learner.record_behavior(action, notes_used=notes_used)

    extract_and_save_facts(req.user_id)

    _save_histories()
    log.info(f"Reply to user={req.user_id}: {reply[:80]}")
    return {"reply": reply, "searched": searched}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
