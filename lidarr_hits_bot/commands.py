"""All @bot.command decorated functions.

Imports:
- bot from .bot (for @bot.command and ctx.invoke)
- database as db from .database
- views from .views (all interactive View/Modal classes)
- helpers from .helpers (fuzzy match, _sync_to_lidarr, _run_import)
- checker functions from .checker
- config from .config
"""

import asyncio
import logging
from datetime import datetime

import discord
from discord.ext import commands

from lidarr_hits_bot import database as db
from lidarr_hits_bot.bot import bot
from lidarr_hits_bot.checker import (
    check_downloads,
    format_download_check_results,
    format_prune_results,
    format_results,
    prune_downloaded_albums,
    run_daily_check,
)
from lidarr_hits_bot.clients.deezer import MusicClient
from lidarr_hits_bot.config import Config
from lidarr_hits_bot.helpers import (
    _run_import,
    _sync_to_lidarr,
    fuzzy_find_artist,
)
from lidarr_hits_bot.views import (
    AddArtistView,
    AddFuzzyPickerView,
    KeepAlbumView,
    KeepArtistView,
    KeepTrackView,
    MenuView,
    PruneArtistView,
    ScanArtistView,
    ScanConfirmView,
    UpdatePickerView,
)

log = logging.getLogger(__name__)


# ── Help ──────────────────────────────────────────────────────────────────────

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
            f"`{prefix}add <artist>` — Add artist (interactive setup dialog)\n"
            f"`{prefix}import` — Import existing Lidarr artists into watchlist\n"
            f"`{prefix}update` — Update artist settings (folder, mode, metadata)\n"
            f"`{prefix}remove <artist>` — Stop tracking\n"
            f"`{prefix}list` — Show watchlist\n"
            f"`{prefix}check` — Run popularity check (recent releases)\n"
            f"`{prefix}scan` — Full catalog scan (pick artist or all)\n"
            f"`{prefix}prune` — Delete below-threshold tracks from downloaded albums\n"
            f"`{prefix}check-downloads` — Check pending downloads, auto-prune completed\n"
            f"`{prefix}keep` — Mark tracks as never-prune (nested menu)\n"
            f"`{prefix}threshold <0-100>` — View/set popularity threshold\n"
            f"`{prefix}mode <tracks|album>` — Download popular tracks only, or whole albums\n"
            f"`{prefix}folder` — Show/set root folders\n"
            f"`{prefix}help` — This message\n"
            f"`{prefix}menu` — Interactive menu with buttons"
        ),
        inline=False,
    )
    embed.add_field(
        name="How it works",
        value=(
            "Every day at the scheduled time, the bot checks each tracked artist "
            "on Deezer for new releases. If an album has tracks above the "
            f"popularity threshold (**{Config.POPULARITY_THRESHOLD}**/100), "
            "it gets processed.\n\n"
            "**Tracks mode** (default): Only the popular tracks are monitored in Lidarr.\n"
            "**Album mode**: The whole album is grabbed."
        ),
        inline=False,
    )
    await ctx.send(embed=embed)


# ── Add ───────────────────────────────────────────────────────────────────────

