"""
Lidarr Hits Bot — Discord bot that tracks artists and only downloads popular songs.

Entry point. Sets up logging, registers events, schedules the daily check,
and starts the bot. Heavy lifting lives in:
    - bot.py       — the Bot instance
    - commands.py  — all @bot.command functions
    - views.py     — all discord.ui.View and Modal classes
    - helpers.py   — shared standalone helpers
"""

import asyncio
import logging
import signal
import sys
from datetime import datetime, timezone

import discord
from croniter import croniter
from discord.ext import commands, tasks

from lidarr_hits_bot import database as db
from lidarr_hits_bot.bot import bot
from lidarr_hits_bot.checker import (
    check_downloads,
    format_download_check_results,
    format_results,
    run_daily_check,
)
from lidarr_hits_bot.config import Config

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("lidarr-hits-bot")


# ── Events ───────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    log.info("Bot online as %s (ID: %s)", bot.user, bot.user.id)
    db.init_db()
    log.info("Database initialized at %s", Config.DB_PATH)

    # Load persisted settings (threshold, mode) from DB
    db.load_settings()
    log.info("Settings loaded — threshold: %d, mode: %s", Config.POPULARITY_THRESHOLD, Config.DOWNLOAD_MODE)

    # Start the daily check scheduler
    if not daily_check_loop.is_running():
        daily_check_loop.start()
        log.info("Daily check scheduled: %s (%s)", Config.DAILY_CHECK_CRON, Config.TIMEZONE)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return  # Ignore unknown commands
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument: `{error.param.name}`. Use `?help` for usage.")
        return
    log.error("Command error: %s", error)
    await ctx.send(f"❌ Error: {error}")


@bot.before_invoke
async def auto_thread(ctx):
    """Auto-create a thread for every command unless already in one."""
    try:
        if isinstance(ctx.channel, discord.Thread):
            ctx._thread_channel = ctx.channel
            return

        thread_name = ctx.command.name
        thread = await ctx.message.create_thread(
            name=thread_name[:100],
            auto_archive_duration=10080,
        )
        ctx._thread_channel = thread
        log.info("Created thread '%s' for command '%s'", thread_name, ctx.command.name)

        # Route ctx.send to the thread
        async def send_to_thread(*args, **kwargs):
            return await thread.send(*args, **kwargs)
        ctx.send = send_to_thread

    except Exception as e:
        log.warning("Failed to create thread for '%s': %s", ctx.command.name, e)
        ctx._thread_channel = ctx.channel


# ── Daily check scheduler ────────────────────────────────────────────────────

@tasks.loop(hours=1)
async def daily_check_loop():
    """
    Runs every hour but only executes the check when the cron expression matches.
    This avoids the complexity of APScheduler while staying cron-compatible.
    """
    now = datetime.now(timezone.utc)
    cron = croniter(Config.DAILY_CHECK_CRON, now)

    # Get the previous cron fire time
    prev_fire = cron.get_prev(datetime)

    # Only run if we're within 1 hour of the last fire time
    if (now - prev_fire).total_seconds() > 3600:
        return

    log.info("Daily check triggered by scheduler.")
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, run_daily_check)
    report = format_results(results)

    # Send to configured channel in a new thread
    channel_id = Config.REPORT_CHANNEL_ID
    if channel_id:
        channel = bot.get_channel(channel_id)
        if channel:
            # Create a thread for today's report
            today = datetime.now().strftime("%Y-%m-%d")
            thread_name = f"📊 Daily Report — {today}"
            try:
                thread = await channel.create_thread(
                    name=thread_name,
                    auto_archive_duration=10080,  # 7 days
                )
                report_channel = thread
                log.info("Created daily report thread: %s", thread_name)
            except discord.HTTPException as e:
                log.warning("Failed to create thread, falling back to channel: %s", e)
                report_channel = channel

            while report:
                chunk = report[:1990]
                if len(report) > 1990:
                    split_at = chunk.rfind("\n")
                    if split_at > 0:
                        chunk = report[:split_at]
                await report_channel.send(chunk)
                report = report[len(chunk):]

            # Check pending downloads and auto-prune completed ones
            log.info("Checking pending downloads...")
            dl_results = await loop.run_in_executor(None, check_downloads)
            dl_report = format_download_check_results(dl_results)
            if "No newly" not in dl_report:
                await report_channel.send(dl_report)
        else:
            log.error("Report channel %s not found!", channel_id)
    else:
        log.info("No REPORT_CHANNEL_ID set, report logged only.")


@daily_check_loop.before_loop
async def before_daily_check():
    await bot.wait_until_ready()


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not Config.DISCORD_TOKEN:
        log.error("DISCORD_TOKEN not set! Check your .env file.")
        sys.exit(1)

    # Register all @bot.command functions from commands.py
    # (Importing the module triggers the @bot.command decorators.)
    from lidarr_hits_bot import commands as _commands  # noqa: F401
    from lidarr_hits_bot import views as _views  # noqa: F401

    # Graceful shutdown
    def shutdown(sig, frame):
        log.info("Shutting down (signal %s)...", sig)
        try:
            asyncio.get_event_loop().stop()
        except RuntimeError:
            pass

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    bot.run(Config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
