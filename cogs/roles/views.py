import nextcord

from core.permissions import can_manage_guild

from .service import STYLE_MAP


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
        raise ValueError("Channel not found.")
    channel = interaction.client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await interaction.client.fetch_channel(channel_id)
        except (nextcord.Forbidden, nextcord.HTTPException, nextcord.NotFound):
            raise ValueError("Channel not found.") from None
    if getattr(channel, "guild", None) is None or channel.guild.id != interaction.guild.id:
        raise ValueError("This channel is not from this server.")
    if not hasattr(channel, "send"):
        raise ValueError("Channel must support sending messages.")
    bot_member = interaction.guild.me
    permissions = channel.permissions_for(bot_member) if bot_member is not None else None
    if permissions is not None and (not permissions.view_channel or not permissions.send_messages):
        raise ValueError("I need permission to view and send messages in that channel.")
    return channel


def apply_button_order(view: nextcord.ui.View, custom_ids: list[str]) -> None:
    order = {custom_id: index for index, custom_id in enumerate(custom_ids)}
    view.children.sort(key=lambda child: order.get(getattr(child, "custom_id", ""), len(order)))
    for index, child in enumerate(view.children):
        child.row = min(index // 5, 4)


class PublicRoleButton(nextcord.ui.Button):
    def __init__(self, cog, button) -> None:
        super().__init__(
            label=button["label"],
            style=cog.service.style_to_button_style(button["style"]),
            custom_id=f"roles:button:{button['id']}",
            row=min(int(button["position"]) // 5, 4),
        )
        self.cog = cog
        self.button_id = button["id"]

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not isinstance(interaction.user, nextcord.Member) or interaction.guild is None:
            await interaction.response.send_message("This can only be used in a server.", ephemeral=True)
            return
        button = await self.cog.service.get_button(self.button_id)
        if button is None or not bool(button["enabled"]):
            await interaction.response.send_message("This role button is disabled.", ephemeral=True)
            return
        panel = await self.cog.service.get_panel_by_id(button["panel_id"])
        if panel is None or not bool(panel["enabled"]):
            await interaction.response.send_message("This role panel is disabled.", ephemeral=True)
            return
        role_rows = await self.cog.service.get_button_roles(self.button_id)
        role_ids = [row["role_id"] for row in role_rows]
        try:
            roles = self.cog.service.validate_roles(interaction.guild, role_ids)
            self.cog.service.validate_bot_can_manage_roles(interaction.guild, roles)
        except ValueError as exc:
            await self.cog.service.log(
                interaction.guild.id,
                "roles_error",
                user_id=interaction.user.id,
                details={"button_id": self.button_id, "role_ids": role_ids, "error": str(exc)},
            )
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        member_roles = set(interaction.user.roles)
        has_all = all(role in member_roles for role in roles)
        try:
            if has_all:
                await interaction.user.remove_roles(*roles, reason="Role button toggle")
                action = "roles_button_remove_roles"
                message = "✅ Removed roles: " + ", ".join(role.name for role in roles)
            else:
                missing = [role for role in roles if role not in member_roles]
                await interaction.user.add_roles(*missing, reason="Role button toggle")
                action = "roles_button_add_roles"
                message = "✅ Added roles: " + ", ".join(role.name for role in missing)
        except nextcord.Forbidden:
            message = "❌ I cannot manage one or more of these roles. Move my bot role above them."
            await interaction.response.send_message(message, ephemeral=True)
            return
        await self.cog.service.log(
            interaction.guild.id,
            action,
            user_id=interaction.user.id,
            details={"panel_id": panel["id"], "button_id": self.button_id, "role_ids": role_ids},
        )
        await interaction.response.send_message(message, ephemeral=True)


class PublicRolePanelView(nextcord.ui.View):
    def __init__(self, cog, buttons) -> None:
        super().__init__(timeout=None)
        for button in buttons[:25]:
            self.add_item(PublicRoleButton(cog, button))


class PanelSelect(nextcord.ui.Select):
    def __init__(self, cog, panels, action: str, interaction: nextcord.Interaction) -> None:
        options = []
        for panel in panels[:25]:
            label, desc = cog.service.format_panel_option(interaction.guild, panel)
            options.append(nextcord.SelectOption(label=label[:100], value=str(panel["id"]), description=desc[:100] or None))
        super().__init__(placeholder="Select role panel", min_values=1, max_values=1, options=options)
        self.cog = cog
        self.action = action

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        panel = await self.cog.service.get_panel_by_id(int(self.values[0]))
        if panel is None:
            await interaction.response.send_message("Role panel not found.", ephemeral=True)
            return
        if self.action == "add_button":
            await interaction.response.send_modal(AddButtonModal(self.cog, panel["id"]))
        elif self.action in {"remove_button", "add_role", "clear_roles"}:
            buttons = await self.cog.service.list_buttons(panel["id"])
            if not buttons:
                await interaction.response.send_message("This panel has no active buttons.", ephemeral=True)
                return
            await interaction.response.send_message(
                "Select button.",
                view=ButtonSelectView(self.cog, panel, buttons, self.action),
                ephemeral=True,
            )
        elif self.action == "delete_panel":
            await interaction.response.send_message(
                f"Delete role panel `{panel['panel_key']}`?",
                view=DeletePanelConfirmView(self.cog, panel),
                ephemeral=True,
            )
        elif self.action == "refresh_panel":
            ok, message = await self.cog.refresh_public_panel(panel["id"])
            await self.cog.service.log(
                interaction.guild_id,
                "roles_refresh_panel",
                user_id=interaction.user.id,
                details={"panel_id": panel["id"], "panel_key": panel["panel_key"]},
            )
            await interaction.response.send_message(message if ok else f"❌ {message}", ephemeral=True)


class PanelSelectView(nextcord.ui.View):
    def __init__(self, cog, panels, action: str, interaction: nextcord.Interaction) -> None:
        super().__init__(timeout=120)
        self.add_item(PanelSelect(cog, panels, action, interaction))


class ButtonSelect(nextcord.ui.Select):
    def __init__(self, cog, panel, buttons, action: str) -> None:
        options = [nextcord.SelectOption(label=button["label"][:100], value=str(button["id"])) for button in buttons[:25]]
        super().__init__(placeholder="Select button", min_values=1, max_values=1, options=options)
        self.cog = cog
        self.panel = panel
        self.action = action

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        button_id = int(self.values[0])
        if self.action == "remove_button":
            await self.cog.service.disable_button(button_id)
            await self.cog.refresh_public_panel(self.panel["id"])
            await self.cog.service.log(
                interaction.guild_id,
                "roles_remove_button",
                user_id=interaction.user.id,
                details={"panel_id": self.panel["id"], "button_id": button_id},
            )
            await interaction.response.send_message("Button removed.", ephemeral=True)
        elif self.action == "add_role":
            await interaction.response.send_modal(AddRoleToButtonModal(self.cog, self.panel["id"], button_id))
        else:
            await interaction.response.send_message(
                "Clear bundled roles from this button?",
                view=ClearButtonRolesConfirmView(self.cog, self.panel["id"], button_id),
                ephemeral=True,
            )


class ButtonSelectView(nextcord.ui.View):
    def __init__(self, cog, panel, buttons, action: str) -> None:
        super().__init__(timeout=120)
        self.add_item(ButtonSelect(cog, panel, buttons, action))


class CreatePanelModal(nextcord.ui.Modal):
    def __init__(self, cog) -> None:
        super().__init__("Create Role Panel")
        self.cog = cog
        self.channel_id_input = nextcord.ui.TextInput("Channel ID", required=True, max_length=32)
        self.description_input = nextcord.ui.TextInput("Description", required=True, style=nextcord.TextInputStyle.paragraph)
        self.title_input = nextcord.ui.TextInput("Title optional", required=False, max_length=256)
        self.image_url_input = nextcord.ui.TextInput("Image URL optional", required=False, max_length=1000)
        self.thumbnail_url_input = nextcord.ui.TextInput("Thumbnail URL optional", required=False, max_length=1000)
        for item in (
            self.channel_id_input,
            self.description_input,
            self.title_input,
            self.image_url_input,
            self.thumbnail_url_input,
        ):
            self.add_item(item)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        try:
            channel = await resolve_sendable_channel(interaction, int(str(self.channel_id_input.value).strip()))
            panel = await self.cog.service.create_panel(
                interaction.guild_id,
                channel.id,
                str(self.description_input.value),
                str(self.title_input.value or ""),
                str(self.image_url_input.value or ""),
                str(self.thumbnail_url_input.value or ""),
                interaction.user.id,
            )
            sent = await channel.send(
                embed=self.cog.service.build_public_embed(panel),
                view=PublicRolePanelView(self.cog, []),
                allowed_mentions=nextcord.AllowedMentions.none(),
            )
            await self.cog.service.update_panel_message(panel["id"], sent.id)
            await self.cog.service.db.save_persistent_view(
                interaction.guild_id,
                "roles",
                channel.id,
                sent.id,
                "roles_public_panel",
                state={"panel_id": panel["id"]},
            )
            await self.cog.service.log(
                interaction.guild_id,
                "roles_create_panel",
                user_id=interaction.user.id,
                target_id=sent.id,
                details={"panel_id": panel["id"], "panel_key": panel["panel_key"], "channel_id": channel.id, "message_id": sent.id},
            )
        except (ValueError, nextcord.HTTPException) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(f"Role panel `{panel['panel_key']}` created in {channel.mention}.", ephemeral=True)


class AddButtonModal(nextcord.ui.Modal):
    def __init__(self, cog, panel_id: int) -> None:
        super().__init__("Add Role Button")
        self.cog = cog
        self.panel_id = panel_id
        self.button_label_input = nextcord.ui.TextInput("Button Label", required=True, max_length=80)
        self.role_ids_input = nextcord.ui.TextInput("Role IDs", required=True, style=nextcord.TextInputStyle.paragraph)
        self.style_input = nextcord.ui.TextInput("Style", required=True, default_value="Primary (Blue)")
        self.position_input = nextcord.ui.TextInput("Position optional", required=False)
        self.nick_change_input = nextcord.ui.TextInput("Nick Change YES/NO optional", required=False)
        for item in (
            self.button_label_input,
            self.role_ids_input,
            self.style_input,
            self.position_input,
            self.nick_change_input,
        ):
            self.add_item(item)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        try:
            panel = await self.cog.service.get_panel_by_id(self.panel_id)
            role_ids = self.cog.service.parse_role_ids(str(self.role_ids_input.value))
            roles = self.cog.service.validate_roles(interaction.guild, role_ids)
            self.cog.service.validate_bot_can_manage_roles(interaction.guild, roles)
            position = int(str(self.position_input.value).strip()) if str(self.position_input.value or "").strip() else None
            nick_change = str(self.nick_change_input.value or "").strip().lower() in {"yes", "y", "true", "1", "on"}
            button = await self.cog.service.add_button(
                self.panel_id,
                str(self.button_label_input.value),
                role_ids,
                str(self.style_input.value),
                position,
                nick_change,
            )
            await self.cog.refresh_public_panel(self.panel_id)
            await self.cog.service.log(
                interaction.guild_id,
                "roles_add_button",
                user_id=interaction.user.id,
                details={"panel_id": self.panel_id, "panel_key": panel["panel_key"], "button_id": button["id"], "role_ids": role_ids},
            )
        except (ValueError, nextcord.HTTPException) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message("Role button saved.", ephemeral=True)


class AddRoleToButtonModal(nextcord.ui.Modal):
    def __init__(self, cog, panel_id: int, button_id: int) -> None:
        super().__init__("Add Role To Button")
        self.cog = cog
        self.panel_id = panel_id
        self.button_id = button_id
        self.role_ids_input = nextcord.ui.TextInput("Role IDs", required=True, style=nextcord.TextInputStyle.paragraph)
        self.add_item(self.role_ids_input)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        try:
            role_ids = self.cog.service.parse_role_ids(str(self.role_ids_input.value))
            roles = self.cog.service.validate_roles(interaction.guild, role_ids)
            self.cog.service.validate_bot_can_manage_roles(interaction.guild, roles)
            await self.cog.service.add_roles_to_button(self.button_id, role_ids)
            await self.cog.refresh_public_panel(self.panel_id)
            await self.cog.service.log(
                interaction.guild_id,
                "roles_add_role_to_button",
                user_id=interaction.user.id,
                details={"panel_id": self.panel_id, "button_id": self.button_id, "role_ids": role_ids},
            )
        except (ValueError, nextcord.HTTPException) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message("Roles added to button.", ephemeral=True)


class ClearButtonRolesConfirmView(nextcord.ui.View):
    def __init__(self, cog, panel_id: int, button_id: int) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.panel_id = panel_id
        self.button_id = button_id

    @nextcord.ui.button(label="Confirm", style=nextcord.ButtonStyle.danger)
    async def confirm(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        removed, kept_base = await self.cog.service.clear_button_roles(self.button_id)
        await self.cog.refresh_public_panel(self.panel_id)
        await self.cog.service.log(
            interaction.guild_id,
            "roles_clear_button_roles",
            user_id=interaction.user.id,
            details={"panel_id": self.panel_id, "button_id": self.button_id},
        )
        msg = f"Cleared {removed} extra role(s)." if kept_base else "No base role existed, so the button was disabled."
        await interaction.response.edit_message(content=msg, view=None)

    @nextcord.ui.button(label="Cancel", style=nextcord.ButtonStyle.secondary)
    async def cancel(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)


class DeletePanelConfirmView(nextcord.ui.View):
    def __init__(self, cog, panel) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.panel = panel

    @nextcord.ui.button(label="Confirm", style=nextcord.ButtonStyle.danger)
    async def confirm(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        await self.cog.delete_public_message(self.panel)
        await self.cog.service.disable_panel(self.panel["id"])
        await self.cog.service.log(
            interaction.guild_id,
            "roles_delete_panel",
            user_id=interaction.user.id,
            details={"panel_id": self.panel["id"], "panel_key": self.panel["panel_key"]},
        )
        await interaction.response.edit_message(content="Role panel deleted.", view=None)

    @nextcord.ui.button(label="Cancel", style=nextcord.ButtonStyle.secondary)
    async def cancel(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)


class BackToAdminPanelButton(nextcord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Back", style=nextcord.ButtonStyle.secondary, custom_id="roles:admin_back", row=4)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        from cogs.admin_panel.views import AdminPanelView
        from core.embeds import admin_panel_embed

        await interaction.response.edit_message(content=None, embed=admin_panel_embed(), view=AdminPanelView(interaction.client))


class RolesAdminPanelView(nextcord.ui.View):
    def __init__(self, cog, show_admin_back: bool = False) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.show_admin_back = show_admin_back
        if show_admin_back:
            self.add_item(BackToAdminPanelButton())
        apply_button_order(
            self,
            [
                "roles:create_panel",
                "roles:add_button",
                "roles:add_role_to_button",
                "roles:refresh_panel",
                "roles:list_panels",
                "roles:remove_button",
                "roles:clear_button_roles",
                "roles:delete_panel",
                "roles:show_settings",
                "roles:admin_back",
            ],
        )

    async def select_panel(self, interaction: nextcord.Interaction, action: str) -> None:
        panels = await self.cog.service.list_panels(interaction.guild_id)
        if not panels:
            await interaction.response.send_message("No active role panels configured for this server.", ephemeral=True)
            return
        await interaction.response.send_message("Select role panel.", view=PanelSelectView(self.cog, panels, action, interaction), ephemeral=True)

    @nextcord.ui.button(label="Create Role Panel", style=nextcord.ButtonStyle.primary, custom_id="roles:create_panel", row=0)
    async def create_panel(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_modal(CreatePanelModal(self.cog))

    @nextcord.ui.button(label="Add Button", style=nextcord.ButtonStyle.primary, custom_id="roles:add_button", row=0)
    async def add_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await self.select_panel(interaction, "add_button")

    @nextcord.ui.button(label="Remove Button", style=nextcord.ButtonStyle.danger, custom_id="roles:remove_button", row=1)
    async def remove_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await self.select_panel(interaction, "remove_button")

    @nextcord.ui.button(label="Add Role To Button", style=nextcord.ButtonStyle.primary, custom_id="roles:add_role_to_button", row=0)
    async def add_role_to_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await self.select_panel(interaction, "add_role")

    @nextcord.ui.button(label="Clear Button Roles", style=nextcord.ButtonStyle.danger, custom_id="roles:clear_button_roles", row=1)
    async def clear_button_roles(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await self.select_panel(interaction, "clear_roles")

    @nextcord.ui.button(label="List Role Panels", style=nextcord.ButtonStyle.secondary, custom_id="roles:list_panels", row=0)
    async def list_panels(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        panels = await self.cog.service.list_panels(interaction.guild_id)
        if not panels:
            await interaction.response.send_message("No active role panels configured for this server.", ephemeral=True)
            return
        lines = [
            f"`{p['panel_key']}` <#{p['channel_id']}> message `{p['message_id']}` buttons `{p['button_count']}` - {(p['description'] or '')[:60]}"
            for p in panels
        ]
        await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

    @nextcord.ui.button(label="Delete Role Panel", style=nextcord.ButtonStyle.danger, custom_id="roles:delete_panel", row=1)
    async def delete_panel(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await self.select_panel(interaction, "delete_panel")

    @nextcord.ui.button(label="Refresh Role Panel", style=nextcord.ButtonStyle.success, custom_id="roles:refresh_panel", row=0)
    async def refresh_panel(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await self.select_panel(interaction, "refresh_panel")

    @nextcord.ui.button(label="Show Settings", style=nextcord.ButtonStyle.secondary, custom_id="roles:show_settings", row=2)
    async def show_settings(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_message(embed=await self.cog.service.build_settings_embed(interaction.guild_id), ephemeral=True)