@bot.command(name="add")
async def add_cmd(ctx: commands.Context, *, artist_name: str = None):
    """Add an artist with an interactive setup dialog."""
    if not artist_name:
        await ctx.send("❌ Give me an artist name. Example: `?add Linkin Park`")
        return

    artist_name = artist_name.strip()

    # Search on Deezer
    artist_data = None
    music_id = None
    display_name = artist_name
    try:
        sp = MusicClient()
        found = sp.search_artist(artist_name)
        if not found:
            # Fuzzy search — show picker
            results = sp.search_artists(artist_name)
            if not results:
                await ctx.send(f"❌ Couldn't find **{artist_name}** on Deezer. Check the spelling?")
                return

            # Check if exact match in results
            exact = None
            for r in results:
                if r["name"].lower() == artist_name.lower():
                    exact = r
                    break
            if exact:
                found = exact
            else:
                # Show picker
                picker = AddFuzzyPickerView(ctx.author.id, results)
                embed = discord.Embed(
                    title=f"🔍 Results for: {artist_name}",
                    description="Pick the artist you meant:",
                    color=0x1DB954,
                )
                await ctx.send(embed=embed, view=picker)
                await picker.wait()
                if not picker.selected:
                    return
                found = picker.selected

        music_id = found["id"]
        display_name = found["name"]
        artist_data = found
    except Exception as e:
        log.warning("Music lookup failed for '%s': %s", artist_name, e)

    # Check if already in watchlist
    existing = db.get_artist(display_name)
    if existing:
        await ctx.send(f"⚠️ **{display_name}** is already in the watchlist.")
        return

    # Fetch Lidarr root folders and metadata profiles
    folders = []
    metadata_profiles = []
    lidarr = None
    try:
        from lidarr_hits_bot.clients.lidarr import LidarrClient
        lidarr = LidarrClient()
        folders = lidarr.get_root_folders()
        metadata_profiles = lidarr.get_metadata_profiles()
    except Exception as e:
        log.warning("Could not fetch Lidarr data: %s", e)

    # Launch the interactive view
    view = AddArtistView(
        author_id=ctx.author.id,
        display_name=display_name,
        music_id=music_id,
        artist_data=artist_data,
        folders=folders,
        metadata_profiles=metadata_profiles,
        lidarr_client=lidarr,
    )

    await ctx.send(embed=view.build_embed(), view=view)

    # Wait for the user to confirm or cancel
    await view.wait()

    if view.cancelled or not view.confirmed:
        return

    # ── User confirmed — save to database ────────────────────────────────
    added_by = str(ctx.author)
    success = db.add_artist(
        display_name, added_by, music_id,
        root_folder=view.selected_folder,
    )

    if not success:
        await ctx.send(f"⚠️ **{display_name}** is already in the watchlist.")
        return

    # Persist threshold/mode if changed from defaults
    if view.threshold != Config.POPULARITY_THRESHOLD:
        Config.POPULARITY_THRESHOLD = view.threshold
        db.set_setting("popularity_threshold", str(view.threshold))

    if view.selected_mode != Config.DOWNLOAD_MODE:
        Config.DOWNLOAD_MODE = view.selected_mode
        db.set_setting("download_mode", view.selected_mode)

    # Store metadata profile and mode for this artist
    if view.selected_metadata_profile:
        db.set_setting(f"meta_profile_{display_name}", str(view.selected_metadata_profile))
    db.set_setting(f"mode_{display_name}", view.selected_mode)

    # Confirmation message
    folder_display = "(default)"
    if view.selected_folder:
        folder_display = view.selected_folder.rstrip("/").split("/")[-1]

    meta_display = "auto"
    if view.selected_metadata_profile and metadata_profiles:
        for p in metadata_profiles:
            if p["id"] == view.selected_metadata_profile:
                meta_display = p["name"]
                break

    embed = discord.Embed(
        title="✅ Artist Added",
        description=(
            f"**{display_name}** is now being tracked.\n\n"
            f"📁 **Folder:** {folder_display}\n"
            f"🎛️ **Mode:** {view.selected_mode}\n"
            f"📊 **Threshold:** {view.threshold}/100\n"
            f"📀 **Metadata:** {meta_display}"
        ),
        color=0x1DB954,
    )
    embed.set_footer(text=f"Added by {added_by}")
    await ctx.send(embed=embed)

    # ── Auto-check this artist for popular tracks (full catalog scan) ───
    await ctx.send(f"🔍 Scanning **{display_name}**'s full catalog for hits...")
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, run_daily_check, display_name, True)
    report = format_results(results)
    await ctx.send(report)


# ── Remove ────────────────────────────────────────────────────────────────────

@bot.command(name="remove")
async def remove_artist(ctx: commands.Context, *, artist_name: str):
    """Remove an artist from the watchlist."""
    success = db.remove_artist(artist_name)
    if success:
        await ctx.send(f"🗑️ **{artist_name.strip()}** removed from the watchlist.")
    else:
        await ctx.send(f"❌ **{artist_name.strip()}** not found in the watchlist.")


# ── Update ────────────────────────────────────────────────────────────────────

