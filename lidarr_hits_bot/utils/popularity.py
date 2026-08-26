"""Unified popularity scorer — Last.fm primary, Deezer fallback."""

import logging
from lidarr_hits_bot.config import Config

log = logging.getLogger(__name__)


def get_artist_track_scores(artist_name: str, deezer_id: str = None) -> dict[str, int]:
    """
    Get track popularity scores for an artist, normalized to 0-100.
    Uses Last.fm as primary (more data), Deezer as fallback.

    Returns {track_name_lower: score} dict.
    """
    scores: dict[str, int] = {}

    # ── Last.fm (primary) ────────────────────────────────────────────────
    if Config.LASTFM_API_KEY:
        try:
            from lidarr_hits_bot.clients.lastfm import LastfmClient
            lfm = LastfmClient()
            lfm_scores = lfm.get_artist_top_tracks_scored(artist_name, limit=50)
            if lfm_scores:
                scores = lfm_scores
                log.info("Last.fm: got %d track scores for '%s'", len(scores), artist_name)
        except Exception as e:
            log.warning("Last.fm scoring failed for '%s': %s", artist_name, e)

    # ── Deezer (fallback / supplement) ───────────────────────────────────
    if deezer_id:
        try:
            from lidarr_hits_bot.clients.deezer import MusicClient
            music = MusicClient()
            top_tracks = music.get_artist_top_tracks(deezer_id)

            if top_tracks:
                # Build Deezer scores
                ranks = [t.get("rank", 0) for t in top_tracks if t.get("rank", 0) > 0]
                max_rank = max(ranks) if ranks else 1

                deezer_scores: dict[str, int] = {}
                id_scores: dict[int, int] = {}

                for i, t in enumerate(top_tracks):
                    tname = t.get("title", "").strip().lower()
                    tid = t.get("id")
                    rank = t.get("rank", 0)
                    if rank > 0:
                        score = max(10, min(100, int((rank / max_rank) * 100)))
                    else:
                        score = max(50, 100 - int((i / len(top_tracks)) * 50)) if top_tracks else 10
                    if tname:
                        deezer_scores[tname] = score
                    if tid:
                        id_scores[tid] = score

                # Merge: Last.fm takes priority, Deezer fills gaps
                for name, score in deezer_scores.items():
                    if name not in scores:
                        scores[name] = score

                # Also store ID-based scores for Deezer matching
                scores["__deezer_ids__"] = id_scores  # Special key for ID lookup

                log.info("Deezer: supplemented %d track scores for '%s'", len(deezer_scores), artist_name)
        except Exception as e:
            log.warning("Deezer scoring failed for '%s': %s", artist_name, e)

    if not scores:
        log.warning("No popularity data found for '%s'", artist_name)

    return scores


def score_track(track_name: str, scores: dict[str, int], deezer_id: int = None) -> int:
    """
    Score a single track using the unified scores dict.
    Tries: Deezer ID match → exact name → fuzzy name → default 10.
    """
    tname = track_name.strip().lower()

    # Try Deezer ID match
    deezer_ids = scores.get("__deezer_ids__", {})
    if deezer_id and deezer_id in deezer_ids:
        return deezer_ids[deezer_id]

    # Try exact name match
    if tname in scores:
        return scores[tname]

    # Try fuzzy name match
    for top_name, top_score in scores.items():
        if top_name.startswith("__"):
            continue
        if top_name in tname or tname in top_name:
            return top_score

    return 10  # Default for unknown tracks
