"""
Lidarr Hits Bot — Discord bot that tracks artists and only downloads popular songs.

Commands:
    ?add <artist>       — Add an artist (interactive setup dialog)
    ?remove <artist>    — Remove an artist from the watchlist
    ?list               — Show all tracked artists
    ?check              — Manually trigger a popularity check
    ?threshold <0-100>  — Show or set the popularity threshold
    ?help               — Show this help message
"""

import asyncio
import logging
import signal
import sys
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands, tasks
from croniter import croniter

import database as db
from checker import format_results, run_daily_check
from config import Config
from music_client import MusicClient

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("lidarr-hits-bot")

# ── Bot setup ────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix=Config.COMMAND_PREFIX,
    intents=intents,
    help_command=None,  # We'll use our own
)


# ── Events ───────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    log.info("Bot online as %s (ID: %s)", bot.user, bot.user.id)
    db.init_db()
    log.info("Database initialized at %s", Config.DB_PATH)

    # Load persisted settings (threshold, mode) from DB
    db.load_settings()
    log.info("Settings loaded — threshold: %d, mode: %s", Config.POPULARITY_THRESHOLD, Config.DOWNLOAD_MODE)

    # Start the daily check scheduler
    if not daily_check_loop.is_running():
        daily_check_loop.start()
        log.info("Daily check scheduled: %s (%s)", Config.DAILY_CHECK_CRON, Config.TIMEZONE)


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return  # Ignore unknown commands
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument: `{error.param.name}`. Use `?help` for usage.")
        return
    log.error("Command error: %s", error)
    await ctx.send(f"❌ Error: {error}")


# ── Interactive Add Artist UI ─────────────────────────────────────────────────


