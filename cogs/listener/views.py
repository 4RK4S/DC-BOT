import nextcord

from core.permissions import can_manage_guild


CHANNEL_NOT_FOUND_MESSAGE = "❌ Channel not found. Make sure the bot can see this channel."
CHANNEL_WRONG_GUILD_MESSAGE = (
    "❌ This channel is not from this server. Open /listener-panel on that server and add it there."
)


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


def option_label(text: str) -> str:
    return text if len(text) <= 100 else text[:97] + "..."


def channel_name(interaction: nextcord.Interaction, channel_id: int) -> str:
    channel = interaction.guild.get_channel(channel_id) if interaction.guild else None
    return f"#{channel.name}" if channel is not None else f"#{channel_id}"


def format_source_list(rows) -> str:
    if not rows:
        return "No active listener sources configured for this server."
    return "\n".join(
        f"`{row['code']}` / {row['label'] or row['code']} → <#{row['channel_id']}>"
        for row in rows
    )


def format_target_list(rows) -> str:
    if not rows:
        return "No active listener targets configured for this server."

    lines: list[str] = []
    for row in rows:
        lines.append(f"`{row['code']}` / {row['label'] or row['code']} → <#{row['channel_id']}>")
        lines.append(f"Location: {row['message_location']}")
        if row["message"]:
            lines.append(f"Message: {row['message']}")
        if row["message_link"]:
            lines.append(f"Message Link: {row['message_link']}")
        lines.append("")
    return "\n".join(lines).strip()


def format_type_list(rows) -> str:
    if not rows:
        return "No listener types configured."
    return "\n".join(f"{row['label']} → `{row['code']}`" for row in rows)


def apply_button_order(view: nextcord.ui.View, custom_ids: list[str]) -> None:
    order = {custom_id: index for index, custom_id in enumerate(custom_ids)}
    view.children.sort(key=lambda child: order.get(getattr(child, "custom_id", ""), len(order)))
    for index, child in enumerate(view.children):
        child.row = min(index // 5, 4)


class BaseListenerModal(nextcord.ui.Modal):
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
                view=ListenerPanelView(self.cog, self.show_admin_back),
            )


class AddSourceModal(BaseListenerModal):
    def __init__(self, cog, code: str, source_message=None, show_admin_back: bool = False) -> None:
        super().__init__("Add Listener Source", cog, source_message, show_admin_back)
        self.code = code
        self.channel_id = nextcord.ui.TextInput(label="Channel ID", required=True)
        self.add_item(self.channel_id)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        try:
            channel_id = int(str(self.channel_id.value).strip())
            await resolve_current_guild_channel(interaction, channel_id)
            await self.cog.service.add_source(interaction.guild_id, channel_id, self.code, interaction.user.id)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self.refresh_panel()
        await interaction.response.send_message("Listener source saved.", ephemeral=True)


