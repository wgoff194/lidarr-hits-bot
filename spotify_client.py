"""Spotify Web API client — uses Client Credentials flow (no user login)."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import spotipy
from spotipy.oauth_manager import SpotifyClientCredentials

from config import Config

log = logging.getLogger(__name__)


@dataclass
class TrackPopularity:
    """Per-track popularity info from Spotify."""
    name: str
    popularity: int


@dataclass
class AlbumInfo:
    name: str
    spotify_id: str
    spotify_url: str
    release_date: str
    album_type: str  # album, single, compilation
    total_tracks: int
    avg_popularity: float
    top_track_names: list[str]
    track_popularities: list[TrackPopularity]  # ALL tracks with their popularity scores


class SpotifyClient:
    """Thin wrapper around spotipy for artist lookups and popularity checks."""

    def __init__(self):
        if not Config.SPOTIFY_CLIENT_ID or not Config.SPOTIFY_CLIENT_SECRET:
            raise ValueError("SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET must be set")
        auth = SpotifyClientCredentials(
            client_id=Config.SPOTIFY_CLIENT_ID,
            client_secret=Config.SPOTIFY_CLIENT_SECRET,
        )
        self.sp = spotipy.Spotify(auth_manager=auth)

    # ── Artist lookup ────────────────────────────────────────────────────────

    def search_artist(self, name: str) -> Optional[dict]:
        """Search Spotify for an artist by name. Returns best match or None."""
        results = self.sp.search(q=f"artist:{name}", type="artist", limit=5)
        artists = results.get("artists", {}).get("items", [])
        if not artists:
            return None

        # Prefer exact-ish name match, otherwise take the first (most popular)
        name_lower = name.strip().lower()
        for a in artists:
            if a["name"].lower() == name_lower:
                return a
        return artists[0]

    def get_artist_top_tracks(self, artist_id: str, market: str = "US") -> list[dict]:
        """Get an artist's top tracks (max 10 from Spotify endpoint)."""
        result = self.sp.artist_top_tracks(artist_id, country=market)
        return result.get("tracks", [])

    # ── Album discovery ──────────────────────────────────────────────────────

    def get_new_releases(
        self, artist_id: str, lookback_days: int = 90
    ) -> list[AlbumInfo]:
        """
        Get an artist's albums/singles released in the last N days,
        scored by average track popularity.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        albums_seen: set[str] = set()
        results: list[AlbumInfo] = []

        # Spotify returns albums in pages; grab all of them
        offset = 0
        while True:
            page = self.sp.artist_albums(
                artist_id,
                album_type="album,single",
                country="US",
                limit=50,
                offset=offset,
            )
            items = page.get("items", [])
            if not items:
                break

            for album in items:
                aid = album["id"]
                if aid in albums_seen:
                    continue
                albums_seen.add(aid)

                # Parse release date (can be YYYY, YYYY-MM, or YYYY-MM-DD)
                rd = album.get("release_date", "")
                try:
                    if len(rd) == 4:
                        release_dt = datetime(int(rd), 1, 1, tzinfo=timezone.utc)
                    elif len(rd) == 7:
                        release_dt = datetime.strptime(rd, "%Y-%m").replace(
                            tzinfo=timezone.utc
                        )
                    else:
                        release_dt = datetime.strptime(rd, "%Y-%m-%d").replace(
                            tzinfo=timezone.utc
                        )
                except ValueError:
                    continue

                if release_dt < cutoff:
                    continue

                # Get full album details for track popularity
                full = self.sp.album(aid)
                tracks = full.get("tracks", {}).get("items", [])
                if not tracks:
                    continue

                # Fetch popularity for each track
                track_ids = [t["id"] for t in tracks if t.get("id")]
                popularities = []
                top_names = []
                track_pops: list[TrackPopularity] = []
                for batch_start in range(0, len(track_ids), 50):
                    batch = track_ids[batch_start : batch_start + 50]
                    audio_features = self.sp.tracks(batch)
                    for t in audio_features.get("tracks", []):
                        pop = t.get("popularity", 0)
                        tname = t.get("name", "Unknown")
                        popularities.append(pop)
                        track_pops.append(TrackPopularity(name=tname, popularity=pop))
                        if pop >= Config.POPULARITY_THRESHOLD:
                            top_names.append(tname)

                avg_pop = sum(popularities) / len(popularities) if popularities else 0

                results.append(
                    AlbumInfo(
                        name=full.get("name", "Unknown"),
                        spotify_id=aid,
                        spotify_url=full.get("external_urls", {}).get("spotify", ""),
                        release_date=rd,
                        album_type=album.get("album_type", "album"),
                        total_tracks=len(tracks),
                        avg_popularity=round(avg_pop, 1),
                        top_track_names=top_names[:5],
                        track_popularities=track_pops,
                    )
                )

            if page.get("next"):
                offset += 50
            else:
                break

        return results

    def should_download_album(self, album: AlbumInfo) -> bool:
        """Decide if an album is worth downloading based on popularity."""
        # Album-level: average popularity must be above threshold
        # OR at least 2 individual tracks above threshold
        if album.avg_popularity >= Config.POPULARITY_THRESHOLD:
            return True
        if len(album.top_track_names) >= 2:
            return True
        return False