class ThresholdModal(discord.ui.Modal, title="Set Popularity Threshold"):
    """Modal popup for editing the threshold value."""

    threshold_input = discord.ui.TextInput(
        label="Popularity Threshold (0-100)",
        placeholder="60",
        default=str(Config.POPULARITY_THRESHOLD),
        min_length=1,
        max_length=3,
        style=discord.TextStyle.short,
    )

    def __init__(self, parent_view: "AddArtistView"):
        super().__init__()
        self.parent_view = parent_view
        self.threshold_input.default = str(Config.POPULARITY_THRESHOLD)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            value = int(self.threshold_input.value)
            if not 0 <= value <= 100:
                await interaction.response.send_message(
                    "❌ Must be 0-100.", ephemeral=True
                )
                return
        except ValueError:
            await interaction.response.send_message(
                "❌ Must be a number.", ephemeral=True
            )
            return

        self.parent_view.threshold = value
        await interaction.response.edit_message(
            embed=self.parent_view.build_embed(),
            view=self.parent_view,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await interaction.response.send_message(
            f"❌ Error: {error}", ephemeral=True
        )


class AddArtistView(discord.ui.View):
    """Interactive view for configuring an artist before adding."""

    def __init__(
        self,
        author_id: int,
        display_name: str,
        spotify_id: Optional[str],
        spotify_data: Optional[dict],
        folders: list[dict],
    ):
        super().__init__(timeout=300)  # 5 minute timeout
        self.author_id = author_id
        self.display_name = display_name
        self.spotify_id = spotify_id
        self.spotify_data = spotify_data
        self.folders = folders
        self.threshold = Config.POPULARITY_THRESHOLD
        self.selected_mode = Config.DOWNLOAD_MODE
        self.selected_folder: Optional[str] = None  # None = use default
        self.confirmed = False
        self.cancelled = False

        # Populate the folder dropdown
        self._setup_folder_select()
        self._setup_mode_select()

    def _setup_folder_select(self):
        """Populate the folder dropdown from Lidarr data."""
        # Get default folder
        default_folder = db.get_setting("default_root_folder")

        options = []
        for f in self.folders:
            label = f["name"]
            is_default = default_folder and f["path"] == default_folder
            options.append(
                discord.SelectOption(
                    label=label,
                    value=f["path"],
                    description=f["path"],
                    default=is_default,
                )
            )

        if not options:
            # Disable if no folders
            self.folder_select.disabled = True
            self.folder_select.placeholder = "No folders found"
        else:
            # Set default selection
            if default_folder:
                self.selected_folder = default_folder
            elif options:
                self.selected_folder = options[0].value

        self.folder_select.options = options[:25]  # Discord max 25 options

    def _setup_mode_select(self):
        """Set the default mode selection."""
        for opt in self.mode_select.options:
            opt.default = opt.value == self.selected_mode

    # ── Folder dropdown ──────────────────────────────────────────────────

    @discord.ui.select(
        placeholder="📁 Choose root folder...",
        min_values=1,
        max_values=1,
        row=0,
    )
    async def folder_select(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ):
        self.selected_folder = select.values[0]
        # Update defaults
        for opt in select.options:
            opt.default = opt.value == self.selected_folder
        await interaction.response.edit_message(
            embed=self.build_embed(), view=self
        )

    # ── Mode dropdown ────────────────────────────────────────────────────

    @discord.ui.select(
        placeholder="🎛️ Download mode...",
        options=[
            discord.SelectOption(
                label="Tracks Only",
                value="tracks",
                description="Only popular tracks above threshold",
                emoji="🎵",
            ),
            discord.SelectOption(
                label="Full Album",
                value="album",
                description="Download the entire album",
                emoji="💿",
            ),
        ],
        min_values=1,
        max_values=1,
        row=1,
    )
    async def mode_select(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ):
        self.selected_mode = select.values[0]
        for opt in select.options:
            opt.default = opt.value == self.selected_mode
        await interaction.response.edit_message(
            embed=self.build_embed(), view=self
        )

    # ── Threshold button (opens modal) ───────────────────────────────────

    @discord.ui.button(
        label="📊 Edit Threshold",
        style=discord.ButtonStyle.secondary,
        row=3,
    )
    async def threshold_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        modal = ThresholdModal(self)
        await interaction.response.send_modal(modal)

    # ── Confirm button ───────────────────────────────────────────────────

    @discord.ui.button(
        label="✅ Add Artist",
        style=discord.ButtonStyle.success,
        row=3,
    )
    async def confirm_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the person who ran `?add` can confirm.", ephemeral=True
            )
            return

        self.confirmed = True
        self.stop()

        # Disable all components
        for item in self.children:
            item.disabled = True

        await interaction.response.edit_message(
            embed=self.build_embed(confirmed=True), view=self
        )

    # ── Cancel button ────────────────────────────────────────────────────

    @discord.ui.button(
        label="❌ Cancel",
        style=discord.ButtonStyle.danger,
        row=3,
    )
    async def cancel_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the person who ran `?add` can cancel.", ephemeral=True
            )
            return

        self.cancelled = True
        self.stop()

        for item in self.children:
            item.disabled = True

        await interaction.response.edit_message(
            embed=discord.Embed(
                title="❌ Cancelled",
                description=f"**{self.display_name}** was not added.",
                color=0xFF0000,
            ),
            view=self,
        )

    # ── Timeout handler ──────────────────────────────────────────────────

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

    # ── Embed builder ────────────────────────────────────────────────────

    def build_embed(self, confirmed: bool = False) -> discord.Embed:
        """Build the configuration embed showing current selections."""
        if confirmed:
            embed = discord.Embed(
                title="✅ Artist Added",
                description=f"**{self.display_name}** is now being tracked.",
                color=0x1DB954,
            )
        else:
            embed = discord.Embed(
                title=f"🎵 Add Artist: {self.display_name}",
                description="Configure settings below, then hit **Add Artist**.",
                color=0x1DB954,
            )

        # Artist info
        if self.spotify_data:
            genres = self.spotify_data.get("genres", [])
            if genres:
                embed.add_field(
                    name="Genres",
                    value=", ".join(genres[:3]),
                    inline=True,
                )
            nb_fan = self.spotify_data.get("nb_fan")
            if nb_fan:
                if nb_fan >= 1_000_000:
                    fan_str = f"{nb_fan / 1_000_000:.1f}M"
                elif nb_fan >= 1_000:
                    fan_str = f"{nb_fan / 1_000:.0f}K"
                else:
                    fan_str = str(nb_fan)
                embed.add_field(
                    name="👥 Fans",
                    value=fan_str,
                    inline=True,
                )

        # Current selections
        folder_display = "(default)"
        if self.selected_folder:
            folder_display = self.selected_folder.rstrip("/").split("/")[-1]

        mode_display = "🎵 Tracks Only" if self.selected_mode == "tracks" else "💿 Full Album"

        embed.add_field(name="📁 Root Folder", value=folder_display, inline=True)
        embed.add_field(name="🎛️ Mode", value=mode_display, inline=True)
        embed.add_field(name="📊 Threshold", value=f"{self.threshold}/100", inline=True)

        if not confirmed:
            embed.set_footer(text="Use the dropdowns and buttons to configure • Times out in 5 min")

        return embed


