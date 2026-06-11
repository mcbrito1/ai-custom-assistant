# Hermes Agent — Claude Code Guide

## Overview

Hermes é um **secretário virtual autônomo** rodando em Docker, conectado a:
- **Ollama** (host.docker.internal:11434) com modelo `gemma2:2b` (cérebro principal)
- **Claude Code** via `host_agent.py` (delegação de tarefas técnicas complexas)
- **Telegram** para interface conversacional
- **Obsidian** vault em `C:\Git\Obsidian` (leitura/escrita/indexação semântica + git sync)
- **Windows host** via host_agent.py na porta 9000 (PowerShell + Claude CLI)
- **APScheduler** para agendamento de tarefas e jobs autônomos

---

## Project Structure

```
C:\Git\Hermes\
├── main.py              # FastAPI agent (chat, intent classification, autonomous jobs)
├── bot.py              # Telegram bot with command handlers
├── obsidian.py         # Vault read/write + #hermes tag scanner + index trigger
├── vault_index.py      # Semantic embeddings (nomic-embed-text) + cosine search
├── memory.py           # Persistent facts + structured profile + system prompt builder
├── scheduler.py        # APScheduler (cron + one-time + internal system jobs)
├── activity.py         # Activity log for delegations and autonomous actions
├── host_agent.py       # Standalone HTTP server on host (PowerShell + Claude CLI)
├── start_host_agent.bat # Auto-start wrapper
├── Dockerfile          # Python 3.12-slim + curl + git
├── docker-compose.yml  # Two services: hermes (agent) + hermes-bot (telegram)
├── requirements.txt    # Python dependencies
├── .env               # Config (NEVER commit this!)
├── .gitignore         # Excludes .env, data/, __pycache__
├── data/              # Persistent: memory.json, tasks.json, history.json,
│                      #   vault_index.json, activity_log.json, backups/
└── README.md          # User-facing docs

C:\Git\Obsidian/      # User's knowledge base (git-synced)
C:\Git\waha-rikin/    # WhatsApp WAHA client (feat/waha-whatsapp branch only)
```

---

## Key Design Decisions

### 1. **Branching Strategy**
- **master**: Production code. No WhatsApp integration. Clean, minimal.
- **feat/waha-whatsapp**: WhatsApp via waha-rikin (on hold). Preserved for future evaluation.

### 2. **Obsidian Sync Pattern**
- **Every read** calls `git pull --rebase --autostash` (rate-limited to 60s cooldown)
- **Every write** calls `git add -A && git commit && git push`
- This ensures the vault stays in sync across devices/sessions
- **Requirement**: `C:\Git\Obsidian` must be a git repo with a configured remote and accessible credentials (SSH key or credential helper)

### 3. **Persistent Memory**
- `/app/data/memory.json`: User facts extracted from conversation
- `/app/data/tasks.json`: Scheduled reminders (one-time + recurring)
- Both mounted via bind volume `./data:/app/data` for portability (not Docker named volumes)

