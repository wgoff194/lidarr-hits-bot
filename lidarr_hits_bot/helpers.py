"""
Shared helper utilities for Lidarr Hits Bot.
Contains reusable functions used across commands and views.
"""

import logging
from typing import Optional

import discord
from discord.ext import commands


# ── Logging ──────────────────────────────────────────────────────────────────

log = logging.getLogger(__name__)


# ── Discord Select Helpers ──────────────────────────────────────────────────

def _opt(label: str, value: str, **kwargs) -> discord.SelectOption:
    """
    Create a SelectOption with label truncated to 25 chars (Discord limit).
    
    Ensures label is never empty or None — Discord requires 1+ chars.
    Also ensures label is never empty after truncation.
    """
    # Ensure label is a string
    if not isinstance(label, str):
        label = str(label) if label else "Untitled"
    
    # Truncate to max 25 chars (Discord limit)
    safe_label = label[:25]
    
    # Ensure minimum 1 char label after stripping
    if not safe_label or safe_label == "":
        safe_label = "Untitled"
    if not safe_label.strip():
        safe_label = "Untitled"
    
    return discord.SelectOption(label=safe_label, value=str(value), **kwargs)


def fuzzy_find_artist(artists: list[dict], query: str) -> Optional[dict]:
    """Fuzzy search for an artist in a list by name."""
    if not query:
        return None
    
    query_lower = query.lower().strip()
    if not query_lower:
        return None
    
    # Exact match first
    for a in artists:
        a_name = (a.get("name") or "").lower()
        if query_lower == a_name:
            return a
    
    # Substring match
    for a in artists:
        a_name = (a.get("name") or "").lower()
        if query_lower in a_name:
            return a
    
    # Prefix match (first word)
    for a in artists:
        a_name = (a.get("name") or "").lower()
        if a_name.startswith(query_lower):
            return a
    
    return None


# ── Lidarr Sync Helpers ──────────────────────────────────────────────────────

def _sync_to_lidarr(
    lidarr,
    lidarr_id: int,
    artist: dict,
    new_meta: int,
    current_meta: int,
    new_folder: str,
    current_folder: str
) -> list[str]:
    """
    Sync artist changes to Lidarr in a background thread.
    Returns list of change description strings.
    """
    changes = []
    
    # Update metadata profile if changed
    if new_meta != current_meta:
        log.info("Before update — artist '%s': metadataProfileId=%s, rootFolderPath=%s",
                artist["name"], current_meta, current_folder)
        log.info("Setting metadataProfileId to %s", new_meta)
        
        artist["metadataProfileId"] = new_meta
        result = lidarr._put(f"/artist/{lidarr_id}", artist)
        
        log.info("PUT response — metadataProfileId=%s", result.get("metadataProfileId"))
        if result.get("metadataProfileId") == new_meta:
            log.info("✅ Metadata profile confirmed in Lidarr")
            changes.append(f"Metadata profile → {new_meta}")
        else:
            log.warning("⚠️ Metadata profile change may not have persisted")
            changes.append(f"Metadata profile → {new_meta} (unconfirmed)")
    
    # Move artist to new root folder if changed
    if new_folder != current_folder:
        log.info("Before move — artist '%s': rootFolderPath=%s", artist["name"], current_folder)
        log.info("Setting rootFolderPath to %s", new_folder)
        
        artist["rootFolderPath"] = new_folder
        result = lidarr._put(f"/artist/{lidarr_id}", artist)
        
        log.info("PUT response — rootFolderPath=%s", result.get("rootFolderPath"))
        if result.get("rootFolderPath") == new_folder:
            log.info("✅ Root folder move confirmed in Lidarr")
            changes.append(f"Root folder → {new_folder}")
        else:
            log.warning("⚠️ Root folder change may not have persisted")
            changes.append(f"Root folder → {new_folder} (unconfirmed)")
    
    return changes


# ── Thread Helpers ───────────────────────────────────────────────────────────

async def create_thread(ctx: commands.Context, thread_name: str = None) -> discord.Thread:
    """
    Auto-create a thread for a command unless already in one.
    Returns the thread channel (or original if already in thread).
    """
    if isinstance(ctx.channel, discord.Thread):
        return ctx.channel
    
    if not thread_name:
        thread_name = ctx.command.name
    
    try:
        thread = await ctx.message.create_thread(
            name=thread_name,
            auto_archive_duration=10080,  # 7 days (max for non-boosted)
        )
        log.info("Created thread '%s' for command '%s'", thread_name, ctx.command.name)
        return thread
    except discord.HTTPException as e:
        log.warning("Failed to create thread for '%s': %s", thread_name, e)
        return ctx.channel


# ── Formatting Helpers ──────────────────────────────────────────────────────

def format_prune_results(results: dict) -> str:
    """Format prune results into a readable string."""
    if not results or results.get("total_pruned", 0) == 0:
        return "Nothing to prune"
    
    lines = []
    for artist_name, albums in results.get("by_artist", {}).items():
        for album_name, counts in albums.items():
            lines.append(f"**{artist_name}** — {album_name}: kept {counts['kept']}, pruned {counts['pruned']}")
    
    total_kept = sum(c["kept"] for a in results.get("by_artist", {}).values() for c in a.values())
    total_pruned = sum(c["pruned"] for a in results.get("by_artist", {}).values() for c in a.values())
    lines.append(f"\n**Summary:** {total_kept} track(s) kept, {total_pruned} pruned")
    
    return "\n".join(lines)


def format_daily_results(results) -> str:
    """Format daily check results into a readable string."""
    # This would be called from the daily check loop
    return str(results) if results else "No results"