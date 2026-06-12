# Hermes Agent — Claude Code Guide

## Overview

Hermes é um **secretário virtual autônomo** rodando em Docker, conectado a:
- **Ollama** (host.docker.internal:11434) — multi-model routing (hermes3:3b, gemma2:2b, qwen2.5-coder:1.5b, nomic-embed-text)
- **Claude Code** via `host/host_agent.py` (delegação de tarefas técnicas complexas)
- **Telegram** para interface conversacional
- **Obsidian** vault em `C:\Git\Obsidian` (leitura/escrita/indexação semântica + git sync)
- **Windows host** via host_agent.py na porta 9000 (PowerShell + Claude CLI)
- **APScheduler** para agendamento de tarefas e jobs autônomos
- **Google Calendar** via OAuth2

---

## Project Structure

```
C:\Git\Hermes\
├── app/                     # Python application (→ /app/ no container)
│   ├── main.py              # FastAPI agent: chat, intent, jobs, endpoints
│   ├── bot.py               # Telegram bot: command handlers, feedback inline
│   ├── memory.py            # Fatos persistentes + perfil estruturado + system prompt
│   ├── obsidian.py          # Vault read/write + #hermes tag scanner + git sync
│   ├── vault_index.py       # Embeddings semânticos (nomic-embed-text) + busca cosine
│   ├── learner.py           # Aprendizado autônomo: comportamento, feedback, dedup semântico
│   ├── conversation_rag.py  # RAG de conversas: embede trocas, recupera contexto longo
│   ├── calendar_integration.py  # Google Calendar OAuth2: criar/listar eventos
│   ├── activity.py          # Log de delegações e ações autônomas
│   ├── scheduler.py         # APScheduler: cron + one-time + jobs internos
│   └── whatsapp.py          # WhatsApp (on hold — branch feat/waha-whatsapp)
├── host/                    # Windows host (não entra no container)
│   ├── host_agent.py        # HTTP server :9000 — PowerShell + Claude CLI
│   └── start_host_agent.bat # Auto-start wrapper (coloque no Startup do Windows)
├── data/                    # Dados persistentes (bind mount ./data:/app/data, gitignored)
│   ├── memory.json          # Fatos + perfil do usuário
│   ├── tasks.json           # Lembretes agendados
│   ├── history.json         # Histórico de conversa por user_id
│   ├── vault_index.json     # Índice semântico do vault
│   ├── activity_log.json    # Log de ações autônomas
│   ├── behavior.json        # Padrões de uso (learner)
│   ├── conversation_index.json  # RAG de conversas
│   ├── google_credentials.json  # OAuth2 credentials (não commitado)
│   ├── google_token.json    # OAuth2 token (não commitado)
│   └── backups/             # Snapshots semanais
├── Dockerfile               # python:3.12-slim; COPY app/ . (não COPY . .)
├── docker-compose.yml       # Dois services: hermes (agent :8000) + hermes-bot
├── requirements.txt         # Python dependencies
├── .env                     # Config (NEVER commit!)
├── .gitignore               # Exclui .env, data/, __pycache__
├── CLAUDE.md                # Este arquivo
└── README.md                # Documentação do usuário

C:\Git\Obsidian/      # Knowledge base do usuário (git-synced)
C:\Git\waha-rikin/    # WhatsApp WAHA client (feat/waha-whatsapp branch only)
```

---

## Key Design Decisions

### 1. **Branching Strategy**
- **master**: Código de produção. Sem WhatsApp.
- **feat/waha-whatsapp**: Integração WhatsApp via waha-rikin (on hold).

### 2. **Multi-Model Routing**

| Constante | Modelo padrão | Papel |
|---|---|---|
| `CHAT_MODEL` | `gemma2:2b` | Conversas em português |
| `INTENT_MODEL` | `hermes3:3b` | Classificação de intent (JSON via ChatML system message) |
| `REASONING_MODEL` | `hermes3:3b` | Análise, reflexão, briefing, extração de fatos |
| `CODE_MODEL` | `qwen2.5-coder:1.5b` | Contexto para pipeline /aprimorar |
| EMBED_MODEL (env) | `nomic-embed-text` | Embeddings para vault index + conversation RAG |

`_llm(model, messages, fallback=CHAT_MODEL, system=None)` — aceita `system=` para injetar system message no formato ChatML do hermes3. Fallback automático para `CHAT_MODEL` se o modelo não estiver disponível.

