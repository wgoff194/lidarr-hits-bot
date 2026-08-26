"""Daily popularity checker — the core logic that ties Spotify + Lidarr together."""

import logging
from dataclasses import dataclass, field

import database as db
from config import Config
from lidarr_client import LidarrClient
from music_client import AlbumInfo, MusicClient

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


def run_daily_check(artist_filter: str = None, full_scan: bool = False) -> list[CheckResult]:
    """
    Check watched artists for new popular releases.
    If artist_filter is set, only check that one artist.
    If full_scan is True, scan the artist's entire catalog (top tracks → albums)
    instead of just the last 90 days. Used on first add.
    Returns a list of per-artist results.
    """
    if artist_filter:
        artist = db.get_artist(artist_filter)
        if not artist:
            log.info("Artist '%s' not found in watchlist.", artist_filter)
            return []
        artists = [artist]
    else:
        artists = db.list_artists()
        if not artists:
            log.info("No artists in watchlist, nothing to check.")
            return []

    log.info("Starting daily check for %d artists (mode: %s)...", len(artists), Config.DOWNLOAD_MODE)

    try:
        music = MusicClient()
    except ValueError as e:
        log.error("Music client init failed: %s", e)
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
        music_id = artist.get("spotify_id")
        if not music_id:
            found = music.search_artist(artist["name"])
            if not found:
                result.errors.append("Not found on Spotify")
                results.append(result)
                continue
            music_id = found["id"]
            db.update_artist_spotify_id(artist["name"], music_id)

        # ── Step 2: Get releases ──────────────────────────────────────────
        try:
            if full_scan:
                new_albums = music.get_artist_top_albums(music_id)
            else:
                new_albums = music.get_new_releases(music_id, lookback_days=90)
        except Exception as e:
            result.errors.append(f"Music API error: {e}")
            results.append(result)
            continue

        # ── Step 3: Filter for popularity ────────────────────────────────
        popular_albums = [a for a in new_albums if music.should_download_album(a)]
        result.new_albums_found = len(popular_albums)

        # ── Step 3.5: Ensure artist is in Lidarr (once per artist) ────────
        lidarr_artist_id = artist.get("lidarr_id")
        lidarr_albums = []
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
                    # Determine metadata profile: per-artist setting > auto-resolve
                    meta_profile_id = db.get_setting(f"meta_profile_{artist['name']}")
                    if meta_profile_id:
                        meta_profile_id = int(meta_profile_id)
                    added = lidarr.add_artist(
                        lidarr_artist.get("foreignArtistId", artist["name"]),
                        root_folder=root_folder,
                        metadata_profile_id=meta_profile_id,
                    )
                    if added:
                        lidarr_artist_id = added["id"]
                    else:
                        result.errors.append("Could not add artist to Lidarr")
                db.update_artist_lidarr_id(artist["name"], lidarr_artist_id)

        # Fetch Lidarr albums ONCE for this artist
        if lidarr_artist_id:
            try:
                lidarr_albums = lidarr.get_artist_albums(lidarr_artist_id)
                log.info("Found %d albums in Lidarr for artist ID %s", len(lidarr_albums), lidarr_artist_id)

                # If no albums found, wait for Lidarr refresh (artist just added)
                if not lidarr_albums:
                    import time
                    log.info("Waiting 15s for Lidarr to populate albums...")
                    time.sleep(15)
                    lidarr_albums = lidarr.get_artist_albums(lidarr_artist_id)
                    log.info("After wait: found %d albums for artist ID %s", len(lidarr_albums), lidarr_artist_id)

                # If still no albums, the stored lidarr_id might be stale — re-resolve
                if not lidarr_albums:
                    log.warning("No albums for artist ID %s, re-resolving from Lidarr...", lidarr_artist_id)
                    all_lidarr = lidarr.get_all_artists()
                    for la in all_lidarr:
                        if la.get("artistName", "").lower() == artist["name"].lower():
                            lidarr_artist_id = la["id"]
                            db.update_artist_lidarr_id(artist["name"], lidarr_artist_id)
                            lidarr_albums = lidarr.get_artist_albums(lidarr_artist_id)
                            log.info("Re-resolved to artist ID %s, found %d albums", lidarr_artist_id, len(lidarr_albums))
                            break
            except Exception as e:
                log.warning("Failed to fetch Lidarr albums: %s", e)

        for album in popular_albums:
            # ── Step 4: Find the album in Lidarr ─────────────────────────
            try:
                if not lidarr_artist_id or not lidarr_albums:
                    db.log_check(artist["id"], album.name, album.deezer_url, album.avg_popularity, False)
                    result.skipped_albums.append(f"{album.name} (not in Lidarr DB yet)")
                    continue

                matched = _match_album(album.name, lidarr_albums)

                if not matched:
                    db.log_check(artist["id"], album.name, album.deezer_url, album.avg_popularity, False)
                    result.skipped_albums.append(f"{album.name} (not in Lidarr DB yet)")
                    continue

                album_id = matched["id"]

                # ── Check if popular tracks are already downloaded ───────
                try:
                    lidarr_tracks = lidarr.get_album_tracks(album_id)
                    downloaded_tracks = {t.get("title", "").strip().lower() for t in lidarr_tracks if t.get("hasFile")}
                except Exception:
                    downloaded_tracks = set()

                # Check which popular tracks are already downloaded
                popular_names = {tp.name.strip().lower() for tp in album.track_popularities if tp.popularity >= Config.POPULARITY_THRESHOLD}
                already_have = popular_names & downloaded_tracks
                missing = popular_names - downloaded_tracks

                if popular_names and not missing:
                    # All popular tracks already downloaded
                    db.set_album_status(artist["id"], album.name, "pruned", album_id)
                    result.albums_skipped += 1
                    result.skipped_albums.append(f"{album.name} (all {len(already_have)} hits already downloaded)")
                    continue

                # ── Album-level: monitor and search ─────────────────────
                success = lidarr.monitor_and_search_album(album_id)
                db.log_check(artist["id"], album.name, album.deezer_url, album.avg_popularity, success)
                if success:
                    result.albums_added += 1
                    status = "downloaded" if not missing else "pending"
                    db.set_album_status(artist["id"], album.name, status, album_id)
                    if missing:
                        result.added_albums.append(
                            f"{album.name} — {len(missing)} track(s) queued ({len(already_have)} already have)"
                        )
                    else:
                        result.added_albums.append(
                            f"{album.name} (pop: {album.avg_popularity}, type: {album.album_type})"
                        )
                    # Record for prune tracking
                    db.record_monitored_tracks(artist["id"], album.name, [
                        {"name": tp.name, "popularity": tp.popularity}
                        for tp in album.track_popularities
                    ])
                else:
                    result.errors.append(f"Failed to trigger download for {album.name}")

            except Exception as e:
                result.errors.append(f"Lidarr error for {album.name}: {e}")
                db.log_check(artist["id"], album.name, album.deezer_url, album.avg_popularity, False)

        db.mark_checked(artist["id"])
        results.append(result)

    log.info("Daily check complete. Processed %d artists.", len(results))
    return results


