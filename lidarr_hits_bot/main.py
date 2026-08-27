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

from lidarr_hits_bot import database as db
from lidarr_hits_bot.checker import format_results, format_prune_results, format_download_check_results, run_daily_check, prune_downloaded_albums, check_downloads
from lidarr_hits_bot.config import Config
from lidarr_hits_bot.clients.deezer import MusicClient

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


async def get_thread_channel(ctx: commands.Context) -> discord.abc.Messageable:
    """
    If already in a thread, return it. Otherwise create a new thread
    from the invoking message and return that.
    """
    if isinstance(ctx.channel, discord.Thread):
        return ctx.channel

    # Create a thread from the command message
    thread_name = f"{ctx.command.name} — {ctx.author.display_name}"
    try:
        thread = await ctx.message.create_thread(
            name=thread_name[:100],  # Discord limit
            auto_archive_duration=10080,  # 7 days (max for non-boosted servers)
        )
        return thread
    except discord.HTTPException:
        # Fallback: if thread creation fails (e.g. already in a thread, no permission)
        return ctx.channel


@bot.before_invoke
async def auto_thread(ctx: commands.Context):
    """Auto-create a thread for every command unless already in one."""
    try:
        if isinstance(ctx.channel, discord.Thread):
            ctx._thread_channel = ctx.channel
            return

        thread_name = ctx.command.name
        thread = await ctx.message.create_thread(
            name=thread_name[:100],
            auto_archive_duration=10080,
        )
        ctx._thread_channel = thread
        log.info("Created thread '%s' for command '%s'", thread_name, ctx.command.name)

        # Route ctx.send to the thread
        async def send_to_thread(*args, **kwargs):
            return await thread.send(*args, **kwargs)
        ctx.send = send_to_thread

    except Exception as e:
        log.warning("Failed to create thread for '%s': %s", ctx.command.name, e)
        ctx._thread_channel = ctx.channel


# ── Interactive Add Artist UI ─────────────────────────────────────────────────


def _opt(label: str, value: str, **kwargs) -> discord.SelectOption:
    """Create a SelectOption with label truncated to 25 chars (Discord limit).
    
    Ensures label is never empty or None — Discord requires 1+ chars.
    """
    # Ensure label is a non-empty string, max 25 chars
    safe_label = (label or "Untitled")[:25]
    if not safe_label.strip():
        safe_label = "Untitled"
    return discord.SelectOption(label=safe_label, value=str(value), **kwargs)


def fuzzy_find_artist(artists: list[dict], query: str) -> Optional[dict]:
    """Fuzzy match an artist name. Returns best match or None."""
    query = query.strip().lower()
    if not query:
        return None
    # Exact match
    for a in artists:
        if a["name"].lower() == query:
            return a
    # Starts with
    for a in artists:
        if a["name"].lower().startswith(query):
            return a
    # Contains
    for a in artists:
        if query in a["name"].lower():
            return a
    return None


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


class SearchModal(discord.ui.Modal, title="Search Artists"):
    """Modal for fuzzy searching artists in a dropdown."""

    search_input = discord.ui.TextInput(
        label="Type artist name (partial OK)",
        placeholder="e.g. 'link' for Linkin Park",
        style=discord.TextStyle.short,
        required=True,
        max_length=100,
    )

    def __init__(self, parent_view, select_component, all_artists: list[dict]):
        super().__init__()
        self.parent_view = parent_view
        self.select_component = select_component
        self.all_artists = all_artists

    async def on_submit(self, interaction: discord.Interaction):
        query = self.search_input.value.strip().lower()
        if not query:
            matches = self.all_artists
        else:
            matches = [a for a in self.all_artists if query in a["name"].lower()]

        if not matches:
            await interaction.response.send_message(
                f"❌ No artists matching '{self.search_input.value}'", ephemeral=True
            )
            return

        # Update the dropdown options
        options = []
        for a in matches[:25]:
            options.append(_opt(a["name"], a["name"]))
        self.select_component.options = options

        # Update placeholder to show filter
        if query:
            self.select_component.placeholder = f"🔍 {len(matches)} match(es) for '{self.search_input.value}'..."
        else:
            self.select_component.placeholder = "Pick an artist..."

        await interaction.response.edit_message(view=self.parent_view)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await interaction.response.send_message(f"❌ Error: {error}", ephemeral=True)


