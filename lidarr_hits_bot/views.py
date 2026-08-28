"""
Discord UI Views for Lidarr Hits Bot.
Contains interactive views and modals for bot commands.
"""

import logging
from typing import Optional

import discord

from .helpers import _opt, fuzzy_find_artist

log = logging.getLogger(__name__)


# ── Interactive Add Artist UI ────────────────────────────────────────────────

class AddArtistView(discord.ui.View):
    """Interactive view for configuring an artist before adding."""

    def __init__(self, author_id: int, artist_data: dict, artists: list[dict]):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.artist_data = artist_data
        self.artists = artists
        self.selected_artist: Optional[dict] = None

        # Build folder dropdown
        self.folder_select = discord.ui.Select(
            placeholder="📁 Choose root folder...",
            min_values=1,
            max_values=1,
            row=0
        )
        self._setup_folder_select()
        self.add_item(self.folder_select)

        # Build mode dropdown
        self.mode_select = discord.ui.Select(
            placeholder="🎛️ Mode...",
            min_values=1,
            max_values=1,
            row=1,
            options=[
                _opt("Tracks (prune below threshold)", "tracks",
                     description="Download album, delete below-threshold tracks",
                     emoji="🎵"),
                _opt("Album (keep everything)", "album",
                     description="Download full album, keep all tracks",
                     emoji="💿"),
            ]
        )
        self.add_item(self.mode_select)

        # Build buttons
        self.threshold_button = discord.ui.Button(
            label="📊 Edit Threshold", style=discord.ButtonStyle.secondary, row=2
        )
        self.threshold_button.callback = self.threshold_btn
        self.add_item(self.threshold_button)

        self.confirm_button = discord.ui.Button(
            label="✅ Add Artist", style=discord.ButtonStyle.success, row=2
        )
        self.confirm_button.callback = self.confirm_btn
        self.add_item(self.confirm_button)

        self.cancel_button = discord.ui.Button(
            label="❌ Cancel", style=discord.ButtonStyle.danger, row=2
        )
        self.cancel_button.callback = self.cancel_btn
        self.add_item(self.cancel_button)

        # Set defaults
        self.selected_folder = self.artist_data.get("root_folder")
        self.selected_mode = self.artist_data.get("mode", "tracks")
        self.selected_threshold = self.artist_data.get("threshold", 20)

    def _setup_folder_select(self):
        """Populate the folder dropdown from Lidarr data."""
        # This would be set externally from the calling code
        pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Ensure only the command author can interact."""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ This isn't your setup.", ephemeral=True)
            return False
        return True

    async def threshold_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Open threshold modal."""
        modal = ThresholdModal(self.selected_threshold)
        await interaction.response.send_modal(modal)

    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Confirm and add the artist."""
        if not self.selected_artist:
            await interaction.response.send_message("❌ Please select an artist first.", ephemeral=True)
            return
        
        # Update artist data with selections
        self.artist_data.update({
            "root_folder": self.selected_folder,
            "mode": self.selected_mode,
            "threshold": self.selected_threshold
        })
        
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Cancel the operation."""
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(title="❌ Cancelled", color=0xFF0000), view=self
        )


class ThresholdModal(discord.ui.Modal, title="Set Popularity Threshold"):
    """Modal for setting the popularity threshold."""

    threshold_input = discord.ui.TextInput(
        label="Popularity Threshold (0-100)",
        placeholder="e.g. 20 for 20%+ popular tracks",
        style=discord.TextStyle.short,
        required=True,
        max_length=3,
    )

    def __init__(self, current_threshold: int = 20):
        super().__init__()
        self.threshold_input.default = str(current_threshold)
        self.add_item(self.threshold_input)

    async def on_submit(self, interaction: discord.Interaction):
        """Handle threshold submission."""
        try:
            threshold = int(self.threshold_input.value)
            if threshold < 0 or threshold > 100:
                await interaction.response.send_message(
                    "❌ Threshold must be between 0 and 100", ephemeral=True
                )
                return
            
            # This would be handled by the parent view
            await interaction.response.send_message(
                f"✅ Threshold set to {threshold}%", ephemeral=True
            )
        except ValueError:
            await interaction.response.send_message(
                "❌ Please enter a valid number", ephemeral=True
            )


# ── Search Modal ───────────────────────────────────────────────────────────

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
        """Handle search submission."""
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
        for a in matches[:25]:  # Discord max 25 options
            options.append(_opt(a["name"], a["name"]))
        self.select_component.options = options

        # Update placeholder to show filter
        if query:
            self.select_component.placeholder = f"🔍 {len(matches)} match(es) for '{self.search_input.value}'..."
        else:
            self.select_component.placeholder = "Pick an artist..."

        await interaction.response.edit_message(view=self.parent_view)


