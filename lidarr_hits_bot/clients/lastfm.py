"""Last.fm API client — free API, best source for track popularity data."""

import logging
from typing import Optional

import requests

from lidarr_hits_bot.config import Config

log = logging.getLogger(__name__)

LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"


def _get(params: dict) -> dict:
    """GET request to Last.fm API."""
    params["api_key"] = Config.LASTFM_API_KEY
    params["format"] = "json"
    resp = requests.get(LASTFM_BASE, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


class LastfmClient:
    """Last.fm client for track popularity data."""

    def __init__(self):
        if not Config.LASTFM_API_KEY:
            raise ValueError("LASTFM_API_KEY must be set")

    def get_artist_top_tracks(self, artist_name: str, limit: int = 50) -> list[dict]:
        """Get artist's top tracks with play counts. Returns up to `limit` tracks."""
        try:
            result = _get({
                "method": "artist.gettoptracks",
                "artist": artist_name,
                "limit": limit,
            })
            tracks = result.get("toptracks", {}).get("track", [])
            if isinstance(tracks, dict):
                tracks = [tracks]
            return tracks
        except Exception as e:
            log.warning("Last.fm top tracks failed for '%s': %s", artist_name, e)
            return []

    def get_track_info(self, artist_name: str, track_name: str) -> Optional[dict]:
        """Get detailed info for a specific track including play count."""
        try:
            result = _get({
                "method": "track.getInfo",
                "artist": artist_name,
                "track": track_name,
            })
            return result.get("track")
        except Exception as e:
            log.warning("Last.fm track info failed for '%s' - '%s': %s", artist_name, track_name, e)
            return None

    def get_artist_top_tracks_scored(self, artist_name: str, limit: int = 50) -> dict[str, int]:
        """
        Get artist's top tracks normalized to 0-100 scale.
        Returns {track_name_lower: score} dict.
        """
        tracks = self.get_artist_top_tracks(artist_name, limit)
        if not tracks:
            return {}

        # Get play counts
        play_counts = []
        for t in tracks:
            count = int(t.get("playcount", 0))
            play_counts.append(count)

        max_count = max(play_counts) if play_counts else 1
        if max_count == 0:
            max_count = 1

        scores: dict[str, int] = {}
        for t in tracks:
            name = t.get("name", "").strip().lower()
            count = int(t.get("playcount", 0))
            # Normalize to 0-100
            score = max(10, min(100, int((count / max_count) * 100)))
            if name:
                scores[name] = score

        return scores
