from datetime import datetime, timezone

import nextcord

from core.permissions import can_manage_guild


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


def apply_button_order(view: nextcord.ui.View, custom_ids: list[str]) -> None:
    order = {custom_id: index for index, custom_id in enumerate(custom_ids)}
    view.children.sort(key=lambda child: order.get(getattr(child, "custom_id", ""), len(order)))
    for index, child in enumerate(view.children):
        child.row = min(index // 5, 4)


class BaseMessageModal(nextcord.ui.Modal):
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
                view=MessagesPanelView(self.cog, self.show_admin_back),
            )


class SendMessageModal(BaseMessageModal):
    def __init__(self, cog, source_message=None, show_admin_back: bool = False) -> None:
        super().__init__("Send Message", cog, source_message, show_admin_back)
        self.channel_id = nextcord.ui.TextInput("Channel ID", required=True)
        self.message = nextcord.ui.TextInput("Message", required=True, style=nextcord.TextInputStyle.paragraph)
        self.spoiler = nextcord.ui.TextInput("Spoiler yes/no", required=False)
        self.add_item(self.channel_id)
        self.add_item(self.message)
        self.add_item(self.spoiler)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        try:
            channel = await self.cog.service.get_channel(str(self.channel_id.value))
            spoiler = self.cog.service.parse_bool(str(self.spoiler.value or ""), False)
            content = self.cog.service.normalize_message_input(str(self.message.value), spoiler)
            sent = await channel.send(content, allowed_mentions=self.cog.service.safe_allowed_mentions())
            await self.cog.service.log_action(interaction.guild_id, "send_message", interaction.user.id, sent.id)
        except (ValueError, nextcord.HTTPException) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self.refresh_panel()
        await interaction.response.send_message("Message sent.", ephemeral=True)


class SendMultipleMessagesModal(BaseMessageModal):
    def __init__(self, cog, source_message=None, show_admin_back: bool = False) -> None:
        super().__init__("Send Multiple Messages", cog, source_message, show_admin_back)
        self.channel_id = nextcord.ui.TextInput("Channel ID", required=True)
        self.messages = nextcord.ui.TextInput("Messages", required=True, style=nextcord.TextInputStyle.paragraph)
        self.spoiler = nextcord.ui.TextInput("Spoiler yes/no", required=False)
        self.add_item(self.channel_id)
        self.add_item(self.messages)
        self.add_item(self.spoiler)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        try:
            channel = await self.cog.service.get_channel(str(self.channel_id.value))
            spoiler = self.cog.service.parse_bool(str(self.spoiler.value or ""), False)
            messages = self.cog.service.split_messages_input(str(self.messages.value), spoiler)
            sent_ids = []
            for content in messages:
                sent = await channel.send(content, allowed_mentions=self.cog.service.safe_allowed_mentions())
                sent_ids.append(sent.id)
            await self.cog.service.log_action(
                interaction.guild_id,
                "send_multiple_messages",
                interaction.user.id,
                details={"message_ids": sent_ids, "count": len(sent_ids)},
            )
        except (ValueError, nextcord.HTTPException) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self.refresh_panel()
        await interaction.response.send_message(f"Sent {len(sent_ids)} message(s).", ephemeral=True)


class SendEmbedModal(BaseMessageModal):
    def __init__(self, cog, source_message=None, show_admin_back: bool = False) -> None:
        super().__init__("Send Embed", cog, source_message, show_admin_back)
        self.channel_id = nextcord.ui.TextInput("Channel ID", required=True)
        self.title_input = nextcord.ui.TextInput("Title", required=False)
        self.description_input = nextcord.ui.TextInput("Description", required=False, style=nextcord.TextInputStyle.paragraph)
        self.color = nextcord.ui.TextInput("Color hex", required=False, placeholder="#3498db")
        self.image_url = nextcord.ui.TextInput("Image URL", required=False)
        self.add_item(self.channel_id)
        self.add_item(self.title_input)
        self.add_item(self.description_input)
        self.add_item(self.color)
        self.add_item(self.image_url)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        try:
            channel = await self.cog.service.get_channel(str(self.channel_id.value))
            embed = nextcord.Embed(
                title=self.cog.service.apply_custom_emojis(str(self.title_input.value or "")) or None,
                description=self.cog.service.normalize_message_input(str(self.description_input.value or "")) or None,
                color=self.cog.service.parse_color(str(self.color.value or "")),
            )
            if self.image_url.value:
                embed.set_image(url=str(self.image_url.value).strip())
            sent = await channel.send(embed=embed, allowed_mentions=self.cog.service.safe_allowed_mentions())
            await self.cog.service.log_action(interaction.guild_id, "send_embed", interaction.user.id, sent.id)
        except (ValueError, nextcord.HTTPException) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self.refresh_panel()
        await interaction.response.send_message("Embed sent.", ephemeral=True)


