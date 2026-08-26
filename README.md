# 🎵 Lidarr Hits Bot

A Discord bot that tracks your favorite artists and only adds **popular** new releases to Lidarr — no more downloading entire discographies you'll never listen to.

## How It Works

1. You add artists via Discord (`?add Linkin Park`)
2. Every day, the bot checks Deezer for new releases from your tracked artists
3. If an album has tracks above your popularity threshold (default: 60/100), it gets added to Lidarr
4. Lidarr handles the actual download

## Setup

### 1. Create a Discord Bot

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application** → name it → **Create**
3. Go to **Bot** tab → click **Reset Token** → copy the token
4. Under **Privileged Gateway Intents**, enable **Message Content Intent**
5. Go to **OAuth2 > URL Generator**:
   - Scopes: `bot`
   - Bot Permissions: `Send Messages`, `Embed Links`, `Read Message History`
6. Copy the generated URL and open it to invite the bot to your server

### 2. Get Your Lidarr API Key

1. Open Lidarr → **Settings** → **General**
2. Copy the **API Key**

### 3. Configure

```bash
cp .env.example .env
# Edit .env with your actual values
nano .env
```

### 4. Deploy with Docker (Portainer)

**Option A: Portainer Stack**
1. In Portainer, go to **Stacks** → **Add Stack**
2. Name it `lidarr-hits-bot`
3. Upload the `docker-compose.yml` or paste its contents
4. Add your `.env` variables in the **Environment variables** section
5. Click **Deploy**

**Option B: Docker Compose (terminal)**
```bash
docker compose up -d
```

**Option C: Build and run manually**
```bash
docker build -t lidarr-hits-bot .
docker run -d --name lidarr-hits-bot --env-file .env -v bot-data:/data lidarr-hits-bot
```

### 5. Lidarr URL Notes

| Lidarr Location | LIDARR_URL |
|---|---|
| Same Docker host | `http://host.docker.internal:8686` |
| Same Docker network | `http://lidarr:8686` (use container name) |
| Different machine | `http://192.168.x.x:8686` |

## Discord Commands

| Command | Description |
|---|---|
| `?add <artist>` | Add artist — opens interactive setup dialog |
| `?remove <artist>` | Stop tracking an artist |
| `?list` | Show all tracked artists (with folder info) |
| `?check` | Manually trigger a popularity check |
| `?threshold <0-100>` | View or set the popularity threshold |
| `?mode <tracks\|album>` | Switch between tracks-only or whole-album mode |
| `?folder` | Show all Lidarr root folders + current default |
| `?folder <name>` | Set the default root folder |
| `?folder set <artist> to <folder>` | Change an existing artist's folder |
| `?help` | Show help |

## Adding Artists (Interactive Dialog)

When you run `?add Linkin Park`, the bot:

1. Validates the artist on Deezer (shows fan count)
2. Presents an interactive dialog with:
   - **📁 Root Folder** dropdown (pre-selected to your default)
   - **🎛️ Mode** dropdown (tracks only / full album)
   - **📊 Edit Threshold** button (opens a popup to change the value)
   - **✅ Add Artist** / **❌ Cancel** buttons
3. You configure everything, hit Add, done

The dialog times out after 5 minutes. Only the person who ran `?add` can confirm/cancel.

## Download Modes

| Mode | Behavior |
|---|---|
| `tracks` (default) | Only downloads individual songs above the popularity threshold. Unmonitors everything else on the album. |
| `album` | Downloads the entire album if it has popular tracks. |

Switch anytime with `?mode tracks` or `?mode album`. Set permanently via `DOWNLOAD_MODE` in `.env`.

## Root Folders

The bot auto-discovers your Lidarr root folders — no need to type paths. Just use the folder name:

```
?folder                          → shows all folders + current default
?folder Warren's Music           → sets default folder
?add Linkin Park to Soundtracks  → per-artist folder override
?folder set Linkin Park to Shared → change an existing artist's folder
```

**Folder priority** (when adding an artist to Lidarr):
1. Per-artist folder (set via `?add ... to <folder>` or `?folder set ...`)
2. Default folder (set via `?folder <name>`)
3. `LIDARR_ROOT_FOLDER` env var (fallback)
4. First folder Lidarr returns (last resort)

## Popularity Threshold

Deezer tracks are scored 0-100 for popularity based on the artist's top tracks. The bot uses this to decide what's worth downloading:

| Threshold | What you get |
|---|---|
| 50 | Deep cuts + hits |
| 60 | Moderate hits (default) |
| 70 | Solid hits only |
| 80 | Only the biggest songs |

An album gets added if **either**:
- Its average track popularity ≥ threshold
- It has 2+ individual tracks above the threshold

## File Structure

```
lidarr-hits-bot/
├── bot.py              # Discord bot + commands
├── checker.py          # Daily popularity check logic
├── config.py           # Environment variable config
├── database.py         # SQLite watchlist storage
├── lidarr_client.py    # Lidarr API client
├── music_client.py     # Deezer API client (free, no auth)
├── requirements.txt    # Python dependencies
├── Dockerfile          # Container build
├── docker-compose.yml  # Easy deployment
├── .env.example        # Config template
└── README.md           # This file
```

## Troubleshooting

**Bot doesn't respond to commands:**
- Make sure Message Content Intent is enabled in the Discord Developer Portal
- Check the bot has permissions in the channel

**Music API errors:**
- Deezer is free and requires no API key — if it's down, the bot will retry on the next check
- Rate limiting: Deezer allows ~50 requests/second, the bot is well under that

**Lidarr connection fails:**
- Test the URL manually: `curl http://your-lidarr-url:8686/api/v1/system/status?apiKey=YOUR_KEY`
- If Lidarr is in Docker, use `host.docker.internal` or the container name

**Check logs:**
```bash
docker logs lidarr-hits-bot
```
