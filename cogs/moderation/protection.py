import asyncio
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath

import nextcord

from .service import parse_duration, timeout_until


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


@dataclass
class SuspiciousMessage:
    created_at: datetime
    channel_id: int
    message_id: int
    attachment_count: int
    fingerprint: tuple
    message: nextcord.Message


class AccountProtection:
    def __init__(self, cog) -> None:
        self.cog = cog
        self.bot = cog.bot
        self._events: dict[tuple[int, int], deque[SuspiciousMessage]] = defaultdict(deque)
        self._triggered_until: dict[tuple[int, int], datetime] = {}
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}

    async def handle_message(self, message: nextcord.Message) -> None:
        if message.guild is None or not isinstance(message.author, nextcord.Member):
            return
        if message.author.bot or message.webhook_id is not None:
            return
        permissions = message.author.guild_permissions
        if permissions.administrator or permissions.manage_guild or permissions.moderate_members:
            return

        settings = await self.cog.service.get_settings(message.guild.id)
        if not settings["enabled"] or not settings["account_protection_enabled"]:
            return

        image_attachments = [attachment for attachment in message.attachments if self._is_image(attachment)]
        minimum_attachments = int(settings["account_protection_min_attachments"])
        if len(image_attachments) < minimum_attachments:
            return

        now = datetime.now(timezone.utc)
        key = (message.guild.id, message.author.id)
        triggered_until = self._triggered_until.get(key)
        if triggered_until is not None and triggered_until > now:
            if timeout_until(message.author) is not None:
                try:
                    await message.delete()
                except (nextcord.Forbidden, nextcord.NotFound, nextcord.HTTPException):
                    pass
                return
            # A moderator (or Discord) removed the timeout early. Forget the old
            # trigger so fresh spam can be detected and timed out again.
            self._triggered_until.pop(key, None)
            self._events.pop(key, None)

        window_seconds = int(settings["account_protection_window_seconds"])
        events = self._events[key]
        events.append(
            SuspiciousMessage(
                created_at=now,
                channel_id=message.channel.id,
                message_id=message.id,
                attachment_count=len(image_attachments),
                fingerprint=self._fingerprint(message, image_attachments),
                message=message,
            )
        )
        cutoff = now - timedelta(seconds=window_seconds)
        while events and events[0].created_at < cutoff:
            events.popleft()

        minimum_messages = int(settings["account_protection_min_messages"])
        minimum_channels = int(settings["account_protection_min_channels"])
        channel_count = len({event.channel_id for event in events})
        fingerprint_counts = Counter(event.fingerprint for event in events)
        repeated_payload = bool(fingerprint_counts and max(fingerprint_counts.values()) >= 2)
        if len(events) < minimum_messages or channel_count < minimum_channels or not repeated_payload:
            return

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = datetime.now(timezone.utc)
            if self._triggered_until.get(key, datetime.min.replace(tzinfo=timezone.utc)) > now:
                return
            await self._trigger(message.guild, message.author, list(events), settings)
            events.clear()

    async def _trigger(self, guild, member, events, settings) -> None:
        reason = (
            "Automated compromised-account protection: repeated multi-image messages "
            f"in {len({event.channel_id for event in events})} channels within "
            f"{settings['account_protection_window_seconds']} seconds."
        )
        duration_text = settings["account_protection_timeout_duration"]
        duration = parse_duration(duration_text)
        action_succeeded = False
        action_error = None
        dm_sent = False
        expires_at = datetime.now(timezone.utc) + duration

        if timeout_until(member) is None:
            try:
                expires_at, dm_sent = await self.cog.service.mute(
                    guild.me,
                    member,
                    duration_text,
                    reason,
                    True,
                    source="account_protection",
                )
                action_succeeded = True
            except ValueError as exc:
                action_error = str(exc)
        else:
            action_succeeded = True
            expires_at = timeout_until(member)

        deleted = 0
        for event in events:
            try:
                await event.message.delete()
            except (nextcord.Forbidden, nextcord.NotFound, nextcord.HTTPException):
                continue
            deleted += 1

        cooldown = duration if action_succeeded else timedelta(minutes=5)
        self._triggered_until[(guild.id, member.id)] = datetime.now(timezone.utc) + cooldown
        await self.bot.db.log_action(
            guild.id,
            "Moderation",
            "account_protection_triggered",
            user_id=guild.me.id if guild.me else None,
            target_id=member.id,
            details={
                "messages": len(events),
                "channels": len({event.channel_id for event in events}),
                "attachments": sum(event.attachment_count for event in events),
                "deleted": deleted,
                "timeout_duration": duration_text,
                "timeout_succeeded": action_succeeded,
                "timeout_error": action_error,
                "dm_sent": dm_sent,
            },
        )
        await self._send_alert(
            guild, member, events, deleted, expires_at, action_succeeded, action_error, dm_sent
        )

    async def _send_alert(self, guild, member, events, deleted, expires_at, action_succeeded, action_error, dm_sent) -> None:
        settings = await self.cog.service.get_settings(guild.id)
        channel_id = settings["account_protection_alert_channel_id"] or settings["log_channel_id"]
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            channel = guild.system_channel

        alert_role_id = settings["account_protection_alert_role_id"]
        alert_user_id = settings["account_protection_alert_user_id"]

        channel_ids = sorted({event.channel_id for event in events})
        embed = nextcord.Embed(
            title="🚨 Possible Compromised Account Stopped",
            description=(
                "The bot detected repeated multi-image spam across several channels. "
                "Please verify the member's account before removing the timeout."
            ),
            color=nextcord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Member", value=f"{member.mention} (`{member.id}`)", inline=False)
        embed.add_field(name="Messages detected", value=str(len(events)), inline=True)
        embed.add_field(name="Channels", value=str(len(channel_ids)), inline=True)
        embed.add_field(name="Images", value=str(sum(event.attachment_count for event in events)), inline=True)
        embed.add_field(name="Messages deleted", value=str(deleted), inline=True)
        embed.add_field(name="DM", value="Sent" if dm_sent else "Not sent / unavailable", inline=True)
        if action_succeeded and expires_at:
            embed.add_field(
                name="Automatic timeout",
                value=f"Until <t:{int(expires_at.timestamp())}:F> (<t:{int(expires_at.timestamp())}:R>)",
                inline=False,
            )
        else:
            embed.add_field(name="Automatic timeout failed", value=action_error or "Unknown error", inline=False)
        embed.add_field(
            name="Affected channels",
            value=" ".join(f"<#{channel_id}>" for channel_id in channel_ids)[:1024],
            inline=False,
        )
        embed.set_footer(text="Account Protection • Review the moderation case and contact the member")
        mentions = []
        if alert_role_id:
            mentions.append(f"<@&{alert_role_id}>")
        if alert_user_id:
            mentions.append(f"<@{alert_user_id}>")

        if channel is not None and hasattr(channel, "send"):
            try:
                await channel.send(
                    content=" ".join(mentions) or None,
                    embed=embed,
                    allowed_mentions=nextcord.AllowedMentions(
                        everyone=False, roles=True, users=True, replied_user=False
                    ),
                )
            except (nextcord.Forbidden, nextcord.HTTPException):
                pass

        if alert_user_id:
            user = guild.get_member(alert_user_id)
            if user is None:
                try:
                    user = await self.bot.fetch_user(alert_user_id)
                except (nextcord.NotFound, nextcord.HTTPException):
                    user = None
            if user is not None:
                try:
                    await user.send(embed=embed)
                except (nextcord.Forbidden, nextcord.HTTPException):
                    pass

    @staticmethod
    def _is_image(attachment: nextcord.Attachment) -> bool:
        content_type = (attachment.content_type or "").lower()
        if content_type.startswith("image/"):
            return True
        suffix = PurePosixPath(attachment.filename.lower()).suffix
        return suffix in IMAGE_EXTENSIONS

    @staticmethod
    def _fingerprint(message: nextcord.Message, attachments: list[nextcord.Attachment]) -> tuple:
        content = " ".join((message.content or "").lower().split())
        files = tuple(
            sorted(
                (
                    attachment.filename.lower().removeprefix("spoiler_"),
                    int(attachment.size or 0),
                    (attachment.content_type or "").lower(),
                )
                for attachment in attachments
            )
        )
        return content, files
