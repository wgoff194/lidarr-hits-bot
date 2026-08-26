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

### 4. Deploy with Portainer (Recommended)

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
