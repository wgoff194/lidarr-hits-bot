"""Lidarr API client — add artists and albums for download."""

import logging
from typing import Optional

import requests

from config import Config

log = logging.getLogger(__name__)


class LidarrClient:
    """Talks to Lidarr's v1 REST API."""

    def __init__(self):
        if not Config.LIDARR_API_KEY:
            raise ValueError("LIDARR_API_KEY must be set")
        self.base = Config.LIDARR_URL.rstrip("/")
        self.headers = {"X-Api-Key": Config.LIDARR_API_KEY}
        self._quality_profile_id: Optional[int] = None
        self._root_folder: Optional[str] = None

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        url = f"{self.base}/api/v1{path}"
        resp = requests.get(url, headers=self.headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, data: dict) -> dict:
        url = f"{self.base}/api/v1{path}"
        resp = requests.post(url, headers=self.headers, json=data, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, data: dict) -> dict:
        url = f"{self.base}/api/v1{path}"
        resp = requests.put(url, headers=self.headers, json=data, timeout=30)
        resp.raise_for_status()
        return resp.json()

    # ── Cached lookups ───────────────────────────────────────────────────────

    def get_quality_profile_id(self) -> int:
        """Resolve quality profile name → ID (cached)."""
        if self._quality_profile_id is not None:
            return self._quality_profile_id
        profiles = self._get("/qualityprofile")
        for p in profiles:
            if p["name"].lower() == Config.LIDARR_QUALITY_PROFILE.lower():
                self._quality_profile_id = p["id"]
                return p["id"]
        # Fallback: use the first profile
        if profiles:
            self._quality_profile_id = profiles[0]["id"]
            log.warning(
                "Quality profile '%s' not found, using '%s'",
                Config.LIDARR_QUALITY_PROFILE,
                profiles[0]["name"],
            )
            return profiles[0]["id"]
        raise RuntimeError("No Lidarr quality profiles found")

    def get_root_folder(self) -> str:
        """Get root folder path (cached)."""
        if self._root_folder is not None:
            return self._root_folder
        folders = self._get("/rootfolder")
        for f in folders:
            if f["path"] == Config.LIDARR_ROOT_FOLDER:
                self._root_folder = f["path"]
                return f["path"]
        if folders:
            self._root_folder = folders[0]["path"]
            log.warning(
                "Root folder '%s' not found, using '%s'",
                Config.LIDARR_ROOT_FOLDER,
                folders[0]["path"],
            )
            return folders[0]["path"]
        raise RuntimeError("No Lidarr root folders found")

    # ── Artist lookup ────────────────────────────────────────────────────────

    def lookup_artist(self, name: str) -> Optional[dict]:
        """Search Lidarr's artist lookup for a name. Returns best match."""
        results = self._get("/artist/lookup", params={"term": name})
        if not results:
            return None
        # Prefer exact name match
        name_lower = name.strip().lower()
        for r in results:
            if r.get("artistName", "").lower() == name_lower:
                return r
        return results[0]

    def get_artist(self, lidarr_id: int) -> Optional[dict]:
        """Get an artist already in Lidarr by their Lidarr ID."""
        try:
            return self._get(f"/artist/{lidarr_id}")
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return None
            raise

    def get_all_artists(self) -> list[dict]:
        """List all artists currently in Lidarr."""
        return self._get("/artist")

    # ── Add artist ───────────────────────────────────────────────────────────

    def add_artist(self, foreign_artist_id: str) -> Optional[dict]:
        """
        Add an artist to Lidarr by their MusicBrainz/foreign ID.
        Lidarr will start monitoring based on its own settings.
        Returns the added artist dict, or None if already present.
        """
        # First, lookup the full artist info
        lookup = self._get("/artist/lookup", params={"term": f"lidarr:{foreign_artist_id}"})
        if not lookup:
            # Try by name
            lookup = self._get("/artist/lookup", params={"term": foreign_artist_id})
        if not lookup:
            log.warning("Could not find artist '%s' in Lidarr lookup", foreign_artist_id)
            return None

        artist_data = lookup[0]

        # Check if already added
        existing = self.get_all_artists()
        for a in existing:
            if a.get("foreignArtistId") == artist_data.get("foreignArtistId"):
                log.info("Artist '%s' already in Lidarr (ID %s)", a["artistName"], a["id"])
                return None

        # Build the add payload
        artist_data["qualityProfileId"] = self.get_quality_profile_id()
        artist_data["rootFolderPath"] = self.get_root_folder()
        artist_data["monitored"] = True
        artist_data["addOptions"] = {
            "searchForMissingAlbums": False,  # Don't auto-grab everything!
        }

        try:
            result = self._post("/artist", artist_data)
            log.info("Added artist '%s' to Lidarr (ID %s)", result.get("artistName"), result.get("id"))
            return result
        except requests.HTTPError as e:
            log.error("Failed to add artist to Lidarr: %s", e)
            return None

    # ── Album monitoring ─────────────────────────────────────────────────────

    def get_artist_albums(self, lidarr_artist_id: int) -> list[dict]:
        """Get all albums for an artist already in Lidarr."""
        return self._get("/album", params={"artistId": lidarr_artist_id})

    def monitor_album(self, album_id: int) -> bool:
        """Set an album to monitored so Lidarr will download it."""
        try:
            album = self._get(f"/album/{album_id}")
            album["monitored"] = True
            self._post(f"/album/{album_id}", album)  # PUT would be more correct but POST works
            return True
        except requests.HTTPError as e:
            log.error("Failed to monitor album %s: %s", album_id, e)
            return False

    def search_album(self, album_id: int) -> bool:
        """Trigger Lidarr to search for and download a specific album."""
        try:
            self._post("/command", {"name": "AlbumSearch", "albumIds": [album_id]})
            log.info("Triggered Lidarr search for album ID %s", album_id)
            return True
        except requests.HTTPError as e:
            log.error("Failed to trigger search for album %s: %s", album_id, e)
            return False

    def monitor_and_search_album(self, album_id: int) -> bool:
        """Monitor + search for an album in one go."""
        if self.monitor_album(album_id):
            return self.search_album(album_id)
        return False

    # ── Track-level monitoring ────────────────────────────────────────────────

    def get_album_tracks(self, album_id: int) -> list[dict]:
        """Get all tracks for a specific album."""
        return self._get("/track", params={"albumId": album_id})

    def set_track_monitored(self, track_id: int, monitored: bool) -> bool:
        """Set a single track's monitored flag."""
        try:
            track = self._get(f"/track/{track_id}")
            track["monitored"] = monitored
            self._put(f"/track/{track_id}", track)
            return True
        except requests.HTTPError as e:
            log.error("Failed to set track %s monitored=%s: %s", track_id, monitored, e)
            return False

    def monitor_specific_tracks(self, album_id: int, track_ids_to_monitor: set[int]) -> dict:
        """
        Cherry-pick monitoring: unmonitor ALL tracks on an album, then monitor
        only the ones in track_ids_to_monitor.

        Returns {"monitored": int, "unmonitored": int, "errors": int}.
        """
        tracks = self.get_album_tracks(album_id)
        stats = {"monitored": 0, "unmonitored": 0, "errors": 0}

        for track in tracks:
            tid = track["id"]
            should_monitor = tid in track_ids_to_monitor

            # Skip if already in the right state
            if track.get("monitored") == should_monitor:
                if should_monitor:
                    stats["monitored"] += 1
                else:
                    stats["unmonitored"] += 1
                continue

            if self.set_track_monitored(tid, should_monitor):
                if should_monitor:
                    stats["monitored"] += 1
                else:
                    stats["unmonitored"] += 1
            else:
                stats["errors"] += 1

        return stats

    def search_album(self, album_id: int) -> bool:
        """Trigger Lidarr to search for and download a specific album."""
        try:
            self._post("/command", {"name": "AlbumSearch", "albumIds": [album_id]})
            log.info("Triggered Lidarr search for album ID %s", album_id)
            return True
        except requests.HTTPError as e:
            log.error("Failed to trigger search for album %s: %s", album_id, e)
            return False