# ── Prune downloaded albums ──────────────────────────────────────────────────

@dataclass
class PruneResult:
    """Summary of what the prune found for one album."""
    artist_name: str
    album_name: str
    total_tracks: int = 0
    kept_tracks: int = 0
    pruned_tracks: int = 0
    already_pruned: bool = False
    error: str = ""


def prune_downloaded_albums(artist_filter: str = None, force: bool = False) -> list[PruneResult]:
    """
    Check artists for downloaded albums. For each:
    1. Find tracks above the popularity threshold
    2. Delete below-threshold track files from disk
    3. Unmonitor the album so Lidarr doesn't re-download

    If artist_filter is set, only check that artist.
    If force is True, re-check even already-pruned albums.
    """
    if artist_filter:
        artist = db.get_artist(artist_filter)
        if not artist:
            return []
        artists = [artist]
    else:
        artists = db.list_artists()
        if not artists:
            return []

    try:
        music = MusicClient()
    except ValueError:
        return []

    try:
        lidarr = LidarrClient()
    except ValueError:
        return []

    results: list[PruneResult] = []

    for artist in artists:
        # Only prune artists set to "tracks" mode
        artist_mode = db.get_setting(f"mode_{artist['name']}") or Config.DOWNLOAD_MODE
        if artist_mode != "tracks":
            continue

        lidarr_artist_id = artist.get("lidarr_id")
        if not lidarr_artist_id:
            continue

        # Get albums from Lidarr
        try:
            lidarr_albums = lidarr.get_artist_albums(lidarr_artist_id)
        except Exception:
            continue

        # Get Deezer top tracks for popularity reference
        music_id = artist.get("spotify_id")
        if not music_id:
            continue

        try:
            top_tracks = music.get_artist_top_tracks(music_id)
        except Exception:
            top_tracks = []

        # Build a name-based popularity map (lowercase name → score)
        total_top = len(top_tracks)
        name_scores: dict[str, int] = {}
        for i, t in enumerate(top_tracks):
            tname = t.get("title", "").strip().lower()
            if tname:
                score = max(50, 100 - int((i / total_top) * 50)) if total_top > 0 else 10
                name_scores[tname] = score

        for la in lidarr_albums:
            album_id = la["id"]
            album_name = la.get("title", "Unknown")

            # Check if already pruned (skip unless force)
            pruned_key = f"pruned_{artist['id']}_{album_name}"
            if not force and db.get_setting(pruned_key):
                continue

            # Get downloaded track files
            lidarr_tracks = lidarr.get_album_tracks(album_id)
            if not lidarr_tracks:
                continue

            # Filter to only tracks that have downloaded files
            downloaded_tracks = [t for t in lidarr_tracks if t.get("hasFile")]
            if not downloaded_tracks:
                continue

            # Build a map of track info
            track_map = {t["id"]: t for t in lidarr_tracks}

            # Score each downloaded track
            keep_tracks: list[dict] = []
            prune_tracks: list[dict] = []

            for track in downloaded_tracks:
                track_name = track.get("title", "").strip().lower()
                # Look up by exact name, then fuzzy match
                score = name_scores.get(track_name, 0)
                if score == 0:
                    # Fuzzy: check if any top track name is a substring
                    for top_name, top_score in name_scores.items():
                        if top_name in track_name or track_name in top_name:
                            score = top_score
                            break
                if score == 0:
                    score = 10  # Not in top tracks at all

                if score >= Config.POPULARITY_THRESHOLD:
                    keep_tracks.append(track)
                else:
                    prune_tracks.append(track)

            log.info("Album '%s': %d downloaded, %d above threshold, %d below",
                     album_name, len(downloaded_tracks), len(keep_tracks), len(prune_tracks))

            # Only prune if there are tracks to prune AND tracks to keep
            if not prune_tracks or not keep_tracks:
                if keep_tracks:
                    db.set_setting(pruned_key, "all_popular")
                continue

            # Delete below-threshold track files
            deleted = 0
            for track in prune_tracks:
                track_file_id = track.get("trackFileId")
                if track_file_id and lidarr.delete_track_file(track_file_id):
                    deleted += 1
                    log.info("Pruned '%s' from '%s' by %s",
                             track.get("title", "?"), album_name, artist["name"])

            # Unmonitor the album so Lidarr doesn't re-download
            lidarr.unmonitor_album(album_id)

            # Mark as pruned
            db.set_setting(pruned_key, f"kept:{len(keep_tracks)}_pruned:{deleted}")

            results.append(PruneResult(
                artist_name=artist["name"],
                album_name=album_name,
                total_tracks=len(downloaded_tracks),
                kept_tracks=len(keep_tracks),
                pruned_tracks=deleted,
            ))

    return results