@bot.command(name="update")
async def update_cmd(ctx: commands.Context, *, artist_name: str = None):
    """Update an artist's settings — folder, mode, threshold, metadata profile."""
    artists = db.list_artists()
    if not artists:
        await ctx.send("📭 Watchlist is empty.")
        return

    # Step 1: Pick artist (fuzzy from arg, or dropdown)
    if artist_name:
        artist = fuzzy_find_artist(artists, artist_name)
        if not artist:
            await ctx.send(f"❌ No artist matching **{artist_name}** in watchlist.")
            return
    else:
        picker = UpdatePickerView(ctx.author.id, artists)
        await ctx.send(embed=discord.Embed(
            title="✏️ Update Artist",
            description="Pick an artist to update their settings.",
            color=0x1DB954,
        ), view=picker)
        await picker.wait()
        if not picker.selected:
            return
        artist = db.get_artist(picker.selected)
        if not artist:
            await ctx.send(f"❌ **{picker.selected}** not found.")
            return

    # Step 2: Fetch Lidarr data
    folders = []
    metadata_profiles = []
    lidarr = None
    try:
        from lidarr_hits_bot.clients.lidarr import LidarrClient
        lidarr = LidarrClient()
        folders = lidarr.get_root_folders()
        metadata_profiles = lidarr.get_metadata_profiles()
    except Exception as e:
        log.warning("Could not fetch Lidarr data: %s", e)

    # Get current settings
    current_folder = artist.get("root_folder") or db.get_setting("default_root_folder") or ""
    current_mode = db.get_setting(f"mode_{artist['name']}") or Config.DOWNLOAD_MODE
    current_threshold = Config.POPULARITY_THRESHOLD
    current_meta = db.get_setting(f"meta_profile_{artist['name']}")
    if current_meta:
        current_meta = int(current_meta)

    # Step 3: Show the same dialog as ?add, pre-populated
    view = AddArtistView(
        author_id=ctx.author.id,
        display_name=artist["name"],
        music_id=artist.get("spotify_id"),
        artist_data=None,
        folders=folders,
        metadata_profiles=metadata_profiles,
        lidarr_client=lidarr,
        button_label="✅ Update Artist",
    )

    # Pre-populate current values
    if current_folder:
        view.selected_folder = current_folder
        for opt in view.folder_select.options:
            opt.default = opt.value == current_folder
    view.selected_mode = current_mode
    for opt in view.mode_select.options:
        opt.default = opt.value == current_mode
    view.threshold = current_threshold
    if current_meta:
        view.selected_metadata_profile = current_meta
        for opt in view.metadata_profile_select.options:
            opt.default = opt.value == str(current_meta)

    await ctx.send(embed=view.build_embed(), view=view)
    await view.wait()

    if view.cancelled or not view.confirmed:
        return

    # Step 4: Apply changes
    changes = []

    # ── Apply changes to Lidarr ──────────────────────────────────────────
    lidarr_id = artist.get("lidarr_id")
    lidarr_changed = False

    # Folder change
    new_folder = view.selected_folder
    if new_folder and new_folder != current_folder:
        db.set_artist_root_folder(artist["name"], new_folder)
        folder_display = new_folder.rstrip("/").split("/")[-1]
        changes.append(f"📁 Folder → {folder_display}")
        lidarr_changed = True

    # Mode change (bot-only, no Lidarr sync needed)
    if view.selected_mode != current_mode:
        db.set_setting(f"mode_{artist['name']}", view.selected_mode)
        changes.append(f"🎛️ Mode → {view.selected_mode}")

    # Threshold change (bot-only)
    if view.threshold != current_threshold:
        Config.POPULARITY_THRESHOLD = view.threshold
        db.set_setting("popularity_threshold", str(view.threshold))
        changes.append(f"📊 Threshold → {view.threshold}/100")

    # Metadata profile change
    if view.selected_metadata_profile and view.selected_metadata_profile != current_meta:
        db.set_setting(f"meta_profile_{artist['name']}", str(view.selected_metadata_profile))
        meta_display = str(view.selected_metadata_profile)
        for p in metadata_profiles:
            if p["id"] == view.selected_metadata_profile:
                meta_display = p["name"]
                break
        changes.append(f"📀 Metadata → {meta_display}")
        lidarr_changed = True

    # Sync all changes to Lidarr in a thread
    if lidarr_changed and lidarr_id and lidarr:
        loop = asyncio.get_event_loop()
        sync_result = await loop.run_in_executor(
            None, _sync_to_lidarr, lidarr, lidarr_id, artist,
            view.selected_metadata_profile, current_meta, new_folder, current_folder
        )
        changes.extend(sync_result)

    if not changes:
        await ctx.send(f"No changes made to **{artist['name']}**.")
    else:
        embed = discord.Embed(
            title=f"✏️ Updated: {artist['name']}",
            description="\n".join(changes),
            color=0x1DB954,
        )
        await ctx.send(embed=embed)


