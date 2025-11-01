import os
import sys
import signal
import logging

import discord
from discord.ext import commands

# ---- hard fail if you forgot the token ----
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN not set")

# ---- minimal logging so you can see what's happening on Railway ----
logging.basicConfig(level=logging.INFO)

# ---- intents (Message Content must also be enabled in the Discord Dev Portal) ----
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    logging.info("✅ Logged in as %s (id=%s)", bot.user, bot.user.id)
    # optional: sync slash commands
    try:
        synced = await bot.tree.sync()
        logging.info("Slash commands synced: %d", len(synced))
    except Exception as e:
        logging.exception("Slash sync failed: %s", e)

# ---- prefix command ----
@bot.command()
async def ping(ctx):
    await ctx.send("pong")

# ---- slash command (mirror of !ping) ----
@bot.tree.command(name="ping", description="Replies with pong")
async def ping_slash(interaction: discord.Interaction):
    await interaction.response.send_message("pong")

# ---- graceful shutdown (Railway sends SIGTERM on redeploy) ----
def _shutdown(*_):
    logging.info("Shutting down...")
    try:
        # Close the Discord connection cleanly
        bot.loop.create_task(bot.close())
    finally:
        sys.exit(0)

signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)

bot.run(TOKEN)