class AddArtistView(discord.ui.View):
    """Interactive view for configuring an artist before adding."""

    def __init__(
        self,
        author_id: int,
        display_name: str,
        music_id: Optional[str],
        artist_data: Optional[dict],
        folders: list[dict],
        metadata_profiles: list[dict],
        lidarr_client,
        button_label: str = "✅ Add Artist",
    ):
        super().__init__(timeout=300)  # 5 minute timeout
        self.author_id = author_id
        self.display_name = display_name
        self.music_id = music_id
        self.artist_data = artist_data
        self.folders = folders
        self.metadata_profiles = metadata_profiles
        self.lidarr = lidarr_client
        self.button_label = button_label
        self.threshold = Config.POPULARITY_THRESHOLD
        self.selected_mode = Config.DOWNLOAD_MODE
        self.selected_folder: Optional[str] = None  # None = use default
        self.selected_metadata_profile: Optional[int] = None
        self.confirmed = False
        self.cancelled = False

        # Populate the folder dropdown
        self._setup_folder_select()
        self._setup_mode_select()
        self._setup_metadata_profile_select()

        # Set dynamic button label
        self.confirm_button.label = button_label

    def _setup_folder_select(self):
        """Populate the folder dropdown from Lidarr data."""
        # Get default folder
        default_folder = db.get_setting("default_root_folder")

        options = []
        for f in self.folders:
            label = f["name"]
            is_default = default_folder and f["path"] == default_folder
            options.append(
                _opt(label, f["path"], description=f["path"],
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

    def _get_folder_name(self) -> str:
        """Get the friendly name of the selected folder."""
        if not self.selected_folder:
            return ""
        for f in self.folders:
            if f["path"] == self.selected_folder:
                return f["name"]
        return self.selected_folder.rstrip("/").split("/")[-1]

    def _get_filtered_profiles(self) -> list[dict]:
        """Get metadata profiles filtered by folder type."""
        folder_name = self._get_folder_name().lower()
        profiles = self.metadata_profiles

        if "comedy" in folder_name:
            return [p for p in profiles if "comedy" in p["name"].lower()]
        elif "soundtrack" in folder_name:
            return [p for p in profiles if "soundtrack" in p["name"].lower()]
        else:
            # All profiles except Comedy and Soundtrack
            return [p for p in profiles
                    if "comedy" not in p["name"].lower()
                    and "soundtrack" not in p["name"].lower()]

    def _setup_metadata_profile_select(self):
        """Populate the metadata profile dropdown based on current folder."""
        filtered = self._get_filtered_profiles()
        options = []
        for p in filtered:
            options.append(_opt(p["name"], p["id"]))

        if not options:
            self.metadata_profile_select.disabled = True
            self.metadata_profile_select.placeholder = "No profiles"
        else:
            # Auto-select based on folder
            if self.selected_folder and self.lidarr:
                folder_name = self._get_folder_name()
                auto_id = self.lidarr.resolve_metadata_profile(folder_name)
                self.selected_metadata_profile = auto_id
                for opt in options:
                    opt.default = opt.value == str(auto_id)
            elif not self.selected_metadata_profile:
                self.selected_metadata_profile = int(options[0].value)
                options[0].default = True

        self.metadata_profile_select.options = options[:25]

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
        # Refresh metadata profiles based on new folder
        self._setup_metadata_profile_select()
        await interaction.response.edit_message(
            embed=self.build_embed(), view=self
        )

    # ── Mode dropdown ────────────────────────────────────────────────────

    @discord.ui.select(
        placeholder="🎛️ Download mode...",
        options=[
            _opt("Tracks (prune below threshold)", "tracks",
                 description="Download album, delete below-threshold tracks",
                 emoji="🎵"),
            _opt("Album (keep everything)", "album",
                 description="Download full album, keep all tracks",
                 emoji="💿"),
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

    # ── Metadata profile dropdown ────────────────────────────────────────

    @discord.ui.select(
        placeholder="📀 Metadata profile...",
        min_values=1,
        max_values=1,
        row=2,
    )
    async def metadata_profile_select(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ):
        self.selected_metadata_profile = int(select.values[0])
        for opt in select.options:
            opt.default = opt.value == select.values[0]
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
        if self.artist_data:
            genres = self.artist_data.get("genres", [])
            if genres:
                embed.add_field(
                    name="Genres",
                    value=", ".join(genres[:3]),
                    inline=True,
                )
            nb_fan = self.artist_data.get("nb_fan")
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

        embed.add_field(name="📁 Root Folder", value=folder_display, inline=True)
        mode_display = "🎵 Tracks (prune)" if self.selected_mode == "tracks" else "💿 Album (keep all)"
        embed.add_field(name="🎛️ Mode", value=mode_display, inline=True)
        embed.add_field(name="📊 Threshold", value=f"{self.threshold}/100", inline=True)

        # Metadata profile
        meta_display = "auto"
        if self.selected_metadata_profile and self.metadata_profiles:
            for p in self.metadata_profiles:
                if p["id"] == self.selected_metadata_profile:
                    meta_display = p["name"]
                    break
        embed.add_field(name="📀 Metadata", value=meta_display, inline=True)

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
            f"`{prefix}import` — Import existing Lidarr artists into watchlist\n"
            f"`{prefix}update` — Update artist settings (folder, mode, metadata)\n"
            f"`{prefix}remove <artist>` — Stop tracking\n"
            f"`{prefix}list` — Show watchlist\n"
            f"`{prefix}check` — Run popularity check (recent releases)\n"
            f"`{prefix}scan` — Full catalog scan (pick artist or all)\n"
            f"`{prefix}prune` — Delete below-threshold tracks from downloaded albums\n"
            f"`{prefix}check-downloads` — Check pending downloads, auto-prune completed\n"
            f"`{prefix}keep` — Mark tracks as never-prune (nested menu)\n"
            f"`{prefix}threshold <0-100>` — View/set popularity threshold\n"
            f"`{prefix}mode <tracks|album>` — Download popular tracks only, or whole albums\n"
            f"`{prefix}folder` — Show/set root folders\n"
            f"`{prefix}help` — This message\n"
            f"`{prefix}menu` — Interactive menu with buttons"
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


class AddFuzzyPickerView(discord.ui.View):
    """Dropdown to pick from fuzzy search results with confirm/cancel."""

    def __init__(self, author_id: int, results: list[dict]):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.selected: Optional[dict] = None

        options = []
        for r in results[:25]:
            fans = r.get("nb_fan", 0)
            if fans >= 1_000_000:
                desc = f"{fans / 1_000_000:.1f}M fans"
            elif fans >= 1_000:
                desc = f"{fans / 1_000:.0f}K fans"
            else:
                desc = f"{fans} fans"
            options.append(_opt(r["name"], r["id"], description=desc,
            ))
        self.artist_select.options = options

    @discord.ui.select(placeholder="Pick the artist you meant...", min_values=1, max_values=1, row=0)
    async def artist_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        # Store the selected artist data
        for opt in select.options:
            if opt.value == select.values[0]:
                self.selected = {"id": opt.value, "name": opt.label, "nb_fan": 0}
                break
        for opt in select.options:
            opt.default = opt.value == select.values[0]
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="✅ Add This Artist", style=discord.ButtonStyle.success, row=1)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        if not self.selected:
            await interaction.response.send_message("❌ Pick an artist first.", ephemeral=True)
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(title=f"✅ Selected: {self.selected['name']}", color=0x1DB954),
            view=self,
        )

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger, row=1)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        self.selected = None
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


@bot.command(name="add")
async def add_artist(ctx: commands.Context, *, artist_name: str = None):
    """Add an artist with an interactive setup dialog."""
    if not artist_name:
        await ctx.send("❌ Give me an artist name. Example: `?add Linkin Park`")
        return

    artist_name = artist_name.strip()

    # Search on Deezer
    artist_data = None
    music_id = None
    display_name = artist_name
    try:
        sp = MusicClient()
        found = sp.search_artist(artist_name)
        if not found:
            # Fuzzy search — show picker
            results = sp.search_artists(artist_name)
            if not results:
                await ctx.send(f"❌ Couldn't find **{artist_name}** on Deezer. Check the spelling?")
                return

            # Check if exact match in results
            exact = None
            for r in results:
                if r["name"].lower() == artist_name.lower():
                    exact = r
                    break
            if exact:
                found = exact
            else:
                # Show picker
                picker = AddFuzzyPickerView(ctx.author.id, results)
                embed = discord.Embed(
                    title=f"🔍 Results for: {artist_name}",
                    description="Pick the artist you meant:",
                    color=0x1DB954,
                )
                await ctx.send(embed=embed, view=picker)
                await picker.wait()
                if not picker.selected:
                    return
                found = picker.selected

        music_id = found["id"]
        display_name = found["name"]
        artist_data = found
    except Exception as e:
        log.warning("Music lookup failed for '%s': %s", artist_name, e)

    # Check if already in watchlist
    existing = db.get_artist(display_name)
    if existing:
        await ctx.send(f"⚠️ **{display_name}** is already in the watchlist.")
        return

    # Fetch Lidarr root folders and metadata profiles
    folders = []
    metadata_profiles = []
    lidarr = None
    try:
        from lidarr_hits_bot.clients.lidarr import LidarrClient
        lidarr = LidarrClient()
        folders = lidarr.get_root_folders()
        metadata_profiles = lidarr.get_metadata_profiles()
    except Exception as e:
        log.warning("Could not fetch Lidarr data: %s", e)

    # Launch the interactive view
    view = AddArtistView(
        author_id=ctx.author.id,
        display_name=display_name,
        music_id=music_id,
        artist_data=artist_data,
        folders=folders,
        metadata_profiles=metadata_profiles,
        lidarr_client=lidarr,
    )

    await ctx.send(embed=view.build_embed(), view=view)

    # Wait for the user to confirm or cancel
    await view.wait()

    if view.cancelled or not view.confirmed:
        return

    # ── User confirmed — save to database ────────────────────────────────
    added_by = str(ctx.author)
    success = db.add_artist(
        display_name, added_by, music_id,
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

    # Store metadata profile and mode for this artist
    if view.selected_metadata_profile:
        db.set_setting(f"meta_profile_{display_name}", str(view.selected_metadata_profile))
    db.set_setting(f"mode_{display_name}", view.selected_mode)

    # Confirmation message
    folder_display = "(default)"
    if view.selected_folder:
        folder_display = view.selected_folder.rstrip("/").split("/")[-1]

    meta_display = "auto"
    if view.selected_metadata_profile and metadata_profiles:
        for p in metadata_profiles:
            if p["id"] == view.selected_metadata_profile:
                meta_display = p["name"]
                break

    embed = discord.Embed(
        title="✅ Artist Added",
        description=(
            f"**{display_name}** is now being tracked.\n\n"
            f"📁 **Folder:** {folder_display}\n"
            f"🎛️ **Mode:** {view.selected_mode}\n"
            f"📊 **Threshold:** {view.threshold}/100\n"
            f"📀 **Metadata:** {meta_display}"
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


# ── Update Artist UI ─────────────────────────────────────────────────────────


class UpdatePickerView(discord.ui.View):
    """Dropdown to pick an artist to update."""

    def __init__(self, author_id: int, artists: list[dict]):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.artists = artists
        self.selected: Optional[str] = None

        options = []
        for a in artists:
            options.append(_opt(a["name"], a["name"]))
        self.artist_select.options = options[:25]

    @discord.ui.select(placeholder="Pick an artist to update...", min_values=1, max_values=1, row=0)
    async def artist_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        self.selected = select.values[0]
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="🔍 Search", style=discord.ButtonStyle.secondary, row=1)
    async def search_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        modal = SearchModal(self, self.artist_select, self.artists)
        await interaction.response.send_modal(modal)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


def _sync_to_lidarr(lidarr, lidarr_id, artist, new_meta, current_meta, new_folder, current_folder):
    """Sync changes to Lidarr in a background thread. Returns list of change strings."""
    changes = []
    try:
        artist_data = lidarr.get_artist(lidarr_id)
        if not artist_data:
            changes.append("  ⚠️ Could not fetch artist from Lidarr")
            return changes

        log.info("Before update — artist '%s': metadataProfileId=%s, rootFolderPath=%s",
                 artist["name"], artist_data.get("metadataProfileId"), artist_data.get("rootFolderPath"))

        # Update metadata profile
        if new_meta and new_meta != current_meta:
            artist_data["metadataProfileId"] = new_meta
            log.info("Setting metadataProfileId to %s", new_meta)
            result = lidarr._put(f"/artist/{lidarr_id}", artist_data)
            log.info("PUT response — metadataProfileId=%s", result.get("metadataProfileId"))

            verify = lidarr.get_artist(lidarr_id)
            if verify.get("metadataProfileId") == new_meta:
                log.info("✅ Metadata profile confirmed in Lidarr")
            else:
                log.warning("⚠️ Metadata mismatch! Sent %s, got %s", new_meta, verify.get("metadataProfileId"))

        # Move artist if folder changed
        if new_folder and new_folder != current_folder:
            if lidarr.move_artist(lidarr_id, new_folder):
                changes.append("  ✅ Moved in Lidarr")
            else:
                changes.append("  ⚠️ Failed to move in Lidarr")

        changes.append("  ✅ Synced to Lidarr")

        # If metadata changed, unmonitor all albums
        if new_meta and new_meta != current_meta:
            artist_data["monitored"] = False
            lidarr._put(f"/artist/{lidarr_id}", artist_data)
            albums = lidarr.get_artist_albums(lidarr_id)
            unmonitored = 0
            for a in albums:
                if a.get("monitored"):
                    lidarr.unmonitor_album(a["id"])
                    unmonitored += 1
            artist_data["monitored"] = True
            lidarr._put(f"/artist/{lidarr_id}", artist_data)
            for a in albums:
                db.set_setting(f"pruned_{artist['id']}_{a.get('title', '')}", "")
            changes.append(f"  ↳ Unmonitored all types ({unmonitored} album(s))")

    except Exception as e:
        changes.append(f"  ⚠️ Lidarr sync failed: {e}")

    return changes


@bot.command(name="update")
async def update_cmd(ctx: commands.Context, *, artist_name: str = None):
    """Update an artist's settings — folder, mode, threshold, metadata profile."""
    artists = db.list_artists()
    if not artists:
        await ctx.send("📭 Watchlist is empty.")
        return

    # Step 1: Pick artist (fuzzy from arg, or dropdown)
    if artist_name:
        artist = fuzzy_find_artist(artists, artist_name)
        if not artist:
            await ctx.send(f"❌ No artist matching **{artist_name}** in watchlist.")
            return
    else:
        picker = UpdatePickerView(ctx.author.id, artists)
        await ctx.send(embed=discord.Embed(
            title="✏️ Update Artist",
            description="Pick an artist to update their settings.",
            color=0x1DB954,
        ), view=picker)
        await picker.wait()
        if not picker.selected:
            return
        artist = db.get_artist(picker.selected)
        if not artist:
            await ctx.send(f"❌ **{picker.selected}** not found.")
            return

    # Step 2: Fetch Lidarr data
    folders = []
    metadata_profiles = []
    lidarr = None
    try:
        from lidarr_hits_bot.clients.lidarr import LidarrClient
        lidarr = LidarrClient()
        folders = lidarr.get_root_folders()
        metadata_profiles = lidarr.get_metadata_profiles()
    except Exception as e:
        log.warning("Could not fetch Lidarr data: %s", e)

    # Get current settings
    current_folder = artist.get("root_folder") or db.get_setting("default_root_folder") or ""
    current_mode = db.get_setting(f"mode_{artist['name']}") or Config.DOWNLOAD_MODE
    current_threshold = Config.POPULARITY_THRESHOLD
    current_meta = db.get_setting(f"meta_profile_{artist['name']}")
    if current_meta:
        current_meta = int(current_meta)

    # Step 3: Show the same dialog as ?add, pre-populated
    view = AddArtistView(
        author_id=ctx.author.id,
        display_name=artist["name"],
        music_id=artist.get("spotify_id"),
        artist_data=None,
        folders=folders,
        metadata_profiles=metadata_profiles,
        lidarr_client=lidarr,
        button_label="✅ Update Artist",
    )

    # Pre-populate current values
    if current_folder:
        view.selected_folder = current_folder
        for opt in view.folder_select.options:
            opt.default = opt.value == current_folder
    view.selected_mode = current_mode
    for opt in view.mode_select.options:
        opt.default = opt.value == current_mode
    view.threshold = current_threshold
    if current_meta:
        view.selected_metadata_profile = current_meta
        for opt in view.metadata_profile_select.options:
            opt.default = opt.value == str(current_meta)

    await ctx.send(embed=view.build_embed(), view=view)
    await view.wait()

    if view.cancelled or not view.confirmed:
        return

    # Step 4: Apply changes
    changes = []

    # ── Apply changes to Lidarr ──────────────────────────────────────────
    lidarr_id = artist.get("lidarr_id")
    lidarr_changed = False

    # Folder change
    new_folder = view.selected_folder
    if new_folder and new_folder != current_folder:
        db.set_artist_root_folder(artist["name"], new_folder)
        folder_display = new_folder.rstrip("/").split("/")[-1]
        changes.append(f"📁 Folder → {folder_display}")
        lidarr_changed = True

    # Mode change (bot-only, no Lidarr sync needed)
    if view.selected_mode != current_mode:
        db.set_setting(f"mode_{artist['name']}", view.selected_mode)
        changes.append(f"🎛️ Mode → {view.selected_mode}")

    # Threshold change (bot-only)
    if view.threshold != current_threshold:
        Config.POPULARITY_THRESHOLD = view.threshold
        db.set_setting("popularity_threshold", str(view.threshold))
        changes.append(f"📊 Threshold → {view.threshold}/100")

    # Metadata profile change
    if view.selected_metadata_profile and view.selected_metadata_profile != current_meta:
        db.set_setting(f"meta_profile_{artist['name']}", str(view.selected_metadata_profile))
        meta_display = str(view.selected_metadata_profile)
        for p in metadata_profiles:
            if p["id"] == view.selected_metadata_profile:
                meta_display = p["name"]
                break
        changes.append(f"📀 Metadata → {meta_display}")
        lidarr_changed = True

    # Sync all changes to Lidarr in a thread
    if lidarr_changed and lidarr_id and lidarr:
        loop = asyncio.get_event_loop()
        sync_result = await loop.run_in_executor(
            None, _sync_to_lidarr, lidarr, lidarr_id, artist,
            view.selected_metadata_profile, current_meta, new_folder, current_folder
        )
        changes.extend(sync_result)

    if not changes:
        await ctx.send(f"No changes made to **{artist['name']}**.")
    else:
        embed = discord.Embed(
            title=f"✏️ Updated: {artist['name']}",
            description="\n".join(changes),
            color=0x1DB954,
        )
        await ctx.send(embed=embed)


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
            options.append(_opt(a["name"], a["name"]))
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

    @discord.ui.button(label="🔍 Search", style=discord.ButtonStyle.secondary, row=1)
    async def search_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        modal = SearchModal(self, self.artist_select, self.artists)
        await interaction.response.send_modal(modal)

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
async def scan_cmd(ctx: commands.Context, *, artist_name: str = None):
    """Full catalog scan — pick an artist or scan all."""
    artists = db.list_artists()
    if not artists:
        await ctx.send("📭 Watchlist is empty. Add an artist with `?add <name>` first.")
        return

    # Fuzzy match from argument
    if artist_name:
        artist = fuzzy_find_artist(artists, artist_name)
        if not artist:
            await ctx.send(f"❌ No artist matching **{artist_name}** in watchlist.")
            return
        await ctx.send(f"🔍 Scanning **{artist['name']}**'s full catalog for hits...")
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, run_daily_check, artist["name"], True)
        report = format_results(results)
        await ctx.send(report)
        return

    # No argument — show dropdown
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


class PruneArtistView(discord.ui.View):
    """Dropdown to pick an artist (or all) for pruning."""

    def __init__(self, author_id: int, artists: list[dict]):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.artists = artists
        self.selected: Optional[str] = None
        self.confirmed = False

        options = []
        for a in artists:
            options.append(_opt(a["name"], a["name"]))
        self.artist_select.options = options[:25]

    @discord.ui.select(placeholder="Pick an artist to prune...", min_values=1, max_values=1, row=0)
    async def artist_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        self.selected = select.values[0]
        for opt in select.options:
            opt.default = opt.value == self.selected
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="🔍 Search", style=discord.ButtonStyle.secondary, row=1)
    async def search_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        modal = SearchModal(self, self.artist_select, self.artists)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="✂️ Prune Artist", style=discord.ButtonStyle.success, row=1)
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
                title=f"✂️ Pruning: {self.selected}",
                description="Checking downloaded albums...",
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


