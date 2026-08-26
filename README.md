# 🎵 Lidarr Hits Bot

A Discord bot that tracks your favorite artists and only adds **popular** new releases to Lidarr — no more downloading entire discographies you'll never listen to.

## How It Works

1. You add artists via Discord (`?add Linkin Park`)
2. Every day, the bot checks Spotify for new releases from your tracked artists
3. If an album's average track popularity is above your threshold (default: 60/100), it gets added to Lidarr
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

### 2. Get Spotify API Credentials

1. Go to [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
2. Click **Create App**
3. Name it anything, set redirect URL to `http://localhost` (won't be used)
4. Copy the **Client ID** and **Client Secret**

### 3. Get Your Lidarr API Key

1. Open Lidarr → **Settings** → **General**
2. Copy the **API Key**

### 4. Configure

```bash
cp .env.example .env
# Edit .env with your actual values
nano .env
```

### 5. Deploy with Docker (Portainer)

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

### 6. Lidarr URL Notes

| Lidarr Location | LIDARR_URL |
|---|---|
| Same Docker host | `http://host.docker.internal:8686` |
| Same Docker network | `http://lidarr:8686` (use container name) |
| Different machine | `http://192.168.x.x:8686` |

## Discord Commands

| Command | Description |
|---|---|
| `?add <artist>` | Track an artist (validates on Spotify first) |
| `?remove <artist>` | Stop tracking an artist |
| `?list` | Show all tracked artists |
| `?check` | Manually trigger a popularity check |
| `?threshold <0-100>` | View or set the popularity threshold |
| `?mode <tracks\|album>` | Switch between tracks-only or whole-album mode |
| `?help` | Show help |

## Download Modes

| Mode | Behavior |
|---|---|
| `tracks` (default) | Only downloads individual songs above the popularity threshold. Unmonitors everything else on the album. |
| `album` | Downloads the entire album if it has popular tracks. |

Switch anytime with `?mode tracks` or `?mode album`. Set permanently via `DOWNLOAD_MODE` in `.env`.

## Popularity Threshold

Spotify rates every track 0-100 for popularity. The bot uses this to decide what's worth downloading:

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
├── spotify_client.py   # Spotify API client
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

**Spotify errors:**
- Verify Client ID and Client Secret are correct
- Make sure there are no extra spaces in the .env values

**Lidarr connection fails:**
- Test the URL manually: `curl http://your-lidarr-url:8686/api/v1/system/status?apiKey=YOUR_KEY`
- If Lidarr is in Docker, use `host.docker.internal` or the container name

**Check logs:**
```bash
docker logs lidarr-hits-bot
```
