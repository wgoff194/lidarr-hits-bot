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
        self._metadata_profile_id: Optional[int] = None

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

    # ── Metadata profiles ─────────────────────────────────────────────────────

    def get_metadata_profiles(self) -> list[dict]:
        """Get all metadata profiles from Lidarr. Returns list of {id, name}."""
        return self._get("/metadataprofile")

    def get_metadata_profile_id(self) -> int:
        """Get the default metadata profile ID (cached)."""
        if self._metadata_profile_id is not None:
            return self._metadata_profile_id
        profiles = self.get_metadata_profiles()
        if profiles:
            self._metadata_profile_id = profiles[0]["id"]
            return profiles[0]["id"]
        raise RuntimeError("No Lidarr metadata profiles found")

    def resolve_metadata_profile(self, folder_name: str) -> int:
        """
        Auto-select metadata profile based on root folder name.
        Comedy folder → Comedy profile
        Soundtracks folder → Soundtrack profile
        Everything else → 99 (Everything) by default
        """
        folder_lower = folder_name.strip().lower()
        profiles = self.get_metadata_profiles()

        for p in profiles:
            pname = p["name"].strip().lower()
            if "comedy" in folder_lower or "comedy" in pname:
                if "comedy" in pname:
                    return p["id"]
            elif "soundtrack" in folder_lower or "soundtrack" in pname:
                if "soundtrack" in pname:
                    return p["id"]

        # Default: 99 - Everything (includes singles)
        for p in profiles:
            if "99" in p["name"] or "everything" in p["name"].lower():
                return p["id"]

        # Fallback: first profile
        return profiles[0]["id"] if profiles else 1

    def get_root_folders(self) -> list[dict]:
        """
        Get all root folders from Lidarr (always fresh from API).
        Returns list of dicts with:
        - path: full filesystem path (e.g. "/music/Warren's Music")
        - name: derived from the last path component (e.g. "Warren's Music")
        """
        raw = self._get("/rootfolder")
        folders = []
        for f in raw:
            path = f["path"].rstrip("/")
            # Derive a friendly name from the last path component
            name = path.split("/")[-1] if "/" in path else path
            folders.append({
                "path": f["path"],
                "name": name,
                "id": f.get("id"),
            })
        return folders

    def resolve_root_folder(self, name_or_path: str) -> Optional[str]:
        """
        Resolve a folder name (case-insensitive) to its full path.
        Accepts either the friendly name ("Warren's Music") or a full path.
        Returns the full path, or None if not found.
        """
        folders = self.get_root_folders()
        search = name_or_path.strip().lower()

        # Try exact path match first
        for f in folders:
            if f["path"].rstrip("/").lower() == search:
                return f["path"]

        # Try name match (last path component)
        for f in folders:
            if f["name"].lower() == search:
                return f["path"]

        # Try partial match
        for f in folders:
            if search in f["name"].lower():
                return f["path"]

        return None

    def get_root_folder(self, folder_path: Optional[str] = None) -> str:
        """
        Get a root folder path. If folder_path is provided, validate it exists.
        Otherwise fall back to the .env default.
        """
        if folder_path:
            resolved = self.resolve_root_folder(folder_path)
            if resolved:
                return resolved
            log.warning("Root folder '%s' not found, falling back to default", folder_path)

        folders = self.get_root_folders()
        for f in folders:
            if f["path"] == Config.LIDARR_ROOT_FOLDER:
                return f["path"]
        if folders:
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

    def add_artist(self, foreign_artist_id: str, root_folder: Optional[str] = None,
                   metadata_profile_id: Optional[int] = None) -> Optional[dict]:
        """
        Add an artist to Lidarr by their MusicBrainz/foreign ID.
        root_folder: full path or friendly name (e.g. "Warren's Music").
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
        if metadata_profile_id:
            artist_data["metadataProfileId"] = metadata_profile_id
        else:
            folder_name = artist_data.get("rootFolderPath", "").rstrip("/").split("/")[-1]
            artist_data["metadataProfileId"] = self.resolve_metadata_profile(folder_name)
        artist_data["rootFolderPath"] = self.get_root_folder(root_folder)
        artist_data["monitored"] = True
        artist_data["addOptions"] = {
            "searchForMissingAlbums": False,
            "monitor": "none",  # Don't monitor any albums — we'll pick them ourselves
        }

        # Remove fields Lidarr doesn't accept on POST
        for key in ["id", "statistics", "genres", "tags", "added"]:
            artist_data.pop(key, None)

        log.info("Adding artist '%s' to Lidarr (root: %s, profile: %s)",
                 artist_data.get("artistName"), artist_data["rootFolderPath"], artist_data["qualityProfileId"])

        try:
            result = self._post("/artist", artist_data)
            log.info("Added artist '%s' to Lidarr (ID %s)", result.get("artistName"), result.get("id"))
            # Refresh artist so Lidarr fetches album metadata from MusicBrainz
            if result.get("id"):
                self._post("/command", {"name": "RefreshArtist", "artistIds": [result["id"]]})
                log.info("Triggered refresh for artist ID %s", result["id"])
            return result
        except requests.HTTPError as e:
            error_body = ""
            if e.response is not None:
                try:
                    error_body = e.response.json()
                except Exception:
                    error_body = e.response.text[:500]
            log.error("Failed to add artist to Lidarr: %s — %s", e, error_body)
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
            self._put(f"/album/{album_id}", album)
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

    # ── Track files (downloaded) ─────────────────────────────────────────────

    def get_album_track_files(self, album_id: int) -> list[dict]:
        """Get downloaded track files for an album."""
        try:
            return self._get("/trackfile", params={"albumId": album_id})
        except requests.HTTPError:
            return []

    def delete_track_file(self, track_file_id: int) -> bool:
        """Delete a downloaded track file from disk."""
        try:
            url = f"{self.base}/api/v1/trackfile/{track_file_id}"
            resp = requests.delete(url, headers=self.headers, timeout=30)
            resp.raise_for_status()
            return True
        except requests.HTTPError as e:
            log.error("Failed to delete track file %s: %s", track_file_id, e)
            return False

    def unmonitor_album(self, album_id: int) -> bool:
        """Set an album to unmonitored so Lidarr won't re-download."""
        try:
            album = self._get(f"/album/{album_id}")
            album["monitored"] = False
            self._put(f"/album/{album_id}", album)
            return True
        except requests.HTTPError as e:
            log.error("Failed to unmonitor album %s: %s", album_id, e)
            return False

    def move_artist(self, lidarr_artist_id: int, new_root_folder: str) -> bool:
        """Move an artist to a different root folder in Lidarr."""
        try:
            artist = self._get(f"/artist/{lidarr_artist_id}")
            resolved = self.resolve_root_folder(new_root_folder)
            if not resolved:
                log.error("Root folder '%s' not found", new_root_folder)
                return False
            artist["rootFolderPath"] = resolved
            self._put(f"/artist/{lidarr_artist_id}", artist)
            log.info("Moved artist %s to %s", lidarr_artist_id, resolved)
            return True
        except requests.HTTPError as e:
            log.error("Failed to move artist %s: %s", lidarr_artist_id, e)
            return False

    # ── Track-level monitoring ────────────────────────────────────────────────

    def get_album_tracks(self, album_id: int) -> list[dict]:
        """Get all tracks for a specific album."""
        return self._get("/track", params={"albumId": album_id})

    def set_track_monitored(self, track_id: int, monitored: bool) -> bool:
        """Set a single track's monitored flag. Uses batch endpoint."""
        try:
            track = self._get(f"/track/{track_id}")
            track["monitored"] = monitored
            self._put("/track", [track])
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

        # Build the batch update — set all tracks in one PUT
        updated_tracks = []
        for track in tracks:
            tid = track["id"]
            should_monitor = tid in track_ids_to_monitor

            if track.get("monitored") == should_monitor:
                if should_monitor:
                    stats["monitored"] += 1
                else:
                    stats["unmonitored"] += 1
                continue

            track["monitored"] = should_monitor
            updated_tracks.append(track)

        if updated_tracks:
            try:
                self._put("/track", updated_tracks)
                for t in updated_tracks:
                    if t["monitored"]:
                        stats["monitored"] += 1
                    else:
                        stats["unmonitored"] += 1
            except requests.HTTPError as e:
                log.error("Failed to batch update tracks: %s", e)
                stats["errors"] += len(updated_tracks)

        return stats