Todos os prompts para INTENT_MODEL e REASONING_MODEL são divididos em:
- `*_SYSTEM`: instrução fixa (schema JSON, regras) → passa como `system=`
- `*_PROMPT`: dados dinâmicos (mensagem, conversa, contexto) → passa como `user`

### 3. **Obsidian Sync Pattern**
- **Toda leitura**: `git pull --rebase --autostash` (rate-limited a 60s de cooldown)
- **Toda escrita**: `git add -A && git commit && git push`
- **Requisito**: `C:\Git\Obsidian` deve ser um repo git com remote e credenciais configuradas

### 4. **Persistent Memory**
- `memory.json`: fatos como objetos `{text, added_at, last_used_at, use_count, stale}`
- `tasks.json`: lembretes one-time e recorrentes
- `behavior.json`: padrões de uso (intents por hora/dia, notas acessadas, feedback)
- `conversation_index.json`: embeddings de conversas para RAG (cap 500 entradas)
- Todos montados via bind volume `./data:/app/data`
- Perfil sincronizado para `Hermes/Perfil.md` no vault após cada update

### 5. **Intent Classification**
Sem tool-use API (hermes3 via Ollama não expõe essa interface da mesma forma).
Classificação via LLM com `system=DECISION_SYSTEM` (schema JSON explícito) e extração por regex.

Actions disponíveis:
- `obsidian_append`, `obsidian_create`, `obsidian_read`, `obsidian_update`
- `schedule_once`, `schedule_recurring`
- `delegate_claude`
- `agent_plan` (multi-step)
- `calendar_create`, `calendar_list`
- `search`
- `answer`

### 6. **Sistema de Aprendizado (learner.py)**
- `record_behavior(intent, notes_used, action_status)` — chamado a cada `/chat`
- `record_feedback(context, rating, action)` — 👍/👎 via botões inline Telegram
- `record_correction(original, correction)` — detecta "na verdade", "não é isso", etc.
- `deduplicate_facts_semantic(facts)` — cosine > 0.92 → fato duplicado removido
- `classify_facts_into_profile(facts, llm_fn)` — categoriza em preferences/projects/people/current_context
- Dados em `behavior.json`

### 7. **Conversation RAG (conversation_rag.py)**
- `index_exchange(user_id, user_msg, assistant_msg)` — embede e persiste cada troca
- `retrieve(user_id, query, top_k=3, min_score=0.5)` — recupera trocas antigas relevantes
- Ignora as últimas 20 entradas (já na janela de contexto ativa)
- Cap de 500 entradas em `conversation_index.json`

### 8. **Google Calendar (calendar_integration.py)**
- OAuth2 via `google-auth`; token salvo em `google_token.json`
- `is_available()` — fallback gracioso se credentials não configurados
- Setup: baixar `credentials.json` do Google Console → salvar em `data/google_credentials.json`

### 9. **#hermes Tag Scanner**
- Thread background: varre vault a cada `HERMES_TAG_SCAN_INTERVAL` segundos
- Linhas com `#hermes` (sem `#hermes/done`) → processa via REASONING_MODEL → executa → marca done
- Notifica via Telegram

### 10. **Host Execution**
- `host/host_agent.py` roda standalone na porta 9000 do Windows host
- Seguro via `X-Agent-Secret` header (mude o padrão!)
- Spawna subprocessos PowerShell; substitui `claude ` pelo caminho completo de `claude.cmd`

### 11. **Corporate SSL Proxy**
- `verify=False` em httpx para Telegram Bot API e host agent
- `--trusted-host` no Dockerfile para pip
- Intencional — apenas em ambiente corporativo controlado

---

## Autonomous Jobs (APScheduler)

| Job | Cron | Função |
|---|---|---|
| Briefing matinal | `0 8 * * *` | Resume vault + tarefas do dia via REASONING_MODEL |
| Reflexão semanal | `0 22 * * 6` | Analisa últimos 7 dias, salva em profile, envia ao Telegram |
| Scanner de projetos | a cada 1h | Alerta sobre `#projeto` com itens parados > PROJECT_STALE_DAYS |
| Backup semanal | `0 3 * * 0` | Snapshot de `data/` em `data/backups/` |
| #hermes tags | a cada 60s | Processa ações marcadas no vault |

---

## Telegram Commands (Referência Completa)