# ── List ──────────────────────────────────────────────────────────────────────

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
        folder = a.get("root_folder")
        folder_str = ""
        if folder:
            folder_display = folder.rstrip("/").split("/")[-1]
            folder_str = f" 📁 {folder_display}"
        lines.append(f"• **{a['name']}** — last checked: {last_str}{folder_str}")

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


# ── Check (popularity) ───────────────────────────────────────────────────────

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


# ── Scan ──────────────────────────────────────────────────────────────────────

@bot.command(name="scan")
async def scan_cmd(ctx: commands.Context, *, artist_name: str = None):
    """Full catalog scan — pick an artist or scan all."""
    artists = db.list_artists()
    if not artists:
        await ctx.send("📭 Watchlist is empty. Add an artist with `?add <name>` first.")
        return

    # Fuzzy match from argument
    if artist_name:
        artist = fuzzy_find_artist(artists, artist_name)
        if not artist:
            await ctx.send(f"❌ No artist matching **{artist_name}** in watchlist.")
            return
        await ctx.send(f"🔍 Scanning **{artist['name']}**'s full catalog for hits...")
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, run_daily_check, artist["name"], True)
        report = format_results(results)
        await ctx.send(report)
        return

    # No argument — show dropdown
    view = ScanArtistView(ctx.author.id, artists)
    embed = discord.Embed(
        title="🔍 Full Catalog Scan",
        description=(
            f"Pick an artist to scan their **entire catalog** for hits.\n"
            f"Or select **All Artists** to scan everyone.\n\n"
            f"Currently tracking **{len(artists)}** artist(s)."
        ),
        color=0x1DB954,
    )
    embed.set_footer(text="Select an artist from the dropdown • Times out in 2 min")
    await ctx.send(embed=embed, view=view)
    await view.wait()

    if not view.selected:
        return

    if view.selected == "__all__":
        # Send confirmation warning
        confirm_view = ScanConfirmView(ctx.author.id)
        confirm_embed = discord.Embed(
            title="⚠️ Scan ALL Artists?",
            description=(
                f"You're about to scan **{len(artists)} artists**' full catalogs.\n\n"
                "This checks every artist's top tracks and queues popular albums.\n"
                "**May take several minutes and trigger many downloads.**\n\n"
                "Are you sure?"
            ),
            color=0xFFA500,
        )
        await ctx.send(embed=confirm_embed, view=confirm_view)
        await confirm_view.wait()

        if not confirm_view.confirmed:
            return

        await ctx.send(f"🔍 Scanning **{len(artists)} artists**... this will take a while.")
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, run_daily_check, None, True)
    else:
        if not view.confirmed:
            return
        await ctx.send(f"🔍 Scanning **{view.selected}**'s full catalog for hits...")
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, run_daily_check, view.selected, True)

    report = format_results(results)
    while report:
        chunk = report[:1990]
        if len(report) > 1990:
            split_at = chunk.rfind("\n")
            if split_at > 0:
                chunk = report[:split_at]
        await ctx.send(chunk)
        report = report[len(chunk):]


# ── Prune ─────────────────────────────────────────────────────────────────────

