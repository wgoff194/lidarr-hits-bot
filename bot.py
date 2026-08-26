"""
Lidarr Hits Bot — Discord bot that tracks artists and only downloads popular songs.

Commands:
    ?add <artist>       — Add an artist to the watchlist
    ?remove <artist>    — Remove an artist from the watchlist
    ?list               — Show all tracked artists
    ?check              — Manually trigger a popularity check
    ?threshold <0-100>  — Show or set the popularity threshold
    ?help               — Show this help message
"""

import asyncio
import logging
import signal
import sys
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks
from croniter import croniter

import database as db
from checker import format_results, run_daily_check
from config import Config
from spotify_client import SpotifyClient

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("lidarr-hits-bot")

# ── Bot setup ────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix=Config.COMMAND_PREFIX,
    intents=intents,
    help_command=None,  # We'll use our own
)


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
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return  # Ignore unknown commands
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument: `{error.param.name}`. Use `?help` for usage.")
        return
    log.error("Command error: %s", error)
    await ctx.send(f"❌ Error: {error}")


# ── Commands ─────────────────────────────────────────────────────────────────

@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    """Show available commands."""
    prefix = Config.COMMAND_PREFIX
    embed = discord.Embed(
        title="🎵 Lidarr Hits Bot",
        description="Only downloads the popular stuff.",
        color=0x1DB954,  # Spotify green
    )
    embed.add_field(
        name="Commands",
        value=(
            f"`{prefix}add <artist>` — Track an artist\n"
            f"`{prefix}remove <artist>` — Stop tracking\n"
            f"`{prefix}list` — Show watchlist\n"
            f"`{prefix}check` — Run popularity check now\n"
            f"`{prefix}threshold <0-100>` — View/set popularity threshold\n"
            f"`{prefix}mode <tracks|album>` — Download popular tracks only, or whole albums\n"
            f"`{prefix}help` — This message"
        ),
        inline=False,
    )
    embed.add_field(
        name="How it works",
        value=(
            "Every day at the scheduled time, the bot checks each tracked artist "
            "on Spotify for new releases. If an album has tracks above the "
            f"popularity threshold (**{Config.POPULARITY_THRESHOLD}**/100), "
            "it gets processed.\n\n"
            "**Tracks mode** (default): Only the popular tracks are monitored in Lidarr.\n"
            "**Album mode**: The whole album is grabbed."
        ),
        inline=False,
    )
    await ctx.send(embed=embed)


@bot.command(name="add")
async def add_artist(ctx: commands.Context, *, artist_name: str):
    """Add an artist to the watchlist."""
    artist_name = artist_name.strip()
    if not artist_name:
        await ctx.send("❌ Give me an artist name. Example: `?add Linkin Park`")
        return

    # Validate that the artist exists on Spotify
    try:
        sp = SpotifyClient()
        found = sp.search_artist(artist_name)
        if not found:
            await ctx.send(f"❌ Couldn't find **{artist_name}** on Spotify. Check the spelling?")
            return
        spotify_id = found["id"]
        display_name = found["name"]
    except Exception as e:
        log.warning("Spotify lookup failed for '%s': %s", artist_name, e)
        spotify_id = None
        display_name = artist_name

    added_by = str(ctx.author)
    success = db.add_artist(display_name, added_by, spotify_id)

    if success:
        embed = discord.Embed(
            title="✅ Artist Added",
            description=f"**{display_name}** is now being tracked.",
            color=0x1DB954,
        )
        embed.set_footer(text=f"Added by {added_by}")
        await ctx.send(embed=embed)
    else:
        await ctx.send(f"⚠️ **{display_name}** is already in the watchlist.")


@bot.command(name="remove")
async def remove_artist(ctx: commands.Context, *, artist_name: str):
    """Remove an artist from the watchlist."""
    success = db.remove_artist(artist_name)
    if success:
        await ctx.send(f"🗑️ **{artist_name.strip()}** removed from the watchlist.")
    else:
        await ctx.send(f"❌ **{artist_name.strip()}** not found in the watchlist.")