# ── Menu UI ────────────────────────────────────────────────────────────────

class MenuInputModal(discord.ui.Modal, title="Enter Search Term"):
    """Modal for entering search terms in the menu."""

    search_input = discord.ui.TextInput(
        label="Search artists (partial match)",
        placeholder="Type to filter the artist list...",
        style=discord.TextStyle.short,
        required=True,
        max_length=100,
    )

    def __init__(self, parent_view):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        """Handle search submission."""
        query = self.search_input.value.strip().lower()
        if not query:
            await self.parent_view._reset_to_full_list()
        else:
            await self.parent_view._filter_artists(query)
        await interaction.response.defer()


class MenuView(discord.ui.View):
    """View for the interactive menu command."""

    def __init__(self, author_id: int):
        super().__init__(timeout=300)  # 5 minute timeout
        self.author_id = author_id
        # This would be populated by the parent
        self.artist_select = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Ensure only the command author can interact."""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ This isn't your menu.", ephemeral=True)
            return False
        return True

    async def _reset_to_full_list(self):
        """Reset to showing all artists."""
        pass

    async def _filter_artists(self, query: str):
        """Filter artists by search query."""
        pass


# ── Keep (Never Prune) UI ────────────────────────────────────────────────────

class KeepArtistView(discord.ui.View):
    """Step 1: Pick an artist for never-prune."""

    def __init__(self, author_id: int, artists: list[dict]):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.artists = artists
        self.selected_artist: Optional[dict] = None
        
        # Build dropdown options
        options = []
        for a in artists[:25]:  # Discord max 25 options
            name = a.get("name") or "Unknown"
            label = str(name)[:25] if name else "Unknown"
            # Ensure label is never empty
            if not label.strip():
                label = "Unknown"
            options.append(_opt(label, str(a.get("id", ""))))
        self.artist_select.options = options
        
        # Auto-select if only 1 artist provided
        if len(artists) == 1:
            self.selected_artist = artists[0]
            self.artist_select.disabled = True
            self.artist_select.placeholder = f"Only artist: {artists[0].get('name', 'Unknown')}"
        else:
            self.artist_select.placeholder = "Pick an artist..."

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
                        if a.get("title") == opt.label:
                            self.selected_artist = a
                            break
                    break
        # Last resort: create a minimal dict
        if not self.selected_artist:
            for opt in select.options:
                if opt.value == selected_value:
                    self.selected_artist = {"id": int(selected_value), "title": opt.label}
                    break
        for opt in select.options:
            opt.default = opt.value == selected_value
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="🔍 Search", style=discord.ButtonStyle.secondary, row=1)
    async def search_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        album_dicts = [{"name": a.get("name", "Unknown"), "id": str(a["id"])} for a in self.artists]
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
            embed=discord.Embed(title="❌ Cancelled", color=0xFF0000), view=self
        )


class KeepAlbumView(discord.ui.View):
    """Step 2: Pick an album (from Lidarr)."""

    def __init__(self, author_id: int, albums: list[dict]):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.albums = albums
        self.selected_album: Optional[dict] = None
        
        # Build dropdown options
        options = []
        for a in albums[:25]:  # Discord max 25 options
            title = (a.get("title") or "Unknown Album").strip()
            if not title:
                title = "Unknown Album"
            label = str(title)[:25] if title else "Unknown Album"
            # Ensure label is never empty
            if not label.strip():
                label = "Unknown Album"
            options.append(_opt(label, str(a.get("id", ""))))
        self.album_select.options = options
        
        # Auto-select if only 1 album provided
        if len(albums) == 1:
            self.selected_album = albums[0]
            self.album_select.disabled = True
            self.album_select.placeholder = f"Only album: {albums[0].get('title', 'Unknown')}"
        else:
            self.album_select.placeholder = "Pick an album..."

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
                    self.selected_album = {"id": int(opt.value), "title": opt.label}
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
        modal = SearchModal(self, self.album_select, self.albums)
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
            embed=discord.Embed(title="❌ Cancelled", color=0xFF0000), view=self
        )


class KeepTrackView(discord.ui.View):
    """Step 3: Pick tracks to protect (multi-select)."""

    def __init__(self, author_id: int, tracks: list[dict], already_protected: set[str]):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.tracks = tracks
        self.already_protected = already_protected
        self.selected_track_ids: list[str] = []
        self.mark_all = False
        
        import logging
        log = logging.getLogger(__name__)
        # Log all track titles (Discord limit is 25 options max, but we log all for debugging)
        track_titles = [(t.get("title") or "Unknown").strip()[:30] for t in tracks]
        log.info(f"KeepTrackView: Received {len(tracks)} tracks from album")
        log.info(f"KeepTrackView: All track titles: {track_titles}")
        
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
            ), view=self
        )

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success, row=1)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        if not self.selected_track_ids:
            await interaction.response.send_message("❝ Select at least one track.", ephemeral=True)
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        count = len(self.selected_track_ids)
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="✅ Tracks protected from pruning",
                description=f"Protected {count} track(s) from pruning",
                color=0x1DB954,
            ), view=self
        )

    @discord.ui.button(label="❝ Cancel", style=discord.ButtonStyle.danger, row=1)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❝ Not yours.", ephemeral=True)
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(title="❝ Cancelled", color=0xFF0000), view=self
        )


# ── Scan UI ────────────────────────────────────────────────────────────────

class ScanConfirmView(discord.ui.View):
    """Confirmation view for full scan."""

    def __init__(self, author_id: int):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.confirmed = False

    @discord.ui.button(label="✅ Confirm Scan", style=discord.ButtonStyle.success)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        self.confirmed = True
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="✅ Scan confirmed",
                description="Starting full artist scan...",
                color=0x1DB954,
            ), view=self
        )

    @discord.ui.button(label="❝ Cancel", style=discord.ButtonStyle.danger)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❝ Not yours.", ephemeral=True)
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(title="❝ Cancelled", color=0xFF0000), view=self
        )


class ScanArtistView(discord.ui.View):
    """View for scanning a specific artist."""

    def __init__(self, author_id: int, artist_name: str):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.artist_name = artist_name

    @discord.ui.button(label="🔍 Scan Artist", style=discord.ButtonStyle.success)
    async def scan_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"🔍 Scanning {self.artist_name}'s full catalog for hits...", ephemeral=True
        )
        self.stop()
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="❝ Cancel", style=discord.ButtonStyle.danger)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❝ Not yours.", ephemeral=True)
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(title="❝ Cancelled", color=0xFF0000), view=self
        )


# ── Update Artist UI ─────────────────────────────────────────────────────────

class UpdatePickerView(discord.ui.View):
    """View for selecting an artist to update."""

    def __init__(self, author_id: int, artists: list[dict]):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.artists = artists
        self.selected_artist: Optional[dict] = None
        
        options = []
        for a in artists[:25]:
            label = str(a.get("name") or "Unknown")[:25] or "Unknown"
            options.append(_opt(label, str(a.get("id", ""))))
        self.artist_select.options = options

    @discord.ui.select(placeholder="Pick an artist to update...", min_values=1, max_values=1, row=0)
    async def artist_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        selected_value = select.values[0]
        for a in self.artists:
            if str(a["id"]) == selected_value:
                self.selected_artist = a
                break
        for opt in select.options:
            opt.default = opt.value == selected_value
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success, row=1)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        if not self.selected_artist:
            await interaction.response.send_message("❌ Pick an artist first.", ephemeral=True)
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="❝ Cancel", style=discord.ButtonStyle.danger, row=1)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❝ Not yours.", ephemeral=True)
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(title="❝ Cancelled", color=0xFF0000), view=self
        )


# ── Add Fuzzy Picker View ────────────────────────────────────────────────────

class AddFuzzyPickerView(discord.ui.View):
    """View for fuzzy picking an artist from search results."""

    def __init__(self, author_id: int, results: list[dict]):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.results = results
        self.selected_result: Optional[dict] = None

        options = []
        for r in results[:25]:
            desc = r.get("description", "")
            options.append(_opt(r["name"], r["id"], description=desc))
        self.artist_select.options = options

    @discord.ui.select(placeholder="Pick an artist from results...", min_values=1, max_values=1, row=0)
    async def artist_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        selected_value = select.values[0]
        for r in self.results:
            if str(r["id"]) == selected_value:
                self.selected_result = r
                break
        for opt in select.options:
            opt.default = opt.value == selected_value
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success, row=1)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not yours.", ephemeral=True)
            return
        if not self.selected_result:
            await interaction.response.send_message("❌ Pick an artist first.", ephemeral=True)
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="❝ Cancel", style=discord.ButtonStyle.danger, row=1)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❝ Not yours.", ephemeral=True)
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(title="❝ Cancelled", color=0xFF0000), view=self
        )