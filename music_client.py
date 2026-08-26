"""Music API client — uses Deezer (free, no auth) for artist lookups and popularity."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from config import Config

log = logging.getLogger(__name__)

DEEZER_BASE = "https://api.deezer.com"


@dataclass
class TrackPopularity:
    """Per-track popularity info."""
    name: str
    popularity: int  # 0-100 normalized score


@dataclass
class AlbumInfo:
    name: str
    deezer_id: str
    deezer_url: str
    release_date: str
    album_type: str  # album, single
    total_tracks: int
    avg_popularity: float
    top_track_names: list[str]
    track_popularities: list[TrackPopularity]


def _get(path: str, params: dict | None = None) -> dict | list:
    """GET request to Deezer API with error handling."""
    url = f"{DEEZER_BASE}{path}"
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


class MusicClient:
    """
    Deezer-based music client. No API key needed.
    Provides the same interface the bot expects: search_artist, get_new_releases, etc.
    """

    def __init__(self):
        pass  # No auth needed for Deezer

    # ── Artist lookup ────────────────────────────────────────────────────────

    def search_artist(self, name: str) -> Optional[dict]:
        """Search for an artist by name. Returns best match or None."""
        results = _get("/search/artist", params={"q": name, "limit": 5})
        artists = results.get("data", [])
        if not artists:
            return None

        # Prefer exact name match
        name_lower = name.strip().lower()
        for a in artists:
            if a.get("name", "").lower() == name_lower:
                return _normalize_artist(a)

        return _normalize_artist(artists[0])

    def get_artist_top_tracks(self, artist_id: str) -> list[dict]:
        """Get an artist's top tracks (up to 50). Used for popularity ranking."""
        result = _get(f"/artist/{artist_id}/top", params={"limit": 50})
        return result.get("data", [])

    # ── Album discovery ──────────────────────────────────────────────────────

    def get_new_releases(
        self, artist_id: str, lookback_days: int = 90
    ) -> list[AlbumInfo]:
        """
        Get an artist's albums/singles released in the last N days,
        scored by track popularity relative to the artist's top tracks.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        albums_seen: set[str] = set()
        results: list[AlbumInfo] = []

        # Get the artist's top tracks for popularity ranking
        top_tracks = self.get_artist_top_tracks(artist_id)
        # Build a set of top track IDs and a rank map
        top_track_ids: set[int] = set()
        top_track_ranks: dict[int, int] = {}  # track_id -> position (0 = most popular)
        for i, t in enumerate(top_tracks):
            tid = t.get("id")
            if tid:
                top_track_ids.add(tid)
                top_track_ranks[tid] = i

        # Get all albums/singles
        offset = 0
        while True:
            page = _get(f"/artist/{artist_id}/albums", params={
                "limit": 50,
                "offset": offset,
            })
            items = page.get("data", [])
            if not items:
                break

            for album in items:
                aid = str(album["id"])
                if aid in albums_seen:
                    continue
                albums_seen.add(aid)

                # Parse release date
                rd = album.get("release_date", "")
                if not rd:
                    continue
                try:
                    release_dt = datetime.strptime(rd, "%Y-%m-%d").replace(
                        tzinfo=timezone.utc
                    )
                except ValueError:
                    continue

                if release_dt < cutoff:
                    continue

                # Get album tracks
                album_tracks_resp = _get(f"/album/{aid}/tracks")
                tracks = album_tracks_resp.get("data", [])
                if not tracks:
                    continue

                # Score each track
                popularities: list[int] = []
                top_names: list[str] = []
                track_pops: list[TrackPopularity] = []

                for track in tracks:
                    tid = track.get("id")
                    tname = track.get("title", "Unknown")

                    # Calculate popularity score (0-100)
                    score = self._calculate_track_score(tid, top_track_ids, top_track_ranks, len(top_tracks))
                    popularities.append(score)
                    track_pops.append(TrackPopularity(name=tname, popularity=score))

                    if score >= Config.POPULARITY_THRESHOLD:
                        top_names.append(tname)

                avg_pop = sum(popularities) / len(popularities) if popularities else 0

                # Determine album type
                record_type = album.get("record_type", "album")
                if record_type not in ("album", "single", "ep"):
                    record_type = "album"

                results.append(AlbumInfo(
                    name=album.get("title", "Unknown"),
                    deezer_id=aid,
                    deezer_url=album.get("link", ""),
                    release_date=rd,
                    album_type=record_type,
                    total_tracks=len(tracks),
                    avg_popularity=round(avg_pop, 1),
                    top_track_names=top_names[:5],
                    track_popularities=track_pops,
                ))

            # Deezer pagination
            next_url = page.get("next")
            if not next_url:
                break
            offset += 50

        return results

    def _calculate_track_score(
        self,
        track_id: int | None,
        top_track_ids: set[int],
        top_track_ranks: dict[int, int],
        total_top: int,
    ) -> int:
        """
        Calculate a 0-100 popularity score for a track.
        Tracks in the artist's top list get high scores based on their position.
        Tracks not in the top list get a low default score.
        """
        if not track_id or total_top == 0:
            return 10  # Unknown track, low default

        if track_id in top_track_ids:
            # Position 0 = most popular = 100, position N = 100 - (N * 100/N)
            position = top_track_ranks.get(track_id, total_top)
            # Linear scale: position 0 → 100, last position → ~50
            score = max(50, 100 - int((position / total_top) * 50))
            return score

        # Not in top tracks — low score
        return 10

    def should_download_album(self, album: AlbumInfo) -> bool:
        """Decide if an album is worth downloading based on popularity."""
        if album.avg_popularity >= Config.POPULARITY_THRESHOLD:
            return True
        if len(album.top_track_names) >= 2:
            return True
        return False

    def get_artist_top_albums(self, artist_id: str) -> list[AlbumInfo]:
        """
        Full-catalog scan: score the artist's ALBUMS based on Deezer top tracks.
        Gets albums from Deezer, filters out singles, and scores by top track overlap.
        """
        top_tracks = self.get_artist_top_tracks(artist_id)
        if not top_tracks:
            return []

        # Build name-based popularity map
        total_top = len(top_tracks)
        name_scores: dict[str, int] = {}
        for i, t in enumerate(top_tracks):
            tname = t.get("title", "").strip().lower()
            if tname:
                score = max(50, 100 - int((i / total_top) * 50)) if total_top > 0 else 10
                name_scores[tname] = score

        # Get all albums from Deezer, filter out singles (1-track albums)
        albums_seen: set[str] = set()
        results: list[AlbumInfo] = []
        offset = 0

        while True:
            page = _get(f"/artist/{artist_id}/albums", params={"limit": 50, "offset": offset})
            items = page.get("data", [])
            if not items:
                break

            for album in items:
                aid = str(album["id"])
                if aid in albums_seen:
                    continue
                albums_seen.add(aid)

                # Skip compilations with very few tracks (likely noise)
                nb_tracks = album.get("nb_tracks", 0)
                record_type = album.get("record_type", "album")
                if nb_tracks < 1:
                    continue

                # Get album tracks
                try:
                    tracks_resp = _get(f"/album/{aid}/tracks")
                    tracks = tracks_resp.get("data", [])
                except Exception:
                    continue

                if not tracks:
                    continue

                # Score each track by name matching
                popularities: list[int] = []
                top_names: list[str] = []
                track_pops: list[TrackPopularity] = []

                for track in tracks:
                    tname = track.get("title", "Unknown")
                    tname_lower = tname.strip().lower()

                    # Exact match
                    score = name_scores.get(tname_lower, 0)
                    # Fuzzy match
                    if score == 0:
                        for top_name, top_score in name_scores.items():
                            if top_name in tname_lower or tname_lower in top_name:
                                score = top_score
                                break
                    if score == 0:
                        score = 10

                    popularities.append(score)
                    track_pops.append(TrackPopularity(name=tname, popularity=score))
                    if score >= Config.POPULARITY_THRESHOLD:
                        top_names.append(tname)

                avg_pop = sum(popularities) / len(popularities) if popularities else 0

                # Parse release date
                rd = album.get("release_date", "")
                if not rd:
                    rd = "0000-00-00"

                results.append(AlbumInfo(
                    name=album.get("title", "Unknown"),
                    deezer_id=aid,
                    deezer_url=album.get("link", ""),
                    release_date=rd,
                    album_type=record_type,
                    total_tracks=len(tracks),
                    avg_popularity=round(avg_pop, 1),
                    top_track_names=top_names[:5],
                    track_popularities=track_pops,
                ))

            if not page.get("next"):
                break
            offset += 50

        # Sort by number of popular tracks (most hits first)
        results.sort(key=lambda a: len(a.top_track_names), reverse=True)
        return results


def _normalize_artist(deezer_artist: dict) -> dict:
    """Normalize a Deezer artist dict to the format the bot expects."""
    return {
        "id": str(deezer_artist.get("id", "")),
        "name": deezer_artist.get("name", "Unknown"),
        "genres": [],  # Deezer doesn't return genres in search
        "popularity": None,  # Deezer uses nb_fan instead
        "nb_fan": deezer_artist.get("nb_fan", 0),
        "images": [{"url": deezer_artist.get("picture_medium", "")}] if deezer_artist.get("picture_medium") else [],
    }
