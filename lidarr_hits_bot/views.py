"""All discord.ui.View and discord.ui.Modal classes.

Imports:
- helpers from .helpers (standalone)
- bot from .bot (only for type hints / ctx.invoke if needed)
- database lazily where needed
"""

import logging
from typing import Optional

import discord

from lidarr_hits_bot import database as db
from lidarr_hits_bot.config import Config
from lidarr_hits_bot.helpers import _opt

log = logging.getLogger(__name__)


# ── Shared Modals ─────────────────────────────────────────────────────────────

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


# ── Add Artist View ────────────────────────────────────────────────────────────

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
            discord.SelectOption(
                label="Tracks (prune below threshold)",
                value="tracks",
                description="Download album, delete below-threshold tracks",
                emoji="🎵",
            ),
            discord.SelectOption(
                label="Album (keep everything)",
                value="album",
                description="Download full album, keep all tracks",
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

    # ── Metadata profile dropdown ───────────────────────────────────────

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

    # ── Embed builder ───────────────────────────────────────────────────

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


# ── Fuzzy Picker ──────────────────────────────────────────────────────────────

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
            options.append(_opt(r["name"], r["id"], description=desc))
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


# ── Update Picker ─────────────────────────────────────────────────────────────

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


# ── Scan Views ────────────────────────────────────────────────────────────────

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


# ── Prune Views ───────────────────────────────────────────────────────────────

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


# ── Menu Modal & View ─────────────────────────────────────────────────────────

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


# ── Keep (Never Prune) Views ─────────────────────────────────────────────────

class KeepArtistView(discord.ui.View):
    """Step 1: Pick an artist for never-prune."""

    def __init__(self, author_id: int, artists: list[dict]):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.artists = artists
        self.selected_artist: Optional[dict] = None

        options = []
        for i, a in enumerate(artists[:25]):
            name = a.get("name") or "Unknown"
            label = str(name)[:25] if name else "Unknown"
            if not label.strip():
                label = "Unknown"
            options.append(_opt(label, str(a.get("id", ""))))
            if i < 3:
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

        options = []
        for i, a in enumerate(albums[:25]):
            title = (a.get("title") or "Unknown Album").strip()
            if not title:
                title = "Unknown Album"
            label = str(title)[:25] if title else "Unknown Album"
            if not label.strip():
                label = "Unknown Album"
            options.append(_opt(label, str(a.get("id", ""))))
            if i < 3:
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