def format_prune_results(results: list[PruneResult]) -> str:
    """Format prune results into a Discord-friendly message."""
    if not results:
        return "✂️ Nothing to prune — no new downloaded albums found."

    lines = ["✂️ **Prune Complete**\n"]
    total_pruned = 0
    total_kept = 0

    for r in results:
        if r.error:
            lines.append(f"**{r.artist_name}** — {r.album_name}: ❌ {r.error}")
        else:
            lines.append(
                f"**{r.artist_name}** — {r.album_name}: "
                f"kept {r.kept_tracks}, pruned {r.pruned_tracks}"
            )
            total_pruned += r.pruned_tracks
            total_kept += r.kept_tracks

    lines.append(f"\n**Summary:** {total_kept} track(s) kept, {total_pruned} pruned")
    return "\n".join(lines)


# ── Album-specific prune ─────────────────────────────────────────────────────

def prune_single_album(artist_id: int, artist_name: str, album_name: str,
                       lidarr_album_id: int) -> PruneResult:
    """Prune a specific album — delete below-threshold tracks and unmonitor."""
    try:
        music = MusicClient()
    except ValueError:
        return PruneResult(artist_name=artist_name, album_name=album_name, error="Music client init failed")
    try:
        lidarr = LidarrClient()
    except ValueError:
        return PruneResult(artist_name=artist_name, album_name=album_name, error="Lidarr client init failed")

    # Get artist's Deezer ID for popularity lookup
    artist = None
    for a in db.list_artists():
        if a["id"] == artist_id:
            artist = a
            break
    if not artist:
        return PruneResult(artist_name=artist_name, album_name=album_name, error="Artist not found")

    music_id = artist.get("spotify_id")

    # Get unified popularity scores (Last.fm primary, Deezer fallback)
    from popularity import get_artist_track_scores, score_track
    scores = get_artist_track_scores(artist_name, deezer_id=music_id)

    # Get never-prune tracks for this album
    never_prune = db.get_never_prune_tracks(artist_id, album_name)
    if never_prune:
        log.info("Never-prune tracks for '%s': %s", album_name, never_prune)

    # Get downloaded tracks
    lidarr_tracks = lidarr.get_album_tracks(lidarr_album_id)
    if not lidarr_tracks:
        return PruneResult(artist_name=artist_name, album_name=album_name, error="No tracks found")

    downloaded = [t for t in lidarr_tracks if t.get("hasFile")]
    if not downloaded:
        return PruneResult(artist_name=artist_name, album_name=album_name, error="No downloaded files")

    # Score and split
    keep_tracks: list[dict] = []
    prune_tracks: list[dict] = []
    for track in downloaded:
        tname = track.get("title", "").strip().lower()
        tid = track.get("id")

        # Never-prune override
        if tname in {np.lower() for np in never_prune}:
            log.info("Track '%s': NEVER PRUNE (protected)", track.get("title"))
            keep_tracks.append(track)
            continue

        score = score_track(tname, scores, deezer_id=tid)
        log.info("Track '%s': score=%d, threshold=%d, → %s",
                 track.get("title", "?"), score, Config.POPULARITY_THRESHOLD,
                 "KEEP" if score >= Config.POPULARITY_THRESHOLD else "PRUNE")
        if score >= Config.POPULARITY_THRESHOLD:
            keep_tracks.append(track)
        else:
            prune_tracks.append(track)

    if not prune_tracks:
        db.set_album_status(artist_id, album_name, "pruned", lidarr_album_id)
        return PruneResult(artist_name=artist_name, album_name=album_name,
                           total_tracks=len(downloaded), kept_tracks=len(keep_tracks), pruned_tracks=0)

    if not keep_tracks:
        return PruneResult(artist_name=artist_name, album_name=album_name,
                           total_tracks=len(downloaded), kept_tracks=0, pruned_tracks=0,
                           error="All tracks below threshold — keeping album")

    # Delete below-threshold tracks
    deleted = 0
    for track in prune_tracks:
        track_file_id = track.get("trackFileId")
        if track_file_id and lidarr.delete_track_file(track_file_id):
            deleted += 1
            log.info("Pruned '%s' from '%s' by %s", track.get("title"), album_name, artist_name)

    # Unmonitor the album
    lidarr.unmonitor_album(lidarr_album_id)

    # Mark as pruned
    db.set_album_status(artist_id, album_name, "pruned", lidarr_album_id)

    return PruneResult(artist_name=artist_name, album_name=album_name,
                       total_tracks=len(downloaded), kept_tracks=len(keep_tracks), pruned_tracks=deleted)


