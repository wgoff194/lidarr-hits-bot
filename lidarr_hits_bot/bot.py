"""Discord bot instance — intents and commands.Bot only.

Kept deliberately small to break the circular import chain:
- bot.py defines `bot` with no other dependencies.
- commands.py imports `bot` from here (for `@bot.command`).
- views.py imports `bot` from here (only if needed for interactions).
- helpers.py does NOT import from here.
"""

import discord
from discord.ext import commands

from lidarr_hits_bot.config import Config

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix=Config.COMMAND_PREFIX,
    intents=intents,
    help_command=None,  # We use our own ?help command
)
