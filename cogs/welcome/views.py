import nextcord

from core.permissions import can_manage_guild

from .service import DEFAULT_BACKGROUND_URL


CHANNEL_NOT_FOUND_MESSAGE = "❌ Channel not found. Make sure the bot can see this channel."
CHANNEL_WRONG_GUILD_MESSAGE = "❌ This channel is not from this server. Open /welcome-panel on that server and add it there."


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


async def resolve_sendable_channel(interaction: nextcord.Interaction, channel_id: int):
    if interaction.guild is None:
        raise ValueError(CHANNEL_NOT_FOUND_MESSAGE)

    channel = interaction.client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await interaction.client.fetch_channel(channel_id)
        except (nextcord.Forbidden, nextcord.HTTPException, nextcord.NotFound):
            raise ValueError(CHANNEL_NOT_FOUND_MESSAGE) from None

    channel_guild = getattr(channel, "guild", None)
    if channel_guild is None:
        raise ValueError(CHANNEL_NOT_FOUND_MESSAGE)
    if channel_guild.id != interaction.guild.id:
        raise ValueError(CHANNEL_WRONG_GUILD_MESSAGE)
    if not hasattr(channel, "send"):
        raise ValueError("Channel must support sending messages.")

    bot_member = interaction.guild.me
    if bot_member is None and interaction.client.user is not None:
        bot_member = interaction.guild.get_member(interaction.client.user.id)
    permissions_for = getattr(channel, "permissions_for", None)
    if bot_member is not None and permissions_for is not None:
        permissions = permissions_for(bot_member)
        if not permissions.view_channel:
            raise ValueError(CHANNEL_NOT_FOUND_MESSAGE)
        if not permissions.send_messages:
            raise ValueError("I need Send Messages permission in that channel.")
    return channel


