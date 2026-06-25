import logging

import nextcord
from nextcord.ext import commands

from core.config import get_development_guild_ids
from core.permissions import require_guild_manager

from .service import MANAGEMENT_VIEW_TYPE, MODULE_NAME, MessageService
from .views import MessagesPanelView


class MessagesCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.logger = logging.getLogger(__name__)
        self.service = MessageService(bot)
        self._restored = False

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._restored:
            return
        await self.restore_management_panels()
        self._restored = True

    @nextcord.slash_command(
        name="messages-panel",
        description="Create or update the message management panel.",
        guild_ids=get_development_guild_ids(),
    )
    async def messages_panel(self, interaction: nextcord.Interaction) -> None:
        if not await require_guild_manager(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        message = await self.create_or_update_management_panel(interaction)
        await interaction.followup.send(f"Messages panel ready in {message.channel.mention}.", ephemeral=True)

    @nextcord.slash_command(
        name="send-img",
        description="Send image files and image links to a channel.",
        guild_ids=get_development_guild_ids(),
    )
    async def send_img(
        self,
        interaction: nextcord.Interaction,
        channel: nextcord.TextChannel = nextcord.SlashOption(description="Channel where images will be sent"),
        description: str | None = nextcord.SlashOption(description="Optional description. Use | for new line.", required=False, default=None),
        spoiler: bool = nextcord.SlashOption(description="Mark uploaded file images as spoiler.", required=False, default=False),
        file_img1: nextcord.Attachment | None = nextcord.SlashOption(description="File image 1", required=False, default=None),
        file_img2: nextcord.Attachment | None = nextcord.SlashOption(description="File image 2", required=False, default=None),
        file_img3: nextcord.Attachment | None = nextcord.SlashOption(description="File image 3", required=False, default=None),
        file_img4: nextcord.Attachment | None = nextcord.SlashOption(description="File image 4", required=False, default=None),
        file_img5: nextcord.Attachment | None = nextcord.SlashOption(description="File image 5", required=False, default=None),
        file_img6: nextcord.Attachment | None = nextcord.SlashOption(description="File image 6", required=False, default=None),
        file_img7: nextcord.Attachment | None = nextcord.SlashOption(description="File image 7", required=False, default=None),
        file_img8: nextcord.Attachment | None = nextcord.SlashOption(description="File image 8", required=False, default=None),
        file_img9: nextcord.Attachment | None = nextcord.SlashOption(description="File image 9", required=False, default=None),
        file_img10: nextcord.Attachment | None = nextcord.SlashOption(description="File image 10", required=False, default=None),
        file_img11: nextcord.Attachment | None = nextcord.SlashOption(description="File image 11", required=False, default=None),
        link_img1: str | None = nextcord.SlashOption(description="Link image 1", required=False, default=None),
        link_img2: str | None = nextcord.SlashOption(description="Link image 2", required=False, default=None),
        link_img3: str | None = nextcord.SlashOption(description="Link image 3", required=False, default=None),
        link_img4: str | None = nextcord.SlashOption(description="Link image 4", required=False, default=None),
        link_img5: str | None = nextcord.SlashOption(description="Link image 5", required=False, default=None),
        link_img6: str | None = nextcord.SlashOption(description="Link image 6", required=False, default=None),
        link_img7: str | None = nextcord.SlashOption(description="Link image 7", required=False, default=None),
        link_img8: str | None = nextcord.SlashOption(description="Link image 8", required=False, default=None),
        link_img9: str | None = nextcord.SlashOption(description="Link image 9", required=False, default=None),
        link_img10: str | None = nextcord.SlashOption(description="Link image 10", required=False, default=None),
        link_img11: str | None = nextcord.SlashOption(description="Link image 11", required=False, default=None),
    ) -> None:
        if not await require_guild_manager(interaction):
            return

        attachments = [
            attachment
            for attachment in (
                file_img1,
                file_img2,
                file_img3,
                file_img4,
                file_img5,
                file_img6,
                file_img7,
                file_img8,
                file_img9,
                file_img10,
                file_img11,
            )
            if attachment is not None
        ]
        links = [
            link.strip()
            for link in (
                link_img1,
                link_img2,
                link_img3,
                link_img4,
                link_img5,
                link_img6,
                link_img7,
                link_img8,
                link_img9,
                link_img10,
                link_img11,
            )
            if link and link.strip()
        ]

        if not attachments and not links:
            await interaction.response.send_message("❌ Provide at least one file image or link image.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            content = self.service.normalize_message_input(description or "")
            files = [await attachment_to_file(attachment, spoiler) for attachment in attachments]
            for index in range(0, len(files), 10):
                await channel.send(
                    content=content if index == 0 and content else None,
                    files=files[index : index + 10],
                    allowed_mentions=self.service.safe_allowed_mentions(),
                )

            for index, link in enumerate(links):
                embed = nextcord.Embed(color=0x3498DB)
                if not attachments and index == 0 and content:
                    embed.description = content
                embed.set_image(url=link)
                await channel.send(embed=embed, allowed_mentions=self.service.safe_allowed_mentions())
        except nextcord.Forbidden:
            await interaction.followup.send("I do not have permission to send images in that channel.", ephemeral=True)
            return
        except nextcord.HTTPException as exc:
            await interaction.followup.send(
                f"Discord rejected one or more images. Check file size, file type, or upload limits. `{exc}`",
                ephemeral=True,
            )
            return

        total_count = len(attachments) + len(links)
        await self.service.log_action(
            interaction.guild_id,
            "send_img",
            interaction.user.id,
            details={
                "channel_id": channel.id,
                "file_count": len(attachments),
                "link_count": len(links),
                "total_count": total_count,
                "has_description": bool(description and description.strip()),
                "spoiler": spoiler,
            },
        )
        await interaction.followup.send(f"✅ Sent {total_count} image(s) to {channel.mention}.", ephemeral=True)

    async def create_or_update_management_panel(self, interaction: nextcord.Interaction) -> nextcord.Message:
        saved = await self.get_saved_management_panel(interaction.guild_id)
        view = self.create_management_view()
        if saved is not None:
            message = await self.fetch_message(saved["channel_id"], saved["message_id"])
            if message is not None:
                await message.edit(content=None, embed=self.service.build_management_embed(), view=view)
                await self.save_management_panel(interaction.guild_id, message.channel.id, message.id)
                return message

        message = await interaction.channel.send(embed=self.service.build_management_embed(), view=view)
        await self.save_management_panel(interaction.guild_id, message.channel.id, message.id)
        return message

    def create_management_view(self, show_admin_back: bool = False) -> MessagesPanelView:
        return MessagesPanelView(self, show_admin_back=show_admin_back)

    async def save_management_panel(self, guild_id: int, channel_id: int, message_id: int) -> None:
        await self.bot.db.save_persistent_view(
            guild_id,
            MODULE_NAME,
            channel_id,
            message_id,
            MANAGEMENT_VIEW_TYPE,
            state={},
        )

    async def get_saved_management_panel(self, guild_id: int):
        return await self.bot.db.fetchone(
            """
            SELECT channel_id, message_id
            FROM persistent_views
            WHERE guild_id = ? AND module_name = ? AND view_type = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (guild_id, MODULE_NAME, MANAGEMENT_VIEW_TYPE),
        )

    async def restore_management_panels(self) -> None:
        rows = await self.bot.db.fetchall(
            """
            SELECT channel_id, message_id
            FROM persistent_views
            WHERE module_name = ? AND view_type = ?
            """,
            (MODULE_NAME, MANAGEMENT_VIEW_TYPE),
        )
        restored = 0
        for row in rows:
            message = await self.fetch_message(row["channel_id"], row["message_id"])
            if message is None:
                self.logger.warning("Messages panel message %s could not be found; removing stale database record", row["message_id"])
                await self.bot.db.delete_persistent_view(row["message_id"])
                continue
            self.bot.add_view(self.create_management_view(), message_id=row["message_id"])
            restored += 1
        self.logger.info("Restored %s messages management panel view(s)", restored)

    async def fetch_message(self, channel_id: int, message_id: int) -> nextcord.Message | None:
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except nextcord.HTTPException:
                return None
        if not hasattr(channel, "fetch_message"):
            return None
        try:
            return await channel.fetch_message(message_id)
        except (nextcord.NotFound, nextcord.Forbidden, nextcord.HTTPException):
            return None


def setup(bot: commands.Bot) -> None:
    bot.add_cog(MessagesCog(bot))


async def attachment_to_file(attachment: nextcord.Attachment, spoiler: bool) -> nextcord.File:
    try:
        return await attachment.to_file(spoiler=spoiler)
    except TypeError:
        file = await attachment.to_file()
        if spoiler and not file.filename.startswith("SPOILER_"):
            file.filename = f"SPOILER_{file.filename}"
        return file
