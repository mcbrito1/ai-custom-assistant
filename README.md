# Hermes — Personal AI Assistant

Hermes é um assistente pessoal inteligente que roda localmente via Docker, aprendendo sobre você, gerenciando suas tarefas e integrando-se ao seu fluxo de trabalho através do Telegram e do seu vault Obsidian.

## Features

✨ **Conversação Natural** — Fale em português, Hermes responde com contexto de sua história e notas pessoais

🔍 **Busca Web** — Faz pesquisas automaticamente quando precisa de informações atualizadas

📝 **Obsidian Integration** — Lê e escreve em suas notas pessoais; sincroniza automaticamente via git

🏷️ **#hermes Tags** — Marque tarefas nas suas notas com `#hermes` e deixe o Hermes executar

⏰ **Agendamento** — Agende lembretes em linguagem natural: "Lembrete segunda às 9h para ligar João"

🧠 **Memória Persistente** — Aprende fatos sobre você a cada conversa

⚡ **Execução de Comandos** — Rode PowerShell commands no Windows via `/run`

---

## Instalação Rápida

### Pré-requisitos
- Windows 11 com Docker Desktop instalado
- Ollama rodando localmente (em `localhost:11434`)
- Git instalado no Windows
- Token do Telegram Bot (@BotFather)
- Obsidian vault em `C:\Git\Obsidian` (opcional, mas recomendado)

### Setup

1. **Clone ou tenha o projeto em `C:\Git\Hermes`**

2. **Configure o .env**
   ```bash
   cp .env.example .env  # ou crie manualmente
   ```
   Edite `C:\Git\Hermes\.env`:
   ```env
   OLLAMA_MODEL=gemma2:2b
   TELEGRAM_TOKEN=seu_token_aqui
   TELEGRAM_OWNER_ID=seu_id_aqui
   HOST_AGENT_SECRET=sua_senha_secreta
   ```

   **Como obter:**
   - `TELEGRAM_TOKEN`: Converse com @BotFather no Telegram, use `/newbot`
   - `TELEGRAM_OWNER_ID`: Converse com @userinfobot, ele vai te dar seu ID
   - `HOST_AGENT_SECRET`: Qualquer string segura que você queira; mudar depois é fácil

3. **Inicie os containers**
   ```bash
   docker compose up -d
   ```

4. **Verifique se está rodando**
   ```bash
   docker logs hermes-agent     # Agent FastAPI
   docker logs hermes-bot       # Telegram bot
   ```

5. **Fale com o Hermes no Telegram**
   - Busque o bot pelo nome configurado (@BotFather vai dar o username)
   - Envie uma mensagem simples: "Oi Hermes!"

---

## Usando Hermes

### Via Telegram

#### Mensagens normais
```
Você: Qual é a capital da França?
Hermes: Paris é a capital da França...

Você: Faça um resumo sobre inteligência artificial
Hermes: [busca na web + responde com contexto]

Você: Adicione leite à minha lista de compras
Hermes: ✅ Adicionado à nota Lista de Compras: - leite
```

#### Comandos
| Comando | Exemplo | O que faz |
|---|---|---|
| `/run` | `/run Get-Date` | Executa comando PowerShell no host |
| `/memory` | `/memory` | Mostra o que Hermes aprendeu sobre você |
| `/remember` | `/remember Sou um desenvolvedor Python` | Ensina um fato novo |
| `/notes` | `/notes projeto` | Busca notas no Obsidian |
| `/obsidian` | `/obsidian` | Lista todas as notas |
| `/hermestags` | `/hermestags` | Mostra `#hermes` tags pendentes |
| `/tasks` | `/tasks` | Lista lembretes agendados |
| `/cancel` | `/cancel abc123` | Cancela um lembrete |

### Via Obsidian

Marque linhas com `#hermes` para que Hermes as processe automaticamente:

```markdown
## Lista de Compras
- [ ] Pão #hermes
- [ ] Leite #hermes
- [ ] Ovos

## Tarefas
- Estudar machine learning #hermes
- Responder email do João amanhã 15h #hermes

## Ideias
#hermes Crie uma nota chamada "Projeto Novo" com template
```

O Hermes verificará a cada minuto, processará, executará (append, create, schedule), e marcará como `#hermes/done`.

### Via Chat Direto (REST API)

```bash
# POST ao endpoint /chat
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Qual é a previsão do tempo?",
    "user_id": "user123",
    "chat_id": "user123"
  }'
```

---

## Agendamento em Linguagem Natural

Hermes entende datas/horários em português:

```
"Me lembrar amanhã às 10h para fazer o café"
→ Agenda para amanhã 10:00

"Lembrete no próximo domingo 14h para ligar para João"
→ Agenda para domingo 14:00

"Todos os dias às 8h, lembrar de fazer exercício"
→ Cron: 0 8 * * *

"De segunda a sexta às 9h, standup"
→ Cron: 0 9 * * 1-5

"A cada 2 horas, revisar inbox"
→ Cron: 0 */2 * * *
```

---

## Sincronização do Obsidian

Hermes **puxa** atualizações do seu vault Obsidian antes de ler, e **empurra** depois de escrever:

```
Você escreve em um device → git push
Hermes lê em outro device → git pull (automático) → encontra as mudanças
Hermes modifica → git push (automático)
Você vê em outro device → git pull (manual ou automático via Obsidian Git plugin)
```

