"""Lidarr API client — add artists and albums for download."""

import logging
from typing import Optional

import requests

from lidarr_hits_bot.config import Config

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
        if profiles:
            self._quality_profile_id = profiles[0]["id"]
            log.warning("Quality profile '%s' not found, using '%s'",
                        Config.LIDARR_QUALITY_PROFILE, profiles[0]["name"])
            return profiles[0]["id"]
        raise RuntimeError("No Lidarr quality profiles found")

    # ── Metadata profiles ────────────────────────────────────────────────────

    def get_metadata_profiles(self) -> list[dict]:
        """Get all metadata profiles from Lidarr."""
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
        """Auto-select metadata profile based on root folder name."""
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

        for p in profiles:
            if "99" in p["name"] or "everything" in p["name"].lower():
                return p["id"]

        return profiles[0]["id"] if profiles else 1

    # ── Root folders ─────────────────────────────────────────────────────────

    def get_root_folders(self) -> list[dict]:
        """Get all root folders from Lidarr (always fresh)."""
        raw = self._get("/rootfolder")
        folders = []
        for f in raw:
            path = f["path"].rstrip("/")
            name = path.split("/")[-1] if "/" in path else path
            folders.append({"path": f["path"], "name": name, "id": f.get("id")})
        return folders

    def resolve_root_folder(self, name_or_path: str) -> Optional[str]:
        """Resolve a folder name to its full path."""
        folders = self.get_root_folders()
        search = name_or_path.strip().lower()

        for f in folders:
            if f["path"].rstrip("/").lower() == search:
                return f["path"]
        for f in folders:
            if f["name"].lower() == search:
                return f["path"]
        for f in folders:
            if search in f["name"].lower():
                return f["path"]
        return None

    def get_root_folder(self, folder_path: Optional[str] = None) -> str:
        """Get a root folder path with fallback."""
        if folder_path:
            resolved = self.resolve_root_folder(folder_path)
            if resolved:
                return resolved
            log.warning("Root folder '%s' not found, falling back", folder_path)

        folders = self.get_root_folders()
        for f in folders:
            if f["path"] == Config.LIDARR_ROOT_FOLDER:
                return f["path"]
        if folders:
            return folders[0]["path"]
        raise RuntimeError("No Lidarr root folders found")

    # ── Artist lookup ────────────────────────────────────────────────────────

    def lookup_artist(self, name: str) -> Optional[dict]:
        """Search Lidarr's artist lookup for a name."""
        results = self._get("/artist/lookup", params={"term": name})
        if not results:
            return None
        name_lower = name.strip().lower()
        for r in results:
            if r.get("artistName", "").lower() == name_lower:
                return r
        return results[0]

    def get_artist(self, lidarr_id: int) -> Optional[dict]:
        """Get an artist by ID."""
        try:
            return self._get(f"/artist/{lidarr_id}")
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return None
            raise

    def get_all_artists(self) -> list[dict]:
        """List all artists in Lidarr."""
        return self._get("/artist")

    # ── Add artist ───────────────────────────────────────────────────────────

    def add_artist(self, foreign_artist_id: str, root_folder: Optional[str] = None,
                   metadata_profile_id: Optional[int] = None) -> Optional[dict]:
        """Add an artist to Lidarr. Returns added artist or None."""
        lookup = self._get("/artist/lookup", params={"term": f"lidarr:{foreign_artist_id}"})
        if not lookup:
            lookup = self._get("/artist/lookup", params={"term": foreign_artist_id})
        if not lookup:
            log.warning("Could not find artist '%s' in Lidarr lookup", foreign_artist_id)
            return None

        artist_data = lookup[0]

        existing = self.get_all_artists()
        for a in existing:
            if a.get("foreignArtistId") == artist_data.get("foreignArtistId"):
                log.info("Artist '%s' already in Lidarr (ID %s)", a["artistName"], a["id"])
                return None

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
            "monitor": "none",
        }

        for key in ["id", "statistics", "genres", "tags", "added"]:
            artist_data.pop(key, None)

        log.info("Adding artist '%s' to Lidarr (root: %s, profile: %s)",
                 artist_data.get("artistName"), artist_data["rootFolderPath"],
                 artist_data.get("metadataProfileId"))

        try:
            result = self._post("/artist", artist_data)
            artist_id = result.get("id")
            log.info("Added artist '%s' to Lidarr (ID %s)", result.get("artistName"), artist_id)

            # Verify artist was added
            if artist_id:
                verify = self.get_artist(artist_id)
                if verify:
                    log.info("✅ Verified: artist '%s' in Lidarr (monitored=%s, root=%s)",
                             verify.get("artistName"), verify.get("monitored"),
                             verify.get("rootFolderPath"))
                else:
                    log.warning("⚠️ Artist add returned ID %s but GET returned nothing", artist_id)

                self._post("/command", {"name": "RefreshArtist", "artistIds": [artist_id]})
                log.info("Triggered refresh for artist ID %s", artist_id)

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
        """Get all albums for an artist."""
        return self._get("/album", params={"artistId": lidarr_artist_id})

    def monitor_album(self, album_id: int) -> bool:
        """Set an album to monitored. Verifies after PUT."""
        try:
            album = self._get(f"/album/{album_id}")
            album["monitored"] = True
            self._put(f"/album/{album_id}", album)

            # Verify
            verify = self._get(f"/album/{album_id}")
            if verify.get("monitored"):
                log.info("✅ Verified: album %s monitored=True", album_id)
                return True
            else:
                log.warning("⚠️ Album %s monitor PUT succeeded but verified monitored=%s",
                            album_id, verify.get("monitored"))
                return False
        except requests.HTTPError as e:
            log.error("Failed to monitor album %s: %s", album_id, e)
            return False

    def search_album(self, album_id: int) -> bool:
        """Trigger Lidarr to search for and download a specific album."""
        try:
            result = self._post("/command", {"name": "AlbumSearch", "albumIds": [album_id]})
            cmd_id = result.get("id")
            log.info("✅ Triggered Lidarr search for album ID %s (command ID %s)", album_id, cmd_id)
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
        """Delete a downloaded track file from disk. Verifies deletion."""
        try:
            url = f"{self.base}/api/v1/trackfile/{track_file_id}"
            resp = requests.delete(url, headers=self.headers, timeout=30)
            resp.raise_for_status()

            # Verify deletion — GET should return 404
            try:
                self._get(f"/trackfile/{track_file_id}")
                log.warning("⚠️ Track file %s delete succeeded but file still accessible", track_file_id)
                return False
            except requests.HTTPError as e2:
                if e2.response is not None and e2.response.status_code == 404:
                    log.info("✅ Verified: track file %s deleted", track_file_id)
                    return True
                return True  # Other error, assume deleted
        except requests.HTTPError as e:
            log.error("Failed to delete track file %s: %s", track_file_id, e)
            return False

    def unmonitor_album(self, album_id: int) -> bool:
        """Set an album to unmonitored. Verifies after PUT."""
        try:
            album = self._get(f"/album/{album_id}")
            album["monitored"] = False
            self._put(f"/album/{album_id}", album)

            # Verify
            verify = self._get(f"/album/{album_id}")
            if not verify.get("monitored"):
                log.info("✅ Verified: album %s monitored=False", album_id)
                return True
            else:
                log.warning("⚠️ Album %s unmonitor PUT succeeded but verified monitored=%s",
                            album_id, verify.get("monitored"))
                return False
        except requests.HTTPError as e:
            log.error("Failed to unmonitor album %s: %s", album_id, e)
            return False

    def move_artist(self, lidarr_artist_id: int, new_root_folder: str) -> bool:
        """Move an artist to a different root folder. Verifies path change."""
        try:
            artist = self._get(f"/artist/{lidarr_artist_id}")
            old_path = artist.get("rootFolderPath", "")
            resolved = self.resolve_root_folder(new_root_folder)
            if not resolved:
                log.error("Root folder '%s' not found", new_root_folder)
                return False

            artist["rootFolderPath"] = resolved
            self._put(f"/artist/{lidarr_artist_id}", artist)

            # Verify path changed
            verify = self._get(f"/artist/{lidarr_artist_id}")
            new_path = verify.get("rootFolderPath", "")
            if new_path == resolved:
                log.info("✅ Verified: artist %s moved from %s to %s", lidarr_artist_id, old_path, new_path)
            else:
                log.warning("⚠️ Artist %s move: expected %s, got %s", lidarr_artist_id, resolved, new_path)

            # Trigger file move
            self._post("/command", {
                "name": "MoveArtist",
                "artistIds": [lidarr_artist_id],
                "destinationRootFolder": resolved,
            })
            log.info("Triggered MoveArtist command for artist %s", lidarr_artist_id)
            return True
        except requests.HTTPError as e:
            log.error("Failed to move artist %s: %s", lidarr_artist_id, e)
            return False

    def get_artist_details(self, lidarr_artist_id: int) -> Optional[dict]:
        """Get full artist details."""
        try:
            return self._get(f"/artist/{lidarr_artist_id}")
        except requests.HTTPError:
            return None

    def unmonitor_all_albums(self, lidarr_artist_id: int) -> int:
        """Unmonitor all albums for an artist. Verifies each."""
        albums = self.get_artist_albums(lidarr_artist_id)
        count = 0
        for a in albums:
            if a.get("monitored"):
                if self.unmonitor_album(a["id"]):
                    count += 1
        log.info("✅ Unmonitored %d/%d albums for artist %s", count, len(albums), lidarr_artist_id)
        return count

    # ── Track-level monitoring ────────────────────────────────────────────────

    def get_album_tracks(self, album_id: int) -> list[dict]:
        """Get all tracks for a specific album."""
        return self._get("/track", params={"albumId": album_id})

    def set_track_monitored(self, track_id: int, monitored: bool) -> bool:
        """Set a single track's monitored flag. Verifies after PUT."""
        try:
            track = self._get(f"/track/{track_id}")
            track["monitored"] = monitored
            self._put("/track", [track])

            # Verify
            verify = self._get(f"/track/{track_id}")
            if verify.get("monitored") == monitored:
                log.info("✅ Verified: track %s monitored=%s", track_id, monitored)
                return True
            else:
                log.warning("⚠️ Track %s: expected monitored=%s, got %s",
                            track_id, monitored, verify.get("monitored"))
                return False
        except requests.HTTPError as e:
            log.error("Failed to set track %s monitored=%s: %s", track_id, monitored, e)
            return False

    def monitor_specific_tracks(self, album_id: int, track_ids_to_monitor: set[int]) -> dict:
        """Batch update track monitoring. Verifies after PUT."""
        tracks = self.get_album_tracks(album_id)
        stats = {"monitored": 0, "unmonitored": 0, "errors": 0}

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

                # Verify all tracks
                verify_tracks = self.get_album_tracks(album_id)
                verify_map = {t["id"]: t.get("monitored") for t in verify_tracks}

                for t in updated_tracks:
                    tid = t["id"]
                    expected = t["monitored"]
                    actual = verify_map.get(tid)
                    if actual == expected:
                        if expected:
                            stats["monitored"] += 1
                        else:
                            stats["unmonitored"] += 1
                    else:
                        log.warning("⚠️ Track %s: expected monitored=%s, verified=%s",
                                    tid, expected, actual)
                        stats["errors"] += 1

                log.info("✅ Batch track update: %d monitored, %d unmonitored, %d errors",
                         stats["monitored"], stats["unmonitored"], stats["errors"])
            except requests.HTTPError as e:
                log.error("Failed to batch update tracks: %s", e)
                stats["errors"] += len(updated_tracks)

        return stats