def apply_button_order(view: nextcord.ui.View, custom_ids: list[str]) -> None:
    order = {custom_id: index for index, custom_id in enumerate(custom_ids)}
    view.children.sort(key=lambda child: order.get(getattr(child, "custom_id", ""), len(order)))
    for index, child in enumerate(view.children):
        child.row = min(index // 5, 4)


class BaseWelcomeModal(nextcord.ui.Modal):
    def __init__(self, title: str, cog, source_message=None, show_admin_back: bool = False) -> None:
        super().__init__(title)
        self.cog = cog
        self.source_message = source_message
        self.show_admin_back = show_admin_back

    async def refresh_panel(self) -> None:
        if self.source_message is not None:
            await self.source_message.edit(
                content=None,
                embed=self.cog.service.build_management_embed(),
                view=WelcomePanelView(self.cog, self.show_admin_back),
            )


class SetChannelModal(BaseWelcomeModal):
    def __init__(self, cog, source_message=None, show_admin_back: bool = False) -> None:
        super().__init__("Set Welcome Channel", cog, source_message, show_admin_back)
        self.channel_id = nextcord.ui.TextInput(label="Channel ID", required=True)
        self.add_item(self.channel_id)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        try:
            channel_id = int(str(self.channel_id.value).strip())
            channel = await resolve_sendable_channel(interaction, channel_id)
            await self.cog.service.set_channel(interaction.guild_id, channel.id, interaction.user.id)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self.refresh_panel()
        await interaction.response.send_message(f"Welcome channel set to {channel.mention}.", ephemeral=True)


class SetBackgroundModal(BaseWelcomeModal):
    def __init__(self, cog, source_message=None, show_admin_back: bool = False) -> None:
        super().__init__("Set Welcome Background", cog, source_message, show_admin_back)
        self.background_url = nextcord.ui.TextInput(label="Background Image URL", required=True)
        self.add_item(self.background_url)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        try:
            await self.cog.service.set_background(interaction.guild_id, str(self.background_url.value), interaction.user.id)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self.refresh_panel()
        await interaction.response.send_message("Welcome background updated.", ephemeral=True)


class SetMessageModal(BaseWelcomeModal):
    def __init__(self, cog, source_message=None, show_admin_back: bool = False) -> None:
        super().__init__("Set Welcome Message", cog, source_message, show_admin_back)
        self.message_text = nextcord.ui.TextInput(
            label="Message Text",
            required=True,
            style=nextcord.TextInputStyle.paragraph,
            placeholder="<@{user_id}>|Welcome to **{guild_name}**!",
        )
        self.add_item(self.message_text)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        await self.cog.service.set_message(interaction.guild_id, str(self.message_text.value), interaction.user.id)
        await self.refresh_panel()
        await interaction.response.send_message("Welcome message updated.", ephemeral=True)


class ClearSettingsConfirmView(nextcord.ui.View):
    def __init__(self, cog) -> None:
        super().__init__(timeout=60)
        self.cog = cog

    @nextcord.ui.button(label="Confirm", style=nextcord.ButtonStyle.danger)
    async def confirm(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        await self.cog.service.clear_settings(interaction.guild_id, interaction.user.id)
        await interaction.response.edit_message(content="Welcome settings cleared and disabled.", view=None)

    @nextcord.ui.button(label="Cancel", style=nextcord.ButtonStyle.secondary)
    async def cancel(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        await interaction.response.edit_message(content="Cancelled.", view=None)


class BackToAdminPanelButton(nextcord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Back", style=nextcord.ButtonStyle.secondary, custom_id="welcome:admin_back", row=4)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        from cogs.admin_panel.views import AdminPanelView
        from core.embeds import admin_panel_embed

        await interaction.response.edit_message(content=None, embed=admin_panel_embed(), view=AdminPanelView(interaction.client))


class WelcomePanelView(nextcord.ui.View):
    def __init__(self, cog, show_admin_back: bool = False) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.show_admin_back = show_admin_back
        if show_admin_back:
            self.add_item(BackToAdminPanelButton())
        apply_button_order(
            self,
            [
                "welcome:set_channel",
                "welcome:set_background",
                "welcome:set_message",
                "welcome:toggle",
                "welcome:toggle_image",
                "welcome:test",
                "welcome:default_background",
                "welcome:reset_background",
                "welcome:clear_settings",
                "welcome:show_settings",
                "welcome:admin_back",
            ],
        )

    @nextcord.ui.button(label="Set Channel", style=nextcord.ButtonStyle.success, custom_id="welcome:set_channel", row=0)
    async def set_channel(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_modal(SetChannelModal(self.cog, interaction.message, self.show_admin_back))

    @nextcord.ui.button(label="Set Background", style=nextcord.ButtonStyle.success, custom_id="welcome:set_background", row=0)
    async def set_background(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_modal(SetBackgroundModal(self.cog, interaction.message, self.show_admin_back))

    @nextcord.ui.button(label="Set Message", style=nextcord.ButtonStyle.success, custom_id="welcome:set_message", row=0)
    async def set_message(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_modal(SetMessageModal(self.cog, interaction.message, self.show_admin_back))

    @nextcord.ui.button(label="Toggle Welcome", style=nextcord.ButtonStyle.secondary, custom_id="welcome:toggle", row=0)
    async def toggle_welcome(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        enabled = await self.cog.service.toggle_enabled(interaction.guild_id, interaction.user.id)
        await interaction.response.edit_message(
            content=None,
            embed=self.cog.service.build_management_embed(),
            view=WelcomePanelView(self.cog, self.show_admin_back),
        )
        await interaction.followup.send("Enabled." if enabled else "Disabled.", ephemeral=True)

    @nextcord.ui.button(label="Toggle Image", style=nextcord.ButtonStyle.secondary, custom_id="welcome:toggle_image", row=0)
    async def toggle_image(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        image_enabled = await self.cog.service.toggle_image(interaction.guild_id, interaction.user.id)
        await interaction.response.edit_message(
            content=None,
            embed=self.cog.service.build_management_embed(),
            view=WelcomePanelView(self.cog, self.show_admin_back),
        )
        await interaction.followup.send("Image enabled." if image_enabled else "Image disabled.", ephemeral=True)

    @nextcord.ui.button(label="Test Welcome", style=nextcord.ButtonStyle.success, custom_id="welcome:test", row=1)
    async def test_welcome(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        ok, message = await self.cog.send_welcome(interaction.user, test=True)
        await interaction.followup.send(message if ok else f"❌ {message}", ephemeral=True)

    @nextcord.ui.button(label="Show Settings", style=nextcord.ButtonStyle.secondary, custom_id="welcome:show_settings", row=2)
    async def show_settings(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        settings = await self.cog.service.get_settings(interaction.guild_id)
        await interaction.response.send_message(
            embed=self.cog.service.build_settings_embed(interaction.guild, settings),
            ephemeral=True,
        )

    @nextcord.ui.button(label="Default Background", style=nextcord.ButtonStyle.secondary, custom_id="welcome:default_background", row=1)
    async def default_background(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        await self.cog.service.log(
            interaction.guild_id,
            "welcome_default_background",
            user_id=interaction.user.id,
            details={"guild_id": interaction.guild_id, "background_url": DEFAULT_BACKGROUND_URL},
        )
        await interaction.response.send_message(
            "Download this image, edit it, upload your edited version somewhere, then use Set Background with your new URL.\n"
            f"{DEFAULT_BACKGROUND_URL}",
            ephemeral=True,
        )

    @nextcord.ui.button(label="Reset Background", style=nextcord.ButtonStyle.danger, custom_id="welcome:reset_background", row=1)
    async def reset_background(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        await self.cog.service.reset_background(interaction.guild_id, interaction.user.id)
        await interaction.response.send_message("Welcome background reset to the default image.", ephemeral=True)

    @nextcord.ui.button(label="Clear Settings", style=nextcord.ButtonStyle.danger, custom_id="welcome:clear_settings", row=1)
    async def clear_settings(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_message(
                "Clear and disable Welcome settings for this server?",
                view=ClearSettingsConfirmView(self.cog),
                ephemeral=True,
            )
