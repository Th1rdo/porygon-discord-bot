# Porygon 🤖

Bot de Discord para as campanhas de TTRPG — dados, missões, countdowns e agendamento de sessões com confirmação de presença.

- **Código:** um único ficheiro, [`bot.py`](bot.py)
- **Prefixo:** `!` (a maioria dos comandos também existe como slash command `/`)
- **Hosting:** Railway (deploy automático a cada push para `main`)

> Este README é documentação viva — quando adicionares/alterares funcionalidades, atualiza-o no mesmo commit.

---

## Índice

1. [Funcionalidades](#funcionalidades)
   - [🎲 Roll (dados)](#-roll-dados)
   - [🎬 Mission (media)](#-mission-media)
   - [⏳ Countdown](#-countdown)
   - [🗓️ Sessões (agendamento + confirmações)](#️-sessões-agendamento--confirmações)
   - [⚙️ Config por servidor](#️-config-por-servidor)
   - [🌐 Webhook HTTP](#-webhook-http)
2. [Persistência de dados](#persistência-de-dados)
3. [Hosting e deploy (Railway)](#hosting-e-deploy-railway)
4. [Desenvolvimento local](#desenvolvimento-local)

---

## Funcionalidades

### 🎲 Roll (dados)

Rola dados no formato `NdS [+/- mod]`.

| Comando | Exemplo | Descrição |
|---|---|---|
| `!roll <expr>` | `!roll 6d6 + 2` | Rola dados (máx. 500 dados, 1000 lados) |
| `/roll <expr>` | `/roll 3d20` | Versão slash |

Resposta: `` `[6d6 + 2]` Rolagem: `[3, 5, 1, 6, 2, 4]` Resultado: 23 ``.
Mensagens acima do limite do Discord são divididas automaticamente.

### 🎬 Mission (media)

Envia ficheiros de `media/` pelo ID.

| Comando | Exemplo | Descrição |
|---|---|---|
| `!mission <id>` / `/mission <id>` | `!mission 001` | Envia `media/mission001.mp4` (aceita `1`, `01`, `001`; prefere `.mp4` se houver vários) |

Para adicionar uma missão: mete o ficheiro em `media/` com o nome `missionXXX.<ext>` e faz commit.

### ⏳ Countdown

Temporizador que **edita a mensagem a cada segundo** até chegar a zero. **Um por servidor** — iniciar um novo substitui o ativo. O tempo restante é recalculado pelo relógio real, por isso não atrasa mesmo que o Discord demore a aceitar edições.

| Comando | Descrição |
|---|---|
| `!countdown` | Mostra a ajuda (handout) |
| `!countdown <tempo> [legenda]` | Inicia (ex.: `!countdown 1h30m Início do evento`) |
| `!countdown stop` / `!countdownstop` | Para o countdown ativo |
| `/countdown <tempo> [legenda]` / `/countdown_stop` | Versões slash |

Formatos de tempo: `90` (segundos), `90s`, `5m`, `1h30m`, `mm:ss`, `hh:mm:ss`. Máximo 24h.

> ⚠️ O countdown vive em memória — um redeploy/restart a meio mata-o (a mensagem fica parada).

### 🗓️ Sessões (agendamento + confirmações)

O sistema para resolver o problema de "malta que não aparece". Todos os comandos de sessão são **só para admins/gestores** (permissão *Manage Server*) e só slash commands. Uma sessão ativa por servidor.

#### Setup (uma vez)

| Comando | Descrição |
|---|---|
| `/session_setup [role] [timezone] [remind_hours_before]` | Role a pingar (ex.: @Sobreviventes), fuso horário (default `Europe/Lisbon`), horas antes para o lembrete (default 24h) |
| `/session_player <player> [personal_channel]` | Regista um jogador e o seu chat pessoal (para os nudges) |
| `/session_player_remove <player>` | Remove um jogador |
| `/session_players` | Lista os jogadores registados |

#### Criar uma sessão

Dois estilos, conforme a fase:

**`/session_propose`** — data ainda em cima da mesa, pede compromisso:

```
/session_propose title:Ferro Podre when:2026-09-05 21:00 deadline:2026-09-01 20:00
```

Publica a mensagem de proposta ("Respondam com ✅ se conseguem comprometer-se…"), já com as reações ✅/❌ adicionadas. Se não deres `deadline`, fica **2 dias antes** da sessão.

**`/session_announce`** — data já decidida, mensagem de hype:

```
/session_announce title:Ferro Podre chapter:Sessão 08, Capítulo II subtitle:Hora de começar a pensar em um novo plano. when:2026-09-05 21:00
```

Produz o formato clássico:

> ## "Ferro Podre" - Sessão 08, Capítulo II
> > ***Hora de começar a pensar em um novo plano.***
> > ### 5 de setembro de 2026 21:00
> > daqui a 8 dias
>
> @Sobreviventes

Formatos aceites no `when`/`deadline`: `AAAA-MM-DD HH:MM` (no fuso do servidor), `DD/MM/AAAA HH:MM`, timestamp Unix, ou um `<t:...:F>` colado do Hammertime.

#### O que acontece automaticamente

Um loop corre a cada 30s e dispara, por ordem:

| Momento | Ação |
|---|---|
| **Deadline** (só `propose`) | Publica o resumo no canal (✅ confirmados / ❌ não podem / ❓ sem resposta) **e manda nudge no chat pessoal de cada jogador sem resposta**, com link direto para reagir |
| **X horas antes** (default 24h) | Lembrete no canal com a lista de confirmados; quem ainda não respondeu é mencionado com "deem sinal!" |
| **À hora da sessão** | "🎲 É HOJE!" com ping |
| **3h depois** | Mensagem de fecho e a sessão ativa é limpa (podes marcar a próxima) |

As confirmações são lidas **das reações da mensagem original** (✅/❌), comparadas com os jogadores registados. Se alguém reagir com ambas, o ✅ ganha.

#### Gerir

| Comando | Descrição |
|---|---|
| `/session_status` | Vê quem confirmou/não confirmou (só tu vês) |
| `/session_cancel` | Cancela/limpa a sessão ativa |

### ⚙️ Config por servidor

Liga/desliga funções por servidor — útil quando outro bot usa o mesmo prefixo `!`. Só admins/gestores.

| Comando | Descrição |
|---|---|
| `!config` / `/config` | Mostra o estado das funções e a ajuda |
| `!config disable <função>` | Desliga (`roll`, `mission` ou `countdown`) |
| `!config enable <função>` | Volta a ligar |

Com uma função desligada: o comando `!` fica **em silêncio total** (o outro bot pode responder); o comando `/` responde só a ti a dizer que está desativada.

### 📬 Manual

| Comando | Descrição |
|---|---|
| `!manual` / `/manual` | Envia este README por DM, dividido em mensagens (reage 📬 à tua mensagem quando envia) |

O manual é lido do `README.md` em runtime — atualizar este ficheiro atualiza o que o comando envia, sem mexer no código.

### 🌐 Webhook HTTP

Servidor aiohttp na porta `PORT` (default 8080) para disparar rolls de fora do Discord.

| Endpoint | Body (JSON) |
|---|---|
| `POST /webhook/roll` | `{"token", "channel_id", "expression", "message"?}` |
| `POST /webhook/rollmessage` | `{"token", "channel_id", "expression", "message"}` |

Se a env var `WEBHOOK_TOKEN` estiver definida, o `token` do body tem de bater certo. URL pública: `https://porygon-discord-bot-production.up.railway.app`.

---

## Persistência de dados

O bot grava JSON em `DATA_DIR` (default `/data`), que no Railway é um **volume persistente** — sobrevive a redeploys e restarts.

| Ficheiro | Conteúdo |
|---|---|
| `/data/guild_config.json` | Funções ligadas/desligadas por servidor (`!config`) |
| `/data/sessions.json` | Config de sessões (role, fuso, jogadores, chats pessoais) + sessão ativa e que lembretes já foram enviados |

Se `DATA_DIR` não for gravável (dev local, ou volume mal montado), o bot avisa nos logs (`DATA_DIR ... not writable`) e grava na pasta do projeto — funciona, mas sem persistência entre deploys. As escritas são atómicas (ficheiro temporário + `os.replace`).

## Hosting e deploy (Railway)

| | |
|---|---|
| Projeto Railway | `pleasant-warmth` |
| Serviço | `porygon-discord-bot` |
| Ambiente | `production` |
| Volume | `porygon-discord-bot-volume` (5 GB) montado em `/data` |
| Deploy | Automático a cada push para `main` no GitHub (`Th1rdo/porygon-discord-bot`) |

**Variáveis de ambiente:**

| Var | Obrigatória | Descrição |
|---|---|---|
| `DISCORD_TOKEN` | ✅ | Token do bot |
| `WEBHOOK_TOKEN` | — | Protege os endpoints do webhook |
| `DATA_DIR` | — | Pasta de dados (default `/data`) |
| `PORT` | — | Porta do webhook (o Railway define) |

**Fluxo normal de trabalho:** editar `bot.py` → commit → `git push` → o Railway faz build e redeploy sozinho (1–2 min). Os slash commands sincronizam no arranque (`on_ready`); comandos novos podem demorar uns minutos a aparecer no Discord.

**Ver logs / estado:** `railway logs`, `railway status` (CLI já linkada na pasta do projeto), ou o dashboard do Railway.

## Desenvolvimento local

```bash
cd ~/PycharmProjects/Porygon
.venv/bin/pip install -r requirements.txt
DISCORD_TOKEN=... .venv/bin/python bot.py
```

Verificação rápida de sintaxe sem arrancar o bot:

```bash
.venv/bin/python -m py_compile bot.py
```

### Estrutura do `bot.py`

O ficheiro está organizado por secções (procura pelos comentários `# ---- ... ----`):

`env/logging` → `discord setup` → `per-guild config` → `roll core` → `webhook HTTP server` → comandos `roll` → `mission` → `countdown` → `config` → `session scheduler` (parsing de datas, mensagens, loop de lembretes, comandos) → `graceful shutdown`.
