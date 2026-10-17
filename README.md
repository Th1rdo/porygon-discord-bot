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
   - [🕵️ Ficha Paranormal (convite para o site)](#️-ficha-paranormal-convite-para-o-site)
   - [🎲 Rolagens da ficha → Discord](#-rolagens-da-ficha--discord)
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

### 🕵️ Ficha Paranormal (convite para o site)

Qualquer pessoa no servidor pode liberar **o seu** e-mail no site da ficha ([ordemcodex.xyz](https://ordemcodex.xyz)), que é só para convidados.

| Comando | Descrição |
|---|---|
| `/ficha_convite <email>` | Põe o e-mail na lista de convidados. Resposta **só para quem usou** (ephemeral). Um e-mail por conta do Discord |

Como funciona: o bot chama a função `ficha_paranormal.invite_from_discord` no Supabase do site (projeto hub
`zktdrataxwkfrdjdkrsl`) com a chave *publishable* (pública). A função só sabe **acrescentar** convites, confere o
segredo `FICHA_INVITE_SECRET` (na base fica só o hash SHA-256, tabela `ficha_paranormal.integration_secrets`) e
guarda o `discord_user_id` — se a pessoa já liberou outro e-mail, recusa e manda falar com o Tiago.
Desligar num servidor: `/config disable ficha`.

Só funciona **dentro de servidores** (não em DM). Se a resposta disser que o e-mail já tem conta, manda entrar com a
senha (ou "Esqueci a senha"); senão, criar a senha. Trocar o e-mail de alguém = o Tiago apaga a linha em
`ficha_paranormal.allowed_emails`.

Variáveis: `FICHA_INVITE_SECRET` (obrigatória para a função), `FICHA_GUILD_IDS` (opcional — IDs de servidor separados
por vírgula; vazio = qualquer servidor onde o bot está), `FICHA_SUPABASE_URL` / `FICHA_SUPABASE_KEY` (opcionais).
Trocar o segredo: gerar um novo, pôr o hash com `update ficha_paranormal.integration_secrets set secret_hash =
encode(extensions.digest('<novo>', 'sha256'), 'hex') where name = 'discord_bot';` e atualizar a variável no Railway.

### 🎲 Rolagens da ficha → Discord

Quando um jogador rola algo na ficha ([ordemcodex.xyz](https://ordemcodex.xyz)), o site manda a rolagem ao bot e o
bot publica um embed compacto no canal `FICHA_ROLL_CHANNEL_ID` (por agora fixo: `1165819468354687066`; mais tarde
escolhe-se por DM). **Não há segredo no browser**: o site envia o *access token* do Supabase do jogador e o bot
confirma-o no servidor.

**`POST /webhook/ficha-roll`**

```http
POST /webhook/ficha-roll
Authorization: Bearer <access_token da sessão Supabase do jogador>
Content-Type: application/json

{
  "agent": "Joui",                 // obrigatório, texto
  "title": "Luta",                 // obrigatório, texto
  "kind": "pericia",               // atributo | pericia | ataque | ritual | livre
  "notation": "2d20kh1+5",         // opcional — aparece em `código`
  "dice": "[18, 7]",               // opcional — dados rolados
  "total": 23,                     // número (opcional se houver attack/damage)
  "attack": { "total": 25, "critical": 1, "info": "margem 19" },   // opcional; critical: -1 falha, 0, 1 crítico
  "damage": { "total": 14, "breakdown": "1d8+3 [5] +6" },          // opcional
  "extra": "texto livre"           // opcional
}
```

Regras: corpo ≤ **4 KB** (senão `413`); textos numa linha só e cortados a **200** caracteres (com `…`); números
finitos com |n| ≤ 1 000 000; tem de haver `total`, `attack` ou `damage`; campos desconhecidos são ignorados.
Todo o texto do jogador é **escapado** (markdown) e o embed vai com `allowed_mentions=none` (nunca pinga ninguém).

Respostas (JSON):

| Status | Corpo | Quando |
|---|---|---|
| `200` | `{"ok": true, "message_id": "…", "channel_id": "…"}` | Publicado |
| `400` | `{"ok": false, "error": "invalid_json"}` / `{"ok": false, "error": "invalid_payload", "detail": "…"}` | Corpo inválido |
| `401` | `{"ok": false, "error": "unauthorized"}` | Sem `Authorization: Bearer`, token inválido/expirado |
| `403` | `{"ok": false, "error": "not_allowed"}` | Conta válida mas fora da lista de convidados |
| `413` | `{"ok": false, "error": "payload_too_large"}` | Corpo > 4 KB |
| `429` | `{"ok": false, "error": "rate_limited", "retry_after": <segundos inteiros>}` + header `Retry-After` (mesmo valor) | > 8 rolagens / 10 s por conta; ou verificações de token novas acima de 30 / min por IP **ou** 120 / min no total |
| `502` | `{"ok": false, "error": "discord_error"}` | O Discord recusou o envio |
| `503` | `{"ok": false, "error": "auth_unavailable" \| "channel_unavailable" \| "disabled"}` | Supabase em baixo / canal inacessível / `ficha_roll` desligada no servidor do canal |

Como autentica: `GET {FICHA_SUPABASE_URL}/auth/v1/user` (com `apikey` + o Bearer) tem de dar 200 **e**
`POST /rest/v1/rpc/am_i_allowed` (header `Content-Profile: ficha_paranormal`, mesmo Bearer) tem de devolver `true`.
As duas chamadas correm em paralelo; o resultado positivo fica em cache **5 min** por token (só o hash SHA-256 do
token, e nunca além do `exp` do JWT) e um token recusado (401) fica em cache negativa **60 s**. Verificações novas no
Supabase têm dois limites: **30/min por IP** (IP = `X-Real-IP` ou o hop **mais à direita** do `X-Forwarded-For`,
nunca o da esquerda, que o cliente controla) e um **teto global de 120/min** que não depende de headers — mesmo
com IPs falsificados ninguém martela o Supabase através do bot.

CORS desta rota: só `https://ordemcodex.xyz`, `https://www.ordemcodex.xyz`,
`https://ficha-paranormal-preview-production.up.railway.app` e `http://localhost:5184` (dev), com
`Authorization` permitido (as outras rotas continuam com `*`). No arranque o bot só **regista nos logs** se o canal
existe e se tem `send_messages` / `embed_links` (procura `ficha-roll:` nos logs) — não publica nada.
Desligar só as rolagens: `/config disable ficha_roll` no servidor do canal (responde `503 disabled`); a função
`ficha` (convites) é independente. Se o envio falhar com *Forbidden*/*Not Found*, o bot esquece o canal em cache e
volta a resolvê-lo no pedido seguinte (repor a permissão/canal não precisa de restart).

Variáveis: `FICHA_ROLL_CHANNEL_ID` (default `1165819468354687066`), `FICHA_ROLL_ORIGINS` (opcional — substitui a
lista de origens, separadas por vírgula), mais `FICHA_SUPABASE_URL` / `FICHA_SUPABASE_KEY` (as mesmas do convite).

### ⚙️ Config por servidor

Liga/desliga funções por servidor — útil quando outro bot usa o mesmo prefixo `!`. Só admins/gestores.

| Comando | Descrição |
|---|---|
| `!config` / `/config` | Mostra o estado das funções e a ajuda |
| `!config disable <função>` | Desliga (`roll`, `mission`, `countdown`, `ficha` = `/ficha_convite`, `ficha_roll` = rolagens do site) |
| `!config enable <função>` | Volta a ligar |

Com uma função desligada: o comando `!` fica **em silêncio total** (o outro bot pode responder); o comando `/` responde só a ti a dizer que está desativada.

### 📨 Send Message (falar pelo bot)

Só para admins/gestores. O bot envia a mensagem como se fosse ele a falar.

| Comando | Descrição |
|---|---|
| `/send_message <canal>` | **Sem** mensagem: abre um formulário com caixa de texto multilinha (Enter = linha nova) |
| `/send_message <canal> <mensagem>` | Com mensagem inline: envia direto (escreve `\n` para quebras de linha) |
| `!send_message <channel_id> <mensagem>` | Versão prefixo — aceita mensagens multilinha e `#canal` ou ID |

### 📬 Ajuda e Manual

| Comando | Descrição |
|---|---|
| `!ajuda` / `/ajuda` | Lista compacta de comandos por categoria (estilo Avrae). A versão slash responde só a ti |
| `!manual` / `/manual` | A mesma lista, mas enviada por DM (reage 📬 quando envia) |

O texto vive em `AJUDA_TEXT` no `bot.py` — ao adicionar um comando novo, acrescenta-o lá (e aqui). O `!help` embutido do discord.py está **desativado** para não entrar em conflito com outros bots (ex.: Avrae).

### 🌐 Webhook HTTP

Servidor aiohttp na porta `PORT` (default 8080) para disparar rolls de fora do Discord.

| Endpoint | Body (JSON) | Descrição |
|---|---|---|
| `POST /webhook/roll` | `{"token", "channel_id", "expression", "message"?}` | Rola dados num canal |
| `POST /webhook/rollmessage` | `{"token", "channel_id", "expression", "message"}` | Roll com cabeçalho |
| `POST /webhook/broadcast` | `{"token", "channel_ids": [..], "content"}` | Envia a mesma mensagem a vários canais (máx. 10, conteúdo ≤2000 chars) |
| `POST /webhook/ficha-roll` | ver [Rolagens da ficha](#-rolagens-da-ficha--discord) | Rolagem vinda da ficha; auth = token Supabase do jogador (sem `token`) |
| `POST /webhook/preset` | `{"token", "preset", "target"?}` | Envia uma mensagem **hardcoded** (`BROADCAST_PRESETS` no `bot.py`) aos canais do target (`BROADCAST_CHANNELS`). `target` é `"test"` (default) ou `"prod"` — usado pelos botões do site. Presets atuais: `reconstrucao` |

O `token` tem de bater com a env var `WEBHOOK_TOKEN` (ou `WEBHOOK_SECRET`); o `/broadcast` **recusa** pedidos se nenhuma estiver definida. As respostas têm CORS aberto para chamadas de browser (exceto `/webhook/ficha-roll`, que só aceita as origens do site). URL pública: `https://porygon-discord-bot-production.up.railway.app`.

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
| `FICHA_INVITE_SECRET` | — | Segredo do `/ficha_convite` (ver acima) |
| `FICHA_GUILD_IDS` | — | Servidores onde o `/ficha_convite` funciona (vazio = todos) |
| `FICHA_ROLL_CHANNEL_ID` | — | Canal das rolagens da ficha (default `1165819468354687066`) |
| `FICHA_ROLL_ORIGINS` | — | Origens CORS do `/webhook/ficha-roll` (default: site, www, preview, `localhost:5184`) |
| `FICHA_SUPABASE_URL` / `FICHA_SUPABASE_KEY` | — | Supabase do site (defaults = projeto hub + chave publishable) |

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

Testes (rolagens da ficha: validação, texto do embed, rate limiter e o handler HTTP com Supabase/Discord falsos —
não tocam em nada real). Importar `bot.py` não arranca o bot (`bot.run` está atrás de `if __name__ == "__main__"`):

```bash
.venv/bin/python tests/test_ficha_roll.py      # ou: .venv/bin/python -m pytest tests/ (se tiveres pytest)
```

### Estrutura do `bot.py`

O ficheiro está organizado por secções (procura pelos comentários `# ---- ... ----`):

`env/logging` → `discord setup` → `per-guild config` → `roll core` → `webhook HTTP server` → comandos `roll` → `mission` → `countdown` → `config` → `session scheduler` (parsing de datas, mensagens, loop de lembretes, comandos) → `send_message` →
`Ficha Paranormal: convite` → `Ficha Paranormal: rolagens da ficha → Discord` → `ajuda / manual` → `graceful shutdown`.