@bot.command(name="prune")
async def prune_cmd(ctx: commands.Context, *, artist_name: str = None):
    """Prune downloaded albums — pick an artist or prune all."""
    artists = db.list_artists()
    if not artists:
        await ctx.send("📭 Watchlist is empty. Add an artist with `?add <name>` first.")
        return

    # Fuzzy match from argument
    if artist_name:
        artist = fuzzy_find_artist(artists, artist_name)
        if not artist:
            await ctx.send(f"❌ No artist matching **{artist_name}** in watchlist.")
            return
        await ctx.send(f"✂️ Pruning **{artist['name']}**'s downloaded albums...")
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, prune_downloaded_albums, artist["name"], True)
        report = format_prune_results(results)
        await ctx.send(report)
        return

    # No argument — show dropdown
    view = PruneArtistView(ctx.author.id, artists)
    embed = discord.Embed(
        title="✂️ Prune Downloaded Albums",
        description=(
            f"Pick an artist to prune their downloaded albums.\n"
            f"Deletes below-threshold tracks and unmonitors albums.\n\n"
            f"Currently tracking **{len(artists)}** artist(s)."
        ),
        color=0x1DB954,
    )
    embed.set_footer(text="Select an artist from the dropdown • Times out in 2 min")
    await ctx.send(embed=embed, view=view)
    await view.wait()

    if not view.selected:
        return

    if view.selected == "__all__":
        # Confirmation warning
        confirm_view = ScanConfirmView(ctx.author.id)
        confirm_embed = discord.Embed(
            title="⚠️ Prune ALL Artists?",
            description=(
                f"You're about to prune **{len(artists)} artists**' downloaded albums.\n\n"
                "This deletes below-threshold tracks from disk and unmonitors albums.\n"
                "**This cannot be undone.**\n\n"
                "Are you sure?"
            ),
            color=0xFFA500,
        )
        await ctx.send(embed=confirm_embed, view=confirm_view)
        await confirm_view.wait()

        if not confirm_view.confirmed:
            return

        await ctx.send(f"✂️ Pruning **{len(artists)} artists**... this may take a while.")
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, prune_downloaded_albums, None, True)
    else:
        if not view.confirmed:
            return
        await ctx.send(f"✂️ Pruning **{view.selected}**'s downloaded albums...")
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, prune_downloaded_albums, view.selected, True)

    report = format_prune_results(results)
    await ctx.send(report)


# ── Check Downloads ──────────────────────────────────────────────────────────

@bot.command(name="check-downloads", aliases=["dl"])
async def check_downloads_cmd(ctx: commands.Context):
    """Check pending downloads and auto-prune completed ones."""
    await ctx.send("📥 Checking pending downloads...")
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, check_downloads)
    report = format_download_check_results(results)
    await ctx.send(report)


# ── Import ───────────────────────────────────────────────────────────────────

@bot.command(name="import")
async def import_cmd(ctx: commands.Context):
    """Import existing Lidarr artists into the bot watchlist."""
    await ctx.send("📥 Importing artists from Lidarr...")

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _run_import)

    embed = discord.Embed(title="📥 Import Complete", color=0x1DB954)
    if result["added"]:
        embed.add_field(name=f"Added ({len(result['added'])})",
                        value="\n".join(result["added"][:20]), inline=False)
        if len(result["added"]) > 20:
            embed.set_footer(text=f"... and {len(result['added']) - 20} more")
    if result["skipped"]:
        embed.add_field(name=f"Skipped ({len(result['skipped'])})",
                        value=f"{len(result['skipped'])} already in watchlist", inline=False)
    if result["errors"]:
        embed.add_field(name=f"Errors ({len(result['errors'])})",
                        value="\n".join(result["errors"][:5]), inline=False)
    if not result["added"] and not result["skipped"]:
        embed.description = "No artists found in Lidarr."
    await ctx.send(embed=embed)


# ── Threshold ────────────────────────────────────────────────────────────────

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


# ── Mode ──────────────────────────────────────────────────────────────────────

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


# ── Folder ────────────────────────────────────────────────────────────────────