# ── Commands ─────────────────────────────────────────────────────────────────

@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    """Show available commands."""
    prefix = Config.COMMAND_PREFIX
    embed = discord.Embed(
        title="🎵 Lidarr Hits Bot",
        description="Only downloads the popular stuff.",
        color=0x1DB954,  # Spotify green
    )
    embed.add_field(
        name="Commands",
        value=(
            f"`{prefix}add <artist>` — Add artist (interactive setup dialog)\n"
            f"`{prefix}remove <artist>` — Stop tracking\n"
            f"`{prefix}list` — Show watchlist\n"
            f"`{prefix}check` — Run popularity check (recent releases)\n"
            f"`{prefix}scan` — Full catalog scan (pick artist or all)\n"
            f"`{prefix}threshold <0-100>` — View/set popularity threshold\n"
            f"`{prefix}mode <tracks|album>` — Download popular tracks only, or whole albums\n"
            f"`{prefix}folder` — Show/set root folders\n"
            f"`{prefix}help` — This message"
        ),
        inline=False,
    )
    embed.add_field(
        name="How it works",
        value=(
            "Every day at the scheduled time, the bot checks each tracked artist "
            "on Deezer for new releases. If an album has tracks above the "
            f"popularity threshold (**{Config.POPULARITY_THRESHOLD}**/100), "
            "it gets processed.\n\n"
            "**Tracks mode** (default): Only the popular tracks are monitored in Lidarr.\n"
            "**Album mode**: The whole album is grabbed."
        ),
        inline=False,
    )
    await ctx.send(embed=embed)


@bot.command(name="add")
async def add_artist(ctx: commands.Context, *, artist_name: str = None):
    """Add an artist with an interactive setup dialog."""
    if not artist_name:
        await ctx.send("❌ Give me an artist name. Example: `?add Linkin Park`")
        return

    artist_name = artist_name.strip()

    # Validate on Spotify
    spotify_data = None
    spotify_id = None
    display_name = artist_name
    try:
        sp = MusicClient()
        found = sp.search_artist(artist_name)
        if not found:
            await ctx.send(f"❌ Couldn't find **{artist_name}** on Deezer. Check the spelling?")
            return
        spotify_id = found["id"]
        display_name = found["name"]
        spotify_data = found  # Contains genres, popularity, images, etc.
    except Exception as e:
        log.warning("Music lookup failed for '%s': %s", artist_name, e)

    # Check if already in watchlist
    existing = db.get_artist(display_name)
    if existing:
        await ctx.send(f"⚠️ **{display_name}** is already in the watchlist.")
        return

    # Fetch Lidarr root folders
    folders = []
    try:
        from lidarr_client import LidarrClient
        lidarr = LidarrClient()
        folders = lidarr.get_root_folders()
    except Exception as e:
        log.warning("Could not fetch Lidarr folders: %s", e)

    # Launch the interactive view
    view = AddArtistView(
        author_id=ctx.author.id,
        display_name=display_name,
        spotify_id=spotify_id,
        spotify_data=spotify_data,
        folders=folders,
    )

    await ctx.send(embed=view.build_embed(), view=view)

    # Wait for the user to confirm or cancel
    await view.wait()

    if view.cancelled or not view.confirmed:
        return

    # ── User confirmed — save to database ────────────────────────────────
    added_by = str(ctx.author)
    success = db.add_artist(
        display_name, added_by, spotify_id,
        root_folder=view.selected_folder,
    )

    if not success:
        await ctx.send(f"⚠️ **{display_name}** is already in the watchlist.")
        return

    # Persist threshold/mode if changed from defaults
    if view.threshold != Config.POPULARITY_THRESHOLD:
        Config.POPULARITY_THRESHOLD = view.threshold
        db.set_setting("popularity_threshold", str(view.threshold))

    if view.selected_mode != Config.DOWNLOAD_MODE:
        Config.DOWNLOAD_MODE = view.selected_mode
        db.set_setting("download_mode", view.selected_mode)

    # Confirmation message
    folder_display = "(default)"
    if view.selected_folder:
        folder_display = view.selected_folder.rstrip("/").split("/")[-1]

    embed = discord.Embed(
        title="✅ Artist Added",
        description=(
            f"**{display_name}** is now being tracked.\n\n"
            f"📁 **Folder:** {folder_display}\n"
            f"🎛️ **Mode:** {view.selected_mode}\n"
            f"📊 **Threshold:** {view.threshold}/100"
        ),
        color=0x1DB954,
    )
    embed.set_footer(text=f"Added by {added_by}")
    await ctx.send(embed=embed)

    # ── Auto-check this artist for popular tracks (full catalog scan) ───
    await ctx.send(f"🔍 Scanning **{display_name}**'s full catalog for hits...")
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, run_daily_check, display_name, True)
    report = format_results(results)
    await ctx.send(report)


