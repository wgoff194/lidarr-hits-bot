"""Daily popularity checker — the core logic that ties Spotify + Lidarr together."""

import logging
from dataclasses import dataclass, field

import database as db
from config import Config
from lidarr_client import LidarrClient
from spotify_client import AlbumInfo, SpotifyClient

log = logging.getLogger(__name__)


@dataclass
class CheckResult:
    """Summary of what the daily check found for one artist."""

    artist_name: str
    new_albums_found: int = 0
    albums_added: int = 0
    tracks_added: int = 0
    tracks_skipped: int = 0
    albums_skipped: int = 0
    newly_popular_tracks: int = 0  # tracks that crossed threshold since last check
    errors: list[str] = field(default_factory=list)
    added_albums: list[str] = field(default_factory=list)
    skipped_albums: list[str] = field(default_factory=list)


def run_daily_check() -> list[CheckResult]:
    """
    Check all watched artists for new popular releases.
    Re-evaluates albums already in Lidarr to catch tracks that became popular.
    Returns a list of per-artist results.
    """
    artists = db.list_artists()
    if not artists:
        log.info("No artists in watchlist, nothing to check.")
        return []

    log.info("Starting daily check for %d artists (mode: %s)...", len(artists), Config.DOWNLOAD_MODE)

    try:
        spotify = SpotifyClient()
    except ValueError as e:
        log.error("Spotify client init failed: %s", e)
        return []

    try:
        lidarr = LidarrClient()
    except ValueError as e:
        log.error("Lidarr client init failed: %s", e)
        return []

    results: list[CheckResult] = []

    for artist in artists:
        result = CheckResult(artist_name=artist["name"])
        log.info("Checking artist: %s", artist["name"])

        # ── Step 1: Resolve Spotify ID if we don't have it ───────────────
        spotify_id = artist.get("spotify_id")
        if not spotify_id:
            found = spotify.search_artist(artist["name"])
            if not found:
                result.errors.append("Not found on Spotify")
                results.append(result)
                continue
            spotify_id = found["id"]
            db.update_artist_spotify_id(artist["name"], spotify_id)

        # ── Step 2: Get new releases from Spotify ────────────────────────
        try:
            new_albums = spotify.get_new_releases(spotify_id, lookback_days=90)
        except Exception as e:
            result.errors.append(f"Spotify error: {e}")
            results.append(result)
            continue

        # ── Step 3: Filter for popularity ────────────────────────────────
        popular_albums = [a for a in new_albums if spotify.should_download_album(a)]
        result.new_albums_found = len(popular_albums)

        for album in popular_albums:
            # ── Step 4: Ensure artist is in Lidarr ───────────────────────
            lidarr_artist_id = artist.get("lidarr_id")
            if not lidarr_artist_id:
                lidarr_artist = lidarr.lookup_artist(artist["name"])
                if lidarr_artist:
                    all_lidarr = lidarr.get_all_artists()
                    existing = next(
                        (a for a in all_lidarr if a.get("foreignArtistId") == lidarr_artist.get("foreignArtistId")),
                        None,
                    )
                    if existing:
                        lidarr_artist_id = existing["id"]
                    else:
                        # Determine root folder: per-artist override > default setting > .env
                        root_folder = artist.get("root_folder")
                        if not root_folder:
                            root_folder = db.get_setting("default_root_folder")
                        added = lidarr.add_artist(
                            lidarr_artist.get("foreignArtistId", artist["name"]),
                            root_folder=root_folder,
                        )
                        if added:
                            lidarr_artist_id = added["id"]
                        else:
                            result.errors.append("Could not add artist to Lidarr")
                            db.log_check(artist["id"], album.name, album.spotify_url, album.avg_popularity, False)
                            continue
                    db.update_artist_lidarr_id(artist["name"], lidarr_artist_id)

            # ── Step 5: Find the album in Lidarr ─────────────────────────
            try:
                lidarr_albums = lidarr.get_artist_albums(lidarr_artist_id)
                matched = _match_album(album.name, lidarr_albums)

                if not matched:
                    db.log_check(artist["id"], album.name, album.spotify_url, album.avg_popularity, False)
                    result.skipped_albums.append(f"{album.name} (not in Lidarr DB yet)")
                    continue

                album_id = matched["id"]

                if Config.DOWNLOAD_MODE == "tracks":
                    # ── Track-level: cherry-pick only popular tracks ─────
                    success, track_stats = _download_popular_tracks(
                        lidarr, album_id, album, artist["id"]
                    )
                    db.log_check(artist["id"], album.name, album.spotify_url, album.avg_popularity, success)
                    if success:
                        if track_stats["new"] > 0:
                            result.albums_added += 1
                            result.tracks_added += track_stats["new"]
                            result.newly_popular_tracks += track_stats["new"]
                            result.added_albums.append(
                                f"{album.name} — {track_stats['new']} new track(s) "
                                f"({track_stats['already']} already monitored, "
                                f"{track_stats['unmonitored']} skipped)"
                            )
                        else:
                            result.albums_skipped += 1
                            result.skipped_albums.append(
                                f"{album.name} (all {track_stats['already']} popular track(s) already monitored)"
                            )
                    else:
                        result.errors.append(f"Failed to configure tracks for {album.name}")
                else:
                    # ── Album-level: grab the whole thing ────────────────
                    # Check if we already processed this album
                    already_monitored = db.get_monitored_tracks(artist["id"], album.name)
                    if already_monitored:
                        result.albums_skipped += 1
                        result.skipped_albums.append(f"{album.name} (already processed)")
                        continue

                    success = lidarr.monitor_and_search_album(album_id)
                    db.log_check(artist["id"], album.name, album.spotify_url, album.avg_popularity, success)
                    if success:
                        result.albums_added += 1
                        result.added_albums.append(
                            f"{album.name} (pop: {album.avg_popularity}, type: {album.album_type})"
                        )
                        # Record so we don't re-add
                        db.record_monitored_tracks(artist["id"], album.name, [
                            {"name": tp.name, "popularity": tp.popularity}
                            for tp in album.track_popularities
                        ])
                    else:
                        result.errors.append(f"Failed to trigger download for {album.name}")

            except Exception as e:
                result.errors.append(f"Lidarr error for {album.name}: {e}")
                db.log_check(artist["id"], album.name, album.spotify_url, album.avg_popularity, False)

        db.mark_checked(artist["id"])
        results.append(result)

    log.info("Daily check complete. Processed %d artists.", len(results))
    return results