@bot.command(name="prune")
async def prune_cmd(ctx: commands.Context, *, artist_name: str = None):
    """Prune downloaded albums — pick an artist or prune all."""
    artists = db.list_artists()
    if not artists:
        await ctx.send("📭 Watchlist is empty. Add an artist with `?add <name>` first.")
        return

    # Fuzzy match from argument
    if artist_name:
        artist = fuzzy_find_artist(artists, artist_name)
        if not artist:
            await ctx.send(f"❌ No artist matching **{artist_name}** in watchlist.")
            return
        await ctx.send(f"✂️ Pruning **{artist['name']}**'s downloaded albums...")
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, prune_downloaded_albums, artist["name"], True)
        report = format_prune_results(results)
        await ctx.send(report)
        return

    # No argument — show dropdown
    view = PruneArtistView(ctx.author.id, artists)
    embed = discord.Embed(
        title="✂️ Prune Downloaded Albums",
        description=(
            f"Pick an artist to prune their downloaded albums.\n"
            f"Deletes below-threshold tracks and unmonitors albums.\n\n"
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
        # Confirmation warning
        confirm_view = ScanConfirmView(ctx.author.id)
        confirm_embed = discord.Embed(
            title="⚠️ Prune ALL Artists?",
            description=(
                f"You're about to prune **{len(artists)} artists**' downloaded albums.\n\n"
                "This deletes below-threshold tracks from disk and unmonitors albums.\n"
                "**This cannot be undone.**\n\n"
                "Are you sure?"
            ),
            color=0xFFA500,
        )
        await ctx.send(embed=confirm_embed, view=confirm_view)
        await confirm_view.wait()

        if not confirm_view.confirmed:
            return

        await ctx.send(f"✂️ Pruning **{len(artists)} artists**... this may take a while.")
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, prune_downloaded_albums, None, True)
    else:
        if not view.confirmed:
            return
        await ctx.send(f"✂️ Pruning **{view.selected}**'s downloaded albums...")
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, prune_downloaded_albums, view.selected, True)

    report = format_prune_results(results)
    await ctx.send(report)


