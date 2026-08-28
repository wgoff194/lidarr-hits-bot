"""
Command handlers for Lidarr Hits Bot.
All bot command implementations are here.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

import discord
from discord.ext import commands

from .bot import bot
from .database import db
from .config import Config
from .clients.lidarr import LidarrClient
from .helpers import _opt, _sync_to_lidarr, create_thread, format_prune_results, fuzzy_find_artist
from .views import (
    AddArtistView, AddFuzzyPickerView, KeepArtistView, KeepAlbumView, KeepTrackView,
    MenuView, PruneArtistView, ScanArtistView, ScanConfirmView, ThresholdModal, UpdatePickerView
)

log = logging.getLogger(__name__)


# ── Help ────────────────────────────────────────────────────────────────────

@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    """Show help message with all available commands."""
    embed = discord.Embed(
        title="🎵 Lidarr Hits Bot — Help",
        description="Track artists and only download popular songs.",
        color=0x1DB954,
    )
    embed.add_field(
        name="📝 Artist Management",
        value=(
            "`?add <artist>` — Add an artist (interactive setup)\n"
            "`?remove <artist>` — Remove an artist from watchlist\n"
            "`?list` — Show all tracked artists\n"
            "`?update <artist>` — Update artist settings (folder, mode, threshold, metadata)\n"
            "`?import` — Import existing artists from Lidarr"
        ),
        inline=False,
    )
    embed.add_field(
        name="🔍 Scanning & Checking",
        value=(
            "`?scan [artist]` — Full scan of artist's catalog for hits\n"
            "`?check` — Manually trigger a popularity check\n"
            "`?check-downloads` / `?dl` — Check pending downloads and prune if complete"
        ),
        inline=False,
    )
    embed.add_field(
        name="🔒 Never Prune",
        value=(
            "`?keep [artist]` — Mark tracks as never-prune (artist → album → tracks)\n"
            "  Use `?keep Linkin Park` to auto-select artist"
        ),
        inline=False,
    )
    embed.add_field(
        name="⚙️ Configuration",
        value=(
            "`?threshold [0-100]` — Show or set popularity threshold\n"
            "`?mode [tracks|album]` — Show or set download mode\n"
            "`?folder [name]` — Show or set default root folder\n"
            "  `?folder set <artist> to <folder>` — Set per-artist folder override"
        ),
        inline=False,
    )
    embed.add_field(
        name="🛠️ Utilities",
        value=(
            "`?menu` — Interactive menu with all commands\n"
            "`?reset confirm` — **Wipe database** (bot owner only)"
        ),
        inline=False,
    )
    await ctx.send(embed=embed)


# ── Add Artist ──────────────────────────────────────────────────────────────

@bot.command(name="add")
async def add_artist(ctx: commands.Context, *, artist_name: str = None):
    """Add an artist to the watchlist with interactive setup."""
    if not artist_name:
        await ctx.send("❌ Usage: `?add <artist name>`")
        return

    # Search for artist on music service (Deezer/Last.fm)
    from .clients.deezer import DeezerClient
    deezer = DeezerClient()
    
    loop = asyncio.get_event_loop()
    search_results = await loop.run_in_executor(None, deezer.search_artist, artist_name)
    
    if not search_results:
        await ctx.send(f"❌ No artists found matching **{artist_name}**")
        return

    if len(search_results) == 1:
        # Single result - auto-select
        artist_data = {
            "name": search_results[0]["name"],
            "deezer_id": search_results[0]["id"],
            "genres": search_results[0].get("genres", []),
            "popularity": search_results[0].get("popularity", 0),
            "mode": "tracks",
            "threshold": Config.POPULARITY_THRESHOLD,
        }
        await _show_add_artist_dialog(ctx, artist_data)
    else:
        # Multiple results - show fuzzy picker
        view = AddFuzzyPickerView(ctx.author.id, search_results)
        await ctx.send(
            embed=discord.Embed(
                title="🔍 Multiple Artists Found",
                description=f"Found {len(search_results)} matches for **{artist_name}**. Select one:",
                color=0x1DB954,
            ),
            view=view,
        )
        await view.wait()
        
        if view.selected_result:
            artist_data = {
                "name": view.selected_result["name"],
                "deezer_id": view.selected_result["id"],
                "genres": view.selected_result.get("genres", []),
                "popularity": view.selected_result.get("popularity", 0),
                "mode": "tracks",
                "threshold": Config.POPULARITY_THRESHOLD,
            }
            await _show_add_artist_dialog(ctx, artist_data)


async def _show_add_artist_dialog(ctx: commands.Context, artist_data: dict):
    """Show the interactive AddArtistView dialog."""
    # Get folders from Lidarr
    lidarr = LidarrClient()
    loop = asyncio.get_event_loop()
    folders = await loop.run_in_executor(None, lidarr.get_root_folders)
    
    if not folders:
        await ctx.send("❌ No root folders found in Lidarr")
        return

    # Create view with folder data
    view = AddArtistView(ctx.author.id, artist_data, [])
    view.folders = folders
    view._setup_folder_select = lambda: None  # We'll populate manually
    
    # Build folder dropdown options
    options = []
    for f in folders[:25]:
        label = str(f.get("name") or "").strip() or "Folder"
        options.append(_opt(label, f.get("path") or "", description=f.get("path") or ""))
    view.folder_select.options = options
    
    # Set default folder
    default_folder = db.get_setting("default_root_folder")
    if default_folder:
        view.selected_folder = default_folder
        for opt in view.folder_select.options:
            if opt.value == default_folder:
                opt.default = True
                break
    elif options:
        view.selected_folder = options[0].value
        options[0].default = True

    # Set mode default
    if artist_data.get("mode") == "tracks":
        view.mode_select.options[0].default = True
    else:
        view.mode_select.options[1].default = True

    await ctx.send(
        embed=discord.Embed(
            title="🎛️ Configure Artist",
            description=(
                f"**{artist_data['name']}**\n"
                f"Genres: {', '.join(artist_data.get('genres', [])) or 'Unknown'}\n"
                f"Popularity: {artist_data.get('popularity', 0)}%\n\n"
                "Configure settings then click **Add Artist**:"
            ),
            color=0x1DB954,
        ),
        view=view,
    )
    await view.wait()

    if not view.selected_artist:
        return

    # Add artist to database and Lidarr
    from .clients.lidarr import LidarrClient
    lidarr = LidarrClient()
    
    try:
        lidarr_id = await loop.run_in_executor(
            None, lidarr.add_artist,
            artist_data["name"],
            view.selected_folder,
            Config.DEFAULT_QUALITY_PROFILE,
            Config.DEFAULT_METADATA_PROFILE,
            view.selected_mode == "album",
            view.selected_threshold,
        )
    except Exception as e:
        await ctx.send(f"❌ Failed to add artist to Lidarr: {e}")
        return

    # Save to database
    db.add_artist(
        name=artist_data["name"],
        deezer_id=artist_data.get("deezer_id"),
        lidarr_id=lidarr_id,
        root_folder=view.selected_folder,
        mode=view.selected_mode,
        threshold=view.selected_threshold,
        added_by=str(ctx.author.id),
    )

    await ctx.send(f"✅ **{artist_data['name']}** added to watchlist and Lidarr!")


# ── Remove Artist ────────────────────────────────────────────────────────────

@bot.command(name="remove")
async def remove_artist(ctx: commands.Context, *, artist_name: str):
    """Remove an artist from the watchlist."""
    artist = db.find_artist(artist_name)
    if not artist:
        await ctx.send(f"❌ Artist **{artist_name}** not found in watchlist")
        return

    db.remove_artist(artist["id"])
    await ctx.send(f"🗑️ Removed **{artist['name']}** from watchlist")


# ── Update Artist ────────────────────────────────────────────────────────────

@bot.command(name="update")
async def update_cmd(ctx: commands.Context, *, artist_name: str = None):
    """Update an artist's settings (folder, mode, threshold, metadata)."""
    if not artist_name:
        # Show picker
        artists = db.list_artists()
        if not artists:
            await ctx.send("📭 Watchlist is empty")
            return
        
        view = UpdatePickerView(ctx.author.id, artists)
        await ctx.send(
            embed=discord.Embed(
                title="✏️ Update Artist",
                description="Select an artist to update:",
                color=0x1DB954,
            ),
            view=view,
        )
        await view.wait()
        
        if not view.selected_artist:
            return
        artist = view.selected_artist
    else:
        # Fuzzy match
        artists = db.list_artists()
        artist = fuzzy_find_artist(artists, artist_name)
        if not artist:
            await ctx.send(f"❌ Artist **{artist_name}** not found in watchlist")
            return

    # Show update dialog (similar to add dialog)
    lidarr = LidarrClient()
    loop = asyncio.get_event_loop()
    folders = await loop.run_in_executor(None, lidarr.get_root_folders)
    
    # Get current Lidarr artist data
    lidarr_artist = await loop.run_in_executor(None, lidarr.get_artist, artist["lidarr_id"])
    current_meta = lidarr_artist.get("metadataProfileId", Config.DEFAULT_METADATA_PROFILE)
    current_folder = lidarr_artist.get("rootFolderPath", "")

    # Create update view
    view = AddArtistView(ctx.author.id, {
        "name": artist["name"],
        "mode": artist.get("mode", "tracks"),
        "threshold": artist.get("threshold", Config.POPULARITY_THRESHOLD),
    }, [])
    
    view.folders = folders
    options = []
    for f in folders[:25]:
        label = str(f.get("name") or "").strip() or "Folder"
        options.append(_opt(label, f.get("path") or "", description=f.get("path") or ""))
    view.folder_select.options = options

    # Set current values as defaults
    view.selected_folder = current_folder
    view.selected_mode = artist.get("mode", "tracks")
    view.selected_threshold = artist.get("threshold", Config.POPULARITY_THRESHOLD)
    
    for opt in view.folder_select.options:
        if opt.value == current_folder:
            opt.default = True
            break
    if view.selected_mode == "tracks":
        view.mode_select.options[0].default = True
    else:
        view.mode_select.options[1].default = True

    await ctx.send(
        embed=discord.Embed(
            title=f"✏️ Update {artist['name']}",
            description=f"Current: {view.selected_mode}, threshold {view.selected_threshold}%",
            color=0x1DB954,
        ),
        view=view,
    )
    await view.wait()

    if not view.selected_artist:
        return

    # Sync changes to Lidarr
    changes = _sync_to_lidarr(
        lidarr=lidarr,
        lidarr_id=artist["lidarr_id"],
        artist=lidarr_artist,
        new_meta=Config.DEFAULT_METADATA_PROFILE,  # Could add metadata profile picker later
        current_meta=current_meta,
        new_folder=view.selected_folder,
        current_folder=current_folder,
    )

    # Update database
    db.update_artist(
        artist["id"],
        root_folder=view.selected_folder,
        mode=view.selected_mode,
        threshold=view.selected_threshold,
    )

    if changes:
        await ctx.send(f"✅ **Updated {artist['name']}**:\n" + "\n".join(changes))
    else:
        await ctx.send("ℹ️ No changes made")


