import os
import sys
import re
import random
import signal
import logging

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

@bot.event
async def on_ready():
    logging.info("✅ Logged in as %s (id=%s)", bot.user, bot.user.id)
    try:
        synced = await bot.tree.sync()
        logging.info("Slash commands synced: %d", len(synced))
    except Exception as e:
        logging.exception("Slash sync failed: %s", e)

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