@bot.command(name="check-downloads", aliases=["dl"])
async def check_downloads_cmd(ctx: commands.Context):
    """Check pending downloads and auto-prune completed ones."""
    await ctx.send("📥 Checking pending downloads...")
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, check_downloads)
    report = format_download_check_results(results)
    await ctx.send(report)


@bot.command(name="import")
async def import_cmd(ctx: commands.Context):
    """Import existing Lidarr artists into the bot watchlist."""
    await ctx.send("📥 Importing artists from Lidarr...")

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _run_import)

    embed = discord.Embed(title="📥 Import Complete", color=0x1DB954)
    if result["added"]:
        embed.add_field(name=f"Added ({len(result['added'])})",
                        value="\n".join(result["added"][:20]), inline=False)
        if len(result["added"]) > 20:
            embed.set_footer(text=f"... and {len(result['added']) - 20} more")
    if result["skipped"]:
        embed.add_field(name=f"Skipped ({len(result['skipped'])})",
                        value=f"{len(result['skipped'])} already in watchlist", inline=False)
    if result["errors"]:
        embed.add_field(name=f"Errors ({len(result['errors'])})",
                        value="\n".join(result["errors"][:5]), inline=False)
    if not result["added"] and not result["skipped"]:
        embed.description = "No artists found in Lidarr."
    await ctx.send(embed=embed)