class ReplyMessageModal(BaseMessageModal):
    def __init__(self, cog, source_message=None, show_admin_back: bool = False) -> None:
        super().__init__("Reply Message", cog, source_message, show_admin_back)
        self.channel_id = nextcord.ui.TextInput("Channel ID", required=True)
        self.message_id = nextcord.ui.TextInput("Message ID", required=True)
        self.message = nextcord.ui.TextInput("Message", required=True, style=nextcord.TextInputStyle.paragraph)
        self.spoiler = nextcord.ui.TextInput("Spoiler yes/no", required=False)
        self.add_item(self.channel_id)
        self.add_item(self.message_id)
        self.add_item(self.message)
        self.add_item(self.spoiler)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        try:
            target = await self.cog.service.get_message(str(self.channel_id.value), str(self.message_id.value))
            spoiler = self.cog.service.parse_bool(str(self.spoiler.value or ""), False)
            content = self.cog.service.normalize_message_input(str(self.message.value), spoiler)
            sent = await target.reply(content, allowed_mentions=self.cog.service.safe_allowed_mentions())
            await self.cog.service.log_action(interaction.guild_id, "reply_message", interaction.user.id, sent.id)
        except (ValueError, nextcord.HTTPException) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self.refresh_panel()
        await interaction.response.send_message("Reply sent.", ephemeral=True)


class EditMessageModal(BaseMessageModal):
    def __init__(self, cog, source_message=None, show_admin_back: bool = False) -> None:
        super().__init__("Edit Message", cog, source_message, show_admin_back)
        self.channel_id = nextcord.ui.TextInput("Channel ID", required=True)
        self.message_id = nextcord.ui.TextInput("Message ID", required=True)
        self.content = nextcord.ui.TextInput("New Content", required=True, style=nextcord.TextInputStyle.paragraph)
        self.spoiler = nextcord.ui.TextInput("Spoiler yes/no", required=False)
        self.add_item(self.channel_id)
        self.add_item(self.message_id)
        self.add_item(self.content)
        self.add_item(self.spoiler)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        try:
            message = await self.cog.service.get_message(str(self.channel_id.value), str(self.message_id.value))
            if message.author.id != interaction.client.user.id:
                await interaction.response.send_message("I can only edit messages sent by this bot.", ephemeral=True)
                return
            spoiler = self.cog.service.parse_bool(str(self.spoiler.value or ""), False)
            content = self.cog.service.normalize_message_input(str(self.content.value), spoiler)
            await message.edit(content=content, allowed_mentions=self.cog.service.safe_allowed_mentions())
            await self.cog.service.log_action(interaction.guild_id, "edit_message", interaction.user.id, message.id)
        except (ValueError, nextcord.HTTPException) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self.refresh_panel()
        await interaction.response.send_message("Message edited.", ephemeral=True)


class ShowMessageModal(BaseMessageModal):
    def __init__(self, cog, source_message=None, show_admin_back: bool = False) -> None:
        super().__init__("Show Message", cog, source_message, show_admin_back)
        self.channel_id = nextcord.ui.TextInput("Channel ID", required=True)
        self.message_id = nextcord.ui.TextInput("Message ID", required=True)
        self.add_item(self.channel_id)
        self.add_item(self.message_id)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        try:
            message = await self.cog.service.get_message(str(self.channel_id.value), str(self.message_id.value))
            content = self.cog.service.clean_content_for_display(message.content or "")
            lines = [content or "(empty)"]
            if message.attachments:
                lines.append(f"Attachment: {message.attachments[0].url}")
            for embed in message.embeds:
                if embed.image and embed.image.url:
                    lines.append(f"Embed image: {embed.image.url}")
                    break
            await self.cog.service.log_action(interaction.guild_id, "show_message", interaction.user.id, message.id)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(f"```text\n{chr(10).join(lines)[:1900]}\n```", ephemeral=True)