# ── List Artists ────────────────────────────────────────────────────────────

@bot.command(name="list")
async def list_artists(ctx: commands.Context):
    """List all tracked artists with their settings."""
    artists = db.list_artists()
    if not artists:
        await ctx.send("📭 Watchlist is empty")
        return

    lines = []
    for a in artists:
        mode_icon = "🎵" if a.get("mode") == "tracks" else "💿"
        lines.append(
            f"{mode_icon} **{a['name']}** — "
            f"mode: {a.get('mode', 'tracks')}, "
            f"threshold: {a.get('threshold', Config.POPULARITY_THRESHOLD)}%, "
            f"folder: {a.get('root_folder', 'default')}"
        )

    embed = discord.Embed(
        title=f"📋 Tracked Artists ({len(artists)})",
        description="\n".join(lines) or "None",
        color=0x1DB954,
    )
    await ctx.send(embed=embed)


# ── Manual Check ────────────────────────────────────────────────────────────

@bot.command(name="check")
async def manual_check(ctx: commands.Context):
    """Manually trigger a popularity check."""
    from .checker import run_daily_check
    
    await ctx.send("🔍 Running popularity check...")
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, run_daily_check)
    
    # Format and send results
    if results and results.albums_added > 0:
        await ctx.send(f"✅ Check complete: {results.albums_added} new album(s) added")
    else:
        await ctx.send("✅ Check complete: no new popular releases")


