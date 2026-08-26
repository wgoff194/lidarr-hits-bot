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

def add_artist(name: str, added_by: str, spotify_id: Optional[str] = None) -> bool:
    """Add an artist to the watchlist. Returns False if already exists."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO artists (name, spotify_id, added_by) VALUES (?, ?, ?)",
            (name.strip(), spotify_id, added_by),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
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