# ── Download status checker ──────────────────────────────────────────────────

def check_downloads() -> list[PruneResult]:
    """
    Check all pending albums in Lidarr. If downloaded, auto-prune.
    Returns prune results for newly completed downloads.
    """
    pending = db.get_pending_albums()
    if not pending:
        return []

    try:
        lidarr = LidarrClient()
    except ValueError:
        return []

    results: list[PruneResult] = []

    for album in pending:
        artist_id = album["artist_id"]
        artist_name = album["artist_name"]
        album_name = album["album_name"]
        lidarr_album_id = album.get("lidarr_album_id")

        if not lidarr_album_id:
            continue

        # Check if album has downloaded files
        tracks = lidarr.get_album_tracks(lidarr_album_id)
        downloaded = [t for t in tracks if t.get("hasFile")]

        if not downloaded:
            # Still downloading or not started
            log.info("Album '%s' by %s: not downloaded yet (%d tracks, 0 files)",
                     album_name, artist_name, len(tracks))
            continue

        # Album is downloaded — prune it
        log.info("Album '%s' by %s: download complete (%d files), pruning...",
                 album_name, artist_name, len(downloaded))
        db.set_album_status(artist_id, album_name, "downloaded", lidarr_album_id)
        result = prune_single_album(artist_id, artist_name, album_name, lidarr_album_id)
        results.append(result)

    return results


def format_download_check_results(results: list[PruneResult]) -> str:
    """Format download check results."""
    if not results:
        return "📥 No newly downloaded albums to prune."

    lines = ["📥 **Download Check + Auto-Prune Complete**\n"]
    for r in results:
        if r.error:
            lines.append(f"**{r.artist_name}** — {r.album_name}: {r.error}")
        else:
            lines.append(f"**{r.artist_name}** — {r.album_name}: kept {r.kept_tracks}, pruned {r.pruned_tracks}")
    return "\n".join(lines)


def _match_album(album_name: str, lidarr_albums: list[dict]) -> dict | None:
    """Try to match a Deezer album name to a Lidarr album."""
    import re
    def normalize(s: str) -> str:
        # Strip punctuation, lowercase, collapse whitespace
        return re.sub(r'[^\w\s]', '', s.strip().lower()).strip()

    album_lower = normalize(album_name)

    # Exact match first (normalized)
    for la in lidarr_albums:
        if normalize(la.get("title", "")) == album_lower:
            return la

    # Contains match (normalized)
    for la in lidarr_albums:
        lidarr_lower = normalize(la.get("title", ""))
        if album_lower in lidarr_lower or lidarr_lower in album_lower:
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
