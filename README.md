# Hermes — Personal AI Assistant

Hermes é um assistente pessoal inteligente que roda localmente via Docker, aprende sobre você a cada conversa, gerencia tarefas e integra-se ao seu fluxo de trabalho pelo Telegram, Obsidian e Google Calendar.

## Features

✨ **Conversação Natural** — Fale em português; Hermes responde com contexto do seu histórico e notas pessoais

🔍 **Busca Web** — Pesquisa automaticamente quando precisa de informações atualizadas

📝 **Obsidian Integration** — Lê, cria e edita notas; sincroniza via git em cada acesso

🏷️ **#hermes Tags** — Marque linhas nas suas notas com `#hermes`; Hermes processa e executa automaticamente

⏰ **Agendamento** — Lembretes pontuais e recorrentes em linguagem natural

🧠 **Aprendizado Autônomo** — Aprende fatos e preferências a cada conversa; perfil estruturado com categorias (preferências, projetos, pessoas, contexto)

🔄 **Memória Longa (RAG)** — Embeddings de conversas antigas para lembrar contexto além das últimas mensagens

🧩 **Modo Agente Multi-Step** — Executa tarefas compostas: "pesquise X, crie uma nota e agende uma revisão"

📅 **Google Calendar** — Cria e lista eventos diretamente via chat

🔁 **Reflexão Semanal** — Toda semana analisa o próprio desempenho e envia insights

⚡ **Execução de Comandos** — Rode PowerShell no Windows host via `/run`

---

## Arquitetura de Modelos

| Papel | Modelo | Função |
|---|---|---|
| CHAT_MODEL | `gemma2:2b` | Conversas em português |
| INTENT_MODEL | `hermes3:3b` | Classificação de intenção (JSON estruturado) |
| REASONING_MODEL | `hermes3:3b` | Análise, reflexão, briefing, extração de fatos |
| CODE_MODEL | `qwen2.5-coder:1.5b` | Contexto para pipeline `/aprimorar` |
| EMBED_MODEL | `nomic-embed-text` | Embeddings (vault index + conversation RAG) |

---

## Instalação Rápida

### Pré-requisitos
- Windows 11 com Docker Desktop instalado
- Ollama rodando localmente (`localhost:11434`)
- Modelos baixados no Ollama:
  ```bash
  ollama pull gemma2:2b
  ollama pull hermes3:3b
  ollama pull qwen2.5-coder:1.5b
  ollama pull nomic-embed-text
  ```
- Git instalado no Windows
- Token do Telegram Bot (via @BotFather)
- Obsidian vault em `C:\Git\Obsidian` (opcional, mas recomendado)

### Setup

1. **Clone ou tenha o projeto em `C:\Git\Hermes`**

2. **Configure o `.env`** (na raiz do projeto):
   ```env
   TELEGRAM_TOKEN=seu_token_aqui
   TELEGRAM_OWNER_ID=seu_id_aqui
   HOST_AGENT_SECRET=sua_senha_secreta
   ```

   **Como obter:**
   - `TELEGRAM_TOKEN`: @BotFather → `/newbot`
   - `TELEGRAM_OWNER_ID`: @userinfobot → ele exibe seu ID
   - `HOST_AGENT_SECRET`: qualquer string segura (mude o padrão antes de produção)

3. **Inicie o host agent no Windows** (para `/run` e `/delegate` funcionarem):
   - Execute `host\start_host_agent.bat` (ou coloque na pasta Startup do Windows)

4. **Inicie os containers**:
   ```bash
   docker compose up -d
   ```

5. **Verifique se está rodando**:
   ```bash
   docker logs hermes-agent
   docker logs hermes-bot
   ```

6. **Fale com o Hermes no Telegram** — envie `/check` para verificar todos os circuitos

---

## Comandos Telegram

### Diagnóstico e Status
| Comando | O que faz |
|---|---|
| `/check` | Verifica todos os circuitos: Ollama (todos os modelos), vault, calendar, scheduler, host agent |
| `/status` | Painel de status: modelos ativos, tamanho do índice, tarefas agendadas, uptime |

### Memória e Aprendizado
| Comando | Exemplo | O que faz |
|---|---|---|
| `/memory` | `/memory` | Mostra fatos aprendidos + perfil estruturado (preferências, projetos, pessoas) |
| `/remember` | `/remember Sou dev Python` | Adiciona fato manualmente |