@bot.command(name="remove")
async def remove_artist(ctx: commands.Context, *, artist_name: str):
    """Remove an artist from the watchlist."""
    success = db.remove_artist(artist_name)
    if success:
        await ctx.send(f"🗑️ **{artist_name.strip()}** removed from the watchlist.")
    else:
        await ctx.send(f"❌ **{artist_name.strip()}** not found in the watchlist.")


@bot.command(name="list")
async def list_artists(ctx: commands.Context):
    """Show all tracked artists."""
    artists = db.list_artists()
    if not artists:
        await ctx.send("📭 Watchlist is empty. Add an artist with `?add <name>`")
        return

    embed = discord.Embed(
        title=f"🎵 Artist Watchlist ({len(artists)})",
        color=0x1DB954,
    )

    lines = []
    for a in artists:
        last = a.get("last_checked")
        if last:
            try:
                dt = datetime.fromisoformat(last)
                last_str = dt.strftime("%b %d %H:%M")
            except ValueError:
                last_str = last
        else:
            last_str = "never"
        folder = a.get("root_folder")
        folder_str = ""
        if folder:
            folder_display = folder.rstrip("/").split("/")[-1]
            folder_str = f" 📁 {folder_display}"
        lines.append(f"• **{a['name']}** — last checked: {last_str}{folder_str}")

    # Split into fields if long (Discord embed value limit is 1024 chars)
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 1024:
            embed.add_field(name="\u200b", value=chunk, inline=False)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        embed.add_field(name="\u200b", value=chunk, inline=False)

    await ctx.send(embed=embed)


@bot.command(name="check")
async def manual_check(ctx: commands.Context):
    """Manually trigger the popularity check."""
    await ctx.send("🔍 Running popularity check... this may take a minute.")

    # Run in executor to not block the bot
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, run_daily_check)
    report = format_results(results)

    # Split long messages (Discord 2000 char limit)
    while report:
        chunk = report[:1990]
        if len(report) > 1990:
            # Find last newline to split cleanly
            split_at = chunk.rfind("\n")
            if split_at > 0:
                chunk = report[:split_at]
        await ctx.send(chunk)
        report = report[len(chunk):]


# ── Full Scan UI ─────────────────────────────────────────────────────────────


class ScanConfirmView(discord.ui.View):
    """Confirmation prompt for scanning all artists."""

    def __init__(self, author_id: int):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.confirmed = False

    @discord.ui.button(label="⚠️ Yes, scan all", style=discord.ButtonStyle.danger)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours to confirm.", ephemeral=True)
            return
        self.confirmed = True
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="🔍 Full Catalog Scan — All Artists",
                description="Scanning... this may take a while.",
                color=0xFFA500,
            ),
            view=self,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours to cancel.", ephemeral=True)
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="❌ Cancelled",
                description="Scan cancelled.",
                color=0xFF0000,
            ),
            view=self,
        )

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


class ScanArtistView(discord.ui.View):
    """Dropdown to pick an artist + buttons for scan/cancel/all."""

    def __init__(self, author_id: int, artists: list[dict]):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.artists = artists
        self.selected: Optional[str] = None  # artist name or "__all__"
        self.confirmed = False

        # Build dropdown — individual artists only
        options = []
        for a in artists:
            options.append(discord.SelectOption(label=a["name"], value=a["name"]))
        self.artist_select.options = options[:25]

    @discord.ui.select(placeholder="Pick an artist...", min_values=1, max_values=1, row=0)
    async def artist_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        self.selected = select.values[0]
        for opt in select.options:
            opt.default = opt.value == self.selected
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="🔍 Scan Artist", style=discord.ButtonStyle.success, row=1)
    async def go_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        if not self.selected:
            await interaction.response.send_message("❌ Pick an artist first.", ephemeral=True)
            return
        self.confirmed = True
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(
                title=f"🔍 Scanning: {self.selected}",
                description="Running full catalog scan...",
                color=0x1DB954,
            ),
            view=self,
        )

    @discord.ui.button(label="📀 All Artists", style=discord.ButtonStyle.danger, row=1)
    async def all_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        self.selected = "__all__"
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(title="❌ Cancelled", color=0xFF0000),
            view=self,
        )

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