@bot.command(name="folder")
async def folder_cmd(ctx: commands.Context, *, folder_name: str = None):
    """Show available root folders or set the default. Per-artist: ?folder set <artist> to <folder>"""
    try:
        from lidarr_hits_bot.clients.lidarr import LidarrClient
        lidarr = LidarrClient()
        folders = lidarr.get_root_folders()
    except Exception as e:
        await ctx.send(f"❌ Couldn't connect to Lidarr: {e}")
        return

    if not folders:
        await ctx.send("❌ No root folders found in Lidarr.")
        return

    # Get current default
    default_folder = db.get_setting("default_root_folder")
    default_display = default_folder.rstrip("/").split("/")[-1] if default_folder else "(using .env)"

    if folder_name is None:
        # Show all folders + current default
        embed = discord.Embed(
            title="📁 Lidarr Root Folders",
            description=f"Default: **{default_display}**",
            color=0x1DB954,
        )
        lines = []
        for f in folders:
            marker = " ⬅️ default" if default_folder and f["path"] == default_folder else ""
            lines.append(f"• **{f['name']}** `{f['path']}`{marker}")
        embed.add_field(name="Available Folders", value="\n".join(lines), inline=False)
        embed.add_field(
            name="Usage",
            value=(
                "`?folder <name>` — set default folder\n"
                "`?add <artist> to <folder>` — per-artist folder\n"
                "`?folder set <artist> to <folder>` — change an existing artist's folder"
            ),
            inline=False,
        )
        await ctx.send(embed=embed)
        return

    # Handle "set <artist> to <folder>" syntax
    if folder_name.lower().startswith("set "):
        rest = folder_name[4:].strip()
        if " to " in rest.lower():
            parts = rest.rsplit(" to ", 1)
            artist_name = parts[0].strip()
            folder_input = parts[1].strip()
            resolved = lidarr.resolve_root_folder(folder_input)
            if not resolved:
                await ctx.send(
                    f"❌ Folder **{folder_input}** not found. Use `?folder` to see available folders."
                )
                return
            artist = db.get_artist(artist_name)
            if not artist:
                await ctx.send(f"❌ **{artist_name}** not in watchlist.")
                return
            db.set_artist_root_folder(artist_name, resolved)
            folder_display = resolved.rstrip("/").split("/")[-1]
            await ctx.send(f"📁 **{artist_name}** will now download to **{folder_display}**.")
            return
        else:
            await ctx.send("❌ Syntax: `?folder set <artist> to <folder>`")
            return

    # Set default folder
    resolved = lidarr.resolve_root_folder(folder_name)
    if not resolved:
        await ctx.send(
            f"❌ Folder **{folder_name}** not found. Use `?folder` to see available folders."
        )
        return

    db.set_setting("default_root_folder", resolved)
    folder_display = resolved.rstrip("/").split("/")[-1]
    await ctx.send(f"📁 Default root folder set to **{folder_display}** (saved permanently).")


# ── Menu ──────────────────────────────────────────────────────────────────────

@bot.command(name="menu")
async def menu_cmd(ctx: commands.Context):
    """Show interactive menu with buttons for all commands."""
    embed = discord.Embed(
        title="🎵 Lidarr Hits Bot — Menu",
        description="Click a button to run a command.\nCommands with arguments will prompt for input.",
        color=0x1DB954,
    )
    embed.add_field(name="➕ Add", value="Add a new artist", inline=True)
    embed.add_field(name="✏️ Update", value="Update artist settings", inline=True)
    embed.add_field(name="🔍 Scan", value="Full catalog scan", inline=True)
    embed.add_field(name="✂️ Prune", value="Prune downloaded albums", inline=True)
    embed.add_field(name="📥 Import", value="Import from Lidarr", inline=True)
    embed.add_field(name="📥 DL Check", value="Check pending downloads", inline=True)
    embed.add_field(name="📋 List", value="Show watchlist", inline=True)
    embed.add_field(name="📊 Check", value="Quick popularity check", inline=True)
    embed.set_footer(text="Times out in 2 min")

    view = MenuView(ctx.author.id)
    await ctx.send(embed=embed, view=view)
    await view.wait()

    if not view.selected_command:
        return

    # Execute the selected command
    if view.search_value:
        # Command needs an argument — invoke with it
        cmd = bot.get_command(view.selected_command)
        if cmd:
            await ctx.invoke(cmd, artist_name=view.search_value)
    else:
        # No argument needed — invoke directly
        cmd = bot.get_command(view.selected_command)
        if cmd:
            await ctx.invoke(cmd)


# ── Keep (Never Prune) ───────────────────────────────────────────────────────

