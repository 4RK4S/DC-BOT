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
    updated: int
    skipped_duplicates: int
    codes: list[str]
    items: list[dict]


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
        result = await self.add_code_items(
            guild_id,
            [
                {
                    "code": code,
                    "expires_at": expires_iso,
                    "source": "Discord panel",
                }
                for code in codes
            ],
            user_id=user_id,
        )

        # Preserve duplicate counting from repeated values in the modal input.
        extra_input_duplicates = max(0, len(raw_codes) - len(codes))
        if extra_input_duplicates:
            result = LiveCodeAddResult(
                added=result.added,
                updated=result.updated,
                skipped_duplicates=result.skipped_duplicates + extra_input_duplicates,
                codes=result.codes,
                items=result.items,
            )
        return result

    async def add_code_items(
        self,
        guild_id: int,
        items: list[dict | str],
        user_id: int | None = None,
    ) -> LiveCodeAddResult:
        if not items:
            raise ValueError("items must include at least one code")

        normalized_items: list[dict] = []
        seen: set[str] = set()
        skipped_duplicates = 0
        for raw in items:
            item = raw if isinstance(raw, dict) else {"code": raw}
            code = normalize_live_code(item.get("code"))
            if not code:
                continue
            code_key = code.upper()
            if code_key in seen:
                skipped_duplicates += 1
                continue
            seen.add(code_key)
            normalized_items.append(
                {
                    "code": code,
                    "expires_at": normalize_expire_iso(item.get("expires_at")),
                    "source": normalize_optional_text(item.get("source"), 200),
                    "reward": normalize_optional_text(item.get("reward"), 500),
                    "note": normalize_optional_text(item.get("note"), 500),
                    "source_url": normalize_optional_text(item.get("source_url"), 1000),
                }
            )

        if not normalized_items:
            raise ValueError("items must include at least one non-empty code")

        added_items: list[dict] = []
        updated = 0
        for item in normalized_items:
            duplicate = await self.db.fetchone(
                """
                SELECT id, code, source, reward, note, source_url, expires_at
                FROM live_codes
                WHERE guild_id = ? AND UPPER(code) = ? AND active = 1
                LIMIT 1
                """,
                (guild_id, item["code"].upper()),
            )
            if duplicate is not None:
                skipped_duplicates += 1
                next_source = item["source"] or duplicate["source"]
                next_reward = item["reward"] or duplicate["reward"]
                next_note = item["note"] or duplicate["note"]
                next_source_url = item["source_url"] or duplicate["source_url"]
                next_expires = item["expires_at"] or duplicate["expires_at"]
                changed = any(
                    (next_value or None) != (duplicate[column] or None)
                    for column, next_value in (
                        ("source", next_source),
                        ("reward", next_reward),
                        ("note", next_note),
                        ("source_url", next_source_url),
                        ("expires_at", next_expires),
                    )
                )
                if changed:
                    await self.db.execute(
                        """
                        UPDATE live_codes
                        SET source = ?, reward = ?, note = ?, source_url = ?, expires_at = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (
                            next_source,
                            next_reward,
                            next_note,
                            next_source_url,
                            next_expires,
                            duplicate["id"],
                        ),
                    )
                    updated += 1
                continue

            await self.db.execute(
                """
                INSERT INTO live_codes (
                    guild_id,
                    code,
                    source,
                    reward,
                    note,
                    source_url,
                    expires_at,
                    active,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    guild_id,
                    item["code"],
                    item["source"],
                    item["reward"],
                    item["note"],
                    item["source_url"],
                    item["expires_at"],
                ),
            )
            added_items.append(item)

        await self.db.log_action(
            guild_id,
            MODULE_NAME,
            "live_codes_added",
            user_id=user_id,
            details={
                "codes": [item["code"] for item in added_items],
                "added": len(added_items),
                "updated": updated,
                "skipped_duplicates": skipped_duplicates,
                "sources": sorted({item["source"] for item in added_items if item["source"]}),
            },
        )
        return LiveCodeAddResult(
            added=len(added_items),
            updated=updated,
            skipped_duplicates=skipped_duplicates,
            codes=[item["code"] for item in added_items],
            items=added_items,
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
                WHERE guild_id = ? AND UPPER(code) = ? AND active = 1
                """,
                (guild_id, value.upper()),
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
            ORDER BY created_at ASC, id ASC
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
            ORDER BY created_at ASC, id ASC
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

        recent_codes = self.list_recent_codes(codes, hours=24)
        recent_ids = {int(row["id"]) for row in recent_codes}
        regular_codes = [row for row in codes if int(row["id"]) not in recent_ids]

        lines = ["ㅤ"]
        for row in regular_codes[:50]:
            lines.append(self._format_public_code_line(row))
        lines.append("ㅤ")
        embed.description = "\n".join(lines)

        if recent_codes:
            new_lines = [self._format_public_code_line(row) for row in recent_codes[:20]]
            if len(recent_codes) > 20:
                new_lines.append(f"…and {len(recent_codes) - 20} more new code(s).")
            embed.add_field(
                name="🆕 NEW",
                value="\n".join(new_lines)[:1024],
                inline=False,
            )

        hidden_count = max(0, len(regular_codes) - 50) + max(0, len(recent_codes) - 20)
        if hidden_count:
            embed.set_footer(text=f"{hidden_count} additional active code(s) are not shown.")
        return embed

    @staticmethod
    def _format_public_code_line(row) -> str:
        if row["expires_at"]:
            timestamp = discord_timestamp(row["expires_at"])
            return f"**{row['code']}** — {timestamp}" if timestamp else f"**`{row['code']}`**"
        return f"**{row['code']}**"

    @staticmethod
    def list_recent_codes(codes, hours: int = 24) -> list:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        recent = []
        for row in codes:
            created_at = parse_database_utc_datetime(row["created_at"])
            if created_at is not None and created_at > cutoff:
                recent.append(row)
        return recent

    @classmethod
    def recent_code_ids(cls, codes, hours: int = 24) -> frozenset[int]:
        return frozenset(int(row["id"]) for row in cls.list_recent_codes(codes, hours=hours))

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

    def build_announcement_embed(self, public_channel_id: int | None, items: list[dict] | None = None) -> nextcord.Embed:
        announced = list(items or [])
        heading = "A new live code has just been added." if len(announced) == 1 else "New live codes have just been added."
        lines = [heading]

        for item in announced[:20]:
            code = str(item.get("code") or "").strip()
            if not code:
                continue

            block_lines = [f"🎟️   **{code}**"]

            reward = str(item.get("reward") or "").strip()
            reward_lines = [
                reward_line.strip()
                for reward_line in reward.replace("\r\n", "\n").replace("\r", "\n").split("\n")
                if reward_line.strip()
            ]
            if reward_lines:
                block_lines.append("**Rewards**")
                block_lines.extend(f"↳ {reward_line[:180]}" for reward_line in reward_lines)

            expires_at = item.get("expires_at")
            timestamp = discord_timestamp(str(expires_at)) if expires_at else ""
            block_lines.append(
                f"⏳ **Expires:** {timestamp}" if timestamp else "⏳ **Expires:** No expiration date"
            )

            lines.append("\n".join(block_lines))

        if len(announced) > 20:
            lines.append(f"…and {len(announced) - 20} more code(s).")

        if public_channel_id is not None:
            lines.append(f"Check the complete list here: <#{public_channel_id}>")
        else:
            lines.append("Check the live codes panel for the complete list.")

        return nextcord.Embed(
            title="🚨 New Live Code Available!" if len(announced) == 1 else "🚨 New Live Codes Available!",
            description="\n\n".join(lines)[:4096],
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


def parse_database_utc_datetime(value) -> datetime | None:
    if value is None:
        return None
    clean = str(value).strip()
    if not clean:
        return None
    try:
        parsed = datetime.fromisoformat(clean.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_live_code(value) -> str:
    code = str(value or "").strip()
    if not code:
        return ""
    if len(code) > 128:
        raise ValueError("live code cannot be longer than 128 characters")
    if re.search(r"\s", code):
        raise ValueError(f"live code cannot contain whitespace: {code!r}")
    return code.upper()


def normalize_optional_text(value, max_length: int) -> str | None:
    text = str(value or "").strip()
    return text[:max_length] if text else None


def normalize_expire_iso(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("expires_at must be a valid ISO date, for example 2026-07-30T00:00:00Z") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def parse_codes_input(text: str) -> list[str]:
    seen: set[str] = set()
    codes: list[str] = []
    for raw_code in re.split(r"[,\s]+", text):
        code = normalize_live_code(raw_code)
        code_key = code.upper()
        if not code or code_key in seen:
            continue
        seen.add(code_key)
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
