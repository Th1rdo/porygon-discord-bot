import os
import sys
import re
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
bot = commands.Bot(command_prefix="!", intents=intents)
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
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN")

def _extract_bearer_token(request: web.Request) -> str | None:
    auth = request.headers.get("Authorization")
    if not auth:
        return None
    parts = auth.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None

async def _handle_roll(request: web.Request) -> web.StreamResponse:
    if request.method != "POST":
        return web.json_response({"error": "method not allowed"}, status=405)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    token = str(data.get("token", "")) or _extract_bearer_token(request) or ""
    if WEBHOOK_TOKEN and token != WEBHOOK_TOKEN:
        return web.json_response({"error": "unauthorized"}, status=401)

    channel_id = data.get("channel_id")
    expression = data.get("expression")
    header_message = data.get("message")  # optional header line
    combine = bool(data.get("combine", False))  # optional: send as one message
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
        await _send_roll_to_channel(channel, msg, header_message, combine=combine)
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

    token = str(data.get("token", "")) or _extract_bearer_token(request) or ""
    if WEBHOOK_TOKEN and token != WEBHOOK_TOKEN:
        return web.json_response({"error": "unauthorized"}, status=401)

    channel_id = data.get("channel_id")
    expression = data.get("expression")
    header_message = data.get("message")
    combine = bool(data.get("combine", False))
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
        await _send_roll_to_channel(channel, msg, header_message, combine=combine)
    except discord.HTTPException as e:
        logging.exception("Discord send failed")
        return web.json_response({"error": f"discord error: {e}"}, status=502)

    return web.json_response({"ok": True})

async def _send_roll_to_channel(channel: discord.abc.Messageable, msg: str, header: str | None = None, *, combine: bool = False):
    # optional header line
    if header and combine:
        combined = f"{header}\n{msg}"
        if len(combined) <= 1900:
            await channel.send(combined)
            return
        # if combined too long, fall back to header + chunked roll
        await channel.send(header[:1900])
    elif header:
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

async def _ensure_webhook_server():
    app = web.Application()
    app.add_routes([
        web.post("/webhook/roll", _handle_roll),
        web.post("/webhook/rollmessage", _handle_rollmessage),
        web.get("/", lambda request: web.json_response({
            "ok": True,
            "service": "porygon-bot",
            "features": ["roll", "rollmessage", "header", "bearer_auth", "combine"],
        })),
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