def _run_import() -> dict:
    """Run the import in a background thread."""
    result = {"added": [], "skipped": [], "errors": []}

    try:
        from lidarr_hits_bot.clients.lidarr import LidarrClient
        lidarr = LidarrClient()
    except Exception as e:
        result["errors"].append(f"Lidarr connection failed: {e}")
        return result

    try:
        lidarr_artists = lidarr.get_all_artists()
    except Exception as e:
        result["errors"].append(f"Failed to fetch artists: {e}")
        return result

    existing = {a["name"].lower() for a in db.list_artists()}
    existing_lidarr_ids = {a.get("lidarr_id") for a in db.list_artists() if a.get("lidarr_id")}

    for la in lidarr_artists:
        artist_name = la.get("artistName", "Unknown")
        lidarr_id = la.get("id")

        if artist_name.lower() in existing or lidarr_id in existing_lidarr_ids:
            result["skipped"].append(artist_name)
            continue

        try:
            details = lidarr.get_artist_details(lidarr_id)
            if not details:
                continue

            root_path = details.get("rootFolderPath", "")
            root_folder = root_path if root_path else None
            meta_profile_id = details.get("metadataProfileId")

            # Add to watchlist (no Deezer lookup — scan does that later)
            success = db.add_artist(artist_name, "imported", None, root_folder=root_folder)
            if not success:
                result["skipped"].append(artist_name)
                continue

            db.set_setting(f"mode_{artist_name}", "album")
            if meta_profile_id:
                db.set_setting(f"meta_profile_{artist_name}", str(meta_profile_id))

            # Unmonitor all albums
            unmonitored = lidarr.unmonitor_all_albums(lidarr_id)

            db.update_artist_lidarr_id(artist_name, lidarr_id)

            folder_display = root_folder.rstrip("/").split("/")[-1] if root_folder else "default"
            result["added"].append(f"{artist_name} (📁 {folder_display})")
        except Exception as e:
            result["errors"].append(f"{artist_name}: {e}")

    return result


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
        from lidarr_hits_bot.clients.lidarr import LidarrClient
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


# ── Menu UI ──────────────────────────────────────────────────────────────────