# ── Full Scan ──────────────────────────────────────────────────────────────

@bot.command(name="scan")
async def scan_cmd(ctx: commands.Context, *, artist_name: str = None):
    """Scan an artist's full catalog for popular tracks."""
    if not artist_name:
        # Show picker
        artists = db.list_artists()
        if not artists:
            await ctx.send("📭 Watchlist is empty")
            return
        
        view = ScanArtistView(ctx.author.id, artists)
        await ctx.send(
            embed=discord.Embed(
                title="🔍 Full Scan",
                description="Select an artist to scan their full catalog:",
                color=0x1DB954,
            ),
            view=view,
        )
        await view.wait()
        
        if not view.selected_artist:
            return
        artist = view.selected_artist
    else:
        artists = db.list_artists()
        artist = fuzzy_find_artist(artists, artist_name)
        if not artist:
            await ctx.send(f"❌ Artist **{artist_name}** not found in watchlist")
            return

    # Run scan for this artist
    await ctx.send(f"🔍 Scanning **{artist['name']}**'s full catalog...")
    
    from .checker import run_check_for_artist
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, run_check_for_artist, artist)
    
    await ctx.send(
        embed=discord.Embed(
            title=f"🎵 Scan Complete — {artist['name']}",
            description=f"Added {result.get('tracks_added', 0)} popular track(s)",
            color=0x1DB954,
        )
    )


