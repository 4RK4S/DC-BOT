import nextcord

from core.permissions import can_manage_guild

from .service import DEFAULT_IMAGE_URL, DEFAULT_MESSAGE_TEMPLATE


async def ensure_manager(interaction: nextcord.Interaction) -> bool:
    if not isinstance(interaction.user, nextcord.Member):
        await interaction.response.send_message("This can only be used in a server.", ephemeral=True)
        return False
    if not can_manage_guild(interaction.user):
        await interaction.response.send_message("You need Administrator or Manage Server permissions to use this.", ephemeral=True)
        return False
    return True


def apply_button_order(view: nextcord.ui.View, custom_ids: list[str]) -> None:
    order = {custom_id: index for index, custom_id in enumerate(custom_ids)}
    view.children.sort(key=lambda child: order.get(getattr(child, "custom_id", ""), len(order)))
    for index, child in enumerate(view.children):
        child.row = min(index // 5, 4)


async def resolve_sendable_channel(interaction: nextcord.Interaction, channel_id: int):
    if interaction.guild is None:
        raise ValueError("Channel not found.")
    channel = interaction.client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await interaction.client.fetch_channel(channel_id)
        except (nextcord.Forbidden, nextcord.HTTPException, nextcord.NotFound):
            raise ValueError("Channel not found. Make sure the bot can see this channel.") from None
    if getattr(channel, "guild", None) is None or channel.guild.id != interaction.guild.id:
        raise ValueError("This channel is not from this server.")
    if not hasattr(channel, "send"):
        raise ValueError("Channel must support sending messages.")
    bot_member = interaction.guild.me
    permissions = channel.permissions_for(bot_member) if bot_member is not None and hasattr(channel, "permissions_for") else None
    if permissions is not None and (not permissions.view_channel or not permissions.send_messages or not permissions.embed_links):
        raise ValueError("I need View Channel, Send Messages, and Embed Links in that channel.")
    return channel


class ChannelModal(nextcord.ui.Modal):
    def __init__(self, cog) -> None:
        super().__init__("Set Boost Channel")
        self.cog = cog
        self.channel_id = nextcord.ui.TextInput("Channel ID", required=True, max_length=32)
        self.add_item(self.channel_id)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        try:
            channel = await resolve_sendable_channel(interaction, int(str(self.channel_id.value).strip()))
            await self.cog.service.set_channel(interaction.guild_id, channel.id, interaction.user.id)
        except (ValueError, nextcord.HTTPException) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(f"Boost channel saved: {channel.mention}.", ephemeral=True)


class RoleModal(nextcord.ui.Modal):
    def __init__(self, cog) -> None:
        super().__init__("Set Booster Role")
        self.cog = cog
        self.role_id = nextcord.ui.TextInput("Role ID optional", required=False, max_length=32)
        self.add_item(self.role_id)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        raw = str(self.role_id.value or "").strip()
        try:
            role_id = int(raw) if raw else None
            if role_id is not None:
                role = interaction.guild.get_role(role_id) if interaction.guild else None
                if role is None:
                    raise ValueError("Role was not found in this server.")
                self.cog.service.validate_bot_can_manage_role(interaction.guild, role)
            await self.cog.service.set_role(interaction.guild_id, role_id, interaction.user.id)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message("Boost role saved." if role_id else "Boost role cleared.", ephemeral=True)


class ImageUrlModal(nextcord.ui.Modal):
    def __init__(self, cog) -> None:
        super().__init__("Set Boost Image URL")
        self.cog = cog
        self.image_url = nextcord.ui.TextInput("Image URL", required=False, default_value=DEFAULT_IMAGE_URL)
        self.add_item(self.image_url)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        try:
            await self.cog.service.set_image_url(interaction.guild_id, str(self.image_url.value or ""), interaction.user.id)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message("Boost image URL saved.", ephemeral=True)


class MessageModal(nextcord.ui.Modal):
    def __init__(self, cog) -> None:
        super().__init__("Set Boost Message")
        self.cog = cog
        self.template = nextcord.ui.TextInput(
            "Message Template",
            required=False,
            style=nextcord.TextInputStyle.paragraph,
            default_value=DEFAULT_MESSAGE_TEMPLATE,
        )
        self.add_item(self.template)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        await self.cog.service.set_message_template(interaction.guild_id, str(self.template.value or ""), interaction.user.id)
        await interaction.response.send_message("Boost message template saved.", ephemeral=True)


class ClearSettingsConfirmView(nextcord.ui.View):
    def __init__(self, cog) -> None:
        super().__init__(timeout=60)
        self.cog = cog

    @nextcord.ui.button(label="Confirm", style=nextcord.ButtonStyle.danger)
    async def confirm(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        await self.cog.service.clear_settings(interaction.guild_id, interaction.user.id)
        await interaction.response.edit_message(content="Server Boost settings cleared.", view=None)

    @nextcord.ui.button(label="Cancel", style=nextcord.ButtonStyle.secondary)
    async def cancel(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)


class BackToAdminPanelButton(nextcord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Back", style=nextcord.ButtonStyle.secondary, custom_id="server_boost:admin_back")

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        from cogs.admin_panel.views import AdminPanelView
        from core.embeds import admin_panel_embed

        await interaction.response.edit_message(content=None, embed=admin_panel_embed(), view=AdminPanelView(interaction.client))


class ServerBoostPanelView(nextcord.ui.View):
    def __init__(self, cog, show_admin_back: bool = False) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.show_admin_back = show_admin_back
        if show_admin_back:
            self.add_item(BackToAdminPanelButton())
        apply_button_order(
            self,
            [
                "server_boost:set_channel",
                "server_boost:set_role",
                "server_boost:set_image",
                "server_boost:set_message",
                "server_boost:toggle",
                "server_boost:toggle_delete",
                "server_boost:toggle_remove_role",
                "server_boost:test_boost",
                "server_boost:test_expire",
                "server_boost:sync_current",
                "server_boost:settings",
                "server_boost:clear",
                "server_boost:admin_back",
            ],
        )

    @nextcord.ui.button(label="Set Channel", style=nextcord.ButtonStyle.success, custom_id="server_boost:set_channel")
    async def set_channel(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_modal(ChannelModal(self.cog))

    @nextcord.ui.button(label="Set Role", style=nextcord.ButtonStyle.success, custom_id="server_boost:set_role")
    async def set_role(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_modal(RoleModal(self.cog))

    @nextcord.ui.button(label="Set Image URL", style=nextcord.ButtonStyle.success, custom_id="server_boost:set_image")
    async def set_image(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_modal(ImageUrlModal(self.cog))

    @nextcord.ui.button(label="Set Message", style=nextcord.ButtonStyle.success, custom_id="server_boost:set_message")
    async def set_message(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_modal(MessageModal(self.cog))

    @nextcord.ui.button(label="Toggle Module", style=nextcord.ButtonStyle.secondary, custom_id="server_boost:toggle")
    async def toggle_module(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        enabled = await self.cog.service.toggle_enabled(interaction.guild_id, interaction.user.id)
        await interaction.response.send_message("Server Boost enabled." if enabled else "Server Boost disabled.", ephemeral=True)

    @nextcord.ui.button(label="Toggle Delete On Expire", style=nextcord.ButtonStyle.secondary, custom_id="server_boost:toggle_delete")
    async def toggle_delete(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        enabled = await self.cog.service.toggle_delete_on_expire(interaction.guild_id, interaction.user.id)
        await interaction.response.send_message("Delete on expire enabled." if enabled else "Delete on expire disabled.", ephemeral=True)

    @nextcord.ui.button(label="Toggle Remove Role", style=nextcord.ButtonStyle.secondary, custom_id="server_boost:toggle_remove_role")
    async def toggle_remove_role(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        enabled = await self.cog.service.toggle_remove_role_on_expire(interaction.guild_id, interaction.user.id)
        await interaction.response.send_message("Remove role on expire enabled." if enabled else "Remove role on expire disabled.", ephemeral=True)

    @nextcord.ui.button(label="Test Boost", style=nextcord.ButtonStyle.success, custom_id="server_boost:test_boost")
    async def test_boost(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        ok, message = await self.cog.service.handle_boost_started(interaction.user, user_id=interaction.user.id)
        await self.cog.service.log(interaction.guild_id, "server_boost_test_boost", user_id=interaction.user.id, details={"guild_id": interaction.guild_id, "user_id": interaction.user.id})
        await interaction.response.send_message(message if ok else f"Could not test boost: {message}", ephemeral=True)

    @nextcord.ui.button(label="Test Expire", style=nextcord.ButtonStyle.success, custom_id="server_boost:test_expire")
    async def test_expire(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        ok, message = await self.cog.service.handle_boost_expired(interaction.user, user_id=interaction.user.id)
        await self.cog.service.log(interaction.guild_id, "server_boost_test_expire", user_id=interaction.user.id, details={"guild_id": interaction.guild_id, "user_id": interaction.user.id})
        await interaction.response.send_message(message if ok else f"Could not test expire: {message}", ephemeral=True)

    @nextcord.ui.button(label="Sync Current Boosters", style=nextcord.ButtonStyle.success, custom_id="server_boost:sync_current")
    async def sync_current(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        ok, message = await self.cog.service.sync_current_boosters(interaction.guild, user_id=interaction.user.id)
        await interaction.followup.send(message if ok else f"Could not sync current boosters: {message}", ephemeral=True)

    @nextcord.ui.button(label="Show Settings", style=nextcord.ButtonStyle.secondary, custom_id="server_boost:settings")
    async def show_settings(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_message(embed=await self.cog.service.build_settings_embed(interaction.guild), ephemeral=True)

    @nextcord.ui.button(label="Clear Settings", style=nextcord.ButtonStyle.danger, custom_id="server_boost:clear")
    async def clear_settings(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_message("Clear Server Boost settings?", view=ClearSettingsConfirmView(self.cog), ephemeral=True)