class MenuInputModal(discord.ui.Modal, title="Enter Search Term"):
    """Modal for entering a search term from the menu."""

    search_input = discord.ui.TextInput(
        label="Artist name (fuzzy OK)",
        placeholder="e.g. 'link' for Linkin Park",
        style=discord.TextStyle.short,
        required=True,
        max_length=100,
    )

    def __init__(self, command_name: str):
        super().__init__()
        self.command_name = command_name
        self.value = None

    async def on_submit(self, interaction: discord.Interaction):
        self.value = self.search_input.value.strip()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title=f"🔍 Running: ?{self.command_name} {self.value}",
                description="Processing...",
                color=0x1DB954,
            ),
            view=None,
        )
        self.stop()

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await interaction.response.send_message(f"❌ Error: {error}", ephemeral=True)


class MenuView(discord.ui.View):
    """Main menu with buttons for each command."""

    def __init__(self, author_id: int):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.selected_command = None
        self.search_value = None

    @discord.ui.button(label="➕ Add", style=discord.ButtonStyle.success, row=0)
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        modal = MenuInputModal("add")
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal.value:
            self.selected_command = "add"
            self.search_value = modal.value
            self.stop()

    @discord.ui.button(label="✏️ Update", style=discord.ButtonStyle.primary, row=0)
    async def update_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        modal = MenuInputModal("update")
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal.value:
            self.selected_command = "update"
            self.search_value = modal.value
            self.stop()

    @discord.ui.button(label="🔍 Scan", style=discord.ButtonStyle.primary, row=0)
    async def scan_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        modal = MenuInputModal("scan")
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal.value:
            self.selected_command = "scan"
            self.search_value = modal.value
            self.stop()

    @discord.ui.button(label="✂️ Prune", style=discord.ButtonStyle.primary, row=1)
    async def prune_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        modal = MenuInputModal("prune")
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal.value:
            self.selected_command = "prune"
            self.search_value = modal.value
            self.stop()

    @discord.ui.button(label="📥 Import", style=discord.ButtonStyle.secondary, row=1)
    async def import_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        self.selected_command = "import"
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="📥 DL Check", style=discord.ButtonStyle.secondary, row=1)
    async def dl_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        self.selected_command = "dl"
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="📋 List", style=discord.ButtonStyle.secondary, row=2)
    async def list_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        self.selected_command = "list"
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="📊 Check", style=discord.ButtonStyle.secondary, row=2)
    async def check_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        self.selected_command = "check"
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger, row=3)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(title="❌ Menu closed", color=0xFF0000),
            view=self,
        )

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


@bot.command(name="menu")
async def menu_cmd(ctx: commands.Context):
    """Show interactive menu with buttons for all commands."""
    embed = discord.Embed(
        title="🎵 Lidarr Hits Bot — Menu",
        description="Click a button to run a command.\nCommands with arguments will prompt for input.",
        color=0x1DB954,
    )
    embed.add_field(name="➕ Add", value="Add a new artist", inline=True)
    embed.add_field(name="✏️ Update", value="Update artist settings", inline=True)
    embed.add_field(name="🔍 Scan", value="Full catalog scan", inline=True)
    embed.add_field(name="✂️ Prune", value="Prune downloaded albums", inline=True)
    embed.add_field(name="📥 Import", value="Import from Lidarr", inline=True)
    embed.add_field(name="📥 DL Check", value="Check pending downloads", inline=True)
    embed.add_field(name="📋 List", value="Show watchlist", inline=True)
    embed.add_field(name="📊 Check", value="Quick popularity check", inline=True)
    embed.set_footer(text="Times out in 2 min")

    view = MenuView(ctx.author.id)
    await ctx.send(embed=embed, view=view)
    await view.wait()

    if not view.selected_command:
        return

    # Execute the selected command
    if view.search_value:
        # Command needs an argument — invoke with it
        cmd = bot.get_command(view.selected_command)
        if cmd:
            await ctx.invoke(cmd, artist_name=view.search_value)
    else:
        # No argument needed — invoke directly
        cmd = bot.get_command(view.selected_command)
        if cmd:
            await ctx.invoke(cmd)


# ── Keep (Never Prune) UI ────────────────────────────────────────────────────


class KeepArtistView(discord.ui.View):
    """Step 1: Pick an artist for never-prune."""

    def __init__(self, author_id: int, artists: list[dict]):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.artists = artists
        self.selected_artist: Optional[dict] = None
        
        # Build dropdown options with logging
        import logging
        log = logging.getLogger(__name__)
        options = []
        for i, a in enumerate(artists[:25]):  # Discord max 25 options
            name = a.get("name") or "Unknown"
            label = str(name)[:25] if name else "Unknown"
            # Ensure label is never empty
            if not label.strip():
                label = "Unknown"
            options.append(_opt(label, str(a.get("id", ""))))
            if i < 3:  # Log first 3
                log.info(f"KeepArtistView option {i}: label='{label}', value='{a.get('id', '')}'")
        
        self.artist_select.options = options
        
        # Auto-select if only 1 artist provided
        if len(artists) == 1:
            self.selected_artist = artists[0]
            self.artist_select.disabled = True
            self.artist_select.placeholder = f"Only artist: {artists[0].get('name', 'Unknown')}"
            log.info(f"KeepArtistView: Auto-selected 1 artist: {artists[0].get('name', 'Unknown')}")
        else:
            self.artist_select.placeholder = "Pick an artist..."
            log.info(f"KeepArtistView: {len(artists)} artists, showing dropdown")

    @discord.ui.select(placeholder="Pick an artist...", min_values=1, max_values=1, row=0)
    async def artist_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        selected_value = select.values[0]
        # Try to find by ID first
        for a in self.artists:
            if str(a["id"]) == selected_value:
                self.selected_artist = a
                break
        # Fallback: find by matching the selected option's label
        if not self.selected_artist:
            for opt in select.options:
                if opt.value == selected_value:
                    for a in self.artists:
                        if a["name"] == opt.label:
                            self.selected_artist = a
                            break
                    break
        # Last resort: create a minimal dict from the option
        if not self.selected_artist:
            for opt in select.options:
                if opt.value == selected_value:
                    self.selected_artist = {"id": int(selected_value), "name": opt.label}
                    break
        for opt in select.options:
            opt.default = opt.value == selected_value
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="🔍 Search", style=discord.ButtonStyle.secondary, row=1)
    async def search_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        modal = SearchModal(self, self.artist_select, self.artists)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success, row=1)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        # Fallback: if selected_artist not set, try to read from dropdown
        if not self.selected_artist:
            for opt in self.artist_select.options:
                if opt.default:
                    for a in self.artists:
                        if a["name"] == opt.label or str(a["id"]) == opt.value:
                            self.selected_artist = a
                            break
                    if not self.selected_artist:
                        self.selected_artist = {"id": int(opt.value), "name": opt.label}
                    break
        if not self.selected_artist:
            await interaction.response.send_message("❌ Pick an artist first.", ephemeral=True)
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger, row=1)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(title="❌ Cancelled", color=0xFF0000), view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


