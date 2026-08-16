import re
from datetime import datetime, timedelta, timezone

import nextcord

from core.embeds import DEFAULT_COLOR


MODULE_NAME = "Moderation"
DEFAULT_DURATION = "1h"
DEFAULT_MUTE_TEMPLATE = (
    "You have been muted in {server}.\n"
    "Duration: {duration}\n"
    "Until: {until}\n"
    "Reason: {reason}\n"
    "Moderator: {moderator}"
)
DEFAULT_UNMUTE_TEMPLATE = (
    "You have been unmuted in {server}.\n"
    "Reason: {reason}\n"
    "Moderator: {moderator}"
)
MAX_TIMEOUT = timedelta(days=28)
MIN_TIMEOUT = timedelta(seconds=10)
_DURATION_PART = re.compile(r"(\d+)\s*(s|m|h|d|w)", re.IGNORECASE)


def parse_duration(value: str, max_duration: timedelta = MAX_TIMEOUT) -> timedelta:
    raw = str(value or "").strip().lower().replace(",", "")
    if not raw:
        raise ValueError("Duration is required. Examples: 30m, 2h, 3d, 1w, 1d12h.")

    matches = list(_DURATION_PART.finditer(raw))
    if not matches or "".join(match.group(0).replace(" ", "") for match in matches) != raw.replace(" ", ""):
        raise ValueError("Invalid duration. Use s, m, h, d, or w, for example 30m, 2h, 3d, 1w.")

    seconds = 0
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    for match in matches:
        seconds += int(match.group(1)) * multipliers[match.group(2).lower()]

    duration = timedelta(seconds=seconds)
    if duration < MIN_TIMEOUT:
        raise ValueError("Mute duration must be at least 10 seconds.")
    if duration > max_duration:
        if max_duration == MAX_TIMEOUT:
            raise ValueError("Discord timeouts can last at most 28 days.")
        raise ValueError(f"Duration cannot exceed {format_duration(max_duration)}.")
    return duration


def format_duration(duration: timedelta) -> str:
    seconds = max(0, int(duration.total_seconds()))
    parts: list[str] = []
    for suffix, unit in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        amount, seconds = divmod(seconds, unit)
        if amount:
            parts.append(f"{amount}{suffix}")
    return " ".join(parts) or "0s"


def timeout_until(member: nextcord.Member) -> datetime | None:
    value = getattr(member, "communication_disabled_until", None)
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value if value > datetime.now(timezone.utc) else None