def _download_popular_tracks(
    lidarr: LidarrClient,
    album_id: int,
    album: AlbumInfo,
    artist_id: int,
) -> tuple[bool, dict]:
    """
    Track-level download: check which popular tracks are already monitored,
    then only monitor NEW ones that crossed the threshold since last check.
    Returns (success, stats_dict).
    """
    # First, make sure the album itself is monitored
    lidarr.monitor_album(album_id)

    # Get Lidarr's tracks for this album
    lidarr_tracks = lidarr.get_album_tracks(album_id)
    if not lidarr_tracks:
        log.warning("No tracks found in Lidarr for album %s", album_id)
        return False, {"new": 0, "already": 0, "unmonitored": 0}

    # Get tracks we've already monitored for this album
    already_monitored_names = db.get_monitored_tracks(artist_id, album.name)

    # Build a set of ALL popular track names from Spotify
    popular_names = {
        tp.name.strip().lower()
        for tp in album.track_popularities
        if tp.popularity >= Config.POPULARITY_THRESHOLD
    }

    if not popular_names:
        log.info("No tracks above threshold for album %s", album.name)
        return False, {"new": 0, "already": len(already_monitored_names), "unmonitored": len(lidarr_tracks)}

    # Figure out which popular tracks are NEW (not already monitored)
    new_popular_names = {
        name for name in popular_names
        if name not in already_monitored_names
    }

    # Match NEW popular tracks to Lidarr track IDs
    new_track_ids: set[int] = set()
    new_track_info: list[dict] = []
    for lt in lidarr_tracks:
        lidarr_title = lt.get("title", "").strip().lower()
        matched = False
        # Exact match
        if lidarr_title in new_popular_names:
            matched = True
        else:
            # Fuzzy: check if any popular name is a substring
            for pn in new_popular_names:
                if pn in lidarr_title or lidarr_title in pn:
                    matched = True
                    break

        if matched:
            new_track_ids.add(lt["id"])
            # Find the Spotify popularity for this track
            pop_score = 0
            for tp in album.track_popularities:
                if tp.name.strip().lower() == lidarr_title:
                    pop_score = tp.popularity
                    break
            new_track_info.append({
                "name": lt.get("title", "Unknown"),
                "popularity": pop_score,
                "lidarr_track_id": lt["id"],
            })

    already_count = len(already_monitored_names)

    if not new_track_ids:
        if already_count > 0:
            log.info("Album '%s': all %d popular track(s) already monitored", album.name, already_count)
            return True, {"new": 0, "already": already_count, "unmonitored": len(lidarr_tracks) - already_count}
        log.warning(
            "Could not match any popular tracks to Lidarr tracks for %s. "
            "Spotify names: %s | Lidarr names: %s",
            album.name, popular_names, [t.get("title") for t in lidarr_tracks],
        )
        return False, {"new": 0, "already": 0, "unmonitored": len(lidarr_tracks)}

    # Get ALL currently popular track IDs (new + already monitored) for full monitoring state
    all_popular_ids: set[int] = set()
    for lt in lidarr_tracks:
        lidarr_title = lt.get("title", "").strip().lower()
        if lidarr_title in popular_names:
            all_popular_ids.add(lt["id"])
        else:
            for pn in popular_names:
                if pn in lidarr_title or lidarr_title in pn:
                    all_popular_ids.add(lt["id"])
                    break

    # Cherry-pick: unmonitor all, monitor only the hits (new + existing popular)
    stats = lidarr.monitor_specific_tracks(album_id, all_popular_ids)
    log.info(
        "Album '%s': %d NEW track(s) added, %d already monitored, %d unmonitored",
        album.name, len(new_track_ids), already_count, stats["unmonitored"],
    )

    # Record the newly monitored tracks in the DB
    db.record_monitored_tracks(artist_id, album.name, new_track_info)

    # Trigger the search (Lidarr will only grab the monitored tracks)
    lidarr.search_album(album_id)

    return True, {"new": len(new_track_ids), "already": already_count, "unmonitored": stats["unmonitored"]}


