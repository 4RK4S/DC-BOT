import nextcord

from core.permissions import can_manage_guild

from .service import TIMEZONE_CHOICES


async def ensure_manager(interaction: nextcord.Interaction) -> bool:
    if not isinstance(interaction.user, nextcord.Member):
        await interaction.response.send_message("This can only be used in a server.", ephemeral=True)
        return False
    if not can_manage_guild(interaction.user):
        await interaction.response.send_message(
            "You need Administrator or Manage Server permissions to use this.",
            ephemeral=True,
        )
        return False
    return True


async def resolve_text_channel(interaction: nextcord.Interaction, channel_id: int):
    channel = interaction.client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await interaction.client.fetch_channel(channel_id)
        except (nextcord.Forbidden, nextcord.HTTPException, nextcord.NotFound):
            raise ValueError("Channel not found. Make sure the bot can see this channel.") from None
    if not isinstance(channel, nextcord.TextChannel):
        raise ValueError("Channel must be a text channel.")
    if channel.guild.id != interaction.guild_id:
        raise ValueError("This channel is not from this server.")
    bot_member = interaction.guild.me if interaction.guild else None
    permissions = channel.permissions_for(bot_member) if bot_member else None
    if permissions is not None and (not permissions.view_channel or not permissions.send_messages):
        raise ValueError("I need permission to view and send messages in that channel.")
    return channel