class ExportMessagesModal(BaseMessageModal):
    def __init__(self, cog, source_message=None, show_admin_back: bool = False) -> None:
        super().__init__("Export Messages", cog, source_message, show_admin_back)
        self.channel_id = nextcord.ui.TextInput("Channel ID", required=True)
        self.hours = nextcord.ui.TextInput("Hours", required=True, placeholder="1-8760")
        self.add_item(self.channel_id)
        self.add_item(self.hours)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        try:
            hours = int(str(self.hours.value).strip())
            channel = await self.cog.service.get_channel(str(self.channel_id.value))
            text = await self.cog.service.export_messages_text(channel, hours)
            await interaction.user.send(file=self.cog.service.export_file(text, channel.id))
            await self.cog.service.log_action(
                interaction.guild_id,
                "export_messages",
                interaction.user.id,
                details={"channel_id": channel.id, "hours": hours},
            )
        except nextcord.Forbidden:
            await interaction.response.send_message("I could not DM you the export file.", ephemeral=True)
            return
        except (ValueError, nextcord.HTTPException) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self.refresh_panel()
        await interaction.response.send_message("Export sent to your DMs.", ephemeral=True)


class LastUpdateModal(BaseMessageModal):
    def __init__(self, cog, source_message=None, show_admin_back: bool = False) -> None:
        super().__init__("Last Update", cog, source_message, show_admin_back)
        self.channel_id = nextcord.ui.TextInput("Channel ID", required=True)
        self.clear_first = nextcord.ui.TextInput("Clear channel first? yes/no", required=False)
        self.add_item(self.channel_id)
        self.add_item(self.clear_first)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        try:
            channel = await self.cog.service.get_channel(str(self.channel_id.value))
            clear = self.cog.service.parse_bool(str(self.clear_first.value or ""), False)
            if clear:
                permissions = channel.permissions_for(interaction.user)
                if not permissions.manage_messages:
                    await interaction.response.send_message("You need Manage Messages to clear first.", ephemeral=True)
                    return
                await channel.purge(limit=100, check=lambda message: not message.pinned)
            unix_time = int(datetime.now(timezone.utc).timestamp())
            sent = await channel.send(
                f"📅 Last updated:\n<t:{unix_time}:f>",
                allowed_mentions=self.cog.service.safe_allowed_mentions(),
            )
            await self.cog.service.log_action(interaction.guild_id, "last_update", interaction.user.id, sent.id)
        except (ValueError, nextcord.HTTPException) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self.refresh_panel()
        await interaction.response.send_message("Last update sent.", ephemeral=True)


class BackToAdminPanelButton(nextcord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Back", style=nextcord.ButtonStyle.secondary, custom_id="messages:admin_back", row=4)

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


class MessagesPanelView(nextcord.ui.View):
    def __init__(self, cog, show_admin_back: bool = False) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.show_admin_back = show_admin_back
        if show_admin_back:
            self.add_item(BackToAdminPanelButton())
        apply_button_order(
            self,
            [
                "messages:send",
                "messages:send_many",
                "messages:embed",
                "messages:reply",
                "messages:edit",
                "messages:show",
                "messages:export",
                "messages:settings",
                "messages:admin_back",
            ],
        )

    @nextcord.ui.button(label="Send Message", style=nextcord.ButtonStyle.primary, custom_id="messages:send", row=0)
    async def send_message(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_modal(SendMessageModal(self.cog, interaction.message, self.show_admin_back))

    @nextcord.ui.button(label="Send Multiple Messages", style=nextcord.ButtonStyle.primary, custom_id="messages:send_many", row=0)
    async def send_many(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_modal(SendMultipleMessagesModal(self.cog, interaction.message, self.show_admin_back))

    @nextcord.ui.button(label="Send Embed", style=nextcord.ButtonStyle.primary, custom_id="messages:embed", row=0)
    async def send_embed(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_modal(SendEmbedModal(self.cog, interaction.message, self.show_admin_back))

    @nextcord.ui.button(label="Reply Message", style=nextcord.ButtonStyle.primary, custom_id="messages:reply", row=0)
    async def reply_message(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_modal(ReplyMessageModal(self.cog, interaction.message, self.show_admin_back))

    @nextcord.ui.button(label="Edit Message", style=nextcord.ButtonStyle.success, custom_id="messages:edit", row=0)
    async def edit_message(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_modal(EditMessageModal(self.cog, interaction.message, self.show_admin_back))

    @nextcord.ui.button(label="Show Message", style=nextcord.ButtonStyle.secondary, custom_id="messages:show", row=1)
    async def show_message(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_modal(ShowMessageModal(self.cog, interaction.message, self.show_admin_back))

    @nextcord.ui.button(label="Export Messages", style=nextcord.ButtonStyle.secondary, custom_id="messages:export", row=1)
    async def export_messages(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_modal(ExportMessagesModal(self.cog, interaction.message, self.show_admin_back))

    @nextcord.ui.button(label="Show Settings", style=nextcord.ButtonStyle.secondary, custom_id="messages:settings", row=1)
    async def show_settings(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_message(embed=await self.cog.service.build_settings_embed(interaction.guild_id), ephemeral=True)