@bot.command(name="keep")
async def keep_cmd(ctx: commands.Context, artist_name: str = None):
    """Mark tracks as never-prune. Nested menu: artist → album → tracks.

    Usage:
    ?keep                          # Interactive menu
    ?keep Linkin Park              # Auto-select Linkin Park artist
    """
    artists = db.list_artists()
    if not artists:
        await ctx.send("📭 Watchlist is empty.")
        return

    # If artist name provided, try to find and auto-select it
    selected_artist = None
    if artist_name:
        # Fuzzy match against artist names
        query = artist_name.lower().strip()
        matched = None
        for a in artists:
            a_name = (a.get("name") or "").lower()
            # Exact match first
            if query == a_name:
                matched = a
                break
            # Substring match
            if query in a_name:
                matched = a
                break
            # Fuzzy: first word match
            if query and a_name.startswith(query):
                matched = a
                break
        if matched:
            selected_artist = matched
            log.info(f"keep_cmd: Auto-selected artist '{matched['name']}' from argument '{artist_name}'")
        else:
            await ctx.send(f"❌ Artist **{artist_name}** not found in watchlist.\nAvailable: {', '.join(a.get('name', 'Unknown') for a in artists[:5])}...")
            return
    else:
        # Interactive mode - show the view
        artist_view = KeepArtistView(ctx.author.id, artists)
        await ctx.send(embed=discord.Embed(
            title="🔒 Never Prune — Step 1: Pick Artist",
            description="Select an artist to protect tracks from pruning.",
            color=0x1DB954,
        ), view=artist_view)
        await artist_view.wait()

        if not artist_view.selected_artist:
            return
        selected_artist = artist_view.selected_artist

    artist = selected_artist
    lidarr_id = artist.get("lidarr_id")

    if not lidarr_id:
        await ctx.send(f"❌ **{artist['name']}** not in Lidarr yet.")
        return

    # Step 2: Pick album (even if interactive, we now auto-have the artist)
    try:
        from lidarr_hits_bot.clients.lidarr import LidarrClient
        lidarr = LidarrClient()
        loop = asyncio.get_event_loop()
        albums = await loop.run_in_executor(None, lidarr.get_artist_albums, lidarr_id)
    except Exception as e:
        await ctx.send(f"❌ Lidarr error: {e}")
        return

    if not albums:
        await ctx.send(f"❌ No albums found for **{artist['name']}** in Lidarr.")
        return

    # Track the picked album (view or direct)
    album_name = None
    album = None

    if len(albums) == 1:
        album = albums[0]
        album_name = album.get("title", "Unknown")
        log.info(f"keep_cmd: Auto-selected 1 album: {album_name}")
    else:
        album_view = KeepAlbumView(ctx.author.id, albums)
        await ctx.send(embed=discord.Embed(
            title=f"🔒 Never Prune — Step 2: Pick Album ({artist['name']})",
            description="Select an album to protect tracks from.",
            color=0x1DB954,
        ), view=album_view)
        await album_view.wait()

        if not album_view.selected_album:
            return
        album = album_view.selected_album
        album_name = album.get("title", "Unknown")

    # Step 3: Pick tracks
    try:
        from lidarr_hits_bot.clients.lidarr import LidarrClient
        lidarr = LidarrClient()
        tracks = await loop.run_in_executor(None, lidarr.get_album_tracks, album["id"])
    except Exception:
        tracks = []

    if not tracks:
        await ctx.send(f"❌ No tracks found for **{album_name}**.")
        return

    already_protected = db.get_never_prune_tracks(artist["id"], album_name)
    track_view = KeepTrackView(ctx.author.id, tracks, already_protected)
    await ctx.send(embed=discord.Embed(
        title=f"🔒 Never Prune — Step 3: Pick Tracks ({album_name})",
        description=(
            f"Select tracks to protect from pruning.\n"
            f"🔒 = already protected\n\n"
            f"**Mark All** = keep entire album"
        ),
        color=0x1DB954,
    ), view=track_view)
    await track_view.wait()

    if not track_view.selected_track_ids:
        return

    # Save to database
    selected_names = []
    for t in tracks:
        if str(t["id"]) in track_view.selected_track_ids:
            selected_names.append(t.get("title", "Unknown"))

    if track_view.mark_all:
        # Clear existing and add all
        db.clear_album_never_prune(artist["id"], album_name)
        db.add_album_never_prune(artist["id"], album_name, selected_names)
    else:
        # Add selected tracks
        for name in selected_names:
            db.add_never_prune(artist["id"], album_name, name)

    # Confirmation
    embed = discord.Embed(
        title="🔒 Tracks Protected",
        description=f"**{artist['name']}** — {album_name}",
        color=0x1DB954,
    )
    embed.add_field(
        name=f"Protected ({len(selected_names)})",
        value="\n".join(f"• {n}" for n in selected_names[:20]),
        inline=False,
    )
    await ctx.send(embed=embed)
