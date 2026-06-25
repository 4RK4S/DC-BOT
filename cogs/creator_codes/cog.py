import asyncio
import logging

import nextcord
from nextcord.ext import commands, tasks

from core.config import get_development_guild_ids
from core.permissions import require_guild_manager

from .api import CreatorCodeApiServer
from .service import ADMIN_VIEW_TYPE, MODULE_NAME, PUBLIC_VIEW_TYPE, CreatorCodeService
from .views import CreatorCodeAdminPanelView, CreatorCodePublicView


class CreatorCodesCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.logger = logging.getLogger(__name__)
        self.service = CreatorCodeService(bot)
        self.api_server = CreatorCodeApiServer(self)
        self._restored = False
        self.cleanup_creator_codes.start()

    def cog_unload(self) -> None:
        self.cleanup_creator_codes.cancel()
        asyncio.create_task(self.api_server.stop())

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._restored:
            return
        await self.restore_views()
        await self.api_server.start()
        self._restored = True

    @nextcord.slash_command(
        name="creator-code-panel",
        description="Create or update the creator code management panel.",
        guild_ids=get_development_guild_ids(),
    )
    async def creator_code_panel(self, interaction: nextcord.Interaction) -> None:
        if not await require_guild_manager(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        message = await self.create_or_update_admin_panel(interaction)
        await interaction.followup.send(f"Creator code panel ready in {message.channel.mention}.", ephemeral=True)

    async def create_or_update_admin_panel(self, interaction: nextcord.Interaction) -> nextcord.Message:
        saved = await self.service.get_admin_panel(interaction.guild_id)
        view = self.create_admin_view()
        if saved is not None:
            message = await self.fetch_message(saved["channel_id"], saved["message_id"])
            if message is not None:
                await message.edit(content=None, embed=self.service.build_admin_embed(), view=view)
                await self.service.save_admin_panel(interaction.guild_id, message.channel.id, message.id)
                return message
            await self.cleanup_stale_admin_panel(saved["guild_id"], saved["message_id"])

        message = await interaction.channel.send(embed=self.service.build_admin_embed(), view=view, allowed_mentions=nextcord.AllowedMentions.none())
        await self.service.save_admin_panel(interaction.guild_id, message.channel.id, message.id)
        return message

    def create_admin_view(self, show_admin_back: bool = False) -> CreatorCodeAdminPanelView:
        return CreatorCodeAdminPanelView(self, show_admin_back=show_admin_back)

    def create_management_view(self, show_admin_back: bool = False) -> CreatorCodeAdminPanelView:
        return self.create_admin_view(show_admin_back=show_admin_back)

    async def restore_views(self) -> None:
        admin_rows = await self.bot.db.fetchall(
            """
            SELECT guild_id, channel_id, message_id
            FROM persistent_views
            WHERE module_name = ? AND view_type = ?
            """,
            (MODULE_NAME, ADMIN_VIEW_TYPE),
        )
        restored_admin = 0
        for row in admin_rows:
            message = await self.fetch_message(row["channel_id"], row["message_id"])
            if message is None:
                await self.cleanup_stale_admin_panel(row["guild_id"], row["message_id"])
                continue
            self.bot.add_view(self.create_admin_view(), message_id=row["message_id"])
            restored_admin += 1

        settings_rows = await self.bot.db.fetchall("SELECT * FROM creator_code_settings WHERE public_channel_id IS NOT NULL")
        restored_public = 0
        for settings in settings_rows:
            message = await self.refresh_public_embed(settings["guild_id"])
            if message is not None:
                restored_public += 1
                await asyncio.sleep(0.03)

        self.logger.info("Restored %s creator code admin view(s) and %s public embed view(s)", restored_admin, restored_public)

    async def refresh_public_embed(self, guild_id: int | None) -> nextcord.Message | None:
        if guild_id is None:
            return None
        settings = await self.service.ensure_settings(guild_id)
        channel_id = settings["public_channel_id"]
        if channel_id is None:
            return None
        channel = await self.fetch_text_channel(channel_id)
        if channel is None:
            await self.service.update_settings(guild_id, public_channel_id=None, public_message_id=None)
            return None
        pools = await self.service.list_active_pools(guild_id)
        embed = self.service.build_public_embed(pools)
        view = CreatorCodePublicView(self, pools)
        message = None
        message_id = settings["public_message_id"]
        if message_id is not None:
            message = await self.fetch_message(channel_id, message_id)
        if message is None:
            message = await channel.send(embed=embed, view=view, allowed_mentions=nextcord.AllowedMentions.none())
        else:
            await message.edit(embed=embed, view=view, allowed_mentions=nextcord.AllowedMentions.none())
        await self.service.update_settings(guild_id, public_channel_id=message.channel.id, public_message_id=message.id)
        await self.bot.db.save_persistent_view(guild_id, MODULE_NAME, message.channel.id, message.id, PUBLIC_VIEW_TYPE, state={})
        self.bot.add_view(view, message_id=message.id)
        return message

    async def send_announcement(self, guild_id: int, pool) -> None:
        settings = await self.service.ensure_settings(guild_id)
        channel_id = settings["announcement_channel_id"]
        if channel_id is None:
            return
        channel = await self.fetch_text_channel(channel_id)
        if channel is None:
            return
        content = None
        allowed_mentions = nextcord.AllowedMentions.none()
        if settings["ping_role_id"]:
            role = nextcord.Object(id=settings["ping_role_id"])
            content = f"<@&{settings['ping_role_id']}>"
            allowed_mentions = nextcord.AllowedMentions(roles=[role], everyone=False, users=False)
        try:
            message = await channel.send(
                content=content,
                embed=self.service.build_announcement_embed(pool, settings["public_channel_id"]),
                allowed_mentions=allowed_mentions,
            )
            await self.service.log(
                guild_id,
                "creator_codes_announcement_sent",
                target_id=message.id,
                details={"guild_id": guild_id, "pool_id": pool["id"], "channel_id": channel.id},
            )
        except nextcord.HTTPException as exc:
            await self.service.log(
                guild_id,
                "creator_codes_error",
                details={"guild_id": guild_id, "pool_id": pool["id"], "error": str(exc)},
            )

    async def cleanup_stale_admin_panel(self, guild_id: int, message_id: int) -> None:
        await self.bot.db.delete_persistent_view(message_id)
        await self.bot.db.execute(
            "DELETE FROM creator_codes_admin_panels WHERE guild_id = ? AND message_id = ?",
            (guild_id, message_id),
        )

    async def fetch_text_channel(self, channel_id: int):
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except nextcord.HTTPException:
                return None
        return channel if isinstance(channel, nextcord.TextChannel) else None

    async def fetch_message(self, channel_id: int, message_id: int | None) -> nextcord.Message | None:
        if message_id is None:
            return None
        channel = await self.fetch_text_channel(channel_id)
        if channel is None:
            return None
        try:
            return await channel.fetch_message(message_id)
        except (nextcord.NotFound, nextcord.Forbidden, nextcord.HTTPException):
            return None

    @tasks.loop(minutes=30)
    async def cleanup_creator_codes(self) -> None:
        await self.bot.wait_until_ready()
        rows = await self.bot.db.fetchall("SELECT guild_id FROM creator_code_settings WHERE public_channel_id IS NOT NULL")
        for row in rows:
            count = await self.service.clear_expired_or_used(row["guild_id"], log_action=False)
            if count:
                await self.refresh_public_embed(row["guild_id"])
                await asyncio.sleep(1)

    @cleanup_creator_codes.before_loop
    async def before_cleanup_creator_codes(self) -> None:
        await self.bot.wait_until_ready()


def setup(bot: commands.Bot) -> None:
    bot.add_cog(CreatorCodesCog(bot))