def _match_album(spotify_name: str, lidarr_albums: list[dict]) -> dict | None:
    """Try to match a Spotify album name to a Lidarr album."""
    spotify_lower = spotify_name.strip().lower()

    # Exact match first
    for la in lidarr_albums:
        if la.get("title", "").strip().lower() == spotify_lower:
            return la

    # Contains match
    for la in lidarr_albums:
        lidarr_lower = la.get("title", "").strip().lower()
        if spotify_lower in lidarr_lower or lidarr_lower in spotify_lower:
            return la

    return None


def format_results(results: list[CheckResult]) -> str:
    """Format check results into a Discord-friendly message."""
    if not results:
        return "📭 No artists in watchlist — add some with `?add <artist>`!"

    mode = Config.DOWNLOAD_MODE
    lines = [f"🎵 **Daily Hits Check Complete** (mode: `{mode}`)\n"]
    total_added = 0
    total_tracks = 0
    total_new_tracks = 0
    total_errors = 0

    for r in results:
        total_added += r.albums_added
        total_tracks += r.tracks_added
        total_new_tracks += r.newly_popular_tracks
        total_errors += len(r.errors)

        if r.albums_added == 0 and not r.errors and not r.skipped_albums:
            lines.append(f"**{r.artist_name}** — no new popular releases")
            continue

        lines.append(f"**{r.artist_name}**:")
        if r.added_albums:
            for a in r.added_albums:
                lines.append(f"  ✅ {a}")
        if r.skipped_albums:
            for s in r.skipped_albums:
                lines.append(f"  ⏭️ {s}")
        if r.errors:
            for e in r.errors:
                lines.append(f"  ❌ {e}")

    if mode == "tracks":
        lines.append(f"\n**Summary:** {total_new_tracks} new track(s) queued")
    else:
        lines.append(f"\n**Summary:** {total_added} album(s) added to Lidarr")
    if total_errors:
        lines.append(f"⚠️ {total_errors} error(s) — check logs for details")

    return "\n".join(lines)