# ── Prune ──────────────────────────────────────────────────────────────────

@bot.command(name="prune")
async def prune_cmd(ctx: commands.Context, *, artist_name: str = None):
    """Manually prune downloaded albums (remove below-threshold tracks)."""
    from .checker import prune_downloaded_albums, format_prune_results
    
    if artist_name:
        # Prune specific artist
        await ctx.send(f"✂️ Pruning **{artist_name}**...")
    else:
        await ctx.send("✂️ Pruning all downloaded albums...")
    
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, prune_downloaded_albums)
    
    report = format_prune_results(results)
    await ctx.send(report)


# ── Check Downloads ────────────────────────────────────────────────────────

@bot.command(name="check-downloads", aliases=["dl"])
async def check_downloads_cmd(ctx: commands.Context):
    """Check pending downloads and prune completed ones."""
    from .checker import check_pending_downloads
    
    await ctx.send("📥 Checking pending downloads...")
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, check_pending_downloads)
    
    if results:
        await ctx.send(f"✅ Download check: {results.get('pruned', 0)} album(s) pruned")
    else:
        await ctx.send("✅ Download check complete: no completed downloads to prune")


# ── Import from Lidarr ────────────────────────────────────────────────────

@bot.command(name="import")
async def import_cmd(ctx: commands.Context):
    """Import existing artists from Lidarr into the watchlist."""
    lidarr = LidarrClient()
    loop = asyncio.get_event_loop()
    lidarr_artists = await loop.run_in_executor(None, lidarr.get_all_artists)
    
    if not lidarr_artists:
        await ctx.send("❌ No artists found in Lidarr")
        return

    added = 0
    skipped = 0
    for la in lidarr_artists:
        if db.find_artist(la["artistName"]):
            skipped += 1
            continue
        
        db.add_artist(
            name=la["artistName"],
            lidarr_id=la["id"],
            root_folder=la.get("rootFolderPath", ""),
            mode="tracks",
            threshold=Config.POPULARITY_THRESHOLD,
            added_by=str(ctx.author.id),
        )
        added += 1

    await ctx.send(f"✅ Import complete: {added} added, {skipped} skipped (already in watchlist)")