**Pré-requisito**: `C:\Git\Obsidian` deve ser um repositório git com remote configurado (GitHub, GitLab, etc.). Hermes usará as credenciais do host (SSH key ou credential helper).

---

## Exemplos de Uso

### Exemplo 1: Adicionar à Lista de Compras
```
Você: Adicione pão integral à minha lista de compras
Hermes: ✅ Adicionado à nota Lista de Compras: - pão integral

[Nos bastidores]
- Procura nota com "compra" no nome
- Faz git pull
- Append "- pão integral"
- git commit + push
```

### Exemplo 2: Agendar com #hermes tag
Você escreve no Obsidian:
```
- Estudar ML #hermes segunda 14h
```

Hermes detecta, interpreta como "agendar para segunda às 14h com mensagem 'Estudar ML'", cria o lembrete, marca como `#hermes/done`:
```
- Estudar ML #hermes/done segunda 14h
```

### Exemplo 3: Aprendizado Progressivo
```
Conversa 1:
Você: Trabalho como engenheiro de software em Python
Hermes: Entendi!

Conversa 5:
Você: Como faço pra melhorar meu código?
Hermes: Baseado em sua experiência com Python, recomendo...
[Consulta seu vault sobre Python + código limpo]
```

---

## Troubleshooting

### Hermes não responde
```bash
# Verifique se está rodando
docker ps | grep hermes

# Veja os logs
docker logs hermes-agent
docker logs hermes-bot

# Verifique Ollama
docker exec hermes-agent curl http://host.docker.internal:11434/api/tags
```

### Obsidian não sincroniza
- Verifique se `C:\Git\Obsidian` é um repositório git: `git status`
- Confirme que tem remote: `git remote -v`
- Teste: `git pull` manualmente
- Verif credenciais: SSH key carregada? Credential helper configurado?

### Telegram token expirou
- Tempo de vida dos tokens: ~40 dias de inatividade
- Se parou de responder: converse com @BotFather, use `/newbot` para gerar novo
- Atualize `.env` e `docker compose restart`

### PowerShell commands não funcionam
- Verifique que `host_agent.py` está rodando no host
- Check: `netstat -ano | findstr 9000` (Windows)
- Restart: execute `start_host_agent.bat` manualmente

---

## Customização

### Trocar o modelo Ollama
```bash
# Edit .env:
OLLAMA_MODEL=llama2  # ou mistral, neural-chat, etc.
docker compose restart hermes
```

### Mudar intervalo de scan de #hermes tags
```bash
# Edit .env:
HERMES_TAG_SCAN_INTERVAL=30  # escanear a cada 30s
docker compose restart
```

### Adicionar nova dependência Python
```bash
# Edit requirements.txt
docker compose up -d --build
```

---

## Segurança

⚠️ **NÃO COMMIT**: Nunca faça git add do `.env` — contém seu token do Telegram

- `.gitignore` já exclui `.env`
- Se expuser o token acidentalmente: converse com @BotFather e use `/revoke`
- `HOST_AGENT_SECRET` tem acesso a PowerShell — mude para algo seguro antes de ir pra produção

---

## Arquitetura

```
┌─────────────────────────────────────────────────────────┐
│ Telegram Bot (@seu_hermes_bot)                          │
└──────────────────┬──────────────────────────────────────┘
                   │ (Python Telegram Bot Library)
                   │ SSL disabled (corporate proxy)
                   ▼
┌─────────────────────────────────────────────────────────┐
│ Docker Container: hermes-agent (FastAPI)                │
│  ├─ main.py: Chat endpoint, intent classification       │
│  ├─ obsidian.py: Read/write vault + git sync            │
│  ├─ memory.py: Persistent facts                         │
│  ├─ scheduler.py: APScheduler (cron + one-time)         │
│  └─ Ollama client → host.docker.internal:11434          │
└────┬────────────────────────┬────────────────────────┬──┘
     │ (hermes-bot talks here)│                        │
     │                        │ (Windows host)         │
     │                        │                        │
┌────▼─────────────────┐   ┌─▼──────────────────────┐ │
│ docker-compose: bot  │   │ host_agent.py (port 9k)│ │
│ (polling Telegram)   │   │ PowerShell execution    │ │
└──────────────────────┘   └────────────────────────┘ │
                                                       │
                           ┌───────────────────────────┘
                           │
                           ▼
                    ┌──────────────────┐
                    │  Obsidian Vault  │
                    │ C:\Git\Obsidian  │ ◄── git pull/push
                    │  (read/write)    │
                    └──────────────────┘
```

---

## Roadmap

- [ ] WhatsApp via waha-rikin (branch `feat/waha-whatsapp`)
- [ ] Suporte a modelos maiores (llama3, mistral)
- [ ] Vision capabilities (upload de imagens)
- [ ] Integração com Slack
- [ ] Dashboard web

---

## Feedback & Issues

- Encontrou um bug? Abra uma issue
- Tem uma ideia? Abra uma discussion
- Quer contribuir? Faça um fork e PR

---

**Status**: ✅ Estável e funcional  
**Última atualização**: 2026-06-10  
**Suporte**: Veja `CLAUDE.md` para detalhes técnicos
