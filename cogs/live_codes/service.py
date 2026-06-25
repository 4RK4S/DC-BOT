import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import nextcord

from core.embeds import DEFAULT_COLOR


MODULE_NAME = "live_codes"
MANAGEMENT_VIEW_TYPE = "live_codes_management_panel"
DATE_FORMATS = ("%d.%m.%Y %H:%M", "%d-%m-%Y %H:%M", "%Y-%m-%d %H:%M")
TIMEZONE_CHOICES = ("KST", "PDT", "PST", "UTC+0", "Japan")
TIMEZONE_ERROR = "timezone must be one of: KST, PDT, PST, UTC+0, Japan"


@dataclass(frozen=True)
class LiveCodePanel:
    guild_id: int
    channel_id: int
    message_id: int | None
    role_id: int | None


@dataclass(frozen=True)
class LiveCodeAddResult:
    added: int
    skipped_duplicates: int
    codes: list[str]


class LiveCodeService:
    def __init__(self, bot) -> None:
        self.bot = bot
        self.db = bot.db

    async def add_codes(
        self,
        guild_id: int,
        codes_text: str,
        expires_at: str | None = None,
        expires_timezone: str = "UTC+0",
        user_id: int | None = None,
    ) -> LiveCodeAddResult:
        raw_codes = [code.strip() for code in re.split(r"[,\s]+", codes_text) if code.strip()]
        codes = parse_codes_input(codes_text)
        if not codes:
            raise ValueError("codes must include at least one code")

        expires_iso = (
            parse_expire_to_utc_iso(expires_at, expires_timezone)
            if expires_at and expires_at.strip()
            else None
        )

        added_codes: list[str] = []
        skipped_duplicates = len(raw_codes) - len(codes)
        for code in codes:
            duplicate = await self.db.fetchone(
                """
                SELECT id
                FROM live_codes
                WHERE guild_id = ? AND code = ? AND active = 1
                LIMIT 1
                """,
                (guild_id, code),
            )
            if duplicate is not None:
                skipped_duplicates += 1
                continue

            await self.db.execute(
                """
                INSERT INTO live_codes (
                    guild_id,
                    code,
                    expires_at,
                    active,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (guild_id, code, expires_iso),
            )
            added_codes.append(code)

        await self.db.log_action(
            guild_id,
            MODULE_NAME,
            "live_codes_added",
            user_id=user_id,
            details={
                "codes": added_codes,
                "added": len(added_codes),
                "skipped_duplicates": skipped_duplicates,
                "expires_at": expires_iso,
                "timezone": expires_timezone,
            },
        )
        return LiveCodeAddResult(
            added=len(added_codes),
            skipped_duplicates=skipped_duplicates,
            codes=added_codes,
        )

    async def remove_code(self, guild_id: int, code_or_id: str, user_id: int | None = None) -> bool:
        value = code_or_id.strip()
        if not value:
            raise ValueError("code_or_id must not be empty")

        if value.isdigit():
            cursor = await self.db.execute(
                """
                UPDATE live_codes
                SET active = 0, updated_at = CURRENT_TIMESTAMP
                WHERE guild_id = ? AND id = ? AND active = 1
                """,
                (guild_id, int(value)),
            )
            target = int(value)
        else:
            cursor = await self.db.execute(
                """
                UPDATE live_codes
                SET active = 0, updated_at = CURRENT_TIMESTAMP
                WHERE guild_id = ? AND code = ? AND active = 1
                """,
                (guild_id, value),
            )
            target = None

        removed = cursor.rowcount > 0
        if removed:
            await self.db.log_action(
                guild_id,
                MODULE_NAME,
                "live_code_removed",
                user_id=user_id,
                target_id=target,
                details={"code_or_id": value, "removed": cursor.rowcount},
            )
        return removed

    async def list_active_codes(self, guild_id: int):
        await self.expire_codes(guild_id)
        return await self.db.fetchall(
            """
            SELECT *
            FROM live_codes
            WHERE guild_id = ? AND active = 1
            ORDER BY created_at DESC, id DESC
            """,
            (guild_id,),
        )

    async def list_all_codes(self, guild_id: int):
        await self.expire_codes(guild_id)
        return await self.db.fetchall(
            """
            SELECT *
            FROM live_codes
            WHERE guild_id = ? AND active = 1
            ORDER BY created_at DESC, id DESC
            LIMIT 20
            """,
            (guild_id,),
        )

    async def expire_codes(self, guild_id: int | None = None) -> set[int]:
        now_iso = utc_now_iso()
        if guild_id is None:
            rows = await self.db.fetchall(
                """
                SELECT id, guild_id, code, expires_at
                FROM live_codes
                WHERE active = 1 AND expires_at IS NOT NULL AND expires_at <= ?
                """,
                (now_iso,),
            )
        else:
            rows = await self.db.fetchall(
                """
                SELECT id, guild_id, code, expires_at
                FROM live_codes
                WHERE guild_id = ? AND active = 1 AND expires_at IS NOT NULL AND expires_at <= ?
                """,
                (guild_id, now_iso),
            )

        affected_guilds: set[int] = set()
        for row in rows:
            await self.db.execute(
                """
                UPDATE live_codes
                SET active = 0, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (row["id"],),
            )
            affected_guilds.add(row["guild_id"])
            await self.db.log_action(
                row["guild_id"],
                MODULE_NAME,
                "live_code_expired",
                target_id=row["id"],
                details={"code": row["code"], "expires_at": row["expires_at"]},
            )

        return affected_guilds

    async def set_public_channel(self, guild_id: int, channel_id: int, user_id: int | None = None) -> None:
        current = await self.get_public_panel(guild_id)
        await self.db.execute(
            """
            INSERT INTO live_code_panels (guild_id, channel_id, message_id, role_id, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (guild_id, channel_id, current.message_id if current else None, current.role_id if current else None),
        )
        await self.db.log_action(
            guild_id,
            MODULE_NAME,
            "public_channel_set",
            user_id=user_id,
            details={"channel_id": channel_id},
        )

    async def set_ping_role(self, guild_id: int, role_id: int | None, user_id: int | None = None) -> None:
        current = await self.get_public_panel(guild_id)
        if current is None:
            raise ValueError("Set a public channel before setting a ping role")
        await self.db.execute(
            """
            UPDATE live_code_panels
            SET role_id = ?, updated_at = CURRENT_TIMESTAMP
            WHERE guild_id = ?
            """,
            (role_id, guild_id),
        )
        await self.db.log_action(
            guild_id,
            MODULE_NAME,
            "ping_role_set",
            user_id=user_id,
            details={"role_id": role_id},
        )

    async def set_announcement_channel(self, guild_id: int, channel_id: int, user_id: int | None = None) -> None:
        await self.db.execute(
            """
            INSERT INTO live_code_settings (guild_id, announcement_channel_id, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id) DO UPDATE SET
                announcement_channel_id = excluded.announcement_channel_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (guild_id, channel_id),
        )
        await self.db.log_action(
            guild_id,
            MODULE_NAME,
            "live_codes_set_announcement_channel",
            user_id=user_id,
            details={"guild_id": guild_id, "announcement_channel_id": channel_id},
        )

    async def get_settings(self, guild_id: int):
        return await self.db.fetchone("SELECT * FROM live_code_settings WHERE guild_id = ?", (guild_id,))

    async def save_public_message(self, guild_id: int, channel_id: int, message_id: int) -> None:
        current = await self.get_public_panel(guild_id)
        await self.db.execute(
            """
            INSERT INTO live_code_panels (guild_id, channel_id, message_id, role_id, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                message_id = excluded.message_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (guild_id, channel_id, message_id, current.role_id if current else None),
        )

    async def get_public_panel(self, guild_id: int) -> LiveCodePanel | None:
        row = await self.db.fetchone(
            """
            SELECT *
            FROM live_code_panels
            WHERE guild_id = ?
            """,
            (guild_id,),
        )
        if row is None:
            return None
        return LiveCodePanel(
            guild_id=row["guild_id"],
            channel_id=row["channel_id"],
            message_id=row["message_id"],
            role_id=row["role_id"],
        )

    async def list_public_panels(self) -> list[LiveCodePanel]:
        rows = await self.db.fetchall("SELECT * FROM live_code_panels")
        return [
            LiveCodePanel(
                guild_id=row["guild_id"],
                channel_id=row["channel_id"],
                message_id=row["message_id"],
                role_id=row["role_id"],
            )
            for row in rows
        ]

    def build_public_embed(self, codes) -> nextcord.Embed:
        embed = nextcord.Embed(title="🎁 Live Codes", color=DEFAULT_COLOR)
        if not codes:
            embed.description = "No active live codes."
            return embed

        lines = ["ㅤ"]
        for row in codes[:50]:
            if row["expires_at"]:
                timestamp = discord_timestamp(row["expires_at"])
                lines.append(f"**{row['code']}** — {timestamp}" if timestamp else f"**`{row['code']}`**")
            else:
                lines.append(f"**{row['code']}**")
        lines.append("ㅤ")

        embed.description = "\n".join(lines)
        if len(codes) > 50:
            embed.set_footer(text=f"Showing 50 of {len(codes)} active codes.")
        return embed

    def build_management_embed(self) -> nextcord.Embed:
        embed = nextcord.Embed(
            title="Live Codes",
            description="Manage live stream, social, and announcement redeem codes.",
            color=DEFAULT_COLOR,
        )
        embed.add_field(
            name="Add Codes",
            value="Multiple codes can be added at once. Choose a timezone before using the panel modal.",
            inline=False,
        )
        embed.add_field(name="Navigation", value="Navigation edits this panel message.", inline=False)
        return embed

    async def build_settings_embed(self, guild_id: int) -> nextcord.Embed:
        settings = await self.get_settings(guild_id)
        panel = await self.get_public_panel(guild_id)
        module_settings = await self.db.get_module_settings(guild_id, MODULE_NAME)
        active_count = await self.count_codes(guild_id, active=True)
        inactive_count = await self.count_codes(guild_id, active=False)
        embed = nextcord.Embed(title="Live Codes Settings", color=DEFAULT_COLOR)
        embed.add_field(
            name="Module",
            value="Enabled" if module_settings is None or bool(module_settings["enabled"]) else "Disabled",
            inline=True,
        )
        embed.add_field(
            name="Public Channel",
            value=f"<#{panel.channel_id}> (`{panel.channel_id}`)" if panel else "Not set",
            inline=False,
        )
        embed.add_field(name="Public Message", value=str(panel.message_id if panel and panel.message_id else "Not set"), inline=True)
        announcement_channel_id = settings["announcement_channel_id"] if settings else None
        embed.add_field(
            name="Announcement Channel",
            value=f"<#{announcement_channel_id}> (`{announcement_channel_id}`)" if announcement_channel_id else "Not set",
            inline=False,
        )
        embed.add_field(
            name="Ping Role",
            value=f"<@&{panel.role_id}> (`{panel.role_id}`)" if panel and panel.role_id else "Not set",
            inline=False,
        )
        embed.add_field(name="Active Live Codes", value=str(active_count), inline=True)
        embed.add_field(name="Expired/Inactive Live Codes", value=str(inactive_count), inline=True)
        embed.add_field(name="Timezones", value=", ".join(TIMEZONE_CHOICES), inline=False)
        return embed

    async def count_codes(self, guild_id: int, active: bool) -> int:
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS count FROM live_codes WHERE guild_id = ? AND active = ?",
            (guild_id, int(active)),
        )
        return int(row["count"] if row else 0)

    def build_announcement_embed(self, public_channel_id: int | None) -> nextcord.Embed:
        description = "A new live code has just been added."
        if public_channel_id is not None:
            description += f"\n\nCheck it here: <#{public_channel_id}>"
        else:
            description += "\n\nCheck the live codes panel for details."
        return nextcord.Embed(
            title="🚨 New Live Code Available!",
            description=description,
            color=0xF1C40F,
        )

    def build_code_list_embed(self, rows) -> nextcord.Embed:
        embed = nextcord.Embed(title="Live Code List", color=DEFAULT_COLOR)
        if not rows:
            embed.description = "No active live codes."
            return embed

        for row in rows:
            expires = row["expires_at"] or "Never"
            embed.add_field(
                name=f"#{row['id']} - `{row['code']}`",
                value=f"Expires: `{expires}`",
                inline=False,
            )
        return embed


def parse_codes_input(text: str) -> list[str]:
    seen: set[str] = set()
    codes: list[str] = []
    for raw_code in re.split(r"[,\s]+", text):
        code = raw_code.strip()
        if not code or code in seen:
            continue
        seen.add(code)
        codes.append(code)
    return codes


def parse_expire_to_utc_iso(value: str, timezone_name: str = "UTC+0") -> str:
    clean = value.strip()
    expires_tz = parse_timezone_choice(timezone_name)
    for date_format in DATE_FORMATS:
        try:
            local_dt = datetime.strptime(clean, date_format).replace(tzinfo=expires_tz)
            return local_dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            continue
    raise ValueError(
        "expires_at must use one of these examples: 07.02.2026 08:59, "
        "07-02-2026 08:59, or 2026-02-07 08:59"
    )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def discord_timestamp(iso_value: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_value)
    except ValueError:
        return ""
    return f"<t:{int(dt.timestamp())}:R>"


def parse_timezone_choice(value: str):
    normalized = (value or "UTC+0").strip().upper()
    if normalized == "KST":
        return ZoneInfo("Asia/Seoul")
    if normalized == "JAPAN":
        return ZoneInfo("Asia/Tokyo")
    if normalized == "PST":
        return timezone(timedelta(hours=-8))
    if normalized == "PDT":
        return timezone(timedelta(hours=-7))
    if normalized == "UTC+0":
        return timezone.utc
    raise ValueError(TIMEZONE_ERROR)