### Vault Obsidian
| Comando | Exemplo | O que faz |
|---|---|---|
| `/notes` | `/notes projeto` | Busca semântica no vault |
| `/obsidian` | `/obsidian` | Lista todas as notas |
| `/hermestags` | `/hermestags` | Mostra `#hermes` tags pendentes de processamento |
| `/reflect` | `/reflect` | Análise do vault: insights, projetos parados, sugestões de ação |

### Projetos
| Comando | Exemplo | O que faz |
|---|---|---|
| `/projects` | `/projects` | Lista notas `#projeto` com status (itens abertos, dias parado) |
| `/projeto` | `/projeto Hermes` | Detalhe de uma nota de projeto |
| `/aprimorar` | `/aprimorar Hermes` | Pipeline de melhoria iterativa via Claude Code |

### Delegação
| Comando | Exemplo | O que faz |
|---|---|---|
| `/delegate` | `/delegate Refatora main.py` | Delega tarefa diretamente ao Claude Code |
| `/run` | `/run Get-Date` | Executa PowerShell no host Windows |
| `/activity` | `/activity 10` | Últimas N delegações e ações autônomas |

### Agendamento e Calendário
| Comando | Exemplo | O que faz |
|---|---|---|
| `/tasks` | `/tasks` | Lista lembretes agendados |
| `/cancel` | `/cancel abc123` | Cancela um lembrete |
| `/agenda` | `/agenda 7` | Lista próximos eventos do Google Calendar (7 dias) |

---

## Conversação Natural

```
Você: Qual é a capital da França?
Hermes: Paris é a capital da França...

Você: Faça um resumo sobre inteligência artificial
Hermes: [busca na web + responde com contexto]

Você: Adicione leite à minha lista de compras
Hermes: ✅ Adicionado à nota Lista de Compras: - leite

Você: Pesquise sobre RAG, crie uma nota e agende revisão para sexta
Hermes: 🧩 Executando plano em 3 etapas...
        ✅ Pesquisa concluída
        ✅ Nota "RAG - Pesquisa" criada
        ✅ Lembrete agendado para sexta às 10h
```

---

## Agendamento em Linguagem Natural

```
"Me lembrar amanhã às 10h para fazer o café"
→ schedule_once: amanhã 10:00

"Lembrete no próximo domingo 14h para ligar para João"
→ schedule_once: domingo 14:00

"Todos os dias às 8h, lembrar de fazer exercício"
→ cron: 0 8 * * *

"De segunda a sexta às 9h, standup"
→ cron: 0 9 * * 1-5
```

---

## Sistema de Aprendizado

### Como Hermes aprende sobre você

**A cada 4 mensagens** — extrai fatos duradouros e classifica automaticamente em:
- `preferences`: gostos, hábitos, preferências
- `projects`: projetos em andamento ou planejados
- `people`: pessoas mencionadas (família, colegas, amigos)
- `current_context`: emprego, localização, fase de vida

**Deduplicação semântica** — fatos parecidos são unificados via embeddings (threshold cosine > 0.92).

**Fatos stale** — fatos não usados há 30 dias são marcados como desatualizados.

**Feedback inline** — botões 👍/👎 aparecem nas respostas de `/reflect` e delegações:
- 👍 → registra como abordagem útil
- 👎 → registra como insatisfatório para ajuste futuro

**Reflexão semanal** — todo sábado às 22h, Hermes analisa as últimas atividades, identifica padrões e envia um resumo com sugestões.

**Memória longa (RAG)** — cada conversa é embedada e indexada; conversas antigas relevantes são recuperadas automaticamente como contexto.

---

## Obsidian Integration

### Sync automático
```
Você escreve em qualquer device → git push
Hermes lê → git pull automático → encontra as mudanças
Hermes modifica → git push automático
Você vê em outro device → git pull (manual ou via Obsidian Git plugin)
```

**Pré-requisito**: `C:\Git\Obsidian` deve ser um repositório git com remote configurado.

### #hermes Tags

Marque linhas com `#hermes` para processamento automático (scan a cada 60s):

```markdown
- [ ] Comprar pão #hermes
- Me ligar amanhã 15h #hermes
#hermes Crie nota de retrospectiva da semana

## Projeto X
- [ ] Refatorar módulo auth #hermes
```