def apply_button_order(view: nextcord.ui.View, custom_ids: list[str]) -> None:
    order = {custom_id: index for index, custom_id in enumerate(custom_ids)}
    view.children.sort(key=lambda child: order.get(getattr(child, "custom_id", ""), len(order)))
    for index, child in enumerate(view.children):
        child.row = min(index // 5, 4)


class AddLiveCodeModal(nextcord.ui.Modal):
    def __init__(
        self,
        cog,
        timezone_name: str = "UTC+0",
        source_message: nextcord.Message | None = None,
        show_admin_back: bool = False,
    ) -> None:
        super().__init__("Add Live Codes")
        self.cog = cog
        self.timezone_name = timezone_name or "UTC+0"
        self.source_message = source_message
        self.show_admin_back = show_admin_back
        self.codes = nextcord.ui.TextInput(
            "Codes",
            placeholder="CODE1, CODE2 or one per line",
            required=True,
            style=nextcord.TextInputStyle.paragraph,
            max_length=1500,
        )
        self.expires_at = nextcord.ui.TextInput(
            "Expires At",
            placeholder="DD.MM.YYYY HH:MM or YYYY-MM-DD HH:MM",
            required=False,
            max_length=32,
        )
        self.add_item(self.codes)
        self.add_item(self.expires_at)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        try:
            result = await self.cog.add_live_codes(
                interaction.guild_id,
                str(self.codes.value),
                str(self.expires_at.value or ""),
                self.timezone_name,
                interaction.user.id,
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await refresh_modal_panel(self.source_message, self.cog, self.show_admin_back)
        duplicate_word = "duplicate" if result.skipped_duplicates == 1 else "duplicates"
        await interaction.response.send_message(
            f"✅ Added {result.added} live code(s). "
            f"Skipped {result.skipped_duplicates} {duplicate_word}. "
            f"Expiration timezone: {self.timezone_name}.",
            ephemeral=True,
        )


class RemoveLiveCodeModal(nextcord.ui.Modal):
    def __init__(self, cog, source_message: nextcord.Message | None = None, show_admin_back: bool = False) -> None:
        super().__init__("Remove Live Code")
        self.cog = cog
        self.source_message = source_message
        self.show_admin_back = show_admin_back
        self.code_or_id = nextcord.ui.TextInput("Code or ID", required=True, max_length=128)
        self.add_item(self.code_or_id)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        removed = await self.cog.remove_live_code(interaction.guild_id, str(self.code_or_id.value), interaction.user.id)
        await refresh_modal_panel(self.source_message, self.cog, self.show_admin_back)
        await interaction.response.send_message("Live code removed." if removed else "Live code not found.", ephemeral=True)


class SetPublicChannelModal(nextcord.ui.Modal):
    def __init__(self, cog, source_message: nextcord.Message | None = None, show_admin_back: bool = False) -> None:
        super().__init__("Set Public Channel")
        self.cog = cog
        self.source_message = source_message
        self.show_admin_back = show_admin_back
        self.channel_id = nextcord.ui.TextInput("Channel ID", required=True, max_length=32)
        self.add_item(self.channel_id)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        try:
            channel_id = int(str(self.channel_id.value).strip())
            channel = await resolve_text_channel(interaction, channel_id)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self.cog.service.set_public_channel(interaction.guild_id, channel.id, interaction.user.id)
        await self.cog.refresh_public_list(interaction.guild_id)
        await refresh_modal_panel(self.source_message, self.cog, self.show_admin_back)
        await interaction.response.send_message("Public channel saved.", ephemeral=True)


class SetAnnouncementChannelModal(nextcord.ui.Modal):
    def __init__(self, cog, source_message: nextcord.Message | None = None, show_admin_back: bool = False) -> None:
        super().__init__("Set Announcement Channel")
        self.cog = cog
        self.source_message = source_message
        self.show_admin_back = show_admin_back
        self.channel_id_input = nextcord.ui.TextInput("Announcement Channel ID", required=True, max_length=32)
        self.add_item(self.channel_id_input)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        try:
            channel = await resolve_text_channel(interaction, int(str(self.channel_id_input.value).strip()))
            await self.cog.service.set_announcement_channel(interaction.guild_id, channel.id, interaction.user.id)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await refresh_modal_panel(self.source_message, self.cog, self.show_admin_back)
        await interaction.response.send_message(f"Announcement channel saved: {channel.mention}.", ephemeral=True)


class SetPingRoleModal(nextcord.ui.Modal):
    def __init__(self, cog, source_message: nextcord.Message | None = None, show_admin_back: bool = False) -> None:
        super().__init__("Set Ping Role")
        self.cog = cog
        self.source_message = source_message
        self.show_admin_back = show_admin_back
        self.role_id = nextcord.ui.TextInput("Role ID", required=False, max_length=32)
        self.add_item(self.role_id)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        raw = str(self.role_id.value or "").strip()
        try:
            role_id = int(raw) if raw else None
            await self.cog.service.set_ping_role(interaction.guild_id, role_id, interaction.user.id)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await refresh_modal_panel(self.source_message, self.cog, self.show_admin_back)
        await interaction.response.send_message("Ping role saved." if role_id else "Ping role cleared.", ephemeral=True)


class BackToManagementButton(nextcord.ui.Button):
    def __init__(self, cog, show_admin_back: bool = False) -> None:
        super().__init__(label="Back", style=nextcord.ButtonStyle.secondary, custom_id="live_codes:back", row=4)
        self.cog = cog
        self.show_admin_back = show_admin_back

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        await interaction.response.edit_message(
            content=None,
            embed=self.cog.service.build_management_embed(),
            view=LiveCodeManagementView(self.cog, show_admin_back=self.show_admin_back),
        )


class BackToAdminPanelButton(nextcord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Back", style=nextcord.ButtonStyle.secondary, custom_id="live_codes:admin_back", row=4)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        from cogs.admin_panel.views import AdminPanelView
        from core.embeds import admin_panel_embed

        await interaction.response.edit_message(
            content=None,
            embed=admin_panel_embed(),
            view=AdminPanelView(interaction.client),
        )


class LiveCodeListView(nextcord.ui.View):
    def __init__(self, cog, show_admin_back: bool = False) -> None:
        super().__init__(timeout=120)
        self.add_item(BackToManagementButton(cog, show_admin_back=show_admin_back))


class TimezoneSelect(nextcord.ui.Select):
    def __init__(self, cog, show_admin_back: bool = False) -> None:
        self.cog = cog
        self.show_admin_back = show_admin_back
        super().__init__(
            placeholder="Select timezone before entering codes",
            options=[
                nextcord.SelectOption(label=timezone_name, value=timezone_name)
                for timezone_name in TIMEZONE_CHOICES
            ],
        )

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        await interaction.response.send_modal(
            AddLiveCodeModal(
                self.cog,
                self.values[0] if self.values else "UTC+0",
                interaction.message,
                self.show_admin_back,
            )
        )


class TimezoneSelectView(nextcord.ui.View):
    def __init__(self, cog, show_admin_back: bool = False) -> None:
        super().__init__(timeout=120)
        self.add_item(TimezoneSelect(cog, show_admin_back=show_admin_back))
        self.add_item(BackToManagementButton(cog, show_admin_back=show_admin_back))


class LiveCodeManagementView(nextcord.ui.View):
    def __init__(self, cog, show_admin_back: bool = False) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.show_admin_back = show_admin_back
        if show_admin_back:
            self.add_item(BackToAdminPanelButton())
        apply_button_order(
            self,
            [
                "live_codes:add",
                "live_codes:set_channel",
                "live_codes:set_announcement",
                "live_codes:set_role",
                "live_codes:refresh",
                "live_codes:remove",
                "live_codes:settings",
                "live_codes:admin_back",
            ],
        )

    @nextcord.ui.button(label="Add Codes", style=nextcord.ButtonStyle.primary, custom_id="live_codes:add", row=0)
    async def add_live_code(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        await interaction.response.edit_message(
            content="Select timezone before entering codes.",
            embed=None,
            view=TimezoneSelectView(self.cog, show_admin_back=self.show_admin_back),
        )

    @nextcord.ui.button(label="Remove Live Code", style=nextcord.ButtonStyle.danger, custom_id="live_codes:remove", row=1)
    async def remove_live_code(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_modal(
                RemoveLiveCodeModal(self.cog, interaction.message, self.show_admin_back)
            )

    @nextcord.ui.button(label="Refresh Public List", style=nextcord.ButtonStyle.success, custom_id="live_codes:refresh", row=1)
    async def refresh_public_list(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        await self.cog.refresh_public_list(interaction.guild_id)
        await self.cog.service.db.log_action(
            interaction.guild_id,
            "live_codes",
            "public_list_refreshed",
            user_id=interaction.user.id,
        )
        await interaction.response.edit_message(
            content=None,
            embed=self.cog.service.build_management_embed(),
            view=LiveCodeManagementView(self.cog, show_admin_back=self.show_admin_back),
        )
        await interaction.followup.send("Public list refreshed.", ephemeral=True)

    @nextcord.ui.button(label="Set Public Channel", style=nextcord.ButtonStyle.success, custom_id="live_codes:set_channel", row=0)
    async def set_public_channel(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_modal(
                SetPublicChannelModal(self.cog, interaction.message, self.show_admin_back)
            )

    @nextcord.ui.button(label="Set Announcement Channel", style=nextcord.ButtonStyle.success, custom_id="live_codes:set_announcement", row=0)
    async def set_announcement_channel(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_modal(
                SetAnnouncementChannelModal(self.cog, interaction.message, self.show_admin_back)
            )

    @nextcord.ui.button(label="Set Ping Role", style=nextcord.ButtonStyle.success, custom_id="live_codes:set_role", row=0)
    async def set_ping_role(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_modal(
                SetPingRoleModal(self.cog, interaction.message, self.show_admin_back)
            )

    @nextcord.ui.button(label="Show Settings", style=nextcord.ButtonStyle.secondary, custom_id="live_codes:settings", row=1)
    async def show_settings(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await self.cog.service.db.log_action(
                interaction.guild_id,
                "live_codes",
                "live_codes_show_settings",
                user_id=interaction.user.id,
            )
            await interaction.response.send_message(embed=await self.cog.service.build_settings_embed(interaction.guild_id), ephemeral=True)


async def refresh_modal_panel(message: nextcord.Message | None, cog, show_admin_back: bool = False) -> None:
    if message is not None:
        await message.edit(
            content=None,
            embed=cog.service.build_management_embed(),
            view=LiveCodeManagementView(cog, show_admin_back=show_admin_back),
        )
