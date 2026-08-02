import asyncio
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
        self._announcement_locks: dict[int, asyncio.Lock] = {}
        self._public_refresh_locks: dict[int, asyncio.Lock] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._recent_code_ids_by_guild: dict[int, frozenset[int]] = {}
        self._manual_notification_guilds: set[int] = set()
        self.expire_live_codes.start()

    def cog_unload(self) -> None:
        self.expire_live_codes.cancel()
        for task in tuple(self._background_tasks):
            task.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._restored:
            return
        await self.restore_management_panels()
        self._restored = True
        self._track_background_task(
            asyncio.create_task(
                self.refresh_all_public_lists(),
                name="live-code-refresh-all-public-lists",
            )
        )

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
            view = self.create_management_view()
            await message.edit(
                content=None,
                embed=self.service.build_management_embed(),
                view=view,
            )
            self.bot.add_view(view, message_id=row["message_id"])
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
        if result.added or result.updated:
            # New-code announcements are sent manually from the management
            # panel. Adding codes only refreshes the public list.
            self.schedule_code_sync(
                guild_id,
                result.items,
                announce=False,
            )
        return result

    async def add_live_code_items(
        self,
        guild_id: int,
        items: list[dict | str],
        user_id: int | None = None,
        announce: bool = True,
        background_sync: bool = False,
    ):
        result = await self.service.add_code_items(guild_id, items, user_id=user_id)

        # A request containing only duplicates must not touch Discord at all.
        if result.added or result.updated:
            # SLAHub only updates the public list. A manager decides when the
            # current NEW codes should be announced by pressing the button.
            should_announce = False
            if background_sync:
                self.schedule_code_sync(guild_id, result.items, announce=should_announce)
            else:
                await self.sync_code_changes(guild_id, result.items, announce=should_announce)
        return result

    def schedule_code_sync(self, guild_id: int, items: list[dict], announce: bool) -> None:
        task = asyncio.create_task(
            self.sync_code_changes(guild_id, items, announce=announce),
            name=f"live-code-discord-sync:{guild_id}",
        )
        self._track_background_task(task)

    def schedule_public_refresh(self, guild_id: int) -> asyncio.Task:
        task = asyncio.create_task(
            self._refresh_public_list_locked(guild_id),
            name=f"live-code-public-refresh:{guild_id}",
        )
        self._track_background_task(task)
        return task

    def _track_background_task(self, task: asyncio.Task) -> None:
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        task.add_done_callback(self._log_background_task_result)

    def _log_background_task_result(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            self.logger.exception("Background live-code Discord sync failed")

    async def sync_code_changes(self, guild_id: int, items: list[dict], announce: bool) -> None:
        # Announcements are serialized separately from public-list edits.
        # A long Discord rate limit on message editing therefore cannot block
        # the next new-code announcement.
        if announce and items:
            lock = self._announcement_locks.setdefault(guild_id, asyncio.Lock())
            async with lock:
                await self.send_new_code_announcement(guild_id, items)
        self.schedule_public_refresh(guild_id)

    async def _refresh_public_list_locked(self, guild_id: int) -> nextcord.Message | None:
        lock = self._public_refresh_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            return await self.refresh_public_list(guild_id)

    async def remove_live_code(self, guild_id: int, code_or_id: str, user_id: int | None) -> bool:
        removed = await self.service.remove_code(guild_id, code_or_id, user_id)
        if removed:
            self.schedule_public_refresh(guild_id)
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
        recent_ids = self.service.recent_code_ids(codes, hours=24)
        embed = self.service.build_public_embed(codes)
        message = None
        if panel.message_id is not None:
            message = await self.fetch_message(panel.channel_id, panel.message_id)
            if message is not None:
                current_embed = self._normalized_embed_dict(message.embeds[0]) if len(message.embeds) == 1 else None
                next_embed = self._normalized_embed_dict(embed)
                if current_embed != next_embed:
                    await message.edit(embed=embed, allowed_mentions=nextcord.AllowedMentions.none())
                else:
                    self.logger.debug(
                        "Live code public list for guild %s is already current; skipping Discord edit",
                        guild_id,
                    )

        if message is None:
            message = await channel.send(embed=embed, allowed_mentions=nextcord.AllowedMentions.none())

        await self.service.save_public_message(guild_id, message.channel.id, message.id)
        self._recent_code_ids_by_guild[guild_id] = recent_ids
        return message

    @staticmethod
    def _normalized_embed_dict(embed: nextcord.Embed) -> dict:
        payload = dict(embed.to_dict())
        payload.pop("type", None)
        return payload

    async def refresh_all_public_lists(self) -> None:
        panels = await self.service.list_public_panels()
        for panel in panels:
            await self._refresh_public_list_locked(panel.guild_id)

    async def queue_new_code_notification(
        self,
        guild_id: int,
        user_id: int | None,
    ) -> tuple[str, int, list[str]]:
        settings = await self.service.get_settings(guild_id)
        if settings is None or settings["announcement_channel_id"] is None:
            return "not_configured", 0, []
        if guild_id in self._manual_notification_guilds:
            return "busy", 0, []

        codes = await self.service.list_active_codes(guild_id)
        recent_codes = self.service.list_recent_codes(codes, hours=24)
        if not recent_codes:
            return "empty", 0, []

        missing_reward_codes = [
            str(row["code"])
            for row in recent_codes
            if not str(row["reward"] or "").strip()
        ]
        if missing_reward_codes:
            await self.bot.db.log_action(
                guild_id,
                MODULE_NAME,
                "live_codes_new_notification_blocked_missing_rewards",
                user_id=user_id,
                details={
                    "codes": missing_reward_codes,
                    "count": len(missing_reward_codes),
                },
            )
            return "missing_rewards", len(missing_reward_codes), missing_reward_codes

        items = [dict(row) for row in recent_codes]
        self._manual_notification_guilds.add(guild_id)
        task = asyncio.create_task(
            self._send_manual_new_code_notification(guild_id, items, user_id),
            name=f"live-code-manual-new-notification:{guild_id}",
        )
        self._track_background_task(task)
        await self.bot.db.log_action(
            guild_id,
            MODULE_NAME,
            "live_codes_new_notification_queued",
            user_id=user_id,
            details={
                "codes": [str(item.get("code") or "") for item in items],
                "count": len(items),
            },
        )
        return "queued", len(items), []

    async def _send_manual_new_code_notification(
        self,
        guild_id: int,
        items: list[dict],
        user_id: int | None,
    ) -> None:
        try:
            lock = self._announcement_locks.setdefault(guild_id, asyncio.Lock())
            async with lock:
                await self.send_new_code_announcement(
                    guild_id,
                    items,
                    requested_by_user_id=user_id,
                )
        finally:
            self._manual_notification_guilds.discard(guild_id)

    async def send_new_code_announcement(
        self,
        guild_id: int,
        items: list[dict] | None = None,
        requested_by_user_id: int | None = None,
    ) -> bool:
        settings = await self.service.get_settings(guild_id)
        if settings is None or settings["announcement_channel_id"] is None:
            return False
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
            return False
        role_id = panel.role_id if panel else None
        content = f"<@&{role_id}>" if role_id else None
        allowed_mentions = nextcord.AllowedMentions.none()
        if role_id:
            allowed_mentions = nextcord.AllowedMentions(roles=[nextcord.Object(id=role_id)], everyone=False, users=False)
        try:
            message = await channel.send(
                content=content,
                embed=self.service.build_announcement_embed(panel.channel_id if panel else None, items),
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
            return False
        await self.bot.db.log_action(
            guild_id,
            MODULE_NAME,
            "live_codes_announcement_sent",
            user_id=requested_by_user_id,
            target_id=message.id,
            details={
                "guild_id": guild_id,
                "announcement_channel_id": settings["announcement_channel_id"],
                "public_channel_id": panel.channel_id if panel else None,
                "public_message_id": panel.message_id if panel else None,
                "manual": requested_by_user_id is not None,
            },
        )
        return True

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
        # This loop has two jobs:
        # 1. remove expired redeem codes;
        # 2. remove the NEW badge after 24 hours.
        # It only queues a Discord edit when the resulting embed state changed.
        affected_guilds = await self.service.expire_codes()
        panels = await self.service.list_public_panels()
        for panel in panels:
            codes = await self.service.list_active_codes(panel.guild_id)
            recent_ids = self.service.recent_code_ids(codes, hours=24)
            previous_ids = self._recent_code_ids_by_guild.get(panel.guild_id)
            if previous_ids is not None and previous_ids != recent_ids:
                affected_guilds.add(panel.guild_id)

        for guild_id in affected_guilds:
            self.schedule_public_refresh(guild_id)

    @expire_live_codes.before_loop
    async def before_expire_live_codes(self) -> None:
        await self.bot.wait_until_ready()


def setup(bot: commands.Bot) -> None:
    bot.add_cog(LiveCodesCog(bot))
