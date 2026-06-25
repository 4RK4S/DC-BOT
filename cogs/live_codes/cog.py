import logging

import nextcord
from nextcord.ext import commands, tasks

from core.config import get_development_guild_ids
from core.permissions import require_guild_manager

from .service import MANAGEMENT_VIEW_TYPE, MODULE_NAME, LiveCodeService
from .views import LiveCodeManagementView


class LiveCodesCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.logger = logging.getLogger(__name__)
        self.service = LiveCodeService(bot)
        self._restored = False
        self.expire_live_codes.start()

    def cog_unload(self) -> None:
        self.expire_live_codes.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._restored:
            return
        await self.restore_management_panels()
        await self.refresh_all_public_lists()
        self._restored = True

    @nextcord.slash_command(
        name="live-code-panel",
        description="Create or update the live code management panel.",
        guild_ids=get_development_guild_ids(),
    )
    async def live_code_panel(self, interaction: nextcord.Interaction) -> None:
        if not await require_guild_manager(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        message = await self.create_or_update_management_panel(interaction)
        await interaction.followup.send(f"Live code panel ready in {message.channel.mention}.", ephemeral=True)

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

    def create_management_view(self, show_admin_back: bool = False) -> LiveCodeManagementView:
        return LiveCodeManagementView(self, show_admin_back=show_admin_back)

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
                self.logger.warning("Live code panel message %s could not be found; removing stale database record", row["message_id"])
                await self.bot.db.delete_persistent_view(row["message_id"])
                continue
            self.bot.add_view(self.create_management_view(), message_id=row["message_id"])
            restored += 1
        self.logger.info("Restored %s live code management panel view(s)", restored)

    async def add_live_codes(
        self,
        guild_id: int,
        codes: str,
        expires_at: str,
        timezone_name: str,
        user_id: int | None,
    ):
        result = await self.service.add_codes(guild_id, codes, expires_at, timezone_name, user_id)
        await self.refresh_public_list(guild_id)
        if result.added:
            await self.send_new_code_announcement(guild_id)
        return result

    async def remove_live_code(self, guild_id: int, code_or_id: str, user_id: int | None) -> bool:
        removed = await self.service.remove_code(guild_id, code_or_id, user_id)
        if removed:
            await self.refresh_public_list(guild_id)
        return removed

    async def refresh_public_list(self, guild_id: int | None) -> nextcord.Message | None:
        if guild_id is None:
            return None
        panel = await self.service.get_public_panel(guild_id)
        if panel is None:
            return None
        channel = await self.fetch_text_channel(panel.channel_id)
        if channel is None:
            self.logger.warning("Live code public channel %s could not be found; removing stale public panel for guild %s", panel.channel_id, guild_id)
            await self.bot.db.execute("DELETE FROM live_code_panels WHERE guild_id = ?", (guild_id,))
            return None

        codes = await self.service.list_active_codes(guild_id)
        embed = self.service.build_public_embed(codes)
        message = None
        if panel.message_id is not None:
            message = await self.fetch_message(panel.channel_id, panel.message_id)
            if message is not None:
                await message.edit(embed=embed, allowed_mentions=nextcord.AllowedMentions.none())

        if message is None:
            message = await channel.send(embed=embed, allowed_mentions=nextcord.AllowedMentions.none())

        await self.service.save_public_message(guild_id, message.channel.id, message.id)
        return message

    async def refresh_all_public_lists(self) -> None:
        panels = await self.service.list_public_panels()
        for panel in panels:
            await self.refresh_public_list(panel.guild_id)

    async def send_new_code_announcement(self, guild_id: int) -> None:
        settings = await self.service.get_settings(guild_id)
        if settings is None or settings["announcement_channel_id"] is None:
            return
        panel = await self.service.get_public_panel(guild_id)
        channel = await self.fetch_text_channel(settings["announcement_channel_id"])
        if channel is None:
            await self.bot.db.log_action(
                guild_id,
                MODULE_NAME,
                "live_codes_announcement_failed",
                details={
                    "guild_id": guild_id,
                    "announcement_channel_id": settings["announcement_channel_id"],
                    "public_channel_id": panel.channel_id if panel else None,
                    "public_message_id": panel.message_id if panel else None,
                    "error": "announcement channel not found",
                },
            )
            return
        role_id = panel.role_id if panel else None
        content = f"<@&{role_id}>" if role_id else None
        allowed_mentions = nextcord.AllowedMentions.none()
        if role_id:
            allowed_mentions = nextcord.AllowedMentions(roles=[nextcord.Object(id=role_id)], everyone=False, users=False)
        try:
            message = await channel.send(
                content=content,
                embed=self.service.build_announcement_embed(panel.channel_id if panel else None),
                allowed_mentions=allowed_mentions,
            )
        except nextcord.HTTPException as exc:
            await self.bot.db.log_action(
                guild_id,
                MODULE_NAME,
                "live_codes_announcement_failed",
                details={
                    "guild_id": guild_id,
                    "announcement_channel_id": settings["announcement_channel_id"],
                    "public_channel_id": panel.channel_id if panel else None,
                    "public_message_id": panel.message_id if panel else None,
                    "error": str(exc),
                },
            )
            return
        await self.bot.db.log_action(
            guild_id,
            MODULE_NAME,
            "live_codes_announcement_sent",
            target_id=message.id,
            details={
                "guild_id": guild_id,
                "announcement_channel_id": settings["announcement_channel_id"],
                "public_channel_id": panel.channel_id if panel else None,
                "public_message_id": panel.message_id if panel else None,
            },
        )

    async def fetch_text_channel(self, channel_id: int):
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except nextcord.HTTPException:
                return None
        return channel if isinstance(channel, nextcord.TextChannel) else None

    async def fetch_message(self, channel_id: int, message_id: int) -> nextcord.Message | None:
        channel = await self.fetch_text_channel(channel_id)
        if channel is None:
            return None
        try:
            return await channel.fetch_message(message_id)
        except (nextcord.NotFound, nextcord.Forbidden, nextcord.HTTPException):
            return None

    @tasks.loop(minutes=5)
    async def expire_live_codes(self) -> None:
        affected_guilds = await self.service.expire_codes()
        for guild_id in affected_guilds:
            await self.refresh_public_list(guild_id)

    @expire_live_codes.before_loop
    async def before_expire_live_codes(self) -> None:
        await self.bot.wait_until_ready()


def setup(bot: commands.Bot) -> None:
    bot.add_cog(LiveCodesCog(bot))
