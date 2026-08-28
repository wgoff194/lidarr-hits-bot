"""
Lidarr Hits Bot — Discord bot that tracks artists and only downloads popular songs.

Commands:
    ?add <artist>       — Add an artist (interactive setup dialog)
    ?remove <artist>   — Remove an artist from the watchlist
    ?list              — Show all tracked artists
    ?update [artist]   — Update artist settings
    ?scan [artist]     — Scan artist's catalog for hits
    ?check             — Manually trigger a popularity check
    ?check-downloads   — Check pending downloads
    ?prune             — Prune below-threshold tracks
    ?keep [artist]     — Mark tracks as never-prune
    ?threshold [0-100] — Show or set popularity threshold
    ?mode [tracks|album] — Show or set download mode
    ?folder [name]     — Show or set default root folder
    ?menu              — Interactive menu
    ?import            — Import existing Lidarr artists
    ?reset confirm     — Wipe database (bot owner only)
    ?help              — Show this help message
"""

import asyncio
import logging
import signal
import sys
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands

from .bot import bot
from .config import Config
from .database import db
from .checker import run_daily_check, run_check_for_artist, prune_downloaded_albums, check_pending_downloads
from .helpers import create_thread, format_prune_results

log = logging.getLogger(__name__)


# ── Bot events are registered in main.py after bot definition (above) ────────

@bot.event
async def on_ready():
    log.info("Bot online as %s (ID: %s)", bot.user, bot.user.id)
    log.info("Database initialized at %s", Config.DB_PATH)
    log.info("Settings loaded — threshold: %d, mode: %s", Config.POPULARITY_THRESHOLD, Config.DOWNLOAD_MODE)
    
    # Sync commands
    await bot.tree.sync()
    log.info("Commands synced")
    
    # Start daily check scheduler
    if Config.DAILY_CHECK_CRON:
        log.info("Daily check scheduled: %s (%s)", Config.DAILY_CHECK_CRON, Config.TIMEZONE)
        asyncio.create_task(daily_check_loop())


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing required argument: `{error.param.name}`")
        return
    log.error("Command error: %s", error)
    await ctx.send(f"❌ Error: {error}")


# ── Thread auto-creation ───────────────────────────────────────────────────────

@bot.before_invoke
async def auto_thread(ctx: commands.Context):
    """Auto-create a thread for every command unless already in one."""
    thread = await create_thread(ctx)
    ctx._thread_channel = thread


# ── Daily check scheduler ─────────────────────────────────────────────────────

def _next_cron_run() -> datetime:
    """Calculate next cron run time."""
    from croniter import croniter
    tz = timezone(Config.TIMEZONE)
    cron = croniter(Config.DAILY_CHECK_CRON, datetime.now(tz))
    return cron.get_next(datetime)


async def daily_check_loop():
    """Background loop that runs the daily check on schedule."""
    await bot.wait_until_ready()
    
    while not bot.is_closed():
        next_run = _next_cron_run()
        delay = (next_run - datetime.now(timezone(Config.TIMEZONE))).total_seconds()
        
        if delay > 0:
            log.info("Next daily check in %.1f minutes", delay / 60)
            await asyncio.sleep(min(delay, 3600))  # Check at most every hour
        
        try:
            await before_daily_check()
        except Exception as e:
            log.error("Daily check error: %s", e)


async def before_daily_check():
    """Run before each daily check."""
    log.info("Daily check triggered by scheduler.")
    
    # Unmonitor all albums before scan
    try:
        lidarr_cl = __import__('lidarr_hits_bot.clients.lidarr', fromlist=['LidarrClient']).LidarrClient()
        artists = db.list_artists()
        for artist in artists:
            if artist.get("lidarr_id"):
                try:
                    lidarr_cl.unmonitor_all_albums(artist["lidarr_id"])
                except Exception as e:
                    log.warning("Failed to unmonitor albums for %s: %s", artist["name"], e)
        log.info("All artist albums set to unmonitored before scan")
    except Exception as e:
        log.error("Failed to unmonitor albums: %s", e)
    
    # Run the check
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, run_daily_check)
    
    # Auto-prune after daily check
    log.info("Running auto-prune after daily check...")
    prune_results = await loop.run_in_executor(None, prune_downloaded_albums)
    prune_report = format_prune_results(prune_results)
    
    if "Nothing to prune" not in prune_report:
        log.info("Prune completed: %s", prune_report.replace("\n", " | "))
    
    report = f"📊 Daily Hits Check Complete\n\n{results}"
    
    # Send to configured channel in a new thread
    channel_id = Config.REPORT_CHANNEL_ID
    if channel_id:
        channel = bot.get_channel(channel_id)
        if channel:
            try:
                # Create a new thread for this report
                today = datetime.now().strftime("%Y-%m-%d")
                thread_name = f"📊 Daily Report — {today}"
                
                message = await channel.send(report)
                thread = await message.create_thread(
                    name=thread_name,
                    auto_archive_duration=10080,  # 7 days
                )
                log.info("Created daily report thread: %s", thread_name)
            except discord.HTTPException as e:
                log.error("Failed to create report thread: %s", e)
                # Fallback: send to channel directly
                await channel.send(report)
        else:
            log.error("Report channel %s not found!", channel_id)
    else:
        log.info("No REPORT_CHANNEL_ID set, report logged only.")
    
    # Check pending downloads
    log.info("Checking pending downloads...")
    await loop.run_in_executor(None, check_pending_downloads)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    # Logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    
    # Load config
    Config.load()
    
    log.info("Starting Lidarr Hits Bot...")
    
    # Load commands and views
    from . import commands  # noqa: F401 - imports register the commands
    from . import views    # noqa: F401 - imports register the views
    
    # Handle graceful shutdown
    def shutdown_handler(signum, frame):
        log.info("Shutting down...")
        sys.exit(0)
    
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)
    
    # Run bot
    bot.run(Config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
