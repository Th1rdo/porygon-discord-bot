import os
import sys
import re
import time
import math
import random
import signal
import logging
import asyncio
import aiohttp
from aiohttp import web
import discord
from discord.ext import commands

# ---- env / logging ---------------------------------------------------------
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN not set")

logging.basicConfig(level=logging.INFO)

# ---- discord setup ---------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
# help_command=None: frees "!help" for other bots (Avrae) — ours is "!ajuda"
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
ready_event = asyncio.Event()

@bot.event
async def on_ready():
    logging.info("✅ Logged in as %s (id=%s)", bot.user, bot.user.id)
    try:
        synced = await bot.tree.sync()
        logging.info("Slash commands synced: %d", len(synced))
    except Exception as e:
        logging.exception("Slash sync failed: %s", e)
    # mark bot ready and ensure webhook server is running
    if not ready_event.is_set():
        ready_event.set()
    if not getattr(bot, "_web_started", False):
        try:
            await _ensure_webhook_server()
            bot._web_started = True
        except Exception:
            logging.exception("Failed to start webhook server")
    if not getattr(bot, "_scheduler_started", False):
        bot.loop.create_task(_session_scheduler_loop())
        bot._scheduler_started = True
        logging.info("🗓️ Session scheduler loop started")

# ---- per-guild config ------------------------------------------------------
import json
from pathlib import Path as _Path

# Toggleable features. A feature is ON unless a guild explicitly turns it off.
FEATURES = ("roll", "mission", "countdown")

# Config is stored as JSON on a persistent path. On Railway, mount a volume and
# point DATA_DIR at it (e.g. /data) so settings survive redeploys/restarts.
# If that path isn't writable (local dev, or volume not yet set up), fall back
# to the project folder so the bot still works.
def _resolve_data_dir() -> _Path:
    candidate = _Path(os.getenv("DATA_DIR", "/data"))
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        if os.access(candidate, os.W_OK):
            return candidate
    except OSError:
        pass
    logging.warning("DATA_DIR %s not writable; config will NOT persist across redeploys", candidate)
    return _Path(__file__).parent

DATA_DIR_PATH = _resolve_data_dir()
CONFIG_PATH = DATA_DIR_PATH / "guild_config.json"

def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("guilds"), dict):
                return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {"guilds": {}}

_config = _load_config()

def _save_config() -> None:
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_config, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except OSError:
        logging.exception("Failed to save guild config to %s", CONFIG_PATH)

def feature_enabled(guild_id: int | None, feature: str) -> bool:
    # No guild (DMs) -> always on; prefix collisions only happen in servers.
    if guild_id is None:
        return True
    return _config["guilds"].get(str(guild_id), {}).get(feature, True)

def set_feature(guild_id: int, feature: str, enabled: bool) -> None:
    guilds = _config["guilds"]
    guilds.setdefault(str(guild_id), {})[feature] = enabled
    _save_config()

# ---- roll core -------------------------------------------------------------
ROLL_RE = re.compile(
    r"""
    ^\s*
    (?P<n>\d{1,4})          # number of dice
    [dD]
    (?P<sides>\d{1,5})      # sides per die
    (?:\s*
       (?P<op>[+\-])        # optional + or -
       \s*
       (?P<mod>\d{1,6})     # modifier
    )?
    \s*$
    """,
    re.VERBOSE,
)

MAX_DICE = 500
MAX_SIDES = 1000

def parse_and_roll(expr: str):
    m = ROLL_RE.match(expr)
    if not m:
        raise ValueError("Formato inválido. Usa algo como `6d6 + 2` ou `3d20`.")

    n = int(m.group("n"))
    sides = int(m.group("sides"))
    op = m.group("op")
    mod = int(m.group("mod")) if m.group("mod") else 0

    if n < 1 or n > MAX_DICE:
        raise ValueError(f"Quantidade de dados inválida (1–{MAX_DICE}).")
    if sides < 2 or sides > MAX_SIDES:
        raise ValueError(f"Lados inválidos (2–{MAX_SIDES}).")

    rolls = [random.randint(1, sides) for _ in range(n)]
    subtotal = sum(rolls)

    total = subtotal
    if op == "+":
        total += mod
    elif op == "-":
        total -= mod

    # Build the exact display strings
    spec = f"{n}d{sides}"
    if op and mod:
        spec += f" {op} {mod}"

    # Inline-code bracket blocks like your screenshots
    spec_block = f"`[{spec}]`"
    rolls_block = f"`[{', '.join(map(str, rolls))}]`"

    # Final message
    msg = f"{spec_block} Rolagem: {rolls_block} Resultado: {total}"
    return msg

# ---- webhook HTTP server ----------------------------------------------------
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN") or os.getenv("WEBHOOK_SECRET")

async def _handle_roll(request: web.Request) -> web.StreamResponse:
    if request.method != "POST":
        return web.json_response({"error": "method not allowed"}, status=405)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    token = str(data.get("token", ""))
    if WEBHOOK_TOKEN and token != WEBHOOK_TOKEN:
        return web.json_response({"error": "unauthorized"}, status=401)

    channel_id = data.get("channel_id")
    expression = data.get("expression")
    header_message = data.get("message")  # optional header line
    if not channel_id or not expression:
        return web.json_response({"error": "channel_id and expression are required"}, status=400)

    try:
        cid = int(channel_id)
    except (TypeError, ValueError):
        return web.json_response({"error": "channel_id must be an integer"}, status=400)

    # wait for bot readiness
    await ready_event.wait()

    # resolve channel (cache or fetch)
    channel = bot.get_channel(cid)
    if channel is None:
        try:
            channel = await bot.fetch_channel(cid)
        except Exception:
            logging.exception("Failed to fetch channel %s", cid)
            return web.json_response({"error": "channel not found or inaccessible"}, status=404)

    # build message using existing roller
    try:
        msg = parse_and_roll(str(expression))
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)

    # send, with same size-guard behavior as commands
    try:
        await _send_roll_to_channel(channel, msg, header_message)
    except discord.HTTPException as e:
        logging.exception("Discord send failed")
        return web.json_response({"error": f"discord error: {e}"}, status=502)

    return web.json_response({"ok": True})