class ModerationService:
    def __init__(self, bot) -> None:
        self.bot = bot

    async def get_settings(self, guild_id: int):
        row = await self.bot.db.fetchone(
            "SELECT * FROM moderation_settings WHERE guild_id = ?",
            (guild_id,),
        )
        if row is not None:
            return row
        await self.bot.db.execute(
            """
            INSERT OR IGNORE INTO moderation_settings (
                guild_id, default_duration, notify_by_default,
                mute_dm_template, unmute_dm_template
            ) VALUES (?, ?, 1, ?, ?)
            """,
            (guild_id, DEFAULT_DURATION, DEFAULT_MUTE_TEMPLATE, DEFAULT_UNMUTE_TEMPLATE),
        )
        return await self.bot.db.fetchone(
            "SELECT * FROM moderation_settings WHERE guild_id = ?",
            (guild_id,),
        )

    async def is_enabled(self, guild_id: int) -> bool:
        settings = await self.get_settings(guild_id)
        return bool(settings["enabled"])

    async def set_enabled(self, guild_id: int, enabled: bool, moderator_id: int) -> None:
        await self.get_settings(guild_id)
        await self.bot.db.execute(
            "UPDATE moderation_settings SET enabled = ?, updated_at = CURRENT_TIMESTAMP WHERE guild_id = ?",
            (int(enabled), guild_id),
        )
        await self.bot.db.log_action(
            guild_id, MODULE_NAME, "module_enabled" if enabled else "module_disabled", user_id=moderator_id
        )

    async def update_settings(
        self,
        guild_id: int,
        default_duration: str,
        notify_by_default: bool,
        log_channel_id: int | None,
        mute_template: str,
        unmute_template: str,
        moderator_id: int,
    ) -> None:
        parse_duration(default_duration)
        for label, template in (("Mute", mute_template), ("Unmute", unmute_template)):
            if len(template) > 1800:
                raise ValueError(f"{label} DM template cannot exceed 1800 characters.")
            self._render_template(
                template,
                server="Server",
                duration="1h",
                until="01.01.2030 12:00 UTC",
                reason="Reason",
                moderator="Moderator",
                user="User",
            )
        await self.get_settings(guild_id)
        await self.bot.db.execute(
            """
            UPDATE moderation_settings
            SET default_duration = ?, notify_by_default = ?, log_channel_id = ?,
                mute_dm_template = ?, unmute_dm_template = ?, updated_at = CURRENT_TIMESTAMP
            WHERE guild_id = ?
            """,
            (
                default_duration.strip(), int(notify_by_default), log_channel_id,
                mute_template or DEFAULT_MUTE_TEMPLATE,
                unmute_template or DEFAULT_UNMUTE_TEMPLATE, guild_id,
            ),
        )
        await self.bot.db.log_action(
            guild_id, MODULE_NAME, "settings_updated", user_id=moderator_id,
            details={"log_channel_id": log_channel_id, "notify_by_default": notify_by_default},
        )

    async def update_warn_automation(
        self,
        guild_id: int,
        threshold: int,
        action: str,
        duration: str,
        moderator_id: int,
    ) -> None:
        action = action.strip().lower()
        if threshold < 0 or threshold > 100:
            raise ValueError("Warning threshold must be between 0 and 100. Use 0 to disable it.")
        if action not in {"mute", "kick", "ban"}:
            raise ValueError("Automatic action must be mute, kick, or ban.")
        if action == "mute":
            parse_duration(duration)
        await self.get_settings(guild_id)
        await self.bot.db.execute(
            """
            UPDATE moderation_settings
            SET warn_threshold = ?, warn_action = ?, warn_action_duration = ?, updated_at = CURRENT_TIMESTAMP
            WHERE guild_id = ?
            """,
            (threshold, action, duration.strip() or "1h", guild_id),
        )
        await self.bot.db.log_action(
            guild_id, MODULE_NAME, "warn_automation_updated", user_id=moderator_id,
            details={"threshold": threshold, "action": action, "duration": duration},
        )

    async def update_account_protection(
        self,
        guild_id: int,
        enabled: bool,
        window_seconds: int,
        min_channels: int,
        min_messages: int,
        min_attachments: int,
        moderator_id: int,
    ) -> None:
        if window_seconds < 10 or window_seconds > 600:
            raise ValueError("Detection window must be between 10 and 600 seconds.")
        if min_channels < 2 or min_channels > 20:
            raise ValueError("Minimum channels must be between 2 and 20.")
        if min_messages < 2 or min_messages > 20:
            raise ValueError("Minimum messages must be between 2 and 20.")
        if min_messages < min_channels:
            raise ValueError("Minimum messages cannot be lower than minimum channels.")
        if min_attachments < 2 or min_attachments > 10:
            raise ValueError("Minimum images per message must be between 2 and 10.")
        await self.get_settings(guild_id)
        await self.bot.db.execute(
            """
            UPDATE moderation_settings
            SET account_protection_enabled = ?,
                account_protection_window_seconds = ?,
                account_protection_min_channels = ?,
                account_protection_min_messages = ?,
                account_protection_min_attachments = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE guild_id = ?
            """,
            (int(enabled), window_seconds, min_channels, min_messages, min_attachments, guild_id),
        )
        await self.bot.db.log_action(
            guild_id, MODULE_NAME, "account_protection_updated", user_id=moderator_id,
            details={
                "enabled": enabled,
                "window_seconds": window_seconds,
                "min_channels": min_channels,
                "min_messages": min_messages,
                "min_attachments": min_attachments,
            },
        )

    async def update_account_protection_timeout(
        self, guild_id: int, duration: str, moderator_id: int
    ) -> None:
        parse_duration(duration)
        await self.get_settings(guild_id)
        await self.bot.db.execute(
            """
            UPDATE moderation_settings
            SET account_protection_timeout_duration = ?, updated_at = CURRENT_TIMESTAMP
            WHERE guild_id = ?
            """,
            (duration.strip(), guild_id),
        )
        await self.bot.db.log_action(
            guild_id, MODULE_NAME, "account_protection_timeout_updated",
            user_id=moderator_id, details={"duration": duration.strip()},
        )

    async def update_account_protection_alerts(
        self,
        guild_id: int,
        channel_id: int | None,
        role_id: int | None,
        user_id: int | None,
        moderator_id: int,
    ) -> None:
        await self.get_settings(guild_id)
        await self.bot.db.execute(
            """
            UPDATE moderation_settings
            SET account_protection_alert_channel_id = ?,
                account_protection_alert_role_id = ?,
                account_protection_alert_user_id = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE guild_id = ?
            """,
            (channel_id, role_id, user_id, guild_id),
        )
        await self.bot.db.log_action(
            guild_id,
            MODULE_NAME,
            "account_protection_alerts_updated",
            user_id=moderator_id,
            details={"channel_id": channel_id, "role_id": role_id, "alert_user_id": user_id},
        )

    @staticmethod
    def validate_actor(actor: nextcord.Member) -> None:
        permissions = actor.guild_permissions
        if not (permissions.administrator or permissions.moderate_members):
            raise ValueError("You need Administrator or Moderate Members permission.")

    @staticmethod
    def validate_target(actor: nextcord.Member, target: nextcord.Member, require_bot_moderate: bool = True) -> None:
        guild = actor.guild
        if actor.id == target.id:
            raise ValueError("You cannot mute or unmute yourself.")
        if target.id == guild.owner_id:
            raise ValueError("The server owner cannot be moderated.")
        if target.bot:
            raise ValueError("This moderation action is intended for server members, not bots.")
        if actor.id != guild.owner_id and actor.top_role <= target.top_role:
            raise ValueError("Your highest role must be above the target member's highest role.")
        me = guild.me
        if me is None:
            raise ValueError("Could not resolve the bot member in this server.")
        if require_bot_moderate and not (me.guild_permissions.administrator or me.guild_permissions.moderate_members):
            raise ValueError("The bot needs Moderate Members permission.")
        if me.top_role <= target.top_role:
            raise ValueError("The bot's highest role must be above the target member's highest role.")

    async def resolve_member(self, guild: nextcord.Guild, user_id: int) -> nextcord.Member:
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(user_id)
        except (nextcord.NotFound, nextcord.Forbidden, nextcord.HTTPException):
            raise ValueError("Member was not found in this server.") from None

    @staticmethod
    def require_permission(actor: nextcord.Member, permission: str, label: str) -> None:
        if actor.guild_permissions.administrator:
            return
        if not getattr(actor.guild_permissions, permission, False):
            raise ValueError(f"You need {label} permission.")

    @staticmethod
    def validate_bot_permission(guild: nextcord.Guild, permission: str, label: str) -> None:
        me = guild.me
        if me is None or not (
            me.guild_permissions.administrator
            or getattr(me.guild_permissions, permission, False)
        ):
            raise ValueError(f"The bot needs {label} permission.")

    async def mute(
        self,
        actor: nextcord.Member,
        target: nextcord.Member,
        duration_text: str,
        reason: str,
        notify: bool,
        source: str = "command",
        edit: bool = False,
    ) -> tuple[datetime, bool]:
        if not await self.is_enabled(actor.guild.id):
            raise ValueError("The Moderation module is disabled.")
        self.validate_actor(actor)
        self.validate_target(actor, target)
        duration = parse_duration(duration_text)
        active_until = timeout_until(target)
        if edit and active_until is None:
            raise ValueError("This member does not have an active mute to edit.")
        if not edit and active_until is not None:
            raise ValueError("This member is already muted. Use /mute-edit to change it.")

        if edit and not str(reason or "").strip():
            current_case = await self.bot.db.fetchone(
                """
                SELECT reason FROM moderation_cases
                WHERE guild_id = ? AND target_id = ? AND active = 1
                ORDER BY id DESC LIMIT 1
                """,
                (actor.guild.id, target.id),
            )
            reason = current_case["reason"] if current_case is not None else "No reason provided."
        reason = (reason or "No reason provided.").strip()[:500]
        expires_at = datetime.now(timezone.utc) + duration
        try:
            await target.timeout(timeout=expires_at, reason=f"{reason} | Moderator: {actor}")
        except nextcord.Forbidden:
            raise ValueError("Discord denied the timeout. Check the bot role and Moderate Members permission.") from None
        except nextcord.HTTPException:
            raise ValueError("Discord rejected the timeout request.") from None

        if edit:
            await self.bot.db.execute(
                """
                UPDATE moderation_cases
                SET active = 0, updated_at = CURRENT_TIMESTAMP
                WHERE guild_id = ? AND target_id = ? AND active = 1
                """,
                (actor.guild.id, target.id),
            )
        dm_sent = await self._notify(
            target, "mute", notify, reason, format_duration(duration), expires_at, actor
        )
        await self._insert_case(
            actor.guild.id, target.id, actor.id, "mute_edit" if edit else "mute",
            reason, int(duration.total_seconds()), expires_at, True, dm_sent, source,
        )
        await self.bot.db.log_action(
            actor.guild.id, MODULE_NAME, "mute_edit" if edit else "mute",
            user_id=actor.id, target_id=target.id,
            details={"duration": duration_text, "expires_at": expires_at.isoformat(), "reason": reason, "dm_sent": dm_sent, "source": source},
        )
        await self._send_log(actor.guild, target, actor, "Mute edited" if edit else "Member muted", reason, expires_at, dm_sent)
        return expires_at, dm_sent

    async def unmute(
        self,
        actor: nextcord.Member,
        target: nextcord.Member,
        reason: str,
        notify: bool,
        source: str = "command",
    ) -> bool:
        if not await self.is_enabled(actor.guild.id):
            raise ValueError("The Moderation module is disabled.")
        self.validate_actor(actor)
        self.validate_target(actor, target)
        if timeout_until(target) is None:
            raise ValueError("This member does not have an active mute.")
        reason = (reason or "Mute removed by a moderator.").strip()[:500]
        try:
            await target.timeout(timeout=None, reason=f"{reason} | Moderator: {actor}")
        except nextcord.Forbidden:
            raise ValueError("Discord denied the unmute. Check the bot role and permissions.") from None
        except nextcord.HTTPException:
            raise ValueError("Discord rejected the unmute request.") from None

        await self.bot.db.execute(
            """
            UPDATE moderation_cases SET active = 0, updated_at = CURRENT_TIMESTAMP
            WHERE guild_id = ? AND target_id = ? AND active = 1
            """,
            (actor.guild.id, target.id),
        )
        dm_sent = await self._notify(target, "unmute", notify, reason, "—", None, actor)
        await self._insert_case(actor.guild.id, target.id, actor.id, "unmute", reason, None, None, False, dm_sent, source)
        await self.bot.db.log_action(
            actor.guild.id, MODULE_NAME, "unmute", user_id=actor.id, target_id=target.id,
            details={"reason": reason, "dm_sent": dm_sent, "source": source},
        )
        await self._send_log(actor.guild, target, actor, "Member unmuted", reason, None, dm_sent)
        return dm_sent

    async def _insert_case(self, guild_id, target_id, moderator_id, action, reason, duration_seconds, expires_at, active, dm_sent, source) -> int:
        cursor = await self.bot.db.execute(
            """
            INSERT INTO moderation_cases (
                guild_id, target_id, moderator_id, action, reason, duration_seconds,
                expires_at, active, dm_sent, source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (guild_id, target_id, moderator_id, action, reason, duration_seconds,
             expires_at.isoformat() if expires_at else None, int(active), int(dm_sent), source),
        )
        return int(cursor.lastrowid)

    async def warn(self, actor, target, reason: str, notify: bool, source: str = "command") -> tuple[int, int, bool]:
        if not await self.is_enabled(actor.guild.id):
            raise ValueError("The Moderation module is disabled.")
        self.validate_actor(actor)
        self.validate_target(actor, target, require_bot_moderate=False)
        reason = (reason or "No reason provided.").strip()[:500]
        dm_sent = await self.notify_custom(
            target, notify,
            f"You received a warning in **{target.guild.name}**.\nReason: {reason}\nModerator: {actor}",
        )
        case_id = await self._insert_case(
            actor.guild.id, target.id, actor.id, "warn", reason, None, None, True, dm_sent, source
        )
        row = await self.bot.db.fetchone(
            "SELECT COUNT(*) AS count FROM moderation_cases WHERE guild_id = ? AND target_id = ? AND action = 'warn' AND active = 1",
            (actor.guild.id, target.id),
        )
        count = int(row["count"] if row else 0)
        await self.bot.db.log_action(
            actor.guild.id, MODULE_NAME, "warn", user_id=actor.id, target_id=target.id,
            details={"case_id": case_id, "reason": reason, "warning_count": count, "dm_sent": dm_sent, "source": source},
        )
        await self._send_log(actor.guild, target, actor, f"Warning #{case_id}", reason, None, dm_sent)
        return case_id, count, dm_sent

    async def edit_case_reason(self, actor, case_id: int, reason: str, warning_only: bool = False) -> None:
        self.validate_actor(actor)
        row = await self.get_case(actor.guild.id, case_id)
        if warning_only and row["action"] != "warn":
            raise ValueError("That case is not a warning.")
        reason = reason.strip()[:500]
        if not reason:
            raise ValueError("Reason cannot be empty.")
        await self.bot.db.execute(
            "UPDATE moderation_cases SET reason = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND guild_id = ?",
            (reason, case_id, actor.guild.id),
        )
        await self.bot.db.log_action(
            actor.guild.id, MODULE_NAME, "case_reason_edited", user_id=actor.id,
            target_id=row["target_id"], details={"case_id": case_id, "reason": reason},
        )

    async def remove_warning(self, actor, case_id: int, reason: str) -> None:
        self.validate_actor(actor)
        row = await self.get_case(actor.guild.id, case_id)
        if row["action"] != "warn":
            raise ValueError("That case is not a warning.")
        if not row["active"]:
            raise ValueError("That warning is already inactive.")
        await self.bot.db.execute(
            "UPDATE moderation_cases SET active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND guild_id = ?",
            (case_id, actor.guild.id),
        )
        await self.bot.db.log_action(
            actor.guild.id, MODULE_NAME, "warning_removed", user_id=actor.id,
            target_id=row["target_id"], details={"case_id": case_id, "reason": reason[:500]},
        )

    async def kick(self, actor, target, reason: str, notify: bool, source: str = "command") -> tuple[int, bool]:
        self.require_permission(actor, "kick_members", "Kick Members")
        self.validate_target(actor, target, require_bot_moderate=False)
        self.validate_bot_permission(actor.guild, "kick_members", "Kick Members")
        reason = (reason or "No reason provided.").strip()[:500]
        dm_sent = await self.notify_custom(
            target, notify,
            f"You have been kicked from **{target.guild.name}**.\nReason: {reason}\nModerator: {actor}",
        )
        try:
            await target.kick(reason=f"{reason} | Moderator: {actor}")
        except nextcord.Forbidden:
            raise ValueError("Discord denied the kick. Check the bot role and permissions.") from None
        except nextcord.HTTPException:
            raise ValueError("Discord rejected the kick request.") from None
        case_id = await self._insert_case(actor.guild.id, target.id, actor.id, "kick", reason, None, None, False, dm_sent, source)
        await self._record_simple_action(actor, target, "kick", case_id, reason, dm_sent, source)
        return case_id, dm_sent

    async def ban(self, actor, target, reason: str, notify: bool, duration_text: str | None = None, delete_seconds: int = 0, source: str = "command") -> tuple[int, datetime | None, bool]:
        guild = actor.guild
        self.require_permission(actor, "ban_members", "Ban Members")
        self.validate_bot_permission(guild, "ban_members", "Ban Members")
        if isinstance(target, nextcord.Member):
            self.validate_target(actor, target, require_bot_moderate=False)
        if target.id == actor.id or target.id == guild.owner_id:
            raise ValueError("That user cannot be banned by this command.")
        reason = (reason or "No reason provided.").strip()[:500]
        expires_at = None
        duration_seconds = None
        action = "ban"
        if duration_text:
            duration = parse_duration(duration_text, max_duration=timedelta(days=3650))
            expires_at = datetime.now(timezone.utc) + duration
            duration_seconds = int(duration.total_seconds())
            action = "tempban"
        dm_sent = await self.notify_custom(
            target, notify,
            f"You have been {'temporarily ' if expires_at else ''}banned from **{guild.name}**.\n"
            f"Reason: {reason}\n"
            + (f"Until: {expires_at.strftime('%d.%m.%Y %H:%M UTC')}\n" if expires_at else "")
            + f"Moderator: {actor}",
        )
        try:
            await guild.ban(target, reason=f"{reason} | Moderator: {actor}", delete_message_seconds=delete_seconds)
        except nextcord.Forbidden:
            raise ValueError("Discord denied the ban. Check the bot role and permissions.") from None
        except nextcord.HTTPException:
            raise ValueError("Discord rejected the ban request.") from None
        case_id = await self._insert_case(
            guild.id, target.id, actor.id, action, reason, duration_seconds, expires_at,
            True, dm_sent, source,
        )
        await self._record_simple_action(actor, target, action, case_id, reason, dm_sent, source, expires_at)
        return case_id, expires_at, dm_sent

    async def unban(self, actor, user_id: int, reason: str, notify: bool, source: str = "command") -> tuple[int, bool]:
        guild = actor.guild
        self.require_permission(actor, "ban_members", "Ban Members")
        self.validate_bot_permission(guild, "ban_members", "Ban Members")
        try:
            user = await self.bot.fetch_user(user_id)
            await guild.fetch_ban(user)
        except nextcord.NotFound:
            raise ValueError("This user is not banned on the server.") from None
        except (nextcord.Forbidden, nextcord.HTTPException):
            raise ValueError("Could not verify that ban.") from None
        reason = (reason or "Ban removed by a moderator.").strip()[:500]
        try:
            await guild.unban(user, reason=f"{reason} | Moderator: {actor}")
        except (nextcord.Forbidden, nextcord.HTTPException):
            raise ValueError("Discord rejected the unban request.") from None
        await self.bot.db.execute(
            "UPDATE moderation_cases SET active = 0, updated_at = CURRENT_TIMESTAMP WHERE guild_id = ? AND target_id = ? AND action IN ('ban', 'tempban') AND active = 1",
            (guild.id, user_id),
        )
        dm_sent = await self.notify_custom(
            user, notify, f"You have been unbanned from **{guild.name}**.\nReason: {reason}\nModerator: {actor}"
        )
        case_id = await self._insert_case(guild.id, user_id, actor.id, "unban", reason, None, None, False, dm_sent, source)
        await self._record_simple_action(actor, user, "unban", case_id, reason, dm_sent, source)
        return case_id, dm_sent

    async def get_case(self, guild_id: int, case_id: int):
        row = await self.bot.db.fetchone(
            "SELECT * FROM moderation_cases WHERE guild_id = ? AND id = ?", (guild_id, case_id)
        )
        if row is None:
            raise ValueError("Moderation case was not found on this server.")
        return row

    async def notify_custom(self, target, notify: bool, content: str) -> bool:
        if not notify:
            return False
        try:
            await target.send(content[:1900], allowed_mentions=nextcord.AllowedMentions.none())
            return True
        except (nextcord.Forbidden, nextcord.HTTPException):
            return False

    async def _record_simple_action(self, actor, target, action, case_id, reason, dm_sent, source, expires_at=None) -> None:
        await self.bot.db.log_action(
            actor.guild.id, MODULE_NAME, action, user_id=actor.id, target_id=target.id,
            details={"case_id": case_id, "reason": reason, "dm_sent": dm_sent, "source": source,
                     "expires_at": expires_at.isoformat() if expires_at else None},
        )
        titles = {"kick": "Member kicked", "ban": "Member banned", "tempban": "Member temporarily banned", "unban": "User unbanned"}
        await self._send_log(actor.guild, target, actor, f"{titles.get(action, action)} — Case #{case_id}", reason, expires_at, dm_sent)

    async def process_expired_tempbans(self) -> int:
        rows = await self.bot.db.fetchall(
            "SELECT * FROM moderation_cases WHERE action = 'tempban' AND active = 1 AND expires_at <= ?",
            (datetime.now(timezone.utc).isoformat(),),
        )
        completed = 0
        for row in rows:
            guild = self.bot.get_guild(row["guild_id"])
            if guild is None:
                continue
            try:
                user = await self.bot.fetch_user(row["target_id"])
                await guild.unban(user, reason=f"Temporary ban expired (case #{row['id']})")
            except nextcord.NotFound:
                pass
            except (nextcord.Forbidden, nextcord.HTTPException):
                continue
            await self.bot.db.execute(
                "UPDATE moderation_cases SET active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (row["id"],),
            )
            await self.bot.db.log_action(
                guild.id, MODULE_NAME, "tempban_expired", target_id=row["target_id"], details={"case_id": row["id"]}
            )
            completed += 1
        return completed

    async def build_case_embed(self, guild_id: int, case_id: int) -> nextcord.Embed:
        row = await self.get_case(guild_id, case_id)
        embed = nextcord.Embed(title=f"Moderation Case #{case_id}", color=DEFAULT_COLOR)
        embed.add_field(name="Action", value=row["action"], inline=True)
        embed.add_field(name="Active", value="Yes" if row["active"] else "No", inline=True)
        embed.add_field(name="DM", value="Sent" if row["dm_sent"] else "Not sent", inline=True)
        embed.add_field(name="Member", value=f"<@{row['target_id']}> (`{row['target_id']}`)", inline=False)
        embed.add_field(name="Moderator", value=f"<@{row['moderator_id']}> (`{row['moderator_id']}`)", inline=False)
        embed.add_field(name="Reason", value=row["reason"], inline=False)
        if row["expires_at"]:
            expires = datetime.fromisoformat(row["expires_at"])
            embed.add_field(name="Expires", value=f"<t:{int(expires.timestamp())}:F>", inline=False)
        embed.set_footer(text=f"Source: {row['source']} • Created: {row['created_at']}")
        return embed

    async def build_member_cases_embed(self, guild_id: int, target_id: int, warnings_only: bool = False) -> nextcord.Embed:
        where = "AND action = 'warn'" if warnings_only else ""
        rows = await self.bot.db.fetchall(
            f"SELECT * FROM moderation_cases WHERE guild_id = ? AND target_id = ? {where} ORDER BY id DESC LIMIT 25",
            (guild_id, target_id),
        )
        lines = [
            f"`#{row['id']}` **{row['action']}** — {'active' if row['active'] else 'closed'}\n↳ {row['reason'][:180]}"
            for row in rows
        ]
        title = "Warnings" if warnings_only else "Moderation Cases"
        return nextcord.Embed(title=f"{title} — {target_id}", description="\n".join(lines) or "No cases found.", color=DEFAULT_COLOR)

    async def _notify(self, target, action, notify, reason, duration, expires_at, actor) -> bool:
        if not notify:
            return False
        settings = await self.get_settings(target.guild.id)
        template = settings["mute_dm_template"] if action == "mute" else settings["unmute_dm_template"]
        until = expires_at.strftime("%d.%m.%Y %H:%M UTC") if expires_at else "—"
        content = self._render_template(
            template, server=target.guild.name, duration=duration, until=until,
            reason=reason, moderator=str(actor), user=str(target),
        )
        try:
            await target.send(content, allowed_mentions=nextcord.AllowedMentions.none())
            return True
        except (nextcord.Forbidden, nextcord.HTTPException):
            return False

    @staticmethod
    def _render_template(template: str, **values: str) -> str:
        try:
            return template.format_map(values)
        except KeyError as exc:
            raise ValueError(f"Unknown template placeholder: {{{exc.args[0]}}}.") from None
        except (ValueError, IndexError):
            raise ValueError("Invalid braces in the notification template.") from None

    async def _send_log(self, guild, target, actor, title, reason, expires_at, dm_sent) -> None:
        settings = await self.get_settings(guild.id)
        channel_id = settings["log_channel_id"]
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if channel is None or not hasattr(channel, "send"):
            return
        embed = nextcord.Embed(title=title, color=DEFAULT_COLOR, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Member", value=f"{target} (`{target.id}`)", inline=False)
        embed.add_field(name="Moderator", value=f"{actor} (`{actor.id}`)", inline=False)
        embed.add_field(name="Reason", value=reason, inline=False)
        if expires_at:
            embed.add_field(name="Until", value=f"<t:{int(expires_at.timestamp())}:F> (<t:{int(expires_at.timestamp())}:R>)", inline=False)
        embed.add_field(name="DM", value="Sent" if dm_sent else "Not sent / unavailable", inline=True)
        try:
            await channel.send(embed=embed, allowed_mentions=nextcord.AllowedMentions.none())
        except (nextcord.Forbidden, nextcord.HTTPException):
            pass

    async def build_panel_embed(self, guild: nextcord.Guild) -> nextcord.Embed:
        settings = await self.get_settings(guild.id)
        now = datetime.now(timezone.utc).isoformat()
        await self.bot.db.execute(
            "UPDATE moderation_cases SET active = 0, updated_at = CURRENT_TIMESTAMP WHERE guild_id = ? AND active = 1 AND action IN ('mute', 'mute_edit') AND expires_at <= ?",
            (guild.id, now),
        )
        active = await self.bot.db.fetchone(
            "SELECT COUNT(*) AS count FROM moderation_cases WHERE guild_id = ? AND active = 1 AND action IN ('mute', 'mute_edit')",
            (guild.id,),
        )
        embed = nextcord.Embed(title="Moderation", color=DEFAULT_COLOR)
        embed.description = "Mute, edit active mutes, unmute members, and review moderation history."
        embed.add_field(name="Status", value="Enabled" if settings["enabled"] else "Disabled", inline=True)
        embed.add_field(name="Active Mutes", value=str(active["count"] if active else 0), inline=True)
        embed.add_field(name="Default Duration", value=settings["default_duration"], inline=True)
        embed.add_field(name="Default DM", value="On" if settings["notify_by_default"] else "Off", inline=True)
        auto_action = "Disabled" if not settings["warn_threshold"] else f"{settings['warn_action']} after {settings['warn_threshold']} warnings"
        embed.add_field(name="Warning Automation", value=auto_action, inline=True)
        protection = (
            f"On • {settings['account_protection_min_messages']} messages / "
            f"{settings['account_protection_min_channels']} channels / "
            f"{settings['account_protection_window_seconds']}s"
            if settings["account_protection_enabled"]
            else "Disabled"
        )
        embed.add_field(name="Account Protection", value=protection, inline=False)
        embed.add_field(
            name="Protection Timeout",
            value=settings["account_protection_timeout_duration"],
            inline=True,
        )
        channel = guild.get_channel(settings["log_channel_id"]) if settings["log_channel_id"] else None
        embed.add_field(name="Log Channel", value=channel.mention if channel else "Not set", inline=True)
        alert_channel = (
            guild.get_channel(settings["account_protection_alert_channel_id"])
            if settings["account_protection_alert_channel_id"]
            else None
        )
        alert_role = (
            guild.get_role(settings["account_protection_alert_role_id"])
            if settings["account_protection_alert_role_id"]
            else None
        )
        alert_user = (
            guild.get_member(settings["account_protection_alert_user_id"])
            if settings["account_protection_alert_user_id"]
            else None
        )
        embed.add_field(
            name="Protection Alerts",
            value=(
                f"Channel: {alert_channel.mention if alert_channel else 'Log/System fallback'}\n"
                f"Role ping: {alert_role.mention if alert_role else 'None'}\n"
                f"Admin user: {alert_user.mention if alert_user else 'None'}"
            ),
            inline=False,
        )
        embed.add_field(name="Duration Format", value="`30m`, `2h`, `3d`, `1w`, `1d12h` (maximum 28 days)", inline=False)
        return embed

    async def build_active_embed(self, guild: nextcord.Guild) -> nextcord.Embed:
        rows = await self.bot.db.fetchall(
            """
            SELECT * FROM moderation_cases
            WHERE guild_id = ? AND active = 1 AND action IN ('mute', 'mute_edit') AND expires_at > ?
            ORDER BY expires_at ASC LIMIT 20
            """,
            (guild.id, datetime.now(timezone.utc).isoformat()),
        )
        lines = []
        for row in rows:
            expires = datetime.fromisoformat(row["expires_at"])
            lines.append(f"<@{row['target_id']}> (`{row['target_id']}`) — <t:{int(expires.timestamp())}:R>\n↳ {row['reason'][:180]}")
        embed = nextcord.Embed(title="Active Mutes", description="\n".join(lines) or "No active mutes.", color=DEFAULT_COLOR)
        return embed

    async def build_history_embed(self, guild_id: int) -> nextcord.Embed:
        rows = await self.bot.db.fetchall(
            "SELECT * FROM moderation_cases WHERE guild_id = ? ORDER BY id DESC LIMIT 15",
            (guild_id,),
        )
        names = {"mute": "Muted", "mute_edit": "Mute edited", "unmute": "Unmuted"}
        lines = []
        for row in rows:
            created = datetime.fromisoformat(row["created_at"].replace(" ", "T") + "+00:00") if "+" not in row["created_at"] else datetime.fromisoformat(row["created_at"])
            lines.append(
                f"**{names.get(row['action'], row['action'])}** <@{row['target_id']}> by <@{row['moderator_id']}> — <t:{int(created.timestamp())}:R>\n↳ {row['reason'][:160]}"
            )
        return nextcord.Embed(title="Moderation History", description="\n".join(lines) or "No moderation history.", color=DEFAULT_COLOR)