@bot.command(name="scan")
async def scan_cmd(ctx: commands.Context):
    """Full catalog scan — pick an artist or scan all."""
    artists = db.list_artists()
    if not artists:
        await ctx.send("📭 Watchlist is empty. Add an artist with `?add <name>` first.")
        return

    view = ScanArtistView(ctx.author.id, artists)
    embed = discord.Embed(
        title="🔍 Full Catalog Scan",
        description=(
            f"Pick an artist to scan their **entire catalog** for hits.\n"
            f"Or select **All Artists** to scan everyone.\n\n"
            f"Currently tracking **{len(artists)}** artist(s)."
        ),
        color=0x1DB954,
    )
    embed.set_footer(text="Select an artist from the dropdown • Times out in 2 min")
    await ctx.send(embed=embed, view=view)
    await view.wait()

    if not view.selected:
        return

    if view.selected == "__all__":
        # Send confirmation warning
        confirm_view = ScanConfirmView(ctx.author.id)
        confirm_embed = discord.Embed(
            title="⚠️ Scan ALL Artists?",
            description=(
                f"You're about to scan **{len(artists)} artists**' full catalogs.\n\n"
                "This checks every artist's top tracks and queues popular albums.\n"
                "**May take several minutes and trigger many downloads.**\n\n"
                "Are you sure?"
            ),
            color=0xFFA500,
        )
        await ctx.send(embed=confirm_embed, view=confirm_view)
        await confirm_view.wait()

        if not confirm_view.confirmed:
            return

        await ctx.send(f"🔍 Scanning **{len(artists)} artists**... this will take a while.")
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, run_daily_check, None, True)
    else:
        if not view.confirmed:
            return
        await ctx.send(f"🔍 Scanning **{view.selected}**'s full catalog for hits...")
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, run_daily_check, view.selected, True)

    report = format_results(results)
    while report:
        chunk = report[:1990]
        if len(report) > 1990:
            split_at = chunk.rfind("\n")
            if split_at > 0:
                chunk = report[:split_at]
        await ctx.send(chunk)
        report = report[len(chunk):]


@bot.command(name="threshold")
async def threshold_cmd(ctx: commands.Context, value: int = None):
    """Show or set the popularity threshold."""
    if value is None:
        await ctx.send(
            f"📊 Current popularity threshold: **{Config.POPULARITY_THRESHOLD}**/100\n"
            f"Change with `?threshold <number>` (0-100)"
        )
        return

    if not 0 <= value <= 100:
        await ctx.send("❌ Threshold must be between 0 and 100.")
        return

    # Update at runtime AND persist to database
    Config.POPULARITY_THRESHOLD = value
    db.set_setting("popularity_threshold", str(value))
    await ctx.send(f"📊 Popularity threshold set to **{value}**/100 (saved permanently).")


@bot.command(name="mode")
async def mode_cmd(ctx: commands.Context, mode: str = None):
    """Show or set the download mode (album or tracks)."""
    if mode is None:
        current = Config.DOWNLOAD_MODE
        desc = {
            "tracks": "Only downloads individual tracks above the popularity threshold",
            "album": "Downloads the entire album if it has popular tracks",
        }
        await ctx.send(
            f"🎛️ Download mode: **{current}**\n"
            f"{desc.get(current, '')}\n"
            f"Switch with `?mode tracks` or `?mode album`"
        )
        return

    mode = mode.strip().lower()
    if mode not in ("tracks", "album"):
        await ctx.send("❌ Mode must be `tracks` or `album`.")
        return

    Config.DOWNLOAD_MODE = mode
    db.set_setting("download_mode", mode)
    if mode == "tracks":
        await ctx.send(
            "🎛️ Mode set to **tracks** — only popular tracks (above threshold) will be downloaded. "
            "(saved permanently)"
        )
    else:
        await ctx.send(
            "🎛️ Mode set to **album** — full albums with popular tracks will be downloaded. "
            "(saved permanently)"
        )


