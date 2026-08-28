"""Standalone helpers used by views and commands.

This module is intentionally dependency-free:
- No imports from bot, commands, views, or database.
- Only stdlib + discord + a couple of in-house utilities that
  themselves don't pull in the bot.
"""

import logging
from typing import Optional

import discord
from discord.ext import commands

log = logging.getLogger(__name__)


# ── Discord option helper ────────────────────────────────────────────────────

def _opt(label: str, value: str, **kwargs) -> discord.SelectOption:
    """Create a SelectOption with label truncated to 25 chars (Discord limit).

    Ensures label is never empty or None — Discord requires 1+ chars.
    """
    # Ensure label is a non-empty string, max 25 chars
    safe_label = (label or "Untitled")[:25]
    if not safe_label.strip():
        safe_label = "Untitled"
    return discord.SelectOption(label=safe_label, value=str(value), **kwargs)


# ── Fuzzy artist matcher ─────────────────────────────────────────────────────

def fuzzy_find_artist(artists: list[dict], query: str) -> Optional[dict]:
    """Fuzzy match an artist name. Returns best match or None."""
    query = query.strip().lower()
    if not query:
        return None
    # Exact match
    for a in artists:
        if a["name"].lower() == query:
            return a
    # Starts with
    for a in artists:
        if a["name"].lower().startswith(query):
            return a
    # Contains
    for a in artists:
        if query in a["name"].lower():
            return a
    return None


# ── Thread routing ───────────────────────────────────────────────────────────

async def get_thread_channel(ctx: commands.Context) -> discord.abc.Messageable:
    """
    If already in a thread, return it. Otherwise create a new thread
    from the invoking message and return that.
    """
    if isinstance(ctx.channel, discord.Thread):
        return ctx.channel

    # Create a thread from the command message
    thread_name = f"{ctx.command.name} — {ctx.author.display_name}"
    try:
        thread = await ctx.message.create_thread(
            name=thread_name[:100],  # Discord limit
            auto_archive_duration=10080,  # 7 days (max for non-boosted servers)
        )
        return thread
    except discord.HTTPException:
        # Fallback: if thread creation fails (e.g. already in a thread, no permission)
        return ctx.channel


# ── Lidarr sync (used by ?update) ────────────────────────────────────────────

def _sync_to_lidarr(lidarr, lidarr_id, artist, new_meta, current_meta, new_folder, current_folder):
    """Sync changes to Lidarr in a background thread. Returns list of change strings."""
    changes = []
    try:
        artist_data = lidarr.get_artist(lidarr_id)
        if not artist_data:
            changes.append("  ⚠️ Could not fetch artist from Lidarr")
            return changes

        log.info("Before update — artist '%s': metadataProfileId=%s, rootFolderPath=%s",
                 artist["name"], artist_data.get("metadataProfileId"), artist_data.get("rootFolderPath"))

        # Update metadata profile
        if new_meta and new_meta != current_meta:
            artist_data["metadataProfileId"] = new_meta
            log.info("Setting metadataProfileId to %s", new_meta)
            result = lidarr._put(f"/artist/{lidarr_id}", artist_data)
            log.info("PUT response — metadataProfileId=%s", result.get("metadataProfileId"))

            verify = lidarr.get_artist(lidarr_id)
            if verify.get("metadataProfileId") == new_meta:
                log.info("✅ Metadata profile confirmed in Lidarr")
            else:
                log.warning("⚠️ Metadata mismatch! Sent %s, got %s", new_meta, verify.get("metadataProfileId"))

        # Move artist if folder changed
        if new_folder and new_folder != current_folder:
            if lidarr.move_artist(lidarr_id, new_folder):
                changes.append("  ✅ Moved in Lidarr")
            else:
                changes.append("  ⚠️ Failed to move in Lidarr")

        changes.append("  ✅ Synced to Lidarr")

        # If metadata changed, unmonitor all albums
        if new_meta and new_meta != current_meta:
            artist_data["monitored"] = False
            lidarr._put(f"/artist/{lidarr_id}", artist_data)
            albums = lidarr.get_artist_albums(lidarr_id)
            unmonitored = 0
            for a in albums:
                if a.get("monitored"):
                    lidarr.unmonitor_album(a["id"])
                    unmonitored += 1
            artist_data["monitored"] = True
            lidarr._put(f"/artist/{lidarr_id}", artist_data)
            for a in albums:
                db_set_setting_pruned(artist["id"], a.get("title", ""))
            changes.append(f"  ↳ Unmonitored all types ({unmonitored} album(s))")

    except Exception as e:
        changes.append(f"  ⚠️ Lidarr sync failed: {e}")

    return changes


# ── Tiny import-late wrapper so helpers.py stays DB-free at module level ─────

def db_set_setting_pruned(artist_id: int, album_name: str) -> None:
    """Mark album as pruned (or clear marker) — thin wrapper to keep
    helpers.py free of top-level `database` imports.
    """
    # Imported lazily so the helpers module stays DB-free.
    from lidarr_hits_bot import database as db
    db.set_setting(f"pruned_{artist_id}_{album_name}", "")


# ── Lidarr import (used by ?import) ───────────────────────────────────────────

def _run_import() -> dict:
    """Run the import in a background thread."""
    # Imported lazily so this module stays free of top-level db/lidarr imports.
    from lidarr_hits_bot import database as db

    result = {"added": [], "skipped": [], "errors": []}

    try:
        from lidarr_hits_bot.clients.lidarr import LidarrClient
        lidarr = LidarrClient()
    except Exception as e:
        result["errors"].append(f"Lidarr connection failed: {e}")
        return result

    try:
        lidarr_artists = lidarr.get_all_artists()
    except Exception as e:
        result["errors"].append(f"Failed to fetch artists: {e}")
        return result

    existing = {a["name"].lower() for a in db.list_artists()}
    existing_lidarr_ids = {a.get("lidarr_id") for a in db.list_artists() if a.get("lidarr_id")}

    for la in lidarr_artists:
        artist_name = la.get("artistName", "Unknown")
        lidarr_id = la.get("id")

        if artist_name.lower() in existing or lidarr_id in existing_lidarr_ids:
            result["skipped"].append(artist_name)
            continue

        try:
            details = lidarr.get_artist_details(lidarr_id)
            if not details:
                continue

            root_path = details.get("rootFolderPath", "")
            root_folder = root_path if root_path else None
            meta_profile_id = details.get("metadataProfileId")

            # Add to watchlist (no Deezer lookup — scan does that later)
            success = db.add_artist(artist_name, "imported", None, root_folder=root_folder)
            if not success:
                result["skipped"].append(artist_name)
                continue

            db.set_setting(f"mode_{artist_name}", "album")
            if meta_profile_id:
                db.set_setting(f"meta_profile_{artist_name}", str(meta_profile_id))

            # Unmonitor all albums
            unmonitored = lidarr.unmonitor_all_albums(lidarr_id)

            db.update_artist_lidarr_id(artist_name, lidarr_id)

            folder_display = root_folder.rstrip("/").split("/")[-1] if root_folder else "default"
            result["added"].append(f"{artist_name} (📁 {folder_display})")
        except Exception as e:
            result["errors"].append(f"{artist_name}: {e}")

    return result
