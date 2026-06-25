import logging
from collections import deque

import nextcord
from nextcord.ext import commands


MODULE_NAME = "autopublish"
MAX_TRACKED_MESSAGES = 1000


class AutopublishCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.logger = logging.getLogger(__name__)
        self._seen_message_ids: set[int] = set()
        self._seen_message_order: deque[int] = deque()

    async def autopublish_message(self, message: nextcord.Message) -> None:
        if message.guild is None:
            self.logger.debug("Skipping announcement autopublish for DM message %s", message.id)
            return

        bot_user_id = self.bot.user.id if self.bot.user is not None else None
        is_own_bot_message = bot_user_id is not None and message.author.id == bot_user_id

        if message.author.bot and not is_own_bot_message:
            self.logger.debug(
                "Skipping announcement autopublish for another bot message %s from author %s",
                message.id,
                message.author.id,
            )
            return

        if message.id in self._seen_message_ids:
            return

        channel = message.channel
        if not isinstance(channel, nextcord.TextChannel):
            return

        if channel.type != nextcord.ChannelType.news:
            return

        if not await self.is_enabled(message.guild.id):
            return

        self.track_message(message.id)

        try:
            await message.publish()
            if is_own_bot_message:
                self.logger.info(
                    "Published own bot announcement message %s in guild %s channel %s",
                    message.id,
                    message.guild.id,
                    channel.id,
                )
            self.logger.info(
                "Published announcement message %s in guild %s channel %s",
                message.id,
                message.guild.id,
                channel.id,
            )
            await self.bot.db.log_action(
                message.guild.id,
                MODULE_NAME,
                "message_published",
                target_id=message.id,
                details={"channel_id": channel.id, "author_id": message.author.id},
            )
        except nextcord.HTTPException as exc:
            self.logger.exception("Failed to publish announcement message %s", message.id)
            await self.bot.db.log_action(
                message.guild.id,
                MODULE_NAME,
                "publish_failed",
                target_id=message.id,
                details={"channel_id": channel.id, "error": str(exc)},
            )

    async def is_enabled(self, guild_id: int) -> bool:
        settings = await self.bot.db.get_module_settings(guild_id, MODULE_NAME)
        if settings is None:
            return True
        return bool(settings["enabled"])

    async def set_enabled(self, guild_id: int, enabled: bool, user_id: int | None = None) -> None:
        await self.bot.db.upsert_module_settings(
            guild_id,
            MODULE_NAME,
            enabled=enabled,
            settings={},
        )
        await self.bot.db.log_action(
            guild_id,
            MODULE_NAME,
            "autopublish_toggled",
            user_id=user_id,
            details={"enabled": enabled},
        )

    def track_message(self, message_id: int) -> None:
        self._seen_message_ids.add(message_id)
        self._seen_message_order.append(message_id)
        if len(self._seen_message_ids) <= MAX_TRACKED_MESSAGES:
            return

        while len(self._seen_message_ids) > MAX_TRACKED_MESSAGES:
            old_message_id = self._seen_message_order.popleft()
            self._seen_message_ids.discard(old_message_id)


def setup(bot: commands.Bot) -> None:
    cog = AutopublishCog(bot)
    bot.add_cog(cog)
    bot.listen("on_message")(cog.autopublish_message)
