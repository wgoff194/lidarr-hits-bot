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

### 3. (Optional) Get a Last.fm API Key

Last.fm provides the best track popularity data — actual play counts with no 50-track limit. Highly recommended.

1. Go to [Last.fm API](https://www.last.fm/api/account/create)
2. Create an account (free)
3. Create an API application
4. Copy the **API Key**

Without Last.fm, the bot falls back to Deezer's top 50 tracks (still works, but less data).

### 4. Configure

```bash
cp .env.example .env
# Edit .env with your actual values
nano .env
```

### 5. Deploy with Portainer (Recommended)

#### Step 1: Push to GitHub

Create a new repo on GitHub and push the project:

```bash
cd lidarr-hits-bot
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USERNAME/lidarr-hits-bot.git
git push -u origin main
```

#### Step 2: Open Portainer

1. Log into your Portainer web UI (usually `http://your-proxmox-ip:9000`)
2. Select your **local** environment (the Docker host where Lidarr runs)

#### Step 3: Create the Stack

1. In the left sidebar, click **Stacks**
2. Click **Add stack**
3. Name it `lidarr-hits-bot`
4. Under **Build method**, select **Repository**
5. Fill in:
   - **Repository URL:** `https://github.com/YOUR_USERNAME/lidarr-hits-bot.git`
   - **Repository reference:** `refs/heads/main`
   - **Compose path:** `docker-compose.yml`
6. Scroll down to **Environment variables** and click **Add an environment variable** for each one:

   > **Important:** Add these through Portainer's UI — do NOT create a `.env` file. Portainer injects these directly into the container.

   | Variable | Value |
   |---|---|
   | `DISCORD_TOKEN` | Your Discord bot token |
   | `LIDARR_URL` | `http://host.docker.internal:8686` |
   | `LIDARR_API_KEY` | Your Lidarr API key |
   | `LIDARR_QUALITY_PROFILE` | `Standard` |
   | `POPULARITY_THRESHOLD` | `60` |
   | `DOWNLOAD_MODE` | `tracks` |
   | `DAILY_CHECK_CRON` | `0 9 * * *` |
   | `TIMEZONE` | `America/Detroit` |
   | `REPORT_CHANNEL_ID` | Your Discord channel ID (or `0`) |

7. Click **Deploy the stack**

#### Step 4: Verify It's Running

1. Go to **Containers** in the left sidebar
2. Find `lidarr-hits-bot` — status should be **running**
3. Click the container name → **Logs** to see the bot startup
4. You should see: `Bot online as <bot name> (ID: ...)`

#### Step 5: Test in Discord

1. Go to your Discord server
2. Type `?help` — the bot should respond
3. Type `?folder` — should show your Lidarr root folders
4. Type `?add Linkin Park` — should open the interactive dialog

#### Updating the Bot

When you push changes to GitHub:
1. Go to Portainer → **Stacks** → `lidarr-hits-bot`
2. Click **Pull and redeploy**
3. Portainer pulls the latest code and restarts the container

Your SQLite database is stored in a Docker volume (`bot-data`) so it persists across redeployments.

---

### Alternative: Docker Compose (terminal)

If you prefer the command line:

```bash
cd lidarr-hits-bot
cp .env.example .env
nano .env  # fill in your values
docker compose up -d
docker logs -f lidarr-hits-bot  # watch startup
```

### Alternative: Build and Run Manually

```bash
docker build -t lidarr-hits-bot .
docker run -d --name lidarr-hits-bot --env-file .env -v bot-data:/data lidarr-hits-bot
```

### 6. Lidarr URL Notes

| Lidarr Location | LIDARR_URL |
|---|---|
| Same Docker host | `http://host.docker.internal:8686` |
| Same Docker network | `http://lidarr:8686` (use container name) |
| Different machine | `http://192.168.x.x:8686` |

## Discord Commands

| Command | Description |
|---|---|
| `?add <artist>` | Add artist — opens interactive setup dialog |
| `?import` | Import existing Lidarr artists into watchlist |
| `?update` / `?update <artist>` | Update artist settings (folder, mode, metadata) |
| `?remove <artist>` | Stop tracking an artist |
| `?list` | Show all tracked artists (with folder info) |
| `?check` | Run popularity check (recent releases) |
| `?scan` / `?scan <artist>` | Full catalog scan (pick artist or all) |
| `?prune` / `?prune <artist>` | Prune downloaded albums |
| `?dl` | Check pending downloads, auto-prune completed |
| `?keep` | Mark tracks as never-prune (nested menu) |
| `?threshold <0-100>` | View or set the popularity threshold |
| `?mode <tracks\|album>` | Switch between tracks-only or whole-album mode |
| `?folder` | Show all Lidarr root folders + current default |
| `?folder <name>` | Set the default root folder |
| `?folder set <artist> to <folder>` | Change an existing artist's folder |
| `?menu` | Interactive menu with buttons for all commands |
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

## Popularity Scoring

The bot scores tracks 0-100 using two sources:

| Source | Priority | Data | Limit |
|---|---|---|---|
| **Last.fm** | Primary | Actual play counts | No limit |
| **Deezer** | Fallback | Track rank | Top 50 only |

**Last.fm is strongly recommended** — it has play count data for millions of tracks with no cap. Without it, the bot only sees Deezer's top 50 tracks per artist, which can miss older hits.

Both sources normalize to 0-100. Last.fm data always takes priority when available.

## Never Prune (`?keep`)

Some tracks should never be deleted, even if they score below threshold. Use `?keep` to protect them:

```
?keep
  → Step 1: Pick artist (dropdown)
  → Step 2: Pick album (dropdown from Lidarr)
  → Step 3: Pick tracks (multi-select with checkboxes)
     • 🔒 = already protected
     • "Mark All Tracks" button = protect entire album
     • "Confirm" button = save selection
     • "Cancel" button = discard
```

**Example:** Protect all tracks on A Perfect Circle's "Thirteenth Step":
1. `?keep` → pick A Perfect Circle
2. Pick "Thirteenth Step"
3. Click "📀 Mark All Tracks"
4. Click "✅ Confirm"

Now none of those tracks will ever be pruned, regardless of popularity score.

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
├── run.py                          # Entry point
├── lidarr_hits_bot/
│   ├── __init__.py
│   ├── main.py                     # Discord bot + commands + UI views
│   ├── config.py                   # Environment variable config
│   ├── database.py                 # SQLite: artists, settings, tracks, never-prune
│   ├── checker.py                  # Daily check, prune, download status logic
│   ├── clients/
│   │   ├── __init__.py
│   │   ├── deezer.py               # Deezer API (artist lookup, albums, tracks)
│   │   ├── lastfm.py               # Last.fm API (play counts, popularity)
│   │   └── lidarr.py               # Lidarr REST API (artists, albums, tracks, files)
│   ├── ui/
│   │   └── __init__.py
│   └── utils/
│       ├── __init__.py
│       └── popularity.py           # Unified scorer (Last.fm + Deezer)
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── requirements.txt
├── .env.example
├── .gitignore
├── .dockerignore
└── README.md
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