class KeepAlbumView(discord.ui.View):
    """Step 2: Pick an album (from Lidarr)."""

    def __init__(self, author_id: int, albums: list[dict]):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.albums = albums
        self.selected_album: Optional[dict] = None
        
        # Build dropdown options with logging
        import logging
        log = logging.getLogger(__name__)
        options = []
        for i, a in enumerate(albums[:25]):  # Discord max 25 options
            title = (a.get("title") or "Unknown Album").strip()
            if not title:
                title = "Unknown Album"
            label = str(title)[:25] if title else "Unknown Album"
            # Ensure label is never empty
            if not label.strip():
                label = "Unknown Album"
            options.append(_opt(label, str(a.get("id", ""))))
            if i < 3:  # Log first 3
                log.info(f"KeepAlbumView option {i}: label='{label}', value='{a.get('id', '')}'")
        
        self.album_select.options = options
        
        # Auto-select if only 1 album provided
        if len(albums) == 1:
            self.selected_album = albums[0]
            self.album_select.disabled = True
            self.album_select.placeholder = f"Only album: {albums[0].get('title', 'Unknown')}"
            log.info(f"KeepAlbumView: Auto-selected 1 album: {albums[0].get('title', 'Unknown')}")
        else:
            self.album_select.placeholder = "Pick an album..."
            log.info(f"KeepAlbumView: {len(albums)} albums, showing dropdown")

    @discord.ui.select(placeholder="Pick an album...", min_values=1, max_values=1, row=0)
    async def album_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        selected_value = select.values[0]
        # Try to find by ID first
        for a in self.albums:
            if str(a["id"]) == selected_value:
                self.selected_album = a
                break
        # Fallback: find by matching the selected option's label
        if not self.selected_album:
            for opt in select.options:
                if opt.value == selected_value:
                    for a in self.albums:
                        if a.get("title") == opt.label:
                            self.selected_album = a
                            break
                    break
        # Last resort: create a minimal dict
        if not self.selected_album:
            for opt in select.options:
                if opt.value == selected_value:
                    self.selected_album = {"id": int(selected_value), "title": opt.label}
                    break
        for opt in select.options:
            opt.default = opt.value == selected_value
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="🔍 Search", style=discord.ButtonStyle.secondary, row=1)
    async def search_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        album_dicts = [{"name": a.get("title", "Unknown"), "id": str(a["id"])} for a in self.albums]
        modal = SearchModal(self, self.album_select, album_dicts)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success, row=1)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        # Fallback: if selected_album not set, try to read from dropdown
        if not self.selected_album:
            for opt in self.album_select.options:
                if opt.default:
                    for a in self.albums:
                        if a.get("title") == opt.label or str(a["id"]) == opt.value:
                            self.selected_album = a
                            break
                    if not self.selected_album:
                        self.selected_album = {"id": int(opt.value), "title": opt.label}
                    break
        if not self.selected_album:
            await interaction.response.send_message("❌ Pick an album first.", ephemeral=True)
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger, row=1)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(title="❌ Cancelled", color=0xFF0000), view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


class KeepTrackView(discord.ui.View):
    """Step 3: Pick tracks to protect (multi-select)."""

    def __init__(self, author_id: int, tracks: list[dict], already_protected: set[str]):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.tracks = tracks
        self.already_protected = already_protected
        self.selected_track_ids: list[str] = []
        self.mark_all = False

        options = []
        for t in tracks[:25]:
            title = (t.get("title") or "Unknown").strip()
            if not title:
                title = "Unknown"
            protected = "🔒" if title.lower() in {p.lower() for p in already_protected} else ""
            options.append(_opt(f"{protected} {title}".strip(), str(t["id"]),
                default=title.lower() in {p.lower() for p in already_protected},
            ))
        self.track_select.options = options

    @discord.ui.select(placeholder="Select tracks to protect (multi-select)...",
                       min_values=0, max_values=25, row=0)
    async def track_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        self.selected_track_ids = select.values
        await interaction.response.defer()

    @discord.ui.button(label="📀 Mark All Tracks", style=discord.ButtonStyle.primary, row=1)
    async def mark_all_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        self.mark_all = True
        self.selected_track_ids = [str(t["id"]) for t in self.tracks]
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="✅ All tracks marked as never-prune",
                description=f"Protecting all {len(self.tracks)} track(s)",
                color=0x1DB954,
            ), view=self)

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success, row=1)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        if not self.selected_track_ids:
            await interaction.response.send_message("❌ Select at least one track.", ephemeral=True)
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        count = len(self.selected_track_ids)
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="✅ Tracks Protected",
                description=f"{count} track(s) marked as never-prune",
                color=0x1DB954,
            ), view=self)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger, row=2)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        self.selected_track_ids = []
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(title="❌ Cancelled", color=0xFF0000), view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


