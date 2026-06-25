import nextcord

from core.permissions import can_manage_guild
from .service import DEFAULT_TYPES


CHANNEL_NOT_FOUND_MESSAGE = "❌ Channel not found. Make sure the bot can see this channel."
CHANNEL_WRONG_GUILD_MESSAGE = (
    "❌ This channel is not from this server. Open /forwarder-panel on that server and add it there."
)
CHANNEL_NOT_CONFIGURED_MESSAGE = "❌ This channel is not configured on this server."


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


async def resolve_current_guild_channel(interaction: nextcord.Interaction, channel_id: int):
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

    bot_member = interaction.guild.me
    if bot_member is None and interaction.client.user is not None:
        bot_member = interaction.guild.get_member(interaction.client.user.id)
    permissions_for = getattr(channel, "permissions_for", None)
    if bot_member is not None and permissions_for is not None:
        if not permissions_for(bot_member).view_channel:
            raise ValueError(CHANNEL_NOT_FOUND_MESSAGE)
    return channel


def apply_button_order(view: nextcord.ui.View, custom_ids: list[str]) -> None:
    order = {custom_id: index for index, custom_id in enumerate(custom_ids)}
    view.children.sort(key=lambda child: order.get(getattr(child, "custom_id", ""), len(order)))
    for index, child in enumerate(view.children):
        child.row = min(index // 5, 4)


class BaseForwarderModal(nextcord.ui.Modal):
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
                view=ForwarderPanelView(self.cog, self.show_admin_back),
            )


class AddSourceModal(BaseForwarderModal):
    def __init__(self, cog, type_name: str, source_message=None, show_admin_back: bool = False) -> None:
        super().__init__("Add Source", cog, source_message, show_admin_back)
        self.type_name = type_name
        self.channel_id = nextcord.ui.TextInput("Channel ID", required=True)
        self.add_item(self.channel_id)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        try:
            channel_id = int(str(self.channel_id.value).strip())
            await resolve_current_guild_channel(interaction, channel_id)
            await self.cog.service.add_source(interaction.guild_id, channel_id, self.type_name, interaction.user.id)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self.refresh_panel()
        await interaction.response.send_message("Source saved.", ephemeral=True)


class RemoveSourceModal(BaseForwarderModal):
    def __init__(self, cog, source_message=None, show_admin_back: bool = False) -> None:
        super().__init__("Remove Source", cog, source_message, show_admin_back)
        self.channel_id = nextcord.ui.TextInput("Channel ID", required=True)
        self.add_item(self.channel_id)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        try:
            channel_id = int(str(self.channel_id.value).strip())
            removed = await self.cog.service.remove_source(interaction.guild_id, channel_id, interaction.user.id)
        except ValueError:
            await interaction.response.send_message("Channel ID must be a number.", ephemeral=True)
            return
        await self.refresh_panel()
        await interaction.response.send_message(
            "Source removed." if removed else CHANNEL_NOT_CONFIGURED_MESSAGE,
            ephemeral=True,
        )


class AddTargetModal(BaseForwarderModal):
    def __init__(self, cog, type_name: str, source_message=None, show_admin_back: bool = False) -> None:
        super().__init__("Add Target", cog, source_message, show_admin_back)
        self.type_name = type_name
        self.channel_id = nextcord.ui.TextInput("Channel ID", required=True)
        self.add_item(self.channel_id)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        try:
            channel_id = int(str(self.channel_id.value).strip())
            await resolve_current_guild_channel(interaction, channel_id)
            await self.cog.service.add_target(interaction.guild_id, channel_id, self.type_name, interaction.user.id)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self.refresh_panel()
        await interaction.response.send_message("Target saved.", ephemeral=True)


