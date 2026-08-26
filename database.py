"""SQLite storage for the artist watchlist, settings, and track monitoring."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import Config


def _connect() -> sqlite3.Connection:
    db_path = Path(Config.DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create tables if they don't exist."""
    conn = _connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS artists (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT    NOT NULL,
            spotify_id      TEXT,
            lidarr_id       INTEGER,
            root_folder     TEXT,
            added_by        TEXT    NOT NULL,
            added_at        TEXT    NOT NULL DEFAULT (datetime('now')),
            last_checked    TEXT,
            UNIQUE(name)
        );

        CREATE TABLE IF NOT EXISTS check_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            artist_id       INTEGER NOT NULL REFERENCES artists(id),
            album_name      TEXT    NOT NULL,
            spotify_url     TEXT,
            avg_popularity  REAL,
            added_to_lidarr INTEGER NOT NULL DEFAULT 0,
            checked_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS monitored_tracks (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            artist_id       INTEGER NOT NULL REFERENCES artists(id),
            album_name      TEXT    NOT NULL,
            track_name      TEXT    NOT NULL,
            popularity      INTEGER NOT NULL,
            lidarr_track_id INTEGER,
            monitored_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(artist_id, album_name, track_name)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key             TEXT PRIMARY KEY,
            value           TEXT NOT NULL,
            updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS album_status (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            artist_id       INTEGER NOT NULL REFERENCES artists(id),
            album_name      TEXT    NOT NULL,
            lidarr_album_id INTEGER,
            status          TEXT    NOT NULL DEFAULT 'pending',
            updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(artist_id, album_name)
        );

        CREATE TABLE IF NOT EXISTS never_prune (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            artist_id       INTEGER NOT NULL REFERENCES artists(id),
            album_name      TEXT    NOT NULL,
            track_name      TEXT    NOT NULL,
            added_at        TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(artist_id, album_name, track_name)
        );
    """)
    conn.commit()
    conn.close()


# ── Settings (persistent config) ─────────────────────────────────────────────

def get_setting(key: str) -> Optional[str]:
    """Get a setting value from the DB."""
    conn = _connect()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    """Upsert a setting."""
    conn = _connect()
    conn.execute(
        """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, datetime('now'))
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
        (key, value),
    )
    conn.commit()
    conn.close()


def load_settings() -> None:
    """Load persisted settings into Config on startup."""
    threshold = get_setting("popularity_threshold")
    if threshold is not None:
        try:
            Config.POPULARITY_THRESHOLD = int(threshold)
        except ValueError:
            pass

    mode = get_setting("download_mode")
    if mode is not None and mode in ("tracks", "album"):
        Config.DOWNLOAD_MODE = mode


# ── Artist CRUD ──────────────────────────────────────────────────────────────

def add_artist(name: str, added_by: str, spotify_id: Optional[str] = None,
               root_folder: Optional[str] = None) -> bool:
    """Add an artist to the watchlist. Returns False if already exists."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO artists (name, spotify_id, added_by, root_folder) VALUES (?, ?, ?, ?)",
            (name.strip(), spotify_id, added_by, root_folder),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def get_artist_root_folder(name: str) -> Optional[str]:
    """Get the per-artist root folder override, or None for default."""
    conn = _connect()
    row = conn.execute(
        "SELECT root_folder FROM artists WHERE name = ? COLLATE NOCASE", (name.strip(),)
    ).fetchone()
    conn.close()
    return row["root_folder"] if row else None


def set_artist_root_folder(name: str, root_folder: Optional[str]) -> None:
    """Set or clear the per-artist root folder."""
    conn = _connect()
    conn.execute(
        "UPDATE artists SET root_folder = ? WHERE name = ? COLLATE NOCASE",
        (root_folder, name.strip()),
    )
    conn.commit()
    conn.close()


def remove_artist(name: str) -> bool:
    """Remove an artist. Returns False if not found."""
    conn = _connect()
    cur = conn.execute("DELETE FROM artists WHERE name = ? COLLATE NOCASE", (name.strip(),))
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


def get_artist(name: str) -> Optional[dict]:
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM artists WHERE name = ? COLLATE NOCASE", (name.strip(),)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_artists() -> list[dict]:
    conn = _connect()
    rows = conn.execute("SELECT * FROM artists ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_artist_spotify_id(name: str, spotify_id: str) -> None:
    conn = _connect()
    conn.execute(
        "UPDATE artists SET spotify_id = ? WHERE name = ? COLLATE NOCASE",
        (spotify_id, name.strip()),
    )
    conn.commit()
    conn.close()


def update_artist_lidarr_id(name: str, lidarr_id: int) -> None:
    conn = _connect()
    conn.execute(
        "UPDATE artists SET lidarr_id = ? WHERE name = ? COLLATE NOCASE",
        (lidarr_id, name.strip()),
    )
    conn.commit()
    conn.close()


def mark_checked(artist_id: int) -> None:
    conn = _connect()
    conn.execute(
        "UPDATE artists SET last_checked = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), artist_id),
    )
    conn.commit()
    conn.close()


# ── Check Log ────────────────────────────────────────────────────────────────

def log_check(
    artist_id: int,
    album_name: str,
    spotify_url: Optional[str],
    avg_popularity: float,
    added_to_lidarr: bool,
) -> None:
    conn = _connect()
    conn.execute(
        """INSERT INTO check_log (artist_id, album_name, spotify_url, avg_popularity, added_to_lidarr)
           VALUES (?, ?, ?, ?, ?)""",
        (artist_id, album_name, spotify_url, avg_popularity, int(added_to_lidarr)),
    )
    conn.commit()
    conn.close()


# ── Monitored Tracks ─────────────────────────────────────────────────────────

def get_monitored_tracks(artist_id: int, album_name: str) -> set[str]:
    """Get the set of track names we've already monitored for this album."""
    conn = _connect()
    rows = conn.execute(
        "SELECT track_name FROM monitored_tracks WHERE artist_id = ? AND album_name = ?",
        (artist_id, album_name),
    ).fetchall()
    conn.close()
    return {r["track_name"] for r in rows}