@bot.command(name="keep")
async def keep_cmd(ctx: commands.Context, artist_name: str = None):
    """Mark tracks as never-prune. Nested menu: artist → album → tracks.
    
    Usage:
    ?keep                          # Interactive menu
    ?keep Linkin Park              # Auto-select Linkin Park artist
    """
    artists = db.list_artists()
    if not artists:
        await ctx.send("📭 Watchlist is empty.")
        return
    
    # If artist name provided, try to find and auto-select it
    selected_artist = None
    if artist_name:
        # Fuzzy match against artist names
        import re
        query = artist_name.lower().strip()
        matched = None
        for a in artists:
            a_name = (a.get("name") or "").lower()
            # Exact match first
            if query == a_name:
                matched = a
                break
            # Substring match
            if query in a_name:
                matched = a
                break
            # Fuzzy: first word match
            if query and a_name.startswith(query):
                matched = a
                break
        if matched:
            selected_artist = matched
            log = logging.getLogger(__name__)
            log.info(f"keep_cmd: Auto-selected artist '{matched['name']}' from argument '{artist_name}'")
        else:
            await ctx.send(f"❌ Artist **{artist_name}** not found in watchlist.\nAvailable: {', '.join(a.get('name', 'Unknown') for a in artists[:5])}...")
            return
    else:
        # Interactive mode - show the view
        artist_view = KeepArtistView(ctx.author.id, artists)
        await ctx.send(embed=discord.Embed(
            title="🔒 Never Prune — Step 1: Pick Artist",
            description="Select an artist to protect tracks from pruning.",
            color=0x1DB954,
        ), view=artist_view)
        await artist_view.wait()
        
        if not artist_view.selected_artist:
            return
        selected_artist = artist_view.selected_artist
    
    artist = selected_artist
    lidarr_id = artist.get("lidarr_id")
    
    if not lidarr_id:
        await ctx.send(f"❌ **{artist['name']}** not in Lidarr yet.")
        return
    
    # Step 2: Pick album (even if interactive, we now auto-have the artist)
    try:
        from lidarr_hits_bot.clients.lidarr import LidarrClient
        lidarr = LidarrClient()
        loop = asyncio.get_event_loop()
        albums = await loop.run_in_executor(None, lidarr.get_artist_albums, lidarr_id)
    except Exception as e:
        await ctx.send(f"❌ Lidarr error: {e}")
        return
    
    if not albums:
        await ctx.send(f"❌ No albums found for **{artist['name']}** in Lidarr.")
        return
    
    # Auto-select if only 1 album
    if len(albums) == 1:
        album_view = type('obj', (object,), {'selected_album': albums[0]})()
        album = albums[0]
        album_name = album.get("title", "Unknown")
        log = logging.getLogger(__name__)
        log.info(f"keep_cmd: Auto-selected 1 album: {album_name}")
    else:
        album_view = KeepAlbumView(ctx.author.id, albums)
        await ctx.send(embed=discord.Embed(
            title=f"🔒 Never Prune — Step 2: Pick Album ({artist['name']})",
            description="Select an album to protect tracks from.",
            color=0x1DB954,
        ), view=album_view)
        await album_view.wait()
        
        if not album_view.selected_album:
            return
        album = album_view.selected_album
        album_name = album.get("title", "Unknown")
    
    # Step 3: Pick tracks
    try:
        from lidarr_hits_bot.clients.lidarr import LidarrClient
        lidarr = LidarrClient()
        tracks = await loop.run_in_executor(None, lidarr.get_album_tracks, album["id"])
    except Exception:
        tracks = []
    
    if not tracks:
        await ctx.send(f"❌ No tracks found for **{album_name}**.")
        return
    
    already_protected = db.get_never_prune_tracks(artist["id"], album_name)
    track_view = KeepTrackView(ctx.author.id, tracks, already_protected)
    await ctx.send(embed=discord.Embed(
        title=f"🔒 Never Prune — Step 3: Pick Tracks ({album_name})",
        description=(
            f"Select tracks to protect from pruning.\n"
            f"🔒 = already protected\n\n"
            f"**Mark All** = keep entire album"
        ),
        color=0x1DB954,
    ), view=track_view)
    await track_view.wait()
    
    if not track_view.selected_track_ids:
        return

    album = album_view.selected_album
    album_name = album.get("title", "Unknown")

    # Step 3: Pick tracks
    try:
        tracks = await loop.run_in_executor(None, lidarr.get_album_tracks, album["id"])
    except Exception:
        tracks = []

    if not tracks:
        await ctx.send(f"❌ No tracks found for **{album_name}**.")
        return

    already_protected = db.get_never_prune_tracks(artist["id"], album_name)

    track_view = KeepTrackView(ctx.author.id, tracks, already_protected)
    await ctx.send(embed=discord.Embed(
        title=f"🔒 Never Prune — Step 3: Pick Tracks ({album_name})",
        description=(
            f"Select tracks to protect from pruning.\n"
            f"🔒 = already protected\n\n"
            f"**Mark All** = keep entire album"
        ),
        color=0x1DB954,
    ), view=track_view)
    await track_view.wait()

    if not track_view.selected_track_ids:
        return

    # Save to database
    selected_names = []
    for t in tracks:
        if str(t["id"]) in track_view.selected_track_ids:
            selected_names.append(t.get("title", "Unknown"))

    if track_view.mark_all:
        # Clear existing and add all
        db.clear_album_never_prune(artist["id"], album_name)
        db.add_album_never_prune(artist["id"], album_name, selected_names)
    else:
        # Add selected tracks
        for name in selected_names:
            db.add_never_prune(artist["id"], album_name, name)

    # Confirmation
    embed = discord.Embed(
        title="🔒 Tracks Protected",
        description=f"**{artist['name']}** — {album_name}",
        color=0x1DB954,
    )
    embed.add_field(
        name=f"Protected ({len(selected_names)})",
        value="\n".join(f"• {n}" for n in selected_names[:20]),
        inline=False,
    )
    await ctx.send(embed=embed)


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

    # Send to configured channel in a new thread
    channel_id = Config.REPORT_CHANNEL_ID
    if channel_id:
        channel = bot.get_channel(channel_id)
        if channel:
            # Create a thread for today's report
            today = datetime.now().strftime("%Y-%m-%d")
            thread_name = f"📊 Daily Report — {today}"
            try:
                thread = await channel.create_thread(
                    name=thread_name,
                    auto_archive_duration=10080,  # 7 days
                )
                report_channel = thread
                log.info("Created daily report thread: %s", thread_name)
            except discord.HTTPException as e:
                log.warning("Failed to create thread, falling back to channel: %s", e)
                report_channel = channel

            while report:
                chunk = report[:1990]
                if len(report) > 1990:
                    split_at = chunk.rfind("\n")
                    if split_at > 0:
                        chunk = report[:split_at]
                await report_channel.send(chunk)
                report = report[len(chunk):]

            # Check pending downloads and auto-prune completed ones
            log.info("Checking pending downloads...")
            dl_results = await loop.run_in_executor(None, check_downloads)
            dl_report = format_download_check_results(dl_results)
            if "No newly" not in dl_report:
                await report_channel.send(dl_report)
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