class RemoveTargetModal(BaseForwarderModal):
    def __init__(self, cog, type_name: str | None, source_message=None, show_admin_back: bool = False) -> None:
        super().__init__("Remove Target", cog, source_message, show_admin_back)
        self.type_name = type_name
        self.channel_id = nextcord.ui.TextInput("Channel ID", required=True)
        self.add_item(self.channel_id)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        try:
            channel_id = int(str(self.channel_id.value).strip())
            removed = await self.cog.service.remove_target(
                interaction.guild_id,
                channel_id,
                self.type_name,
                interaction.user.id,
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self.refresh_panel()
        await interaction.response.send_message(
            "Target removed." if removed else CHANNEL_NOT_CONFIGURED_MESSAGE,
            ephemeral=True,
        )


class ForwarderTypeSelect(nextcord.ui.Select):
    def __init__(self, action: str) -> None:
        options = [nextcord.SelectOption(label=type_name, value=type_name) for type_name in DEFAULT_TYPES]
        if action == "remove_target":
            options.insert(0, nextcord.SelectOption(label="All Types", value="__all__"))

        super().__init__(
            placeholder="Select type",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"forwarder:type_select:{action}",
        )
        self.action = action

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return

        view = self.view
        if not isinstance(view, ForwarderTypeSelectView):
            await interaction.response.send_message("Forwarder selection expired. Try again.", ephemeral=True)
            return

        selected_type = self.values[0]
        if self.action == "add_source":
            modal = AddSourceModal(view.cog, selected_type, view.source_message, view.show_admin_back)
        elif self.action == "add_target":
            modal = AddTargetModal(view.cog, selected_type, view.source_message, view.show_admin_back)
        else:
            modal = RemoveTargetModal(
                view.cog,
                None if selected_type == "__all__" else selected_type,
                view.source_message,
                view.show_admin_back,
            )
        await interaction.response.send_modal(modal)


class ForwarderTypeSelectView(nextcord.ui.View):
    def __init__(self, cog, action: str, source_message=None, show_admin_back: bool = False) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.action = action
        self.source_message = source_message
        self.show_admin_back = show_admin_back
        self.add_item(ForwarderTypeSelect(action))


class BackToAdminPanelButton(nextcord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Back", style=nextcord.ButtonStyle.secondary, custom_id="forwarder:admin_back", row=4)

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


class ClearForwarderConfirmView(nextcord.ui.View):
    def __init__(self, cog, clear_kind: str) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.clear_kind = clear_kind

    @nextcord.ui.button(label="Confirm", style=nextcord.ButtonStyle.danger)
    async def confirm(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        if interaction.guild_id is None:
            await interaction.response.edit_message(content="This can only be used in a server.", view=None)
            return

        if self.clear_kind == "sources":
            await self.cog.service.clear_sources(interaction.guild_id, interaction.user.id)
            message = "✅ Cleared all sources for this server."
        elif self.clear_kind == "targets":
            await self.cog.service.clear_targets(interaction.guild_id, interaction.user.id)
            message = "✅ Cleared all targets for this server."
        else:
            await self.cog.service.clear_everything(interaction.guild_id, interaction.user.id)
            message = "✅ Cleared all forwarder settings for this server."

        await interaction.response.edit_message(content=message, view=None)

    @nextcord.ui.button(label="Cancel", style=nextcord.ButtonStyle.secondary)
    async def cancel(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        await interaction.response.edit_message(content="Cancelled.", view=None)


class ForwarderToggleButton(nextcord.ui.Button):
    def __init__(self, target_enabled: bool) -> None:
        super().__init__(
            label="Enable" if target_enabled else "Disable",
            style=nextcord.ButtonStyle.success if target_enabled else nextcord.ButtonStyle.danger,
            custom_id=f"forwarder:{'enable' if target_enabled else 'disable'}",
        )
        self.target_enabled = target_enabled

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        forwarder_cog = interaction.client.get_cog("ForwarderCog")
        await forwarder_cog.service.set_enabled(interaction.guild_id, self.target_enabled, interaction.user.id)
        await interaction.response.edit_message(
            content=None,
            embed=forwarder_cog.service.build_management_embed(),
            view=ForwarderPanelView(forwarder_cog),
        )
        await interaction.followup.send(
            "Forwarder enabled." if self.target_enabled else "Forwarder disabled.",
            ephemeral=True,
        )


class ForwarderPanelView(nextcord.ui.View):
    def __init__(self, cog, show_admin_back: bool = False) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.show_admin_back = show_admin_back
        if show_admin_back:
            self.add_item(BackToAdminPanelButton())
        apply_button_order(
            self,
            [
                "forwarder:add_source",
                "forwarder:add_target",
                "forwarder:list_sources",
                "forwarder:list_targets",
                "forwarder:toggle",
                "forwarder:remove_source",
                "forwarder:remove_target",
                "forwarder:clear_sources",
                "forwarder:clear_targets",
                "forwarder:clear_everything",
                "forwarder:settings",
                "forwarder:admin_back",
            ],
        )

    @nextcord.ui.button(label="Add Source", style=nextcord.ButtonStyle.primary, custom_id="forwarder:add_source", row=0)
    async def add_source(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_message(
                "Select a source type.",
                view=ForwarderTypeSelectView(self.cog, "add_source", interaction.message, self.show_admin_back),
                ephemeral=True,
            )

    @nextcord.ui.button(label="Remove Source", style=nextcord.ButtonStyle.danger, custom_id="forwarder:remove_source", row=1)
    async def remove_source(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_modal(RemoveSourceModal(self.cog, interaction.message, self.show_admin_back))

    @nextcord.ui.button(label="Add Target", style=nextcord.ButtonStyle.primary, custom_id="forwarder:add_target", row=0)
    async def add_target(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_message(
                "Select a target type.",
                view=ForwarderTypeSelectView(self.cog, "add_target", interaction.message, self.show_admin_back),
                ephemeral=True,
            )

    @nextcord.ui.button(label="Remove Target", style=nextcord.ButtonStyle.danger, custom_id="forwarder:remove_target", row=1)
    async def remove_target(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_message(
                "Select which target type to remove.",
                view=ForwarderTypeSelectView(self.cog, "remove_target", interaction.message, self.show_admin_back),
                ephemeral=True,
            )

    @nextcord.ui.button(label="List Sources", style=nextcord.ButtonStyle.secondary, custom_id="forwarder:list_sources", row=0)
    async def list_sources(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        rows = await self.cog.service.list_sources(interaction.guild_id)
        await interaction.response.send_message(
            self.cog.service.format_mapping_list(rows, "No active sources configured for this server."),
            ephemeral=True,
        )

    @nextcord.ui.button(label="List Targets", style=nextcord.ButtonStyle.secondary, custom_id="forwarder:list_targets", row=0)
    async def list_targets(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        rows = await self.cog.service.list_targets(interaction.guild_id)
        await interaction.response.send_message(
            self.cog.service.format_mapping_list(rows, "No active targets configured for this server."),
            ephemeral=True,
        )

    @nextcord.ui.button(label="Clear Sources", style=nextcord.ButtonStyle.danger, custom_id="forwarder:clear_sources", row=2)
    async def clear_sources(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        await interaction.response.send_message(
            "Clear all Forwarder sources for this server?",
            view=ClearForwarderConfirmView(self.cog, "sources"),
            ephemeral=True,
        )

    @nextcord.ui.button(label="Clear Targets", style=nextcord.ButtonStyle.danger, custom_id="forwarder:clear_targets", row=2)
    async def clear_targets(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        await interaction.response.send_message(
            "Clear all Forwarder targets for this server?",
            view=ClearForwarderConfirmView(self.cog, "targets"),
            ephemeral=True,
        )

    @nextcord.ui.button(label="Clear Everything", style=nextcord.ButtonStyle.danger, custom_id="forwarder:clear_everything", row=2)
    async def clear_everything(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        await interaction.response.send_message(
            "Clear all Forwarder sources and targets for this server?",
            view=ClearForwarderConfirmView(self.cog, "everything"),
            ephemeral=True,
        )

    @nextcord.ui.button(label="Enable/Disable", style=nextcord.ButtonStyle.secondary, custom_id="forwarder:toggle", row=0)
    async def toggle_menu(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        enabled = await self.cog.service.is_enabled(interaction.guild_id)
        new_enabled = not enabled
        await self.cog.service.set_enabled(interaction.guild_id, new_enabled, interaction.user.id)
        await interaction.response.edit_message(
            content=None,
            embed=self.cog.service.build_management_embed(),
            view=ForwarderPanelView(self.cog, self.show_admin_back),
        )
        await interaction.followup.send(
            "Forwarder enabled." if new_enabled else "Forwarder disabled.",
            ephemeral=True,
        )

    @nextcord.ui.button(label="Show Settings", style=nextcord.ButtonStyle.secondary, custom_id="forwarder:settings", row=3)
    async def show_settings(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_message(embed=await self.cog.service.build_settings_embed(interaction.guild_id), ephemeral=True)