def record_monitored_tracks(
    artist_id: int,
    album_name: str,
    tracks: list[dict],
) -> None:
    """
    Record which tracks we've monitored for an album.
    Each track dict should have: name, popularity, lidarr_track_id (optional).
    """
    conn = _connect()
    for t in tracks:
        conn.execute(
            """INSERT OR REPLACE INTO monitored_tracks
               (artist_id, album_name, track_name, popularity, lidarr_track_id)
               VALUES (?, ?, ?, ?, ?)""",
            (
                artist_id,
                album_name,
                t["name"],
                t["popularity"],
                t.get("lidarr_track_id"),
            ),
        )
    conn.commit()
    conn.close()


# ── Album Status ─────────────────────────────────────────────────────────────

def set_album_status(artist_id: int, album_name: str, status: str,
                     lidarr_album_id: int = None) -> None:
    """Set or update album download status. Statuses: pending, downloaded, pruned, skipped."""
    conn = _connect()
    conn.execute(
        """INSERT INTO album_status (artist_id, album_name, lidarr_album_id, status, updated_at)
           VALUES (?, ?, ?, ?, datetime('now'))
           ON CONFLICT(artist_id, album_name)
           DO UPDATE SET status = excluded.status,
                         lidarr_album_id = COALESCE(excluded.lidarr_album_id, album_status.lidarr_album_id),
                         updated_at = excluded.updated_at""",
        (artist_id, album_name, lidarr_album_id, status),
    )
    conn.commit()
    conn.close()


def get_album_status(artist_id: int, album_name: str) -> Optional[str]:
    """Get album status. Returns None if not tracked."""
    conn = _connect()
    row = conn.execute(
        "SELECT status FROM album_status WHERE artist_id = ? AND album_name = ?",
        (artist_id, album_name),
    ).fetchone()
    conn.close()
    return row["status"] if row else None


def get_albums_by_status(status: str) -> list[dict]:
    """Get all albums with a specific status."""
    conn = _connect()
    rows = conn.execute(
        "SELECT a.name as artist_name, als.* FROM album_status als "
        "JOIN artists a ON a.id = als.artist_id WHERE als.status = ?",
        (status,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_pending_albums() -> list[dict]:
    """Get all albums waiting for download."""
    conn = _connect()
    rows = conn.execute(
        "SELECT a.name as artist_name, als.* FROM album_status als "
        "JOIN artists a ON a.id = als.artist_id WHERE als.status IN ('pending', 'downloading')"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Never Prune ─────────────────────────────────────────────────────────────

def add_never_prune(artist_id: int, album_name: str, track_name: str) -> None:
    """Mark a track as never-prune."""
    conn = _connect()
    conn.execute(
        "INSERT OR IGNORE INTO never_prune (artist_id, album_name, track_name) VALUES (?, ?, ?)",
        (artist_id, album_name, track_name),
    )
    conn.commit()
    conn.close()


def remove_never_prune(artist_id: int, album_name: str, track_name: str) -> None:
    """Remove a track from never-prune list."""
    conn = _connect()
    conn.execute(
        "DELETE FROM never_prune WHERE artist_id = ? AND album_name = ? AND track_name = ?",
        (artist_id, album_name, track_name),
    )
    conn.commit()
    conn.close()


def add_album_never_prune(artist_id: int, album_name: str, track_names: list[str]) -> None:
    """Mark all tracks on an album as never-prune."""
    conn = _connect()
    for t in track_names:
        conn.execute(
            "INSERT OR IGNORE INTO never_prune (artist_id, album_name, track_name) VALUES (?, ?, ?)",
            (artist_id, album_name, t),
        )
    conn.commit()
    conn.close()


def is_never_prune(artist_id: int, album_name: str, track_name: str) -> bool:
    """Check if a track is marked as never-prune."""
    conn = _connect()
    row = conn.execute(
        "SELECT 1 FROM never_prune WHERE artist_id = ? AND album_name = ? AND track_name = ?",
        (artist_id, album_name, track_name),
    ).fetchone()
    conn.close()
    return row is not None


def get_never_prune_tracks(artist_id: int, album_name: str) -> set[str]:
    """Get all never-prune track names for an album."""
    conn = _connect()
    rows = conn.execute(
        "SELECT track_name FROM never_prune WHERE artist_id = ? AND album_name = ?",
        (artist_id, album_name),
    ).fetchall()
    conn.close()
    return {r["track_name"] for r in rows}


def get_never_prune_albums(artist_id: int) -> list[str]:
    """Get all album names that have never-prune tracks for an artist."""
    conn = _connect()
    rows = conn.execute(
        "SELECT DISTINCT album_name FROM never_prune WHERE artist_id = ?",
        (artist_id,),
    ).fetchall()
    conn.close()
    return [r["album_name"] for r in rows]


def clear_album_never_prune(artist_id: int, album_name: str) -> None:
    """Remove all never-prune entries for an album."""
    conn = _connect()
    conn.execute(
        "DELETE FROM never_prune WHERE artist_id = ? AND album_name = ?",
        (artist_id, album_name),
    )
    conn.commit()
    conn.close()