async def _handle_rollmessage(request: web.Request) -> web.StreamResponse:
    if request.method != "POST":
        return web.json_response({"error": "method not allowed"}, status=405)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    token = str(data.get("token", ""))
    if WEBHOOK_TOKEN and token != WEBHOOK_TOKEN:
        return web.json_response({"error": "unauthorized"}, status=401)

    channel_id = data.get("channel_id")
    expression = data.get("expression")
    header_message = data.get("message")
    if not channel_id or not expression or not header_message:
        return web.json_response({"error": "channel_id, expression and message are required"}, status=400)

    try:
        cid = int(channel_id)
    except (TypeError, ValueError):
        return web.json_response({"error": "channel_id must be an integer"}, status=400)

    await ready_event.wait()

    channel = bot.get_channel(cid)
    if channel is None:
        try:
            channel = await bot.fetch_channel(cid)
        except Exception:
            logging.exception("Failed to fetch channel %s", cid)
            return web.json_response({"error": "channel not found or inaccessible"}, status=404)

    try:
        msg = parse_and_roll(str(expression))
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)

    try:
        await _send_roll_to_channel(channel, msg, header_message)
    except discord.HTTPException as e:
        logging.exception("Discord send failed")
        return web.json_response({"error": f"discord error: {e}"}, status=502)

    return web.json_response({"ok": True})

async def _send_roll_to_channel(channel: discord.abc.Messageable, msg: str, header: str | None = None):
    # optional header line
    if header:
        # keep first line concise
        if len(header) > 1900:
            header = header[:1900]
        await channel.send(header)

    if len(msg) <= 1900:
        await channel.send(msg)
        return

    # split long roll message like command handlers
    header_part = msg.split(" Rolagem: ")[0]
    await channel.send(header_part)
    left = msg[len(header_part) + 1:]
    try:
        rolls_part = left.split(" Resultado: ")[0].replace("Rolagem: ", "")
        result_part = "Resultado: " + left.split(" Resultado: ")[1]
    except Exception:
        rolls_part, result_part = "", msg
    chunk = 1700
    text = rolls_part
    while text:
        await channel.send(text[:chunk])
        text = text[chunk:]
    await channel.send(result_part)

# hardcoded broadcast presets, so the website only needs {token, preset[, target]}
BROADCAST_CHANNELS = {
    "test": ["1542985444214579292"],
    "prod": [
        "1281641786678448261",
        "1281645729567346821",
        "1445798523672072384",
        "1281645678920994888",
    ],
}

BROADCAST_PRESETS = {
    "reconstrucao": """# ⏳ SOLICITAÇÃO DE RECONSTRUÇÃO TEMPORAL
-# PROTOCOLO DA ORDEM · REGISTO Nº 14-JF · CLASSIFICAÇÃO: INCOMPLETO

> Foi identificado um intervalo de **14 dias** entre o encerramento de **"O Juízo Final"** e o próximo registo confirmado.
> Esse período permanece **incompleto**.

Para prosseguir, precisamos de reconstruir as ações do agente durante esse intervalo.
Responde com base no que o teu personagem **tentou** fazer — não apenas no que conseguiu concluir.

1. Qual foi a **principal coisa** que ele quis fazer nessas duas semanas?
2. Qual foi a **maior ação concreta** que tentou realizar?
3. Houve alguém que ele **procurou**, **evitou**, ou com quem **precisava de falar**?
4. Houve alguma **consequência** do fim da operação que tentou resolver, corrigir ou enfrentar?
5. Tomou alguma **decisão importante** sobre o que pretende fazer da vida daqui para a frente?
6. **Mudou** alguma coisa importante nele?
   -# Relações, objetivos, posição dentro da Ordem, aparência, comportamento, equipamento…
7. Existe algo que ele **ainda queria fazer**, mas nunca teve oportunidade durante a operação?

## ⚠️ ATENÇÃO
- **Não assumas** que todas as ações tiveram sucesso — descreve a **intenção**, a **tentativa** e o resultado que o personagem procurava.
- Ações de grande impacto poderão exigir **validação**, reconstrução adicional ou **execução direta**.
- O agente **não precisa** de ter feito tudo nestas duas semanas. O fim da operação não é o fim da vida do personagem.

-# ⌛ Aguardando preenchimento do intervalo…""",
}

async def _broadcast_to(channel_ids: list[str], content: str) -> dict[str, str]:
    await ready_event.wait()
    results: dict[str, str] = {}
    for cid in channel_ids:
        channel = await _resolve_channel(int(cid))
        if channel is None:
            results[str(cid)] = "error: channel not found"
            continue
        try:
            await channel.send(content)
            results[str(cid)] = "ok"
        except discord.HTTPException as e:
            logging.exception("Broadcast send failed for channel %s", cid)
            results[str(cid)] = f"error: {e}"
    return results

async def _handle_preset(request: web.Request) -> web.StreamResponse:
    """The lazy one. Body: {"token": str, "preset": str, "target": "test"|"prod"}
    target defaults to "test" so nothing goes to the players by accident."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    token = str(data.get("token", ""))
    if not WEBHOOK_TOKEN or token != WEBHOOK_TOKEN:
        return web.json_response({"error": "unauthorized"}, status=401)

    preset = str(data.get("preset", ""))
    target = str(data.get("target", "test"))
    if preset not in BROADCAST_PRESETS:
        return web.json_response(
            {"error": f"unknown preset. options: {sorted(BROADCAST_PRESETS)}"}, status=400)
    if target not in BROADCAST_CHANNELS:
        return web.json_response(
            {"error": f"unknown target. options: {sorted(BROADCAST_CHANNELS)}"}, status=400)

    results = await _broadcast_to(BROADCAST_CHANNELS[target], BROADCAST_PRESETS[preset])
    ok = all(v == "ok" for v in results.values())
    return web.json_response({"ok": ok, "target": target, "results": results},
                             status=200 if ok else 207)

async def _handle_broadcast(request: web.Request) -> web.StreamResponse:
    """Send the same message to several channels. Body:
    {"token": str, "channel_ids": [int, ...], "content": str}
    Used by the website's buttons (e.g. temporal-reconstruction prompts)."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    token = str(data.get("token", ""))
    if not WEBHOOK_TOKEN or token != WEBHOOK_TOKEN:
        return web.json_response({"error": "unauthorized"}, status=401)

    channel_ids = data.get("channel_ids")
    content = data.get("content")
    if not isinstance(channel_ids, list) or not channel_ids or len(channel_ids) > 10:
        return web.json_response({"error": "channel_ids must be a list of 1-10 ids"}, status=400)
    if not isinstance(content, str) or not content.strip() or len(content) > 2000:
        return web.json_response({"error": "content must be a string of 1-2000 chars"}, status=400)

    try:
        cids = [str(int(c)) for c in channel_ids]
    except (TypeError, ValueError):
        return web.json_response({"error": "channel_ids must be integers"}, status=400)

    results = await _broadcast_to(cids, content)
    ok = all(v == "ok" for v in results.values())
    return web.json_response({"ok": ok, "results": results}, status=200 if ok else 207)

async def _handle_options(request: web.Request) -> web.StreamResponse:
    return web.Response(status=204)

