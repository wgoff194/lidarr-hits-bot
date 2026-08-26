"""Configuration loaded from environment variables."""

import os


class Config:
    """All config pulled from env vars (set via .env or docker-compose)."""

    # Discord
    DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "")
    COMMAND_PREFIX: str = os.getenv("COMMAND_PREFIX", "?")
    # Channel ID where the bot posts daily reports (0 = same channel as commands)
    REPORT_CHANNEL_ID: int = int(os.getenv("REPORT_CHANNEL_ID", "0"))

    # Spotify (Client Credentials flow — no user login needed)
    # DEPRECATED: Spotify now requires Premium. Using Deezer instead.
    # These are kept for backward compatibility but not used.
    SPOTIFY_CLIENT_ID: str = os.getenv("SPOTIFY_CLIENT_ID", "")
    SPOTIFY_CLIENT_SECRET: str = os.getenv("SPOTIFY_CLIENT_SECRET", "")
    # Minimum popularity score (0-100) to consider a track "popular"
    POPULARITY_THRESHOLD: int = int(os.getenv("POPULARITY_THRESHOLD", "60"))

    # Lidarr
    LIDARR_URL: str = os.getenv("LIDARR_URL", "http://localhost:8686")
    LIDARR_API_KEY: str = os.getenv("LIDARR_API_KEY", "")
    # Lidarr quality profile name to use for new artists
    LIDARR_QUALITY_PROFILE: str = os.getenv("LIDARR_QUALITY_PROFILE", "Standard")
    # Lidarr root folder for music
    LIDARR_ROOT_FOLDER: str = os.getenv("LIDARR_ROOT_FOLDER", "/music")
    # Download mode: "album" = grab whole album, "tracks" = only popular tracks
    DOWNLOAD_MODE: str = os.getenv("DOWNLOAD_MODE", "tracks")

    # Database
    DB_PATH: str = os.getenv("DB_PATH", "/data/watchlist.db")

    # Scheduler — cron expression for daily check (default 9 AM)
    DAILY_CHECK_CRON: str = os.getenv("DAILY_CHECK_CRON", "0 9 * * *")
    # Timezone for the cron schedule
    TIMEZONE: str = os.getenv("TIMEZONE", "America/Detroit")