# ── Threshold ──────────────────────────────────────────────────────────────

@bot.command(name="threshold")
async def threshold_cmd(ctx: commands.Context, value: int = None):
    """Show or set the popularity threshold."""
    if value is None:
        current = db.get_setting("popularity_threshold", Config.POPULARITY_THRESHOLD)
        await ctx.send(f"📊 Current threshold: **{current}%**")
    else:
        if value < 0 or value > 100:
            await ctx.send("❌ Threshold must be between 0 and 100")
            return
        db.set_setting("popularity_threshold", str(value))
        Config.POPULARITY_THRESHOLD = value
        await ctx.send(f"✅ Threshold set to **{value}%**")


# ── Mode ──────────────────────────────────────────────────────────────────

@bot.command(name="mode")
async def mode_cmd(ctx: commands.Context, mode: str = None):
    """Show or set the download mode (tracks or album)."""
    if mode is None:
        current = db.get_setting("download_mode", Config.DOWNLOAD_MODE)
        await ctx.send(f"🎛️ Current mode: **{current}**")
    else:
        mode = mode.lower()
        if mode not in ("tracks", "album"):
            await ctx.send("❌ Mode must be `tracks` or `album`")
            return
        db.set_setting("download_mode", mode)
        Config.DOWNLOAD_MODE = mode
        await ctx.send(f"✅ Download mode set to **{mode}**")


# ── Folder ────────────────────────────────────────────────────────────────

@bot.command(name="folder")
async def folder_cmd(ctx: commands.Context, *, folder_name: str = None):
    """Show or set the default root folder, or set per-artist folder."""
    if not folder_name:
        # Show available folders
        lidarr = LidarrClient()
        loop = asyncio.get_event_loop()
        folders = await loop.run_in_executor(None, lidarr.get_root_folders)
        
        if not folders:
            await ctx.send("❌ No root folders found in Lidarr")
            return
        
        current = db.get_setting("default_root_folder")
        lines = []
        for f in folders:
            name = f.get("name", "Unknown")
            path = f.get("path", "")
            marker = " ⭐ DEFAULT" if path == current else ""
            lines.append(f"📁 **{name}** → `{path}`{marker}")
        
        await ctx.send(
            embed=discord.Embed(
                title="📁 Lidarr Root Folders",
                description="\n".join(lines),
                color=0x1DB954,
            )
        )
        return

    # Check for "set <artist> to <folder>" syntax
    import re
    match = re.match(r"set\s+(.+?)\s+to\s+(.+)", folder_name, re.IGNORECASE)
    if match:
        artist_name = match.group(1).strip()
        target_folder = match.group(2).strip()
        
        artists = db.list_artists()
        artist = fuzzy_find_artist(artists, artist_name)
        if not artist:
            await ctx.send(f"❌ Artist **{artist_name}** not found")
            return
        
        # Validate folder exists
        lidarr = LidarrClient()
        resolved = lidarr.resolve_root_folder(target_folder)
        if not resolved:
            await ctx.send(f"❌ Folder **{target_folder}** not found in Lidarr")
            return
        
        db.set_artist_root_folder(artist["id"], resolved)
        await ctx.send(f"✅ Set **{artist['name']}** root folder to **{resolved}**")
        return

    # Set default folder
    lidarr = LidarrClient()
    resolved = lidarr.resolve_root_folder(folder_name)
    if not resolved:
        await ctx.send(f"❌ Folder **{folder_name}** not found. Use `?folder` to see available folders.")
        return

    db.set_setting("default_root_folder", resolved)
    folder_display = resolved.rstrip("/").split("/")[-1]
    await ctx.send(f"📁 Default root folder set to **{folder_display}** (saved permanently).")


