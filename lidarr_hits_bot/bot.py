"""
Bot instance for Lidarr Hits Bot.
Imported by main.py (for events) and commands.py (for @bot.command decorators).
"""

import discord
from discord.ext import commands


intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="?", intents=intents)