### 4. **Intent Classification**
- No tool-use API calls (gemma2:2b doesn't support them)
- Instead: LLM-based classification with regex JSON extraction
- Actions: `search`, `schedule_once`, `schedule_recurring`, `obsidian_append`, `obsidian_create`, `answer`

### 5. **Corporate SSL Proxy**
- httpx with `verify=False` for Telegram Bot API (corporate cert interception)
- pip with `--trusted-host` in Dockerfile RUN command
- This is intentional and only safe within a controlled corporate environment

### 6. **#hermes Tag Scanner**
- Background thread scans vault every 60s for lines with `#hermes` (but not `#hermes/done`)
- Auto-processes via LLM, executes actions (append, create, schedule), marks as done
- Notifies user via Telegram upon completion
- Run `/hermestags` in Telegram to see pending actions

### 7. **Host Execution**
- `host_agent.py` runs standalone on Windows host port 9000
- Secured via `X-Agent-Secret` header (change the default secret!)
- Spawns PowerShell subprocesses; replaces `claude ` prefix with full path to `claude.cmd`
- Started via `start_host_agent.bat` in Windows Startup folder

---

## Important Security Notes

### Credentials & Secrets
- **TELEGRAM_TOKEN**: Real token in `.env` (DO NOT commit). Revoke via @BotFather if exposed.
- **HOST_AGENT_SECRET**: Set to production-grade secret before deployment. Current default: `hermes-secret-mude-isso`
- **SSH/Git credentials**: Ensure `C:\Git\Obsidian` remote has valid credentials. Use SSH keys or credential helper, not plaintext passwords.
- **.env is in .gitignore**: `git add .env` will be blocked by auto-mode. Never force-add it.

### SSL Verification
- Disabled (`verify=False`) for Telegram + WAHA due to corporate proxy
- **Only safe in a controlled, monitored corporate environment**
- Before deploying elsewhere, restore SSL verification or use proper certificate handling

---

## Development Workflow

### 1. **Local Changes to Code**
```bash
cd C:\Git\Hermes
# Edit files (main.py, bot.py, obsidian.py, etc.)
docker compose up -d --build   # Rebuild and restart
docker logs hermes-agent        # Verify startup
```

### 2. **Updating Obsidian Vault**
- Push changes from any device to the remote
- Next time Hermes reads the vault, it will pull automatically
- Any write triggers a commit + push

### 3. **Adding Python Dependencies**
```bash
# Edit requirements.txt
docker compose up -d --build
```

### 4. **Changing Ollama Model**
```bash
# In .env:
OLLAMA_MODEL=llama2  # or any model available on host
docker compose restart hermes
```

### 5. **Debugging**
- **Chat endpoint**: POST http://localhost:8000/chat
- **Health check**: GET http://localhost:8000/health
- **Telegram logs**: `docker logs hermes-bot`
- **Hermes logs**: `docker logs hermes-agent`
- **Memory check**: GET http://localhost:8000/memory
- **Pending tasks**: GET http://localhost:8000/tasks
- **#hermes tags**: GET http://localhost:8000/obsidian/hermes-tags

---

## User-Facing Features

### Telegram Commands
| Command | Purpose |
|---|---|
| `/run <PowerShell cmd>` | Execute command on Windows host |
| `/delegate <task>` | Delegate task directly to Claude Code |
| `/aprimorar <nota>` | Run improvement pipeline on a project note |
| `/projeto <nome>` | Show project note details (open items, activity) |
| `/projects` | List all project notes (#projeto) with status |
| `/reflect` | Vault analysis — insights, stale projects, suggestions |
| `/status` | Agent status panel (model, index, tasks, uptime) |
| `/activity [n]` | Show last N delegations and autonomous actions |
| `/memory` | View learned facts about user |
| `/remember <fact>` | Manually add a fact |
| `/notes <search>` | Search Obsidian vault (semantic + keyword fallback) |
| `/obsidian [name]` | List all notes or search |
| `/hermestags` | Show pending #hermes tags |
| `/tasks` | List scheduled reminders |
| `/cancel <id>` | Cancel a task |

### Chat Features
- **Web search**: "O que é machine learning?" → searches DuckDuckGo automatically
- **One-time tasks**: "Lembrete amanhã às 10h para fazer café"
- **Recurring tasks**: "Me lembrar todo dia às 8h de fazer exercício" (cron generation)
- **Obsidian append**: "Adicione chocolate à minha lista de compras"
- **Obsidian create**: "Crie uma nota de ideias para o projeto X"

### Obsidian Integration
Mark lines in notes with `#hermes` to trigger actions:
```markdown
- [ ] Comprar pão #hermes
- Me ligar amanhã 15h #hermes
#hermes criar nota de retrospectiva da semana
```
Hermes will process automatically every 60s, execute, and mark as `#hermes/done`.

---

## Future Directions (Hold/Backlog)

1. **WhatsApp via waha-rikin**: Branch `feat/waha-whatsapp` has full integration. Pending: user evaluation and credential setup.
2. **Larger models**: Currently using gemma2:2b for speed. Can upgrade to llama2, llama3, or others on the host.
3. **Vision**: Could add image upload support if a vision model is available on Ollama.
4. **Slack integration**: Parallel to Telegram.

---

## When Something Breaks

### Hermes won't start
```bash
docker logs hermes-agent
# Check: Ollama reachable? host.docker.internal resolves?
docker exec hermes-agent curl http://host.docker.internal:11434/api/tags
```

### Git pull/push fails
```bash
# Inside the container
docker exec hermes-agent bash
cd /obsidian && git status
# Check: SSH keys? Remote URL correct?
```

### Telegram bot not responding
```bash
docker logs hermes-bot
# Check: TELEGRAM_TOKEN in docker-compose.yml?
# Token expires after ~40 days; may need refresh via @BotFather
```

### #hermes tags not scanning
- Check HERMES_TAG_SCAN_INTERVAL in .env (default 60s)
- Ensure vault has write permissions in container
- Watch logs: `docker logs hermes-agent | grep hermes`

---

## Code Style & Patterns

- **No comments on WHAT**: Good variable names + function signatures are enough
- **Comments on WHY**: Hidden constraints, workarounds, non-obvious invariants
- **Imports organized**: stdlib, third-party, local modules
- **Error handling at boundaries**: Validate input, trust internal code
- **Logging**: Use `logging` module, not print()
- **Type hints**: Use them when they clarify intent
- **Regex extraction**: `extract_json()` pattern used throughout for LLM outputs

---

## Testing & Deployment

- **No automated tests yet**: Manual testing via Telegram preferred for now
- **Pre-deployment checks**:
  1. `docker logs hermes-agent` — no errors
  2. `docker logs hermes-bot` — no errors
  3. Send a test message to Hermes in Telegram
  4. Test `/obsidian` command
  5. Test `/run pwd` command
  6. Verify memory extraction: `/memory` after a few messages
  7. Check pending tasks: `/tasks`

---

## Useful Commands

```bash
# View all container info
docker compose ps
docker compose logs -f hermes-agent

# Rebuild from scratch
docker compose down && docker compose up -d --build

# Remove all stopped containers and dangling images
docker system prune

# Check if Ollama is reachable from container
docker exec hermes-agent curl http://host.docker.internal:11434/api/tags

# Full restart (keeps data)
docker compose restart

# See what's in persistent data
ls -la C:\Git\Hermes\data\

# Change Telegram token (never commit it!)
# 1. Get new token via @BotFather /newbot
# 2. Update .env
# 3. docker compose restart
```

---

## Contact & Support

- **Owner ID for Telegram commands**: 449989534 (guards /run, /memory, /tasks, etc.)
- **Config file**: `.env` (never commit)
- **Logs location**: Docker container logs (use `docker logs <container>`)
- **Obsidian repo**: Must be a valid git repo with remote for sync to work

---

## Escalada Gemma → Claude Code

O `gemma2:2b` é o cérebro para classificação de intents, conversas e operações simples no vault.

Escala automaticamente para Claude Code quando:
- Tarefa envolve escrever/refatorar código
- Scripts, automações, ou análise multi-arquivo
- Intent `delegate_claude` detectado no classificador
- Comando `/delegate` ou `/aprimorar` enviado manualmente

## Autonomous Jobs (APScheduler)

| Job | Cron | Função |
|---|---|---|
| Briefing matinal | `0 8 * * *` | Resume vault + tarefas do dia |
| Scanner de projetos | a cada 1h | Alerta sobre #projeto com itens parados |
| Backup semanal | `0 3 * * 0` | Snapshot de data/ em data/backups/ |
| #hermes tags | a cada 60s | Processa ações marcadas no vault |

**Last Updated**: 2026-06-11
