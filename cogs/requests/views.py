import re

import nextcord

from core.permissions import can_manage_guild


async def ensure_manager(interaction: nextcord.Interaction) -> bool:
    if not isinstance(interaction.user, nextcord.Member):
        await interaction.response.send_message("This can only be used in a server.", ephemeral=True)
        return False
    if not can_manage_guild(interaction.user):
        await interaction.response.send_message("You need Administrator or Manage Server permissions to use this.", ephemeral=True)
        return False
    return True


def can_review(member: nextcord.Member) -> bool:
    perms = member.guild_permissions
    return bool(perms.manage_roles or perms.manage_guild or perms.administrator)


def apply_button_order(view: nextcord.ui.View, custom_ids: list[str]) -> None:
    order = {custom_id: index for index, custom_id in enumerate(custom_ids)}
    view.children.sort(key=lambda child: order.get(getattr(child, "custom_id", ""), len(order)))
    for index, child in enumerate(view.children):
        child.row = min(index // 5, 4)


async def resolve_sendable_channel(interaction: nextcord.Interaction, channel_id: int):
    channel = interaction.client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await interaction.client.fetch_channel(channel_id)
        except (nextcord.Forbidden, nextcord.HTTPException, nextcord.NotFound):
            raise ValueError("Channel not found.") from None
    if getattr(channel, "guild", None) is None or channel.guild.id != interaction.guild_id:
        raise ValueError("This channel is not from this server.")
    if not hasattr(channel, "send"):
        raise ValueError("Channel must support sending messages.")
    bot_member = interaction.guild.me if interaction.guild else None
    permissions = channel.permissions_for(bot_member) if bot_member is not None else None
    if permissions is not None and (not permissions.view_channel or not permissions.send_messages):
        raise ValueError("I need permission to view and send messages in that channel.")
    return channel


class RequestSendButton(nextcord.ui.Button):
    def __init__(self, cog, panel_id: int) -> None:
        super().__init__(label="Send", style=nextcord.ButtonStyle.primary, custom_id=f"requests:send:{panel_id}")
        self.cog = cog
        self.panel_id = panel_id

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not isinstance(interaction.user, nextcord.Member) or interaction.guild is None:
            await interaction.response.send_message("This can only be used in a server.", ephemeral=True)
            return
        panel = await self.cog.service.get_panel(self.panel_id)
        if panel is None or not bool(panel["enabled"]):
            await interaction.response.send_message("This request panel is disabled.", ephemeral=True)
            return
        create_result, status = await self.cog.service.create_pending_request_if_allowed(
            interaction.guild,
            panel,
            interaction.user.id,
        )
        if create_result in {"has_access", "approved"}:
            await interaction.response.send_message("✅ You already have access.", ephemeral=True)
            return
        if create_result == "pending":
            await interaction.response.send_message("⚠️ Your request is already pending.", ephemeral=True)
            return
        if create_result != "created" or status is None:
            await interaction.response.send_message("Could not create this request.", ephemeral=True)
            return
        review_channel = interaction.client.get_channel(panel["review_channel_id"])
        if review_channel is None:
            try:
                review_channel = await interaction.client.fetch_channel(panel["review_channel_id"])
            except nextcord.HTTPException:
                await self.cog.service.set_status(status["id"], "cancelled")
                await interaction.response.send_message("Review channel is missing.", ephemeral=True)
                return
        try:
            review = await review_channel.send(
                content=f"<@{interaction.user.id}> is requesting access",
                embed=self.cog.service.build_review_embed(panel, interaction.user),
                view=RequestReviewView(self.cog, status["id"]),
                allowed_mentions=nextcord.AllowedMentions(everyone=False, users=True, roles=False),
            )
            await self.cog.service.update_review_message(status["id"], review.id)
            await self.cog.bot.db.save_persistent_view(
                interaction.guild_id,
                "requests",
                review_channel.id,
                review.id,
                "requests_review_panel",
                state={"status_id": status["id"]},
            )
        except nextcord.HTTPException as exc:
            await self.cog.service.set_status(status["id"], "cancelled")
            await interaction.response.send_message("Could not send this to review.", ephemeral=True)
            await self.cog.service.log(interaction.guild_id, "requests_error", user_id=interaction.user.id, details={"panel_id": self.panel_id, "error": str(exc)})
            return
        await self.cog.service.log(
            interaction.guild_id,
            "requests_user_sent",
            user_id=interaction.user.id,
            target_id=review.id,
            details={"panel_id": self.panel_id, "panel_key": panel["panel_key"], "review_message_id": review.id},
        )
        await interaction.response.send_message("✅ Sent to review.", ephemeral=True)


class RequestPublicView(nextcord.ui.View):
    def __init__(self, cog, panel_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(RequestSendButton(cog, panel_id))


class ReviewDecisionButton(nextcord.ui.Button):
    def __init__(self, cog, status_id: int, decision: str) -> None:
        label = "✅ Approve" if decision == "approved" else "❌ Deny"
        style = nextcord.ButtonStyle.success if decision == "approved" else nextcord.ButtonStyle.danger
        super().__init__(label=label, style=style, custom_id=f"requests:review:{decision}:{status_id}")
        self.cog = cog
        self.status_id = status_id
        self.decision = decision

    async def disable_buttons(self, interaction: nextcord.Interaction, note: str | None = None) -> None:
        message = interaction.message
        if message is None:
            return
        content = message.content or ""
        if note and note not in content:
            content = f"{content}\n**{note}.**" if content else f"**{note}.**"
        try:
            await message.edit(content=content, view=RequestReviewView(self.cog, self.status_id, disabled=True))
        except nextcord.HTTPException:
            pass

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not isinstance(interaction.user, nextcord.Member) or not can_review(interaction.user):
            await interaction.response.send_message("You need Manage Roles or Manage Server to review this.", ephemeral=True)
            return
        status = await self.cog.service.get_status(self.status_id)
        if status is None:
            await self.disable_buttons(interaction)
            await interaction.response.send_message("❌ This request no longer exists.", ephemeral=True)
            return
        panel = await self.cog.service.get_panel(status["panel_id"])
        if panel is None:
            await self.disable_buttons(interaction)
            await interaction.response.send_message("Request panel was not found.", ephemeral=True)
            return

        if status["status"] == "approved":
            if interaction.guild is not None:
                await self.cog.service.ensure_user_role(interaction.guild, status["user_id"], panel["role_id"], "Request access restored")
            await self.disable_buttons(interaction, "Approved")
            await interaction.response.send_message("⚠️ This request was already approved.", ephemeral=True)
            return
        if status["status"] == "denied":
            await self.disable_buttons(interaction, "Denied")
            await interaction.response.send_message("⚠️ This request was already denied.", ephemeral=True)
            return

        result, saved_status = await self.cog.service.set_status_safely(status["id"], self.decision)
        if result == "missing":
            await self.disable_buttons(interaction)
            await interaction.response.send_message("❌ This request no longer exists.", ephemeral=True)
            return
        if result == "already_approved":
            if interaction.guild is not None:
                await self.cog.service.ensure_user_role(interaction.guild, status["user_id"], panel["role_id"], "Request access restored")
            await self.disable_buttons(interaction, "Approved")
            await interaction.response.send_message("⚠️ This request was already approved.", ephemeral=True)
            return
        if result == "already_denied":
            await self.disable_buttons(interaction, "Denied")
            await interaction.response.send_message("⚠️ This request was already denied.", ephemeral=True)
            return
        if result == "duplicate_approved":
            if interaction.guild is not None:
                await self.cog.service.ensure_user_role(interaction.guild, status["user_id"], panel["role_id"], "Request access restored")
            await self.disable_buttons(interaction, "Approved")
            await interaction.response.send_message("✅ This user already has approved access.", ephemeral=True)
            return
        if result == "integrity_error":
            await self.disable_buttons(interaction)
            await interaction.response.send_message("This request could not be updated because a matching status already exists.", ephemeral=True)
            return

        if self.decision == "approved" and interaction.guild is not None:
            await self.cog.service.ensure_user_role(interaction.guild, status["user_id"], panel["role_id"], "Request approved")

        note = "Approved" if self.decision == "approved" else "Denied"
        await self.disable_buttons(interaction, note)
        member = interaction.guild.get_member(status["user_id"]) if interaction.guild else None
        if member is not None:
            try:
                await member.send("✅ Your request has been approved!" if self.decision == "approved" else "❌ Your request has been denied.")
            except nextcord.Forbidden:
                pass
        await self.cog.service.log(
            interaction.guild_id,
            "requests_approve" if self.decision == "approved" else "requests_deny",
            user_id=interaction.user.id,
            details={"panel_id": panel["id"], "user_id": status["user_id"], "staff_id": interaction.user.id, "review_message_id": status["review_message_id"], "status_id": saved_status["id"] if saved_status is not None else status["id"]},
        )
        await interaction.response.send_message("✅ Approved." if self.decision == "approved" else "✅ Denied.", ephemeral=True)


class RequestReviewView(nextcord.ui.View):
    def __init__(self, cog, status_id: int, disabled: bool = False) -> None:
        super().__init__(timeout=None)
        approve = ReviewDecisionButton(cog, status_id, "approved")
        deny = ReviewDecisionButton(cog, status_id, "denied")
        approve.disabled = disabled
        deny.disabled = disabled
        self.add_item(approve)
        self.add_item(deny)


class CreateRequestModal(nextcord.ui.Modal):
    def __init__(self, cog) -> None:
        super().__init__("Create Request")
        self.cog = cog
        self.channels_input = nextcord.ui.TextInput(
            "Public + Private Channel IDs",
            placeholder="public_channel_id, private_channel_id",
            required=True,
        )
        self.role_id_input = nextcord.ui.TextInput("Role ID", required=True)
        self.title_input = nextcord.ui.TextInput("Title", required=True, max_length=256)
        self.description_input = nextcord.ui.TextInput("Description", required=True, style=nextcord.TextInputStyle.paragraph)
        self.image_url_input = nextcord.ui.TextInput("Image URL optional", required=False)
        for item in (
            self.channels_input,
            self.role_id_input,
            self.title_input,
            self.description_input,
            self.image_url_input,
        ):
            self.add_item(item)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        try:
            channel_ids = [part for part in re.split(r"[\s,]+", str(self.channels_input.value).strip()) if part]
            if len(channel_ids) < 2:
                raise ValueError("Provide both public channel ID and private/review channel ID.")
            public_channel = await resolve_sendable_channel(interaction, int(channel_ids[0]))
            review_channel = await resolve_sendable_channel(interaction, int(channel_ids[1]))
            role = interaction.guild.get_role(int(str(self.role_id_input.value).strip()))
            if role is None:
                raise ValueError("Role was not found in this server.")
            self.cog.service.validate_bot_can_manage_role(interaction.guild, role)
            panel = await self.cog.service.create_panel(
                interaction.guild_id,
                public_channel.id,
                review_channel.id,
                role.id,
                str(self.title_input.value),
                str(self.description_input.value),
                str(self.image_url_input.value or ""),
                interaction.user.id,
            )
            sent = await public_channel.send(
                embed=self.cog.service.build_public_embed(panel),
                view=RequestPublicView(self.cog, panel["id"]),
                allowed_mentions=nextcord.AllowedMentions.none(),
            )
            await self.cog.service.update_public_message(panel["id"], sent.id)
            await self.cog.bot.db.save_persistent_view(interaction.guild_id, "requests", public_channel.id, sent.id, "requests_public_panel", state={"panel_id": panel["id"]})
            await self.cog.service.log(interaction.guild_id, "requests_create_panel", user_id=interaction.user.id, target_id=sent.id, details={"panel_id": panel["id"], "panel_key": panel["panel_key"], "request_channel_id": public_channel.id, "review_channel_id": review_channel.id, "role_id": role.id, "public_message_id": sent.id})
        except (ValueError, nextcord.HTTPException) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(f"Request panel `{panel['panel_key']}` created in {public_channel.mention}.", ephemeral=True)


class SetImageUrlModal(nextcord.ui.Modal):
    def __init__(self, cog, panel_id: int) -> None:
        super().__init__("Set Request Image URL")
        self.cog = cog
        self.panel_id = panel_id
        self.image_url_input = nextcord.ui.TextInput("Image URL", required=False)
        self.add_item(self.image_url_input)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        try:
            panel = await self.cog.service.get_panel(self.panel_id)
            if panel is None:
                raise ValueError("Request panel not found.")
            await self.cog.service.set_image_url(self.panel_id, str(self.image_url_input.value or ""))
            ok, message = await self.cog.refresh_public_panel(self.panel_id)
            await self.cog.service.log(
                interaction.guild_id,
                "requests_refresh_panel",
                user_id=interaction.user.id,
                details={"panel_id": self.panel_id, "panel_key": panel["panel_key"], "image_url": str(self.image_url_input.value or "")},
            )
        except (ValueError, nextcord.HTTPException) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(message if ok else f"❌ {message}", ephemeral=True)


class PanelSelect(nextcord.ui.Select):
    def __init__(self, cog, panels, action: str) -> None:
        options = [
            nextcord.SelectOption(label=f"{p['panel_key']} <#{p['request_channel_id']}>", value=str(p["id"]), description=(p["message"] or "")[:100])
            for p in panels[:25]
        ]
        super().__init__(placeholder="Select request panel", min_values=1, max_values=1, options=options)
        self.cog = cog
        self.action = action

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        panel = await self.cog.service.get_panel(int(self.values[0]))
        if panel is None:
            await interaction.response.send_message("Request panel not found.", ephemeral=True)
            return
        if self.action == "delete":
            await interaction.response.send_message(f"Delete request panel `{panel['panel_key']}`?", view=DeletePanelConfirmView(self.cog, panel), ephemeral=True)
        elif self.action == "refresh":
            ok, message = await self.cog.refresh_public_panel(panel["id"])
            await self.cog.service.log(interaction.guild_id, "requests_refresh_panel", user_id=interaction.user.id, details={"panel_id": panel["id"], "panel_key": panel["panel_key"]})
            await interaction.response.send_message(message if ok else f"❌ {message}", ephemeral=True)
        elif self.action == "set_image":
            await interaction.response.send_modal(SetImageUrlModal(self.cog, panel["id"]))


class PanelSelectView(nextcord.ui.View):
    def __init__(self, cog, panels, action: str) -> None:
        super().__init__(timeout=120)
        self.add_item(PanelSelect(cog, panels, action))


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
        await self.cog.service.log(interaction.guild_id, "requests_delete_panel", user_id=interaction.user.id, details={"panel_id": self.panel["id"], "panel_key": self.panel["panel_key"]})
        await interaction.response.edit_message(content="Request panel deleted.", view=None)

    @nextcord.ui.button(label="Cancel", style=nextcord.ButtonStyle.secondary)
    async def cancel(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)


class ClearPendingConfirmView(nextcord.ui.View):
    def __init__(self, cog) -> None:
        super().__init__(timeout=60)
        self.cog = cog

    @nextcord.ui.button(label="Confirm", style=nextcord.ButtonStyle.danger)
    async def confirm(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        count = await self.cog.service.clear_pending(interaction.guild_id)
        await self.cog.service.log(interaction.guild_id, "requests_clear_pending", user_id=interaction.user.id, details={"guild_id": interaction.guild_id, "count": count})
        await interaction.response.edit_message(content=f"Cancelled {count} pending request(s).", view=None)

    @nextcord.ui.button(label="Cancel", style=nextcord.ButtonStyle.secondary)
    async def cancel(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)


class BackToAdminPanelButton(nextcord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Back", style=nextcord.ButtonStyle.secondary, custom_id="requests:admin_back", row=4)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        from cogs.admin_panel.views import AdminPanelView
        from core.embeds import admin_panel_embed

        await interaction.response.edit_message(content=None, embed=admin_panel_embed(), view=AdminPanelView(interaction.client))


class RequestsAdminPanelView(nextcord.ui.View):
    def __init__(self, cog, show_admin_back: bool = False) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.show_admin_back = show_admin_back
        if show_admin_back:
            self.add_item(BackToAdminPanelButton())
        apply_button_order(
            self,
            [
                "requests:create",
                "requests:set_image",
                "requests:refresh",
                "requests:list",
                "requests:delete",
                "requests:clear_pending",
                "requests:settings",
                "requests:admin_back",
            ],
        )

    async def select_panel(self, interaction: nextcord.Interaction, action: str) -> None:
        panels = await self.cog.service.list_panels(interaction.guild_id)
        if not panels:
            await interaction.response.send_message("No active requests configured for this server.", ephemeral=True)
            return
        await interaction.response.send_message("Select request panel.", view=PanelSelectView(self.cog, panels, action), ephemeral=True)

    @nextcord.ui.button(label="Create Request", style=nextcord.ButtonStyle.primary, custom_id="requests:create", row=0)
    async def create_request(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_modal(CreateRequestModal(self.cog))

    @nextcord.ui.button(label="Set Image URL", style=nextcord.ButtonStyle.success, custom_id="requests:set_image", row=0)
    async def set_image_url(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await self.select_panel(interaction, "set_image")

    @nextcord.ui.button(label="Refresh Request", style=nextcord.ButtonStyle.success, custom_id="requests:refresh", row=0)
    async def refresh_request(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await self.select_panel(interaction, "refresh")

    @nextcord.ui.button(label="List Requests", style=nextcord.ButtonStyle.secondary, custom_id="requests:list", row=0)
    async def list_requests(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        panels = await self.cog.service.list_panels(interaction.guild_id)
        if not panels:
            await interaction.response.send_message("No active requests configured for this server.", ephemeral=True)
            return
        lines = []
        for panel in panels:
            line = (
                f"`{panel['panel_key']}` public <#{panel['request_channel_id']}> "
                f"review <#{panel['review_channel_id']}> role <@&{panel['role_id']}> "
                f"message `{panel['public_message_id']}` - {(panel['message'] or '')[:55]}"
            )
            if panel["image_url"]:
                line += f"\nImage: {panel['image_url']}"
            lines.append(line)
        await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

    @nextcord.ui.button(label="Delete Request", style=nextcord.ButtonStyle.danger, custom_id="requests:delete", row=1)
    async def delete_request(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await self.select_panel(interaction, "delete")

    @nextcord.ui.button(label="Clear Pending", style=nextcord.ButtonStyle.danger, custom_id="requests:clear_pending", row=1)
    async def clear_pending(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_message("Cancel all pending requests for this server?", view=ClearPendingConfirmView(self.cog), ephemeral=True)

    @nextcord.ui.button(label="Show Settings", style=nextcord.ButtonStyle.secondary, custom_id="requests:settings", row=2)
    async def show_settings(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_message(embed=await self.cog.service.build_settings_embed(interaction.guild_id), ephemeral=True)