# ── Menu ──────────────────────────────────────────────────────────────────

@bot.command(name="menu")
async def menu_cmd(ctx: commands.Context):
    """Show interactive menu with all commands."""
    view = MenuView(ctx.author.id)
    
    embed = discord.Embed(
        title="🎵 Lidarr Hits Bot — Menu",
        description="Click a button to run a command:",
        color=0x1DB954,
    )
    embed.add_field(name="📝 Artists", value="`?add`, `?remove`, `?list`, `?update`", inline=True)
    embed.add_field(name="🔍 Scan", value="`?scan`, `?check`, `?dl`", inline=True)
    embed.add_field(name="🔒 Never Prune", value="`?keep`", inline=True)
    embed.add_field(name="⚙️ Config", value="`?threshold`, `?mode`, `?folder`", inline=True)
    embed.add_field(name="🛠️ Utils", value="`?import`, `?reset confirm`", inline=True)
    
    await ctx.send(embed=embed, view=view)


# ── Keep (Never Prune) ────────────────────────────────────────────────────

@bot.command(name="keep")
async def keep_cmd(ctx: commands.Context, artist_name: str = None):
    """Mark tracks as never-prune. Nested menu: artist → album → tracks."""
    artists = db.list_artists()
    if not artists:
        await ctx.send("📭 Watchlist is empty.")
        return
    
    # If artist name provided, try to find and auto-select it
    selected_artist = None
    if artist_name:
        matched = fuzzy_find_artist(artists, artist_name)
        if matched:
            selected_artist = matched
            log.info(f"keep_cmd: Auto-selected artist '{matched['name']}' from argument '{artist_name}'")
        else:
            await ctx.send(f"❌ Artist **{artist_name}** not found in watchlist.")
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
    
    # Step 2: Pick album
    try:
        lidarr = LidarrClient()
        loop = asyncio.get_event_loop()
        albums = await loop.run_in_executor(None, lidarr.get_artist_albums, lidarr_id)
    except Exception as e:
        await ctx.send(f"❌ Lidarr error: {e}")
        return
    
    if not albums:
        await ctx.send(f"❌ No albums found for **{artist['name']}** in Lidarr.")
        return
    
    # Auto-select if only 1 album
    if len(albums) == 1:
        album_view = type('obj', (object,), {'selected_album': albums[0]})()
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
    
    # Save protected tracks
    count = 0
    for track_id in track_view.selected_track_ids:
        track = next((t for t in tracks if str(t["id"]) == track_id), None)
        if track:
            db.add_never_prune_track(
                artist["id"],
                album_name,
                track["title"],
                track_id
            )
            count += 1
    
    await ctx.send(f"✅ Protected **{count}** track(s) from pruning for **{album_name}**")


# ── Reset Database ──────────────────────────────────────────────────────────

@bot.command(name="reset")
@commands.is_owner()
async def reset_cmd(ctx: commands.Context, confirm: str = None):
    """Reset the bot database — wipes all artists, settings, and prune state."""
    if confirm != "confirm":
        await ctx.send("⚠️ This will **permanently delete** all bot data.\nRun `?reset confirm` to proceed.")
        return

    import os
    db_path = os.getenv("DB_PATH", "lidarr_hits.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    # Re-initialize
    from .database import Database
    Database()  # creates fresh tables
    await ctx.send("✅ Database reset complete. Fresh start!")