| Comando | Função |
|---|---|
| `/check` | Verifica todos os circuitos (Ollama, vault, calendar, scheduler, host agent) |
| `/status` | Painel: modelos, índice, tarefas, uptime |
| `/memory` | Fatos aprendidos + perfil estruturado |
| `/remember <fato>` | Adiciona fato manualmente |
| `/notes <busca>` | Busca semântica no vault |
| `/obsidian [nome]` | Lista ou busca notas |
| `/hermestags` | Tags `#hermes` pendentes |
| `/reflect` | Análise do vault (timeout 600s) |
| `/projects` | Lista notas `#projeto` com status |
| `/projeto <nome>` | Detalhe de projeto (itens abertos, atividade) |
| `/aprimorar <nota>` | Pipeline de melhoria via Claude Code |
| `/delegate <tarefa>` | Delegação direta ao Claude Code |
| `/run <cmd>` | PowerShell no host Windows |
| `/activity [n]` | Últimas N ações autônomas |
| `/tasks` | Lembretes agendados |
| `/cancel <id>` | Cancela lembrete |
| `/agenda [days]` | Eventos Google Calendar |

---

## API Endpoints (FastAPI :8000)

| Endpoint | Método | Descrição |
|---|---|---|
| `/chat` | POST | Chat principal |
| `/health` | GET | Health check (usado pelo Docker healthcheck) |
| `/status` | GET | Status completo |
| `/memory` | GET | Fatos do usuário |
| `/memory/profile` | GET | Perfil estruturado |
| `/reflect` | GET | Reflexão do vault |
| `/activity` | GET | Log de atividades |
| `/feedback` | POST | Registrar 👍/👎 |
| `/behavior` | GET | Resumo de padrões de uso |
| `/projects` | GET | Lista projetos |
| `/projects/improve` | POST | Iniciar pipeline /aprimorar |
| `/projects/detail` | GET | Detalhe de projeto |
| `/calendar/events` | GET | Listar eventos |
| `/calendar/event` | POST | Criar evento |
| `/tasks` | GET | Lembretes |
| `/obsidian/hermes-tags` | GET | Tags pendentes |

---

## Development Workflow

### Alterar código Python
```bash
cd C:\Git\Hermes
# Edite arquivos em app/
docker compose up -d --build
docker logs hermes-agent
```

### Adicionar dependência Python
```bash
# Edite requirements.txt
docker compose up -d --build
```

### Trocar modelo Ollama
```bash
# No .env:
OLLAMA_MODEL=llama3.2
docker compose restart hermes
```

### Debug
```bash
docker logs hermes-agent -f
docker logs hermes-bot -f
curl http://localhost:8000/health
curl http://localhost:8000/status
```

### Testar via API
```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Olá Hermes!", "user_id": "test", "chat_id": "test"}'
```

---

## Security Notes

- **TELEGRAM_TOKEN**: token real em `.env` (NÃO commitar). Revogar via @BotFather se exposto.
- **HOST_AGENT_SECRET**: mude `hermes-secret-mude-isso` antes de produção.
- **google_credentials.json** e **google_token.json**: ficam em `data/` (gitignored).
- **SSL disabled**: `verify=False` para Telegram + host agent — intencional em ambiente corporativo.
- `.env` está no `.gitignore` — nunca force-add.

---

## Code Style & Patterns

- **Sem comentários óbvios**: nomes de variáveis e funções são suficientes
- **Comentários no WHY**: constraints ocultas, workarounds, invariantes
- **`extract_json(text)`**: extrai o primeiro objeto JSON via regex — usado em todos os outputs LLM
- **`_llm(model, messages, fallback, system=)`**: wrapper central com fallback e strip de `<think>` blocks
- **`_with_retry(fn, *args, attempts, backoff)`**: retry com backoff exponencial
- **Logging**: `logging` module, não `print()`
- **Type hints**: quando clarificam a intenção
- **Threads**: operações de I/O (vault, LLM, Telegram) em `threading.Thread(daemon=True)`

---

## When Something Breaks

### Agent não inicia
```bash
docker logs hermes-agent
docker exec hermes-agent curl http://host.docker.internal:11434/api/tags
```

### Git pull/push falha
```bash
docker exec hermes-agent bash
cd /obsidian && git status
```

### Bot não responde
```bash
docker logs hermes-bot
# Token expirado? → @BotFather /revoke
```

### /run não funciona
```bash
# No host Windows:
netstat -ano | findstr 9000
# Se não estiver rodando:
# Execute host\start_host_agent.bat
```

### Modelos não disponíveis
```bash
# No host Windows:
ollama pull hermes3:3b
ollama pull nomic-embed-text
```

---

**Last Updated**: 2026-06-12