class AddTargetModal(BaseListenerModal):
    def __init__(self, cog, code: str, source_message=None, show_admin_back: bool = False) -> None:
        super().__init__("Add Listener Target", cog, source_message, show_admin_back)
        self.code = code
        self.channel_id = nextcord.ui.TextInput(label="Channel ID", required=True)
        self.message_location = nextcord.ui.TextInput(label="Message Location", placeholder="before or after", required=True, default_value="before")
        self.message = nextcord.ui.TextInput(label="Message", required=False, style=nextcord.TextInputStyle.paragraph)
        self.message_link = nextcord.ui.TextInput(label="Message Link", required=False)
        self.add_item(self.channel_id)
        self.add_item(self.message_location)
        self.add_item(self.message)
        self.add_item(self.message_link)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        try:
            channel_id = int(str(self.channel_id.value).strip())
            await resolve_current_guild_channel(interaction, channel_id)
            await self.cog.service.add_target(
                interaction.guild_id,
                channel_id,
                self.code,
                str(self.message_location.value),
                str(self.message.value or ""),
                str(self.message_link.value or ""),
                interaction.user.id,
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self.refresh_panel()
        await interaction.response.send_message("Listener target saved.", ephemeral=True)


class AddTypeModal(BaseListenerModal):
    def __init__(self, cog, source_message=None, show_admin_back: bool = False) -> None:
        super().__init__("Add Listener Type", cog, source_message, show_admin_back)
        self.label_input = nextcord.ui.TextInput(label="Label", required=True)
        self.code_input = nextcord.ui.TextInput(label="Code", required=True)
        self.add_item(self.label_input)
        self.add_item(self.code_input)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        try:
            await self.cog.service.upsert_type(
                interaction.guild_id,
                str(self.label_input.value),
                str(self.code_input.value),
                interaction.user.id,
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self.refresh_panel()
        await interaction.response.send_message("Listener type saved.", ephemeral=True)


class ListenerTypeSelect(nextcord.ui.Select):
    def __init__(self, rows, action: str) -> None:
        options = [
            nextcord.SelectOption(label=option_label(row["label"]), value=row["code"], description=option_label(row["code"]))
            for row in rows[:25]
        ]
        super().__init__(
            placeholder="Select listener type",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"listener:type_select:{action}",
        )
        self.action = action

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        view = self.view
        if not isinstance(view, ListenerTypeSelectView):
            await interaction.response.send_message("Listener selection expired. Try again.", ephemeral=True)
            return
        code = self.values[0]
        if self.action == "add_source":
            modal = AddSourceModal(view.cog, code, view.source_message, view.show_admin_back)
        elif self.action == "add_target":
            modal = AddTargetModal(view.cog, code, view.source_message, view.show_admin_back)
        else:
            try:
                await view.cog.service.remove_type(interaction.guild_id, code, interaction.user.id)
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            await interaction.response.send_message("Listener type removed.", ephemeral=True)
            return
        await interaction.response.send_modal(modal)


class ListenerTypeSelectView(nextcord.ui.View):
    def __init__(self, cog, rows, action: str, source_message=None, show_admin_back: bool = False) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.action = action
        self.source_message = source_message
        self.show_admin_back = show_admin_back
        self.add_item(ListenerTypeSelect(rows, action))


class ListenerSourceSelect(nextcord.ui.Select):
    def __init__(self, interaction: nextcord.Interaction, rows) -> None:
        options = [
            nextcord.SelectOption(
                label=option_label(f"{row['code']} → {channel_name(interaction, row['channel_id'])}"),
                value=str(row["id"]),
            )
            for row in rows[:25]
        ]
        super().__init__(placeholder="Select listener source", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        view = self.view
        if not isinstance(view, ListenerSourceSelectView):
            await interaction.response.send_message("Listener selection expired. Try again.", ephemeral=True)
            return
        removed = await view.cog.service.remove_source_by_id(interaction.guild_id, int(self.values[0]), interaction.user.id)
        await interaction.response.send_message(
            "Listener source removed." if removed else "No active listener sources configured for this server.",
            ephemeral=True,
        )


class ListenerSourceSelectView(nextcord.ui.View):
    def __init__(self, cog, interaction: nextcord.Interaction, rows) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.add_item(ListenerSourceSelect(interaction, rows))


class ListenerTargetSelect(nextcord.ui.Select):
    def __init__(self, interaction: nextcord.Interaction, rows) -> None:
        options = [
            nextcord.SelectOption(
                label=option_label(f"{row['code']} → {channel_name(interaction, row['channel_id'])}"),
                value=str(row["id"]),
            )
            for row in rows[:25]
        ]
        super().__init__(placeholder="Select listener target", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        view = self.view
        if not isinstance(view, ListenerTargetSelectView):
            await interaction.response.send_message("Listener selection expired. Try again.", ephemeral=True)
            return
        removed = await view.cog.service.remove_target_by_id(interaction.guild_id, int(self.values[0]), interaction.user.id)
        await interaction.response.send_message(
            "Listener target removed." if removed else "No active listener targets configured for this server.",
            ephemeral=True,
        )


class ListenerTargetSelectView(nextcord.ui.View):
    def __init__(self, cog, interaction: nextcord.Interaction, rows) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.add_item(ListenerTargetSelect(interaction, rows))


class ListenerClearConfirmView(nextcord.ui.View):
    def __init__(self, cog, clear_kind: str) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.clear_kind = clear_kind

    @nextcord.ui.button(label="Confirm", style=nextcord.ButtonStyle.danger)
    async def confirm(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        if self.clear_kind == "sources":
            await self.cog.service.clear_sources(interaction.guild_id, interaction.user.id)
            message = "Cleared all listener sources for this server."
        elif self.clear_kind == "targets":
            await self.cog.service.clear_targets(interaction.guild_id, interaction.user.id)
            message = "Cleared all listener targets for this server."
        else:
            await self.cog.service.clear_everything(interaction.guild_id, interaction.user.id)
            message = "Cleared all listener sources and targets for this server."
        await interaction.response.edit_message(content=message, view=None)

    @nextcord.ui.button(label="Cancel", style=nextcord.ButtonStyle.secondary)
    async def cancel(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        await interaction.response.edit_message(content="Cancelled.", view=None)


class BackToAdminPanelButton(nextcord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Back", style=nextcord.ButtonStyle.secondary, custom_id="listener:admin_back", row=4)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        from cogs.admin_panel.views import AdminPanelView
        from core.embeds import admin_panel_embed

        await interaction.response.edit_message(content=None, embed=admin_panel_embed(), view=AdminPanelView(interaction.client))


class ListenerPanelView(nextcord.ui.View):
    def __init__(self, cog, show_admin_back: bool = False) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.show_admin_back = show_admin_back
        if show_admin_back:
            self.add_item(BackToAdminPanelButton())
        apply_button_order(
            self,
            [
                "listener:add_source",
                "listener:add_target",
                "listener:add_type",
                "listener:list_sources",
                "listener:list_targets",
                "listener:list_types",
                "listener:toggle",
                "listener:remove_source",
                "listener:remove_target",
                "listener:remove_type",
                "listener:clear_sources",
                "listener:clear_targets",
                "listener:clear_everything",
                "listener:settings",
                "listener:admin_back",
            ],
        )

    async def send_type_select(self, interaction: nextcord.Interaction, action: str, prompt: str) -> None:
        rows = await self.cog.service.list_types()
        if not rows:
            await interaction.response.send_message("No listener types configured.", ephemeral=True)
            return
        await interaction.response.send_message(
            prompt,
            view=ListenerTypeSelectView(self.cog, rows, action, interaction.message, self.show_admin_back),
            ephemeral=True,
        )

    @nextcord.ui.button(label="Add Source", style=nextcord.ButtonStyle.primary, custom_id="listener:add_source", row=0)
    async def add_source(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await self.send_type_select(interaction, "add_source", "Select listener type for this source.")

    @nextcord.ui.button(label="Remove Source", style=nextcord.ButtonStyle.danger, custom_id="listener:remove_source", row=2)
    async def remove_source(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        rows = await self.cog.service.list_sources(interaction.guild_id)
        if not rows:
            await interaction.response.send_message("No active listener sources configured for this server.", ephemeral=True)
            return
        await interaction.response.send_message("Select source to remove.", view=ListenerSourceSelectView(self.cog, interaction, rows), ephemeral=True)

    @nextcord.ui.button(label="Add Target", style=nextcord.ButtonStyle.primary, custom_id="listener:add_target", row=0)
    async def add_target(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await self.send_type_select(interaction, "add_target", "Select listener type for this target.")

    @nextcord.ui.button(label="Remove Target", style=nextcord.ButtonStyle.danger, custom_id="listener:remove_target", row=2)
    async def remove_target(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        rows = await self.cog.service.list_targets(interaction.guild_id)
        if not rows:
            await interaction.response.send_message("No active listener targets configured for this server.", ephemeral=True)
            return
        await interaction.response.send_message("Select target to remove.", view=ListenerTargetSelectView(self.cog, interaction, rows), ephemeral=True)

    @nextcord.ui.button(label="List Sources", style=nextcord.ButtonStyle.secondary, custom_id="listener:list_sources", row=0)
    async def list_sources(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_message(format_source_list(await self.cog.service.list_sources(interaction.guild_id)), ephemeral=True)

    @nextcord.ui.button(label="List Targets", style=nextcord.ButtonStyle.secondary, custom_id="listener:list_targets", row=0)
    async def list_targets(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_message(format_target_list(await self.cog.service.list_targets(interaction.guild_id)), ephemeral=True)

    @nextcord.ui.button(label="List Types", style=nextcord.ButtonStyle.secondary, custom_id="listener:list_types", row=1)
    async def list_types(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_message(format_type_list(await self.cog.service.list_types()), ephemeral=True)

    @nextcord.ui.button(label="Add Type", style=nextcord.ButtonStyle.primary, custom_id="listener:add_type", row=0)
    async def add_type(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_modal(AddTypeModal(self.cog, interaction.message, self.show_admin_back))

    @nextcord.ui.button(label="Remove Type", style=nextcord.ButtonStyle.danger, custom_id="listener:remove_type", row=2)
    async def remove_type(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await self.send_type_select(interaction, "remove_type", "Select listener type to remove.")

    @nextcord.ui.button(label="Clear Sources", style=nextcord.ButtonStyle.danger, custom_id="listener:clear_sources", row=3)
    async def clear_sources(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_message("Clear all listener sources for this server?", view=ListenerClearConfirmView(self.cog, "sources"), ephemeral=True)

    @nextcord.ui.button(label="Clear Targets", style=nextcord.ButtonStyle.danger, custom_id="listener:clear_targets", row=3)
    async def clear_targets(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_message("Clear all listener targets for this server?", view=ListenerClearConfirmView(self.cog, "targets"), ephemeral=True)

    @nextcord.ui.button(label="Clear Everything", style=nextcord.ButtonStyle.danger, custom_id="listener:clear_everything", row=3)
    async def clear_everything(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_message("Clear all listener sources and targets for this server?", view=ListenerClearConfirmView(self.cog, "everything"), ephemeral=True)

    @nextcord.ui.button(label="Enable/Disable", style=nextcord.ButtonStyle.secondary, custom_id="listener:toggle", row=1)
    async def toggle(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        enabled = await self.cog.service.is_enabled(interaction.guild_id)
        new_enabled = not enabled
        await self.cog.service.set_enabled(interaction.guild_id, new_enabled, interaction.user.id)
        await interaction.response.edit_message(
            content=None,
            embed=self.cog.service.build_management_embed(),
            view=ListenerPanelView(self.cog, self.show_admin_back),
        )
        await interaction.followup.send("Listener enabled." if new_enabled else "Listener disabled.", ephemeral=True)

    @nextcord.ui.button(label="Show Settings", style=nextcord.ButtonStyle.secondary, custom_id="listener:settings", row=4)
    async def show_settings(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_message(embed=await self.cog.service.build_settings_embed(interaction.guild_id), ephemeral=True)