@bot.command(name="folder")
async def folder_cmd(ctx: commands.Context, *, folder_name: str = None):
    """Show available root folders or set the default. Per-artist: ?folder set <artist> to <folder>"""
    try:
        from lidarr_client import LidarrClient
        lidarr = LidarrClient()
        folders = lidarr.get_root_folders()
    except Exception as e:
        await ctx.send(f"❌ Couldn't connect to Lidarr: {e}")
        return

    if not folders:
        await ctx.send("❌ No root folders found in Lidarr.")
        return

    # Get current default
    default_folder = db.get_setting("default_root_folder")
    default_display = default_folder.rstrip("/").split("/")[-1] if default_folder else "(using .env)"

    if folder_name is None:
        # Show all folders + current default
        embed = discord.Embed(
            title="📁 Lidarr Root Folders",
            description=f"Default: **{default_display}**",
            color=0x1DB954,
        )
        lines = []
        for f in folders:
            marker = " ⬅️ default" if default_folder and f["path"] == default_folder else ""
            lines.append(f"• **{f['name']}** `{f['path']}`{marker}")
        embed.add_field(name="Available Folders", value="\n".join(lines), inline=False)
        embed.add_field(
            name="Usage",
            value=(
                "`?folder <name>` — set default folder\n"
                "`?add <artist> to <folder>` — per-artist folder\n"
                "`?folder set <artist> to <folder>` — change an existing artist's folder"
            ),
            inline=False,
        )
        await ctx.send(embed=embed)
        return

    # Handle "set <artist> to <folder>" syntax
    if folder_name.lower().startswith("set "):
        rest = folder_name[4:].strip()
        if " to " in rest.lower():
            parts = rest.rsplit(" to ", 1)
            artist_name = parts[0].strip()
            folder_input = parts[1].strip()
            resolved = lidarr.resolve_root_folder(folder_input)
            if not resolved:
                await ctx.send(
                    f"❌ Folder **{folder_input}** not found. Use `?folder` to see available folders."
                )
                return
            artist = db.get_artist(artist_name)
            if not artist:
                await ctx.send(f"❌ **{artist_name}** not in watchlist.")
                return
            db.set_artist_root_folder(artist_name, resolved)
            folder_display = resolved.rstrip("/").split("/")[-1]
            await ctx.send(f"📁 **{artist_name}** will now download to **{folder_display}**.")
            return
        else:
            await ctx.send("❌ Syntax: `?folder set <artist> to <folder>`")
            return

    # Set default folder
    resolved = lidarr.resolve_root_folder(folder_name)
    if not resolved:
        await ctx.send(
            f"❌ Folder **{folder_name}** not found. Use `?folder` to see available folders."
        )
        return

    db.set_setting("default_root_folder", resolved)
    folder_display = resolved.rstrip("/").split("/")[-1]
    await ctx.send(f"📁 Default root folder set to **{folder_display}** (saved permanently).")


# ── Daily check scheduler ────────────────────────────────────────────────────

def _next_cron_run() -> datetime:
    """Calculate the next run time from the cron expression."""
    cron = croniter(Config.DAILY_CHECK_CRON, datetime.now(timezone.utc))
    return cron.get_next(datetime)


@tasks.loop(hours=1)
async def daily_check_loop():
    """
    Runs every hour but only executes the check when the cron expression matches.
    This avoids the complexity of APScheduler while staying cron-compatible.
    """
    now = datetime.now(timezone.utc)
    cron = croniter(Config.DAILY_CHECK_CRON, now)

    # Get the previous cron fire time
    prev_fire = cron.get_prev(datetime)

    # Only run if we're within 1 hour of the last fire time
    if (now - prev_fire).total_seconds() > 3600:
        return

    log.info("Daily check triggered by scheduler.")
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, run_daily_check)
    report = format_results(results)

    # Send to configured channel
    channel_id = Config.REPORT_CHANNEL_ID
    if channel_id:
        channel = bot.get_channel(channel_id)
        if channel:
            while report:
                chunk = report[:1990]
                if len(report) > 1990:
                    split_at = chunk.rfind("\n")
                    if split_at > 0:
                        chunk = report[:split_at]
                await channel.send(chunk)
                report = report[len(chunk):]
        else:
            log.error("Report channel %s not found!", channel_id)
    else:
        log.info("No REPORT_CHANNEL_ID set, report logged only.")


@daily_check_loop.before_loop
async def before_daily_check():
    await bot.wait_until_ready()


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not Config.DISCORD_TOKEN:
        log.error("DISCORD_TOKEN not set! Check your .env file.")
        sys.exit(1)

    # Graceful shutdown
    def shutdown(sig, frame):
        log.info("Shutting down (signal %s)...", sig)
        asyncio.get_event_loop().stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    bot.run(Config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
