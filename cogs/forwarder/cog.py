import logging

import nextcord
from nextcord.ext import commands

from core.config import get_development_guild_ids
from core.permissions import require_guild_manager

from .service import MANAGEMENT_VIEW_TYPE, MODULE_NAME, ForwarderService
from .views import ForwarderPanelView


class ForwarderCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.logger = logging.getLogger(__name__)
        self.service = ForwarderService(bot)
        self._restored = False

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._restored:
            return
        await self.service.init_defaults()
        await self.restore_management_panels()
        self._restored = True

    @commands.Cog.listener()
    async def on_message(self, message: nextcord.Message) -> None:
        if message.guild is None:
            self.logger.debug("Skipping forwarder message %s because it is not from a guild", message.id)
            return

        bot_user_id = self.bot.user.id if self.bot.user is not None else None
        if bot_user_id is not None and message.author.id == bot_user_id:
            self.logger.debug("Skipping forwarder message %s because it was sent by this bot", message.id)
            return

        if not await self.service.is_enabled(message.guild.id):
            return

        source = await self.service.get_source_for_channel(message.guild.id, message.channel.id)
        if source is None:
            self.logger.debug(
                "Skipping forwarder message %s because channel %s is not a configured source in guild %s",
                message.id,
                message.channel.id,
                message.guild.id,
            )
            return

        targets = await self.service.get_targets_for_type(source["type_name"])
        if not targets:
            return

        content, embeds = self.service.build_forward_payload(message)
        if content is None and not embeds:
            return

        for target in targets:
            if target["guild_id"] == message.guild.id and target["channel_id"] == message.channel.id:
                continue
            channel = await self.fetch_sendable_channel(target["channel_id"])
            if channel is None:
                await self.log_forward_error(message, target, "target channel not found")
                continue
            try:
                sent = await channel.send(
                    content=content,
                    embeds=embeds[:10],
                    allowed_mentions=self.service.safe_allowed_mentions(),
                )
                await self.service.log(
                    message.guild.id,
                    "forward_message",
                    target_id=sent.id,
                    details=self.forward_details(message, target),
                )
            except nextcord.HTTPException as exc:
                self.logger.exception("Failed to forward message %s to channel %s", message.id, target["channel_id"])
                await self.log_forward_error(message, target, str(exc))

    @nextcord.slash_command(
        name="forwarder-panel",
        description="Create or update the forwarder management panel.",
        guild_ids=get_development_guild_ids(),
    )
    async def forwarder_panel(self, interaction: nextcord.Interaction) -> None:
        if not await require_guild_manager(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await self.service.init_defaults()
        message = await self.create_or_update_management_panel(interaction)
        await interaction.followup.send(f"Forwarder panel ready in {message.channel.mention}.", ephemeral=True)

    async def create_or_update_management_panel(self, interaction: nextcord.Interaction) -> nextcord.Message:
        saved = await self.service.get_panel(interaction.guild_id)
        view = self.create_management_view()
        if saved is not None:
            message = await self.fetch_message(saved["channel_id"], saved["message_id"])
            if message is not None:
                await message.edit(content=None, embed=self.service.build_management_embed(), view=view)
                await self.service.save_panel(interaction.guild_id, message.channel.id, message.id)
                return message

        message = await interaction.channel.send(embed=self.service.build_management_embed(), view=view)
        await self.service.save_panel(interaction.guild_id, message.channel.id, message.id)
        return message

    def create_management_view(self, show_admin_back: bool = False) -> ForwarderPanelView:
        return ForwarderPanelView(self, show_admin_back=show_admin_back)

    async def restore_management_panels(self) -> None:
        rows = await self.bot.db.fetchall(
            """
            SELECT guild_id, channel_id, message_id
            FROM persistent_views
            WHERE module_name = ? AND view_type = ?
            """,
            (MODULE_NAME, MANAGEMENT_VIEW_TYPE),
        )
        restored = 0
        for row in rows:
            message = await self.fetch_message(row["channel_id"], row["message_id"])
            if message is None:
                self.logger.warning("Forwarder panel message %s could not be found; removing stale database record", row["message_id"])
                await self.bot.db.delete_persistent_view(row["message_id"])
                await self.bot.db.execute(
                    "DELETE FROM forwarder_panels WHERE guild_id = ? AND message_id = ?",
                    (row["guild_id"], row["message_id"]),
                )
                continue
            self.bot.add_view(self.create_management_view(), message_id=row["message_id"])
            restored += 1
        self.logger.info("Restored %s forwarder management panel view(s)", restored)

    async def fetch_sendable_channel(self, channel_id: int):
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except nextcord.HTTPException:
                return None
        return channel if hasattr(channel, "send") else None

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

    async def log_forward_error(self, message: nextcord.Message, target, error: str) -> None:
        await self.service.log(
            message.guild.id,
            "forward_error",
            target_id=message.id,
            details={**self.forward_details(message, target), "error": error},
        )

    def forward_details(self, message: nextcord.Message, target) -> dict:
        return {
            "source_guild_id": message.guild.id,
            "source_channel_id": message.channel.id,
            "target_guild_id": target["guild_id"],
            "target_channel_id": target["channel_id"],
            "type_name": target["type_name"],
            "message_id": message.id,
        }


def setup(bot: commands.Bot) -> None:
    bot.add_cog(ForwarderCog(bot))