@bot.command(name="list")
async def list_artists(ctx: commands.Context):
    """Show all tracked artists."""
    artists = db.list_artists()
    if not artists:
        await ctx.send("📭 Watchlist is empty. Add an artist with `?add <name>`")
        return

    embed = discord.Embed(
        title=f"🎵 Artist Watchlist ({len(artists)})",
        color=0x1DB954,
    )

    lines = []
    for a in artists:
        last = a.get("last_checked")
        if last:
            try:
                dt = datetime.fromisoformat(last)
                last_str = dt.strftime("%b %d %H:%M")
            except ValueError:
                last_str = last
        else:
            last_str = "never"
        lines.append(f"• **{a['name']}** — last checked: {last_str}")

    # Split into fields if long (Discord embed value limit is 1024 chars)
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 1024:
            embed.add_field(name="\u200b", value=chunk, inline=False)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        embed.add_field(name="\u200b", value=chunk, inline=False)

    await ctx.send(embed=embed)


@bot.command(name="check")
async def manual_check(ctx: commands.Context):
    """Manually trigger the popularity check."""
    await ctx.send("🔍 Running popularity check... this may take a minute.")

    # Run in executor to not block the bot
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, run_daily_check)
    report = format_results(results)

    # Split long messages (Discord 2000 char limit)
    while report:
        chunk = report[:1990]
        if len(report) > 1990:
            # Find last newline to split cleanly
            split_at = chunk.rfind("\n")
            if split_at > 0:
                chunk = report[:split_at]
        await ctx.send(chunk)
        report = report[len(chunk):]


@bot.command(name="threshold")
async def threshold_cmd(ctx: commands.Context, value: int = None):
    """Show or set the popularity threshold."""
    if value is None:
        await ctx.send(
            f"📊 Current popularity threshold: **{Config.POPULARITY_THRESHOLD}**/100\n"
            f"Change with `?threshold <number>` (0-100)"
        )
        return

    if not 0 <= value <= 100:
        await ctx.send("❌ Threshold must be between 0 and 100.")
        return

    # Update at runtime AND persist to database
    Config.POPULARITY_THRESHOLD = value
    db.set_setting("popularity_threshold", str(value))
    await ctx.send(f"📊 Popularity threshold set to **{value}**/100 (saved permanently).")


@bot.command(name="mode")
async def mode_cmd(ctx: commands.Context, mode: str = None):
    """Show or set the download mode (album or tracks)."""
    if mode is None:
        current = Config.DOWNLOAD_MODE
        desc = {
            "tracks": "Only downloads individual tracks above the popularity threshold",
            "album": "Downloads the entire album if it has popular tracks",
        }
        await ctx.send(
            f"🎛️ Download mode: **{current}**\n"
            f"{desc.get(current, '')}\n"
            f"Switch with `?mode tracks` or `?mode album`"
        )
        return

    mode = mode.strip().lower()
    if mode not in ("tracks", "album"):
        await ctx.send("❌ Mode must be `tracks` or `album`.")
        return

    Config.DOWNLOAD_MODE = mode
    db.set_setting("download_mode", mode)
    if mode == "tracks":
        await ctx.send(
            "🎛️ Mode set to **tracks** — only popular tracks (above threshold) will be downloaded. "
            "(saved permanently)"
        )
    else:
        await ctx.send(
            "🎛️ Mode set to **album** — full albums with popular tracks will be downloaded. "
            "(saved permanently)"
        )


# ── Daily check scheduler ────────────────────────────────────────────────────

def _next_cron_run() -> datetime:
    """Calculate the next run time from the cron expression."""
    cron = croniter(Config.DAILY_CHECK_CRON, datetime.now(timezone.utc))
    return cron.get_next(datetime)


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

    # Send to configured channel
    channel_id = Config.REPORT_CHANNEL_ID
    if channel_id:
        channel = bot.get_channel(channel_id)
        if channel:
            while report:
                chunk = report[:1990]
                if len(report) > 1990:
                    split_at = chunk.rfind("\n")
                    if split_at > 0:
                        chunk = report[:split_at]
                await channel.send(chunk)
                report = report[len(chunk):]
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

    # Graceful shutdown
    def shutdown(sig, frame):
        log.info("Shutting down (signal %s)...", sig)
        asyncio.get_event_loop().stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    bot.run(Config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