Hermes processa, executa (append, create, schedule) e marca como `#hermes/done`.

### Notas de Projeto

Adicione `#projeto` a uma nota para que Hermes a rastreie:
```markdown
# Projeto Hermes #projeto

## Itens em Aberto
- [ ] Implementar suporte a voz
- [ ] Integrar com Slack

## Concluídos
- [x] Sistema de aprendizado autônomo
```

Use `/projects` para ver status de todos os projetos e `/aprimorar <nota>` para delegar os itens em aberto ao Claude Code.

---

## Google Calendar

### Setup (uma vez)

1. Acesse [console.cloud.google.com](https://console.cloud.google.com)
2. APIs & Services → Credentials → Create OAuth 2.0 Client (Desktop app)
3. Baixe como `credentials.json` → copie para `C:\Git\Hermes\data\google_credentials.json`
4. Na primeira chamada, Hermes solicitará autorização via URL (console)

### Uso

```
Você: Agende reunião de projeto amanhã às 14h
Hermes: ✅ Evento criado: "Reunião de projeto" — amanhã 14:00

/agenda 7
Hermes: 📅 Próximos 7 dias:
        • amanhã 14:00 — Reunião de projeto
        • sexta 10:00 — Revisão semanal
```

---

## Variáveis de Ambiente

| Variável | Padrão | Descrição |
|---|---|---|
| `TELEGRAM_TOKEN` | — | Token do bot (obrigatório) |
| `TELEGRAM_OWNER_ID` | — | Seu ID do Telegram (obrigatório) |
| `OLLAMA_MODEL` | `gemma2:2b` | Modelo de chat conversacional |
| `INTENT_MODEL` | `hermes3:3b` | Modelo de classificação de intenção |
| `REASONING_MODEL` | `hermes3:3b` | Modelo de raciocínio e análise |
| `CODE_MODEL` | `qwen2.5-coder:1.5b` | Modelo para contexto de código |
| `EMBED_MODEL` | `nomic-embed-text` | Modelo de embeddings |
| `EMBED_SIMILARITY_THRESHOLD` | `0.3` | Threshold de similaridade (dinâmico) |
| `HERMES_TAG_SCAN_INTERVAL` | `60` | Intervalo de scan de #hermes tags (segundos) |
| `MORNING_BRIEFING_CRON` | `0 8 * * *` | Cron do briefing matinal |
| `WEEKLY_REFLECTION_CRON` | `0 22 * * 6` | Cron da reflexão semanal (sáb 22h) |
| `BACKUP_CRON` | `0 3 * * 0` | Cron do backup semanal (dom 3h) |
| `PROJECT_SCAN_INTERVAL` | `3600` | Intervalo de scan de projetos (segundos) |
| `PROJECT_STALE_DAYS` | `7` | Dias sem atividade para alerta de projeto parado |
| `MAX_IMPROVE_ITERATIONS` | `2` | Iterações máximas do pipeline /aprimorar |
| `AGENT_MAX_STEPS` | `5` | Passos máximos no modo agente |
| `FACT_STALE_DAYS` | `30` | Dias sem uso para marcar fato como stale |
| `AUTO_SUMMARIZE_THRESHOLD` | `1000` | Chars mínimos para auto-resumo de nota |
| `RETRY_ATTEMPTS` | `3` | Tentativas de retry em chamadas LLM |
| `GOOGLE_CALENDAR_ID` | `primary` | ID do calendário Google |
| `HOST_AGENT_SECRET` | — | Segredo do host agent (mude em produção!) |
| `HOST_AGENT_URL` | `http://host.docker.internal:9000/exec` | URL do host agent |

---

## Arquitetura

```
┌─────────────────────────────────────────────────────────────┐
│  Telegram (@seu_hermes_bot)                                 │
└──────────────────────┬──────────────────────────────────────┘
                       │ python-telegram-bot (SSL disabled)
                       ▼
┌─────────────────────────────────────────────────────────────┐
│  Docker: hermes-bot (bot.py)                                │
│  • Command handlers (/check, /reflect, /agenda, ...)        │
│  • Feedback inline 👍/👎                                    │
└──────────────────────┬──────────────────────────────────────┘
                       │ HTTP POST /chat
                       ▼
┌─────────────────────────────────────────────────────────────┐
│  Docker: hermes-agent (main.py — FastAPI :8000)             │
│                                                             │
│  Intent Classification ──► hermes3:3b (INTENT_MODEL)        │
│  Chat / Answer        ──► gemma2:2b   (CHAT_MODEL)          │
│  Reasoning / Reflect  ──► hermes3:3b  (REASONING_MODEL)     │
│  Code Context         ──► qwen2.5-coder (CODE_MODEL)        │
│                                                             │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────────┐    │
│  │ memory.py   │  │ learner.py   │  │ conversation_   │    │
│  │ facts+      │  │ behavior +   │  │ rag.py          │    │
│  │ profile     │  │ feedback +   │  │ embeddings +    │    │
│  └─────────────┘  │ corrections  │  │ long-term mem   │    │
│                   └──────────────┘  └─────────────────┘    │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────────┐    │
│  │ obsidian.py │  │vault_index   │  │calendar_        │    │
│  │ read/write  │  │.py semantic  │  │integration.py   │    │
│  │ + git sync  │  │index + RAG   │  │Google Calendar  │    │
│  └─────────────┘  └──────────────┘  └─────────────────┘    │
│  ┌─────────────┐  ┌──────────────┐                         │
│  │scheduler.py │  │ activity.py  │                         │
│  │ cron + jobs │  │ audit log    │                         │
│  └─────────────┘  └──────────────┘                         │
└──────┬─────────────────────────────────┬────────────────────┘
       │                                 │
       ▼                                 ▼
┌──────────────────┐           ┌─────────────────────────┐
│  Ollama          │           │  host/host_agent.py      │
│  host:11434      │           │  Windows port 9000       │
│  gemma2, hermes3 │           │  PowerShell + Claude CLI │
│  nomic-embed-text│           └─────────────────────────┘
└──────────────────┘
       │
       ▼
┌──────────────────┐
│  Obsidian Vault  │
│  C:\Git\Obsidian │ ◄── git pull/push automático
└──────────────────┘
```

---

## Troubleshooting

### Diagnóstico rápido
```
/check
```
Verifica: hermes_api, ollama (todos os modelos), vault_index, obsidian_vault, host_agent, scheduler, google_calendar.

### Hermes não responde
```bash
docker ps | grep hermes
docker logs hermes-agent
docker logs hermes-bot
docker exec hermes-agent curl http://host.docker.internal:11434/api/tags
```

### Obsidian não sincroniza
```bash
# No host Windows:
cd C:\Git\Obsidian
git status
git remote -v
git pull   # teste manual
```

### /run e /delegate não funcionam
- Verifique que `host\host_agent.py` está rodando: `netstat -ano | findstr 9000`
- Execute `host\start_host_agent.bat` manualmente

### Telegram token expirado
- Tokens expiram após ~40 dias de inatividade
- @BotFather → `/revoke` → gere novo token → atualize `.env` → `docker compose restart`

### Modelos não disponíveis
```bash
# Verifique quais modelos estão no Ollama:
docker exec hermes-agent curl http://host.docker.internal:11434/api/tags
# Baixe o que falta:
ollama pull hermes3:3b
```

---

## Segurança

- **NÃO COMMIT `.env`** — contém seu token do Telegram. O `.gitignore` já exclui, mas nunca force-add.
- Se expor o token: @BotFather → `/revoke` imediatamente.
- `HOST_AGENT_SECRET` dá acesso ao PowerShell do host — use uma string longa e aleatória em produção.
- SSL verification desabilitado (`verify=False`) para Telegram e host agent — intencional em ambiente corporativo controlado.

---

## Customização

```bash
# Trocar modelo de chat
OLLAMA_MODEL=llama3.2  # no .env
docker compose restart hermes

# Aumentar frequência de scan de tags
HERMES_TAG_SCAN_INTERVAL=30  # no .env
docker compose restart

# Adicionar dependência Python
# Edite requirements.txt, depois:
docker compose up -d --build
```

---

## Roadmap

- [ ] WhatsApp via waha-rikin (branch `feat/waha-whatsapp`)
- [ ] Suporte a modelos maiores (llama3, mistral)
- [ ] Vision (upload de imagens)
- [ ] Integração com Slack

---

**Status**: ✅ Estável e funcional  
**Última atualização**: 2026-06-12  
**Detalhes técnicos**: veja `CLAUDE.md`