@web.middleware
async def _cors_middleware(request: web.Request, handler):
    # allow browser calls from the website (fetch + JSON body triggers preflight)
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp

async def _ensure_webhook_server():
    app = web.Application(middlewares=[_cors_middleware])
    app.add_routes([
        web.post("/webhook/roll", _handle_roll),
        web.post("/webhook/rollmessage", _handle_rollmessage),
        web.post("/webhook/broadcast", _handle_broadcast),
        web.post("/webhook/preset", _handle_preset),
        web.route("OPTIONS", "/{tail:.*}", _handle_options),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logging.info("🌐 Webhook server running on 0.0.0.0:%s", port)

# ---- prefix command --------------------------------------------------------
@bot.command(name="roll")
async def roll_cmd(ctx, *, expression: str):
    # silently ignore if disabled here, so a different bot with the same prefix can answer
    if not feature_enabled(ctx.guild.id if ctx.guild else None, "roll"):
        return
    try:
        msg = parse_and_roll(expression)
    except ValueError as e:
        await ctx.send(str(e))
        return

    # Discord message size guard
    if len(msg) > 1900:
        # keep the exact look but split safely
        header = msg.split(" Rolagem: ")[0]
        await ctx.send(header)  # e.g. `[400d100]`
        # send the list in chunks then the result line
        # extract rolls and result again
        left = msg[len(header) + 1:]  # remove trailing space
        # left like: 'Rolagem: `[ ... ]` Resultado: 12345'
        # we’ll just resend the rolls block, then a short result line
        try:
            rolls_part = left.split(" Resultado: ")[0].replace("Rolagem: ", "")
            result_part = "Resultado: " + left.split(" Resultado: ")[1]
        except Exception:
            rolls_part, result_part = "", msg

        # chunk rolls_part if needed
        chunk = 1700
        text = rolls_part
        while text:
            await ctx.send(text[:chunk])
            text = text[chunk:]
        await ctx.send(result_part)
    else:
        await ctx.send(msg)

# ---- slash command mirror --------------------------------------------------
@bot.tree.command(name="roll", description="Rolar dados. Ex: 6d6 + 2 ou 3d20")
async def roll_slash(interaction: discord.Interaction, expression: str):
    if not feature_enabled(interaction.guild_id, "roll"):
        await interaction.response.send_message("Esta função está desativada neste servidor.", ephemeral=True)
        return
    try:
        msg = parse_and_roll(expression)
    except ValueError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return

    if len(msg) <= 1900:
        await interaction.response.send_message(msg)
    else:
        # same split logic as above
        header = msg.split(" Rolagem: ")[0]
        await interaction.response.send_message(header)
        left = msg[len(header) + 1:]
        try:
            rolls_part = left.split(" Resultado: ")[0].replace("Rolagem: ", "")
            result_part = "Resultado: " + left.split(" Resultado: ")[1]
        except Exception:
            rolls_part, result_part = "", msg
        chunk = 1700
        text = rolls_part
        while text:
            await interaction.channel.send(text[:chunk])
            text = text[chunk:]
        await interaction.channel.send(result_part)

# ---- mission sending -------------------------------------------------------
from pathlib import Path

MEDIA_DIR = Path(__file__).parent / "media"
# If you want a whitelist, map ids to base names here:
# MISSIONS = {"001": "mission001.mp4", "002": "mission002.mp4"}
# For now we auto-discover by id (mission###.*)

def _normalize_id(raw: str) -> str:
    # accept "1", "01", "001" -> "001"; only digits, max 4 just to be safe
    digits = "".join(ch for ch in raw if ch.isdigit())[:4]
    if not digits:
        raise ValueError("ID inválido. Usa `!mission 001` por exemplo.")
    return digits.zfill(3)

def _find_mission_file(mission_id: str) -> Path:
    # look for missionXXX with any extension
    pattern = f"mission{mission_id}."
    candidates = [p for p in MEDIA_DIR.iterdir() if p.name.startswith(pattern)]
    if not candidates:
        raise FileNotFoundError(f"Missão {mission_id} não encontrada em `{MEDIA_DIR}`.")
    # prefer mp4 if multiple
    candidates.sort(key=lambda p: (p.suffix != ".mp4", p.name))
    return candidates[0]

@bot.command(name="mission")
async def mission_cmd(ctx, mission_id: str):
    """Ex.: !mission 001  -> envia media/mission001.mp4 (ou o que existir)"""
    if not feature_enabled(ctx.guild.id if ctx.guild else None, "mission"):
        return
    try:
        mid = _normalize_id(mission_id)
        path = _find_mission_file(mid)
    except Exception as e:
        await ctx.send(str(e))
        return

    # Discord size limits apply. This will fail if the file is too large.
    try:
        await ctx.send(file=discord.File(fp=path, filename=path.name))
    except discord.HTTPException as e:
        await ctx.send(f"Falha ao enviar `{path.name}` ({e}). "
                       f"Arquivo pode ser grande demais. Considera enviar um link/CDN.")

# Slash version
@bot.tree.command(name="mission", description="Enviar a missão (ex.: 001)")
async def mission_slash(interaction: discord.Interaction, mission_id: str):
    if not feature_enabled(interaction.guild_id, "mission"):
        await interaction.response.send_message("Esta função está desativada neste servidor.", ephemeral=True)
        return
    try:
        mid = _normalize_id(mission_id)
        path = _find_mission_file(mid)
    except Exception as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return

    # respond + attach
    await interaction.response.send_message(f"Missão {mid}:")
    try:
        await interaction.followup.send(file=discord.File(fp=path, filename=path.name))
    except discord.HTTPException as e:
        await interaction.followup.send(f"Falha ao enviar `{path.name}` ({e}). "
                                        f"Arquivo pode ser grande demais.")

# ---- countdown -------------------------------------------------------------
# One active countdown per guild (server). Keyed by guild id.
active_countdowns: dict[int, asyncio.Task] = {}

MAX_COUNTDOWN = 24 * 3600  # 24h cap

_DURATION_RE = re.compile(
    r"^\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?\s*$",
    re.IGNORECASE,
)

def parse_duration(text: str) -> int:
    """Accepts '90', '90s', '5m', '1h30m', 'mm:ss' or 'hh:mm:ss'. Returns seconds."""
    text = text.strip()
    if not text:
        raise ValueError("Duração vazia.")

    # plain number -> seconds
    if text.isdigit():
        total = int(text)
    # colon format mm:ss or hh:mm:ss
    elif ":" in text:
        parts = text.split(":")
        if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
            raise ValueError("Formato inválido. Usa `mm:ss` ou `hh:mm:ss`.")
        nums = [int(p) for p in parts]
        if len(parts) == 2:
            h, m, s = 0, nums[0], nums[1]
        else:
            h, m, s = nums
        total = h * 3600 + m * 60 + s
    # 1h30m15s format
    else:
        match = _DURATION_RE.match(text)
        if not match or not any(match.groups()):
            raise ValueError("Formato inválido. Usa `90`, `5m`, `1h30m` ou `mm:ss`.")
        h = int(match.group(1) or 0)
        m = int(match.group(2) or 0)
        s = int(match.group(3) or 0)
        total = h * 3600 + m * 60 + s

    if total < 1:
        raise ValueError("A duração tem de ser pelo menos 1 segundo.")
    if total > MAX_COUNTDOWN:
        raise ValueError("Máximo 24 horas.")
    return total

def format_remaining(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def _render_countdown(seconds: int, label: str | None) -> str:
    line = f"⏳ `{format_remaining(seconds)}`"
    if label:
        return f"**{label}**\n{line}"
    return line

async def _run_countdown(message: discord.Message, total: int, label: str | None, guild_id: int):
    # Remaining is recomputed from the wall clock each tick, so a slow edit
    # never makes the countdown drift behind real time.
    end = time.monotonic() + total
    try:
        while True:
            now = time.monotonic()
            remaining = math.ceil(end - now)
            if remaining <= 0:
                break
            try:
                await message.edit(content=_render_countdown(remaining, label))
            except discord.HTTPException:
                pass  # rate-limited or transient; just try again next tick
            # sleep until the next whole-second boundary
            sleep_for = (end - time.monotonic()) - (remaining - 1)
            await asyncio.sleep(max(0.05, sleep_for))

        done = f"⏰ **{label}** — Terminou! 🎉" if label else "⏰ Terminou! 🎉"
        try:
            await message.edit(content=done)
        except discord.HTTPException:
            pass
    except asyncio.CancelledError:
        try:
            await message.edit(content="🛑 Countdown cancelado.")
        except discord.HTTPException:
            pass
        raise
    finally:
        # only clear if this task is still the registered one
        if active_countdowns.get(guild_id) is asyncio.current_task():
            active_countdowns.pop(guild_id, None)

async def _start_countdown(channel: discord.abc.Messageable, guild_id: int, total: int, label: str | None):
    # enforce one per server: cancel any existing one first
    existing = active_countdowns.get(guild_id)
    if existing and not existing.done():
        existing.cancel()
    message = await channel.send(_render_countdown(total, label))
    task = asyncio.create_task(_run_countdown(message, total, label, guild_id))
    active_countdowns[guild_id] = task

def _stop_countdown(guild_id: int) -> bool:
    task = active_countdowns.get(guild_id)
    if task and not task.done():
        task.cancel()
        return True
    return False

COUNTDOWN_HELP = (
    "⏳ **Countdown** — temporizador ao segundo (um por servidor)\n"
    "\n"
    "**Iniciar:**\n"
    "`!countdown <tempo> [legenda]`\n"
    "Exemplos:\n"
    "• `!countdown 90` — 90 segundos\n"
    "• `!countdown 5m` — 5 minutos\n"
    "• `!countdown 1h30m Início do evento` — com legenda\n"
    "• `!countdown 10:00` — formato mm:ss (também aceita hh:mm:ss)\n"
    "\n"
    "**Parar:** `!countdown stop`\n"
    "\n"
    "Formatos de tempo: `90`, `90s`, `5m`, `1h30m`, `mm:ss`, `hh:mm:ss` (máx. 24h).\n"
    "Iniciar um novo countdown substitui o que estiver a correr."
)

@bot.command(name="countdown")
async def countdown_cmd(ctx, duration: str | None = None, *, label: str | None = None):
    """!countdown -> ajuda | !countdown stop -> parar | !countdown 5m [legenda] -> iniciar"""
    if ctx.guild is None:
        await ctx.send("Os countdowns só funcionam num servidor.")
        return
    if not feature_enabled(ctx.guild.id, "countdown"):
        return

    # no args -> handout
    if duration is None:
        await ctx.send(COUNTDOWN_HELP)
        return

    # "!countdown stop" -> stop active one
    if duration.lower() == "stop":
        if _stop_countdown(ctx.guild.id):
            await ctx.send("🛑 Countdown parado.")
        else:
            await ctx.send("Não há nenhum countdown ativo.")
        return

    try:
        total = parse_duration(duration)
    except ValueError as e:
        await ctx.send(str(e))
        return
    await _start_countdown(ctx.channel, ctx.guild.id, total, label)

@bot.command(name="countdownstop")
async def countdown_stop_cmd(ctx):
    if ctx.guild is None:
        await ctx.send("Os countdowns só funcionam num servidor.")
        return
    if _stop_countdown(ctx.guild.id):
        await ctx.send("🛑 Countdown parado.")
    else:
        await ctx.send("Não há nenhum countdown ativo.")

@bot.tree.command(name="countdown", description="Iniciar um countdown ao segundo. Ex.: 5m, 1h30m, mm:ss")
async def countdown_slash(interaction: discord.Interaction, duration: str, label: str | None = None):
    if interaction.guild_id is None:
        await interaction.response.send_message("Os countdowns só funcionam num servidor.", ephemeral=True)
        return
    if not feature_enabled(interaction.guild_id, "countdown"):
        await interaction.response.send_message("Esta função está desativada neste servidor.", ephemeral=True)
        return
    try:
        total = parse_duration(duration)
    except ValueError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return
    await interaction.response.send_message(
        f"A iniciar countdown de `{format_remaining(total)}`…", ephemeral=True
    )
    await _start_countdown(interaction.channel, interaction.guild_id, total, label)

@bot.tree.command(name="countdown_stop", description="Parar o countdown ativo do servidor")
async def countdown_stop_slash(interaction: discord.Interaction):
    if interaction.guild_id is None:
        await interaction.response.send_message("Os countdowns só funcionam num servidor.", ephemeral=True)
        return
    if _stop_countdown(interaction.guild_id):
        await interaction.response.send_message("🛑 Countdown parado.", ephemeral=True)
    else:
        await interaction.response.send_message("Não há nenhum countdown ativo.", ephemeral=True)

# ---- config command --------------------------------------------------------
def _is_manager(member) -> bool:
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and (perms.manage_guild or perms.administrator))

def _render_config(guild_id: int) -> str:
    lines = ["⚙️ **Config do Porygon neste servidor**", ""]
    for feat in FEATURES:
        estado = "✅ ativada" if feature_enabled(guild_id, feat) else "🚫 desativada"
        lines.append(f"• `{feat}` — {estado}")
    lines += [
        "",
        "**Como mudar** (só admins/gestores):",
        "• `!config disable roll` — desligar uma função",
        "• `!config enable roll` — voltar a ligar",
        f"\nFunções: {', '.join(f'`{f}`' for f in FEATURES)}.",
        "Quando uma função está desligada, o Porygon ignora-a por completo "
        "(útil se tiveres outro bot com o mesmo prefixo `!`).",
    ]
    return "\n".join(lines)

@bot.command(name="config")
async def config_cmd(ctx, action: str | None = None, feature: str | None = None):
    """!config -> ver | !config disable <função> | !config enable <função>"""
    if ctx.guild is None:
        await ctx.send("O `!config` só funciona dentro de um servidor.")
        return
    if not _is_manager(ctx.author):
        await ctx.send("Só administradores/gestores do servidor (permissão *Manage Server*) podem usar o `!config`.")
        return

    if action is None:
        await ctx.send(_render_config(ctx.guild.id))
        return

    action = action.lower()
    if action not in ("enable", "disable"):
        await ctx.send(_render_config(ctx.guild.id))
        return

    feat = (feature or "").lower()
    if feat not in FEATURES:
        await ctx.send(f"Função inválida. Opções: {', '.join(FEATURES)}.")
        return

    set_feature(ctx.guild.id, feat, action == "enable")
    estado = "ativada ✅" if action == "enable" else "desativada 🚫"
    await ctx.send(f"`{feat}` {estado} neste servidor.")

@bot.tree.command(name="config", description="Ligar/desligar funções do Porygon neste servidor (só admins)")
async def config_slash(interaction: discord.Interaction, action: str | None = None, feature: str | None = None):
    if interaction.guild_id is None:
        await interaction.response.send_message("O config só funciona dentro de um servidor.", ephemeral=True)
        return
    if not _is_manager(interaction.user):
        await interaction.response.send_message(
            "Só administradores/gestores (permissão *Manage Server*) podem mudar a config.", ephemeral=True
        )
        return

    if action is None or action.lower() not in ("enable", "disable"):
        await interaction.response.send_message(_render_config(interaction.guild_id), ephemeral=True)
        return

    feat = (feature or "").lower()
    if feat not in FEATURES:
        await interaction.response.send_message(f"Função inválida. Opções: {', '.join(FEATURES)}.", ephemeral=True)
        return

    set_feature(interaction.guild_id, feat, action.lower() == "enable")
    estado = "ativada ✅" if action.lower() == "enable" else "desativada 🚫"
    await interaction.response.send_message(f"`{feat}` {estado} neste servidor.", ephemeral=True)

# ---- session scheduler -----------------------------------------------------
from datetime import datetime
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Europe/Lisbon"
SESSIONS_PATH = DATA_DIR_PATH / "sessions.json"

YES_EMOJI = "✅"
NO_EMOJI = "❌"

def _load_sessions() -> dict:
    try:
        with open(SESSIONS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("guilds"), dict):
                return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {"guilds": {}}

_sessions = _load_sessions()

def _save_sessions() -> None:
    tmp = SESSIONS_PATH.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_sessions, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SESSIONS_PATH)
    except OSError:
        logging.exception("Failed to save sessions to %s", SESSIONS_PATH)

def _guild_state(guild_id: int) -> dict:
    g = _sessions["guilds"].setdefault(str(guild_id), {})
    s = g.setdefault("settings", {})
    s.setdefault("timezone", DEFAULT_TZ)
    s.setdefault("role_id", None)
    s.setdefault("announce_channel_id", None)
    s.setdefault("remind_hours_before", 24)
    s.setdefault("players", {})  # {uid: {"name": str, "channel_id": int|None}}
    g.setdefault("active", None)
    return g

def _parse_when(text: str, tzname: str) -> int:
    """Accepts a Unix timestamp, a pasted <t:...:F>, or 'YYYY-MM-DD HH:MM' in the guild tz."""
    text = text.strip()
    if text.isdigit() and len(text) >= 9:
        return int(text)
    m = re.match(r"^<t:(\d+)(?::[a-zA-Z])?>$", text)
    if m:
        return int(m.group(1))
    tz = ZoneInfo(tzname)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return int(dt.replace(tzinfo=tz).timestamp())
    raise ValueError(
        "Data inválida. Usa `AAAA-MM-DD HH:MM` (ex.: `2026-08-15 21:00`), "
        "um timestamp Unix, ou cola um `<t:...:F>`."
    )

def _announce_message(state_settings: dict, sess: dict) -> str:
    role_id = state_settings.get("role_id")
    ping = f"\n<@&{role_id}>" if role_id else ""
    title = sess["title"]
    header = f'## "{title}"'
    if sess.get("chapter"):
        header += f" - {sess['chapter']}"
    lines = [header]
    if sess.get("subtitle"):
        lines.append(f"> ***{sess['subtitle']}***")
    lines.append(f"> ### <t:{sess['when']}:F>")
    lines.append(f"> <t:{sess['when']}:R>")
    return "\n".join(lines) + ping

def _confirm_message(state_settings: dict, sess: dict) -> str:
    role_id = state_settings.get("role_id")
    ping = f"<@&{role_id}>\n" if role_id else ""
    return (
        f"{ping}"
        f'Pessoal, **próxima sessão** de **"{sess["title"]}"** proposta para '
        f"<t:{sess['when']}:F> (<t:{sess['when']}:R>).\n\n"
        f"Respondam com {YES_EMOJI} se **conseguem comprometer-se** com a data, "
        f"ou {NO_EMOJI} se **não** conseguem, até <t:{sess['deadline']}:F> "
        f"(<t:{sess['deadline']}:R>). Não vou contar silêncio como confirmação. "
        f"Quem **não puder**, avise no seu chat pessoal.\n\n"
        f"Imprevistos reais acontecem, mas **evitem marcar** outros planos por cima "
        f"**depois** de confirmar a data."
    )

def _jump_link(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"

async def _resolve_channel(channel_id: int):
    ch = bot.get_channel(channel_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(channel_id)
        except Exception:
            logging.exception("Failed to fetch channel %s", channel_id)
            return None
    return ch

async def _gather_confirmations(guild_id: int, sess: dict):
    """Returns (confirmed_ids, declined_ids, no_response_ids) based on reactions vs registered players."""
    yes, no = set(), set()
    channel = await _resolve_channel(sess["channel_id"])
    if channel is not None:
        try:
            msg = await channel.fetch_message(sess["message_id"])
            for reaction in msg.reactions:
                if str(reaction.emoji) == YES_EMOJI:
                    async for u in reaction.users():
                        if not u.bot:
                            yes.add(u.id)
                elif str(reaction.emoji) == NO_EMOJI:
                    async for u in reaction.users():
                        if not u.bot:
                            no.add(u.id)
        except Exception:
            logging.exception("Failed to read reactions for session in guild %s", guild_id)
    players = {int(uid) for uid in _guild_state(guild_id)["settings"]["players"]}
    # a ✅ wins over ❌ if someone reacted both
    confirmed = yes
    declined = no - yes
    no_response = players - yes - no
    return confirmed, declined, no_response

def _mentions(ids) -> str:
    return " ".join(f"<@{i}>" for i in ids) if ids else "—"

async def _create_session(interaction: discord.Interaction, *, style: str, title: str,
                          when: int, chapter: str | None, subtitle: str | None,
                          deadline: int | None):
    guild_id = interaction.guild_id
    g = _guild_state(guild_id)
    settings = g["settings"]

    channel = interaction.channel
    sess = {
        "title": title,
        "chapter": chapter,
        "subtitle": subtitle,
        "when": when,
        "deadline": deadline if deadline is not None else max(int(when - 2 * 86400), 0),
        "channel_id": channel.id,
        "message_id": None,
        "style": style,
        "sent": {"deadline": False, "before": False, "start": False, "post": False},
    }
    # confirm-style sessions track a deadline; announce-style ones are already decided
    if style == "announce":
        sess["sent"]["deadline"] = True
        content = _announce_message(settings, sess)
    else:
        content = _confirm_message(settings, sess)

    message = await channel.send(content)
    try:
        await message.add_reaction(YES_EMOJI)
        await message.add_reaction(NO_EMOJI)
    except discord.HTTPException:
        pass
    sess["message_id"] = message.id
    g["active"] = sess
    _save_sessions()
    return sess

# ---- reminder loop ----------------------------------------------------------
async def _do_deadline(guild_id: int, sess: dict):
    settings = _guild_state(guild_id)["settings"]
    confirmed, declined, no_response = await _gather_confirmations(guild_id, sess)
    channel = await _resolve_channel(sess["channel_id"])
    jump = _jump_link(guild_id, sess["channel_id"], sess["message_id"])
    if channel is not None:
        await channel.send(
            f"⏰ **Prazo de confirmação terminou** — sessão de **{sess['title']}** "
            f"em <t:{sess['when']}:F>.\n"
            f"{YES_EMOJI} Confirmados ({len(confirmed)}): {_mentions(confirmed)}\n"
            f"{NO_EMOJI} Não podem ({len(declined)}): {_mentions(declined)}\n"
            f"❓ Sem resposta ({len(no_response)}): {_mentions(no_response)}"
        )
    # nudge non-responders in their personal channels
    players = settings["players"]
    for uid in no_response:
        cid = players.get(str(uid), {}).get("channel_id")
        if not cid:
            continue
        pch = await _resolve_channel(cid)
        if pch is None:
            continue
        try:
            await pch.send(
                f"Ei <@{uid}>! Ainda não confirmaste a sessão de **{sess['title']}** "
                f"em <t:{sess['when']}:F> (<t:{sess['when']}:R>).\n"
                f"Reage {YES_EMOJI} ou {NO_EMOJI} aqui: {jump}"
            )
        except discord.HTTPException:
            logging.exception("Failed to nudge player %s", uid)

async def _do_before(guild_id: int, sess: dict):
    settings = _guild_state(guild_id)["settings"]
    channel = await _resolve_channel(sess["channel_id"])
    if channel is None:
        return
    confirmed, _declined, no_response = await _gather_confirmations(guild_id, sess)
    ping = f"<@&{settings['role_id']}>" if settings.get("role_id") else _mentions(confirmed)
    extra = ""
    if no_response:
        extra = f"\nAinda sem resposta: {_mentions(no_response)} — deem sinal! 🙏"
    await channel.send(
        f"⏳ **Falta pouco!** Sessão de **{sess['title']}** <t:{sess['when']}:R> "
        f"— <t:{sess['when']}:F>.\n"
        f"Confirmados ({len(confirmed)}): {_mentions(confirmed)}{extra}\n{ping}"
    )

async def _do_start(guild_id: int, sess: dict):
    settings = _guild_state(guild_id)["settings"]
    channel = await _resolve_channel(sess["channel_id"])
    if channel is None:
        return
    confirmed, _d, _n = await _gather_confirmations(guild_id, sess)
    ping = f"<@&{settings['role_id']}>" if settings.get("role_id") else _mentions(confirmed)
    await channel.send(
        f"🎲 **É HOJE!** A sessão de **{sess['title']}** começa <t:{sess['when']}:R>. "
        f"Preparem-se! {ping}"
    )

async def _do_post(guild_id: int, sess: dict):
    channel = await _resolve_channel(sess["channel_id"])
    if channel is not None:
        await channel.send(
            f"📖 A sessão de **{sess['title']}** terminou (ou já passou). "
            f"Obrigado a quem apareceu! Bora marcar a próxima? 🔥"
        )
    # clear active session so a new one can be scheduled
    _guild_state(guild_id)["active"] = None
    _save_sessions()

async def _tick_sessions():
    now = time.time()
    for gid_str in list(_sessions["guilds"].keys()):
        g = _sessions["guilds"][gid_str]
        sess = g.get("active")
        if not sess:
            continue
        gid = int(gid_str)
        sent = sess["sent"]
        when = sess["when"]
        remind_s = _guild_state(gid)["settings"].get("remind_hours_before", 24) * 3600
        changed = False
        try:
            if not sent.get("deadline") and now >= sess["deadline"] and now < when:
                await _do_deadline(gid, sess); sent["deadline"] = True; changed = True
            if not sent.get("before") and (when - remind_s) <= now < when:
                await _do_before(gid, sess); sent["before"] = True; changed = True
            if not sent.get("start") and when <= now < when + 3 * 3600:
                await _do_start(gid, sess); sent["start"] = True; changed = True
            if not sent.get("post") and now >= when + 3 * 3600:
                await _do_post(gid, sess); sent["post"] = True; changed = True
        except Exception:
            logging.exception("Reminder action failed for guild %s", gid)
        if changed:
            _save_sessions()

async def _session_scheduler_loop():
    await ready_event.wait()
    while True:
        try:
            await _tick_sessions()
        except Exception:
            logging.exception("session scheduler tick failed")
        await asyncio.sleep(30)

# ---- session commands (GM / admins only) -----------------------------------
async def _require_manager_slash(interaction: discord.Interaction) -> bool:
    if interaction.guild_id is None:
        await interaction.response.send_message("Só funciona dentro de um servidor.", ephemeral=True)
        return False
    if not _is_manager(interaction.user):
        await interaction.response.send_message(
            "Só administradores/gestores (permissão *Manage Server*) podem gerir sessões.", ephemeral=True
        )
        return False
    return True

@bot.tree.command(name="session_setup", description="Configurar o role a pingar, canal e fuso horário")
async def session_setup(interaction: discord.Interaction,
                        role: discord.Role | None = None,
                        announce_channel: discord.TextChannel | None = None,
                        timezone: str | None = None,
                        remind_hours_before: int | None = None):
    if not await _require_manager_slash(interaction):
        return
    settings = _guild_state(interaction.guild_id)["settings"]
    if role is not None:
        settings["role_id"] = role.id
    if announce_channel is not None:
        settings["announce_channel_id"] = announce_channel.id
    if timezone is not None:
        try:
            ZoneInfo(timezone)
        except Exception:
            await interaction.response.send_message(
                "Fuso horário inválido. Ex.: `Europe/Lisbon`.", ephemeral=True)
            return
        settings["timezone"] = timezone
    if remind_hours_before is not None:
        settings["remind_hours_before"] = max(1, remind_hours_before)
    _save_sessions()
    role_txt = f"<@&{settings['role_id']}>" if settings.get("role_id") else "—"
    await interaction.response.send_message(
        "⚙️ **Config de sessões**\n"
        f"• Role: {role_txt}\n"
        f"• Fuso: `{settings['timezone']}`\n"
        f"• Lembrete: {settings['remind_hours_before']}h antes\n"
        f"• Jogadores registados: {len(settings['players'])}",
        ephemeral=True,
    )

@bot.tree.command(name="session_player", description="Registar/atualizar um jogador e o seu chat pessoal")
async def session_player(interaction: discord.Interaction,
                         player: discord.User,
                         personal_channel: discord.TextChannel | None = None):
    if not await _require_manager_slash(interaction):
        return
    settings = _guild_state(interaction.guild_id)["settings"]
    settings["players"][str(player.id)] = {
        "name": player.display_name,
        "channel_id": personal_channel.id if personal_channel else None,
    }
    _save_sessions()
    ch_txt = f"<#{personal_channel.id}>" if personal_channel else "sem chat pessoal"
    await interaction.response.send_message(
        f"✅ Jogador <@{player.id}> registado ({ch_txt}). "
        f"Total: {len(settings['players'])}.", ephemeral=True)

@bot.tree.command(name="session_player_remove", description="Remover um jogador da lista")
async def session_player_remove(interaction: discord.Interaction, player: discord.User):
    if not await _require_manager_slash(interaction):
        return
    settings = _guild_state(interaction.guild_id)["settings"]
    if settings["players"].pop(str(player.id), None) is None:
        await interaction.response.send_message("Esse jogador não estava registado.", ephemeral=True)
        return
    _save_sessions()
    await interaction.response.send_message(
        f"🗑️ <@{player.id}> removido. Total: {len(settings['players'])}.", ephemeral=True)

@bot.tree.command(name="session_players", description="Ver os jogadores registados")
async def session_players(interaction: discord.Interaction):
    if not await _require_manager_slash(interaction):
        return
    players = _guild_state(interaction.guild_id)["settings"]["players"]
    if not players:
        await interaction.response.send_message(
            "Nenhum jogador registado. Usa `/session_player`.", ephemeral=True)
        return
    lines = []
    for uid, p in players.items():
        ch = f"<#{p['channel_id']}>" if p.get("channel_id") else "⚠️ sem chat"
        lines.append(f"• <@{uid}> — {ch}")
    await interaction.response.send_message(
        "👥 **Jogadores registados:**\n" + "\n".join(lines), ephemeral=True)

@bot.tree.command(name="session_propose",
                  description="Propor uma sessão e pedir confirmação (✅/❌) até um prazo")
async def session_propose(interaction: discord.Interaction,
                          title: str, when: str,
                          chapter: str | None = None,
                          subtitle: str | None = None,
                          deadline: str | None = None):
    if not await _require_manager_slash(interaction):
        return
    settings = _guild_state(interaction.guild_id)["settings"]
    try:
        when_ts = _parse_when(when, settings["timezone"])
        deadline_ts = _parse_when(deadline, settings["timezone"]) if deadline else None
    except ValueError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return
    await interaction.response.send_message("A publicar proposta de sessão… 📨", ephemeral=True)
    await _create_session(interaction, style="propose", title=title, when=when_ts,
                          chapter=chapter, subtitle=subtitle, deadline=deadline_ts)

@bot.tree.command(name="session_announce",
                  description="Anunciar uma sessão já marcada (mensagem de hype)")
async def session_announce(interaction: discord.Interaction,
                           title: str, when: str,
                           chapter: str | None = None,
                           subtitle: str | None = None):
    if not await _require_manager_slash(interaction):
        return
    settings = _guild_state(interaction.guild_id)["settings"]
    try:
        when_ts = _parse_when(when, settings["timezone"])
    except ValueError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return
    await interaction.response.send_message("A publicar anúncio de sessão… 🎲", ephemeral=True)
    await _create_session(interaction, style="announce", title=title, when=when_ts,
                          chapter=chapter, subtitle=subtitle, deadline=None)

@bot.tree.command(name="session_status", description="Ver quem confirmou / não confirmou a sessão ativa")
async def session_status(interaction: discord.Interaction):
    if not await _require_manager_slash(interaction):
        return
    sess = _guild_state(interaction.guild_id).get("active")
    if not sess:
        await interaction.response.send_message("Não há sessão ativa.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    confirmed, declined, no_response = await _gather_confirmations(interaction.guild_id, sess)
    await interaction.followup.send(
        f"📊 **{sess['title']}** — <t:{sess['when']}:F>\n"
        f"{YES_EMOJI} Confirmados ({len(confirmed)}): {_mentions(confirmed)}\n"
        f"{NO_EMOJI} Não podem ({len(declined)}): {_mentions(declined)}\n"
        f"❓ Sem resposta ({len(no_response)}): {_mentions(no_response)}",
        ephemeral=True,
    )

@bot.tree.command(name="session_cancel", description="Cancelar/limpar a sessão ativa")
async def session_cancel(interaction: discord.Interaction):
    if not await _require_manager_slash(interaction):
        return
    g = _guild_state(interaction.guild_id)
    if not g.get("active"):
        await interaction.response.send_message("Não há sessão ativa.", ephemeral=True)
        return
    g["active"] = None
    _save_sessions()
    await interaction.response.send_message("🛑 Sessão cancelada.", ephemeral=True)

# ---- send_message (falar pelo bot) -----------------------------------------
class _SendMessageModal(discord.ui.Modal, title="Enviar mensagem pelo bot"):
    def __init__(self, channel: discord.TextChannel):
        super().__init__()
        self.channel = channel
        self.message = discord.ui.TextInput(
            label=f"Mensagem para #{channel.name}"[:45],
            style=discord.TextStyle.paragraph,
            placeholder="Escreve aqui — Enter faz linha nova, markdown funciona.",
            max_length=2000,
        )
        self.add_item(self.message)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await self.channel.send(str(self.message.value))
        except discord.HTTPException as e:
            await interaction.response.send_message(f"Falha ao enviar: {e}", ephemeral=True)
            return
        await interaction.response.send_message(f"📨 Enviado para {self.channel.mention}.", ephemeral=True)

@bot.tree.command(name="send_message", description="Enviar uma mensagem pelo bot para um canal (só admins)")
async def send_message_slash(interaction: discord.Interaction,
                             channel: discord.TextChannel,
                             message: str | None = None):
    if not await _require_manager_slash(interaction):
        return
    # without a message, open a modal — its textbox supports real multiline
    if message is None:
        await interaction.response.send_modal(_SendMessageModal(channel))
        return
    # inline message: slash inputs are single-line; allow literal \n for line breaks
    content = message.replace("\\n", "\n")
    try:
        await channel.send(content)
    except discord.HTTPException as e:
        await interaction.response.send_message(f"Falha ao enviar: {e}", ephemeral=True)
        return
    await interaction.response.send_message(f"📨 Enviado para {channel.mention}.", ephemeral=True)

@bot.command(name="send_message")
async def send_message_cmd(ctx, channel_id: str, *, message: str):
    """!send_message <channel_id> <mensagem> — suporta várias linhas."""
    if ctx.guild is None or not _is_manager(ctx.author):
        return
    try:
        cid = int(channel_id.strip("<#>"))
    except ValueError:
        await ctx.send("ID de canal inválido. Usa o ID numérico ou #canal.")
        return
    channel = await _resolve_channel(cid)
    if channel is None:
        await ctx.send("Canal não encontrado.")
        return
    try:
        await channel.send(message)
    except discord.HTTPException as e:
        await ctx.send(f"Falha ao enviar: {e}")
        return
    try:
        await ctx.message.add_reaction("📨")
    except discord.HTTPException:
        pass

# ---- ajuda / manual ---------------------------------------------------------
# Compact Avrae-style command reference, grouped by category.
AJUDA_TEXT = """# 📖 Manual do Bot
Bot da campanha — dados, missões, countdowns e agendamento de sessões.
-# `<arg>` = obrigatório · `[arg]` = opcional · 🔒 = só admins/gestores (*Manage Server*)

## 🎲 Dados
- `!roll <dados>` — Rola no formato XdY ± Z. Ex.: `!roll 6d6 + 2`, `!roll 3d20` (também `/roll`)

## 🎬 Missões
- `!mission <id>` — Envia o ficheiro da missão. Ex.: `!mission 001` (também `/mission`)

## ⏳ Countdown
- `!countdown <tempo> [legenda]` — Timer ao vivo, atualizado a cada segundo. Ex.: `!countdown 1h30m Início do evento`
- `!countdown stop` — Para o countdown ativo
- `!countdown` — Ajuda detalhada do countdown (também `/countdown`, `/countdown_stop`)
-# Tempos: `90`, `90s`, `5m`, `1h30m`, `mm:ss`, `hh:mm:ss` · máx. 24h · um por servidor

## 🗓️ Sessões 🔒
- `/session_propose <title> <when> [chapter] [subtitle] [deadline]` — Propõe uma data e pede ✅/❌ até um prazo; quem não responder leva lembrete no chat pessoal
- `/session_announce <title> <when> [chapter] [subtitle]` — Anúncio de hype para sessão já marcada
- `/session_status` — Vê quem confirmou / falta responder
- `/session_cancel` — Cancela a sessão ativa
- `/session_setup [role] [timezone] [remind_hours_before]` — Configura role a pingar, fuso e antecedência do lembrete
- `/session_player <player> [personal_channel]` — Regista um jogador e o seu chat pessoal
- `/session_players` · `/session_player_remove <player>` — Lista / remove jogadores
-# Datas: `AAAA-MM-DD HH:MM` (fuso do servidor), `DD/MM/AAAA HH:MM`, timestamp Unix ou `<t:...:F>` colado
-# Automático: resumo no deadline → lembrete 24h antes → "É HOJE!" → fecho 3h depois

## 🛠️ Utilidades
- `!config` 🔒 — Liga/desliga funções neste servidor. Ex.: `!config disable roll` (também `/config`)
- `/send_message <canal> [mensagem]` 🔒 — Fala pelo bot; sem mensagem abre caixa de texto multilinha
- `!ajuda` — Mostra esta lista (também `/ajuda`)
- `!manual` — Recebe esta lista por DM (também `/manual`)"""

def _split_discord_chunks(text: str, limit: int = 1900) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        # start a fresh chunk on top-level sections to keep them together
        if line.startswith("## ") and len(current) > limit // 2:
            chunks.append(current)
            current = ""
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current)
    return chunks

@bot.command(name="ajuda")
async def ajuda_cmd(ctx):
    """Mostra a lista de comandos."""
    for chunk in _split_discord_chunks(AJUDA_TEXT):
        await ctx.send(chunk)

@bot.tree.command(name="ajuda", description="Ver a lista de comandos do bot")
async def ajuda_slash(interaction: discord.Interaction):
    chunks = _split_discord_chunks(AJUDA_TEXT)
    await interaction.response.send_message(chunks[0], ephemeral=True)
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk, ephemeral=True)

def _manual_chunks() -> list[str]:
    return _split_discord_chunks(AJUDA_TEXT)

async def _send_manual(user: discord.User | discord.Member) -> bool:
    try:
        for chunk in _manual_chunks():
            await user.send(chunk)
        return True
    except discord.Forbidden:
        return False
    except discord.HTTPException:
        logging.exception("Failed to DM manual to %s", user)
        return False

@bot.command(name="manual")
async def manual_cmd(ctx):
    """Envia o manual do bot por DM."""
    ok = await _send_manual(ctx.author)
    if ok:
        try:
            await ctx.message.add_reaction("📬")
        except discord.HTTPException:
            pass
    else:
        await ctx.send("Não consegui enviar DM — verifica se tens DMs abertas para membros deste servidor.")

@bot.tree.command(name="manual", description="Receber o manual do Porygon por DM")
async def manual_slash(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    ok = await _send_manual(interaction.user)
    if ok:
        await interaction.followup.send("📬 Manual enviado por DM!", ephemeral=True)
    else:
        await interaction.followup.send(
            "Não consegui enviar DM — verifica se tens DMs abertas para membros deste servidor.",
            ephemeral=True,
        )

# ---- graceful shutdown -----------------------------------------------------
def _shutdown(*_):
    logging.info("Shutting down...")
    try:
        bot.loop.create_task(bot.close())
    finally:
        sys.exit(0)

signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)

bot.run(TOKEN)