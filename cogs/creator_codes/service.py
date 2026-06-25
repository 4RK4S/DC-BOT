import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiosqlite
import nextcord

from core.emoji import replace_custom_emoji_keys
from core.embeds import DEFAULT_COLOR


MODULE_NAME = "creator_codes"
ADMIN_VIEW_TYPE = "creator_codes_admin_panel"
PUBLIC_VIEW_TYPE = "creator_codes_public_embed"
KST = timezone(timedelta(hours=9))
EXPIRE_FORMATS = ("%m.%d.%Y %H:%M", "%m.%d.%Y, %H:%M")
MAX_PUBLIC_POOLS = 25


@dataclass(frozen=True)
class ActivePool:
    id: int
    key_words: str
    expire_at: str | None
    left: int
    used: int


@dataclass(frozen=True)
class ClaimResult:
    status: str
    key_words: str | None = None
    code: str | None = None
    code_id: int | None = None


class CreatorCodeService:
    def __init__(self, bot) -> None:
        self.bot = bot
        self.db = bot.db

    async def get_settings(self, guild_id: int):
        return await self.db.fetchone("SELECT * FROM creator_code_settings WHERE guild_id = ?", (guild_id,))

    async def ensure_settings(self, guild_id: int):
        await self.db.execute(
            """
            INSERT INTO creator_code_settings (guild_id, updated_at)
            VALUES (?, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id) DO NOTHING
            """,
            (guild_id,),
        )
        return await self.get_settings(guild_id)

    async def update_settings(self, guild_id: int, **values) -> None:
        await self.ensure_settings(guild_id)
        allowed = {"public_channel_id", "public_message_id", "announcement_channel_id", "ping_role_id", "enabled"}
        fields = [key for key in values if key in allowed]
        if not fields:
            return
        assignments = ", ".join(f"{field} = ?" for field in fields)
        params = [values[field] for field in fields]
        params.append(guild_id)
        await self.db.execute(
            f"UPDATE creator_code_settings SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE guild_id = ?",
            params,
        )

    async def save_admin_panel(self, guild_id: int, channel_id: int, message_id: int) -> None:
        await self.db.execute(
            """
            INSERT INTO creator_codes_admin_panels (guild_id, channel_id, message_id, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                message_id = excluded.message_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (guild_id, channel_id, message_id),
        )
        await self.db.save_persistent_view(guild_id, MODULE_NAME, channel_id, message_id, ADMIN_VIEW_TYPE, state={})

    async def get_admin_panel(self, guild_id: int):
        return await self.db.fetchone("SELECT * FROM creator_codes_admin_panels WHERE guild_id = ?", (guild_id,))

    async def set_public_channel(self, guild_id: int, channel_id: int, user_id: int | None) -> None:
        await self.update_settings(guild_id, public_channel_id=channel_id)
        await self.log(guild_id, "creator_codes_set_public_channel", user_id=user_id, details={"channel_id": channel_id})

    async def set_announcement_channel(self, guild_id: int, channel_id: int, user_id: int | None) -> None:
        await self.update_settings(guild_id, announcement_channel_id=channel_id)
        await self.log(guild_id, "creator_codes_set_announcement_channel", user_id=user_id, details={"channel_id": channel_id})

    async def set_ping_role(self, guild_id: int, role_id: int | None, user_id: int | None) -> None:
        await self.update_settings(guild_id, ping_role_id=role_id)
        await self.log(guild_id, "creator_codes_set_ping_role", user_id=user_id, details={"role_id": role_id})

    async def add_codes(
        self,
        guild_id: int,
        codes_text: str | list[str],
        key_words: str,
        expire: str,
        user_id: int | None = None,
        source: str = "panel",
    ) -> tuple[object, int, int, bool]:
        codes = parse_codes_input(codes_text)
        if not codes:
            raise ValueError("Provide at least one code.")
        clean_key_words = self.normalize_text(key_words)
        if not clean_key_words:
            raise ValueError("Key Words are required.")
        expire_at = parse_expire_to_utc_iso(expire)
        existing = await self.db.fetchone(
            """
            SELECT *
            FROM creator_code_pools
            WHERE guild_id = ? AND lower(key_words) = lower(?)
            ORDER BY id DESC
            LIMIT 1
            """,
            (guild_id, clean_key_words),
        )
        was_active = False
        if existing is None:
            cursor = await self.db.execute(
                """
                INSERT INTO creator_code_pools (
                    guild_id,
                    name,
                    keywords,
                    key_words,
                    expires_at,
                    expire_at,
                    active,
                    enabled,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (guild_id, clean_key_words, clean_key_words, clean_key_words, expire_at, expire_at),
            )
            pool_id = int(cursor.lastrowid)
        else:
            pool_id = int(existing["id"])
            was_active = await self.is_pool_publicly_active(existing)
            await self.db.execute(
                """
                UPDATE creator_code_pools
                SET key_words = ?,
                    keywords = ?,
                    name = ?,
                    expire_at = ?,
                    expires_at = ?,
                    enabled = 1,
                    active = 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (clean_key_words, clean_key_words, clean_key_words, expire_at, expire_at, pool_id),
            )

        added = 0
        skipped = 0
        for code in codes:
            existing_code = await self.db.fetchone(
                "SELECT 1 FROM creator_codes WHERE pool_id = ? AND code = ? LIMIT 1",
                (pool_id, code),
            )
            if existing_code is not None:
                skipped += 1
                continue
            cursor = await self.db.execute(
                """
                INSERT INTO creator_codes (
                    guild_id,
                    pool_id,
                    code,
                    used,
                    enabled,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, 0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (guild_id, pool_id, code),
            )
            added += cursor.rowcount

        pool = await self.get_pool(pool_id)
        became_active = not was_active and await self.is_pool_publicly_active(pool)
        await self.log(
            guild_id,
            "creator_codes_add_codes",
            user_id=user_id,
            target_id=pool_id,
            details={"pool_id": pool_id, "key_words": clean_key_words, "added": added, "skipped": skipped, "source": source},
        )
        return pool, added, skipped, became_active

    async def get_pool(self, pool_id: int):
        return await self.db.fetchone("SELECT * FROM creator_code_pools WHERE id = ?", (pool_id,))

    async def is_pool_publicly_active(self, pool) -> bool:
        if pool is None or not bool(pool["enabled"]):
            return False
        if is_expired(pool["expire_at"]):
            return False
        row = await self.db.fetchone(
            "SELECT 1 FROM creator_codes WHERE pool_id = ? AND enabled = 1 AND COALESCE(used, 0) = 0 LIMIT 1",
            (pool["id"],),
        )
        return row is not None

    async def list_active_pools(self, guild_id: int) -> list[ActivePool]:
        await self.clear_expired_or_used(guild_id, log_action=False)
        rows = await self.db.fetchall(
            """
            SELECT
                p.id,
                p.key_words,
                p.expire_at,
                SUM(CASE WHEN c.enabled = 1 AND COALESCE(c.used, 0) = 0 THEN 1 ELSE 0 END) AS left_count,
                SUM(CASE WHEN COALESCE(c.used, 0) = 1 THEN 1 ELSE 0 END) AS used_count
            FROM creator_code_pools p
            LEFT JOIN creator_codes c ON c.pool_id = p.id
            WHERE p.guild_id = ?
                AND p.enabled = 1
                AND (p.expire_at IS NULL OR p.expire_at > ?)
            GROUP BY p.id
            HAVING left_count > 0
            ORDER BY CASE WHEN p.expire_at IS NULL THEN 1 ELSE 0 END, p.expire_at ASC, p.id ASC
            LIMIT ?
            """,
            (guild_id, utc_now_iso(), MAX_PUBLIC_POOLS),
        )
        return [
            ActivePool(row["id"], row["key_words"], row["expire_at"], int(row["left_count"] or 0), int(row["used_count"] or 0))
            for row in rows
        ]

    async def list_pools(self, guild_id: int):
        return await self.db.fetchall(
            """
            SELECT
                p.*,
                SUM(CASE WHEN c.enabled = 1 AND COALESCE(c.used, 0) = 0 THEN 1 ELSE 0 END) AS left_count,
                SUM(CASE WHEN COALESCE(c.used, 0) = 1 THEN 1 ELSE 0 END) AS used_count
            FROM creator_code_pools p
            LEFT JOIN creator_codes c ON c.pool_id = p.id
            WHERE p.guild_id = ?
            GROUP BY p.id
            ORDER BY p.enabled DESC, p.updated_at DESC, p.id DESC
            """,
            (guild_id,),
        )

    async def remove_pool(self, guild_id: int, pool_id: int, user_id: int | None) -> bool:
        cursor = await self.db.execute(
            """
            UPDATE creator_code_pools
            SET enabled = 0, active = 0, updated_at = CURRENT_TIMESTAMP
            WHERE guild_id = ? AND id = ? AND enabled = 1
            """,
            (guild_id, pool_id),
        )
        removed = cursor.rowcount > 0
        if removed:
            await self.log(guild_id, "creator_codes_remove_pool", user_id=user_id, target_id=pool_id, details={"pool_id": pool_id})
        return removed

    async def clear_expired_or_used(self, guild_id: int | None = None, user_id: int | None = None, log_action: bool = True) -> int:
        params: list[object] = [utc_now_iso()]
        guild_filter = ""
        if guild_id is not None:
            guild_filter = "AND p.guild_id = ?"
            params.append(guild_id)
        rows = await self.db.fetchall(
            f"""
            SELECT p.id, p.guild_id
            FROM creator_code_pools p
            WHERE p.enabled = 1
                {guild_filter}
                AND (
                    (p.expire_at IS NOT NULL AND p.expire_at <= ?)
                    OR NOT EXISTS (
                        SELECT 1
                        FROM creator_codes c
                        WHERE c.pool_id = p.id
                            AND c.enabled = 1
                            AND COALESCE(c.used, 0) = 0
                    )
                )
            """,
            tuple(reversed(params)) if guild_id is not None else tuple(params),
        )
        count = 0
        affected_guilds: set[int] = set()
        for row in rows:
            await self.db.execute(
                "UPDATE creator_code_pools SET enabled = 0, active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (row["id"],),
            )
            count += 1
            affected_guilds.add(row["guild_id"])
        if log_action and guild_id is not None:
            await self.log(guild_id, "creator_codes_clear_expired", user_id=user_id, details={"count": count})
        return count

    async def claim(self, guild_id: int, pool_id: int, user) -> ClaimResult:
        pool = await self.get_pool(pool_id)
        if pool is None or pool["guild_id"] != guild_id or not bool(pool["enabled"]) or is_expired(pool["expire_at"]):
            return ClaimResult("inactive")

        existing = await self.db.fetchone(
            """
            SELECT code, id
            FROM creator_codes
            WHERE guild_id = ?
                AND pool_id = ?
                AND user_id = ?
                AND COALESCE(used, 0) = 1
            ORDER BY claimed_at ASC, id ASC
            LIMIT 1
            """,
            (guild_id, pool_id, user.id),
        )
        if existing is not None:
            return ClaimResult("already", pool["key_words"], existing["code"], existing["id"])

        connection = self.db._connection()
        await connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = await connection.execute(
                """
                SELECT id, code
                FROM creator_codes
                WHERE guild_id = ?
                    AND pool_id = ?
                    AND enabled = 1
                    AND COALESCE(used, 0) = 0
                ORDER BY id ASC
                LIMIT 1
                """,
                (guild_id, pool_id),
            )
            code = await cursor.fetchone()
            await cursor.close()
            if code is None:
                await connection.rollback()
                return ClaimResult("empty", pool["key_words"])
            await connection.execute(
                """
                UPDATE creator_codes
                SET used = 1,
                    used_by_user_id = ?,
                    user_id = ?,
                    nick = ?,
                    used_at = ?,
                    claimed_at = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND COALESCE(used, 0) = 0
                """,
                (user.id, user.id, str(user), utc_now_iso(), utc_now_iso(), code["id"]),
            )
            await connection.commit()
        except Exception:
            await connection.rollback()
            raise

        await self.log(
            guild_id,
            "creator_codes_claim",
            user_id=user.id,
            target_id=code["id"],
            details={"guild_id": guild_id, "pool_id": pool_id, "code_id": code["id"], "user_id": user.id},
        )
        return ClaimResult("claimed", pool["key_words"], code["code"], code["id"])

    async def stats(self, guild_id: int) -> dict[str, int]:
        active = await self.list_active_pools(guild_id)
        unused = sum(pool.left for pool in active)
        expired_or_used = await self.db.fetchone(
            """
            SELECT COUNT(*) AS count
            FROM creator_code_pools p
            WHERE p.guild_id = ?
                AND p.enabled = 1
                AND (
                    (p.expire_at IS NOT NULL AND p.expire_at <= ?)
                    OR NOT EXISTS (
                        SELECT 1
                        FROM creator_codes c
                        WHERE c.pool_id = p.id
                            AND c.enabled = 1
                            AND COALESCE(c.used, 0) = 0
                    )
                )
            """,
            (guild_id, utc_now_iso()),
        )
        return {
            "active_pools": len(active),
            "unused_codes": unused,
            "expired_or_used_pools": int(expired_or_used["count"] if expired_or_used else 0),
        }

    def build_admin_embed(self) -> nextcord.Embed:
        embed = nextcord.Embed(
            title="Creator Codes",
            description="Manage creator code pools and the public claim embed.",
            color=DEFAULT_COLOR,
        )
        embed.add_field(name="Navigation", value="Navigation edits this panel message.", inline=False)
        return embed

    def build_public_embed(self, pools: list[ActivePool], total_active_count: int | None = None) -> nextcord.Embed:
        embed = nextcord.Embed(title="🎁 Active Creator Codes", color=DEFAULT_COLOR)
        if not pools:
            embed.description = "*No active creator codes currently available.*"
            return embed
        lines = []
        for index, pool in enumerate(pools, start=1):
            line = f"**Code {index}** - {pool.key_words} ({pool.left} left)"
            timestamp = discord_timestamp(pool.expire_at)
            if timestamp:
                line += f" ({timestamp})"
            lines.append(line)
        if total_active_count and total_active_count > MAX_PUBLIC_POOLS:
            lines.append(f"\nShowing first {MAX_PUBLIC_POOLS} of {total_active_count} active creator code pools.")
        embed.description = "\n".join(lines)
        return embed

    async def build_settings_embed(self, guild_id: int) -> nextcord.Embed:
        settings = await self.ensure_settings(guild_id)
        stats = await self.stats(guild_id)
        embed = nextcord.Embed(title="Creator Codes Settings", color=DEFAULT_COLOR)
        embed.add_field(name="Enabled", value=str(bool(settings["enabled"])), inline=True)
        embed.add_field(name="Public Channel", value=f"<#{settings['public_channel_id']}>" if settings["public_channel_id"] else "Not set", inline=True)
        embed.add_field(name="Public Message", value=str(settings["public_message_id"] or "Not set"), inline=True)
        embed.add_field(name="Announcement Channel", value=f"<#{settings['announcement_channel_id']}>" if settings["announcement_channel_id"] else "Not set", inline=True)
        embed.add_field(name="Ping Role", value=f"<@&{settings['ping_role_id']}>" if settings["ping_role_id"] else "Not set", inline=True)
        embed.add_field(name="Active Pools", value=str(stats["active_pools"]), inline=True)
        embed.add_field(name="Unused Codes", value=str(stats["unused_codes"]), inline=True)
        embed.add_field(name="Expired / Used Pools", value=str(stats["expired_or_used_pools"]), inline=True)
        return embed

    def build_announcement_embed(self, pool, public_channel_id: int | None) -> nextcord.Embed:
        embed = nextcord.Embed(
            title="📢 New Creator Codes Available!",
            description="A brand-new code has just been released - don't miss out!",
            color=DEFAULT_COLOR,
        )
        timestamp = discord_timestamp(pool["expire_at"])
        if timestamp:
            embed.add_field(name="Expires", value=timestamp, inline=False)
        embed.add_field(name="🎁 Code Rewards", value=f"* {pool['key_words']}", inline=False)
        target = f"<#{public_channel_id}>" if public_channel_id else "the Creator Codes panel"
        embed.add_field(name="💡 How to Redeem", value=f"Click the matching Code button in {target}.", inline=False)
        return embed

    def normalize_text(self, value: str | None) -> str:
        return replace_custom_emoji_keys((value or "").strip())

    async def log(self, guild_id: int, action: str, user_id: int | None = None, target_id: int | None = None, details: dict | None = None) -> None:
        await self.db.log_action(guild_id, MODULE_NAME, action, user_id=user_id, target_id=target_id, details=details or {})


def parse_codes_input(value: str | list[str]) -> list[str]:
    if isinstance(value, list):
        raw = "\n".join(str(item) for item in value)
    else:
        raw = value
    seen: set[str] = set()
    codes: list[str] = []
    for item in re.split(r"[\s,|]+", raw.strip()):
        clean = item.strip()
        if clean and clean not in seen:
            seen.add(clean)
            codes.append(clean)
    return codes


def parse_expire_to_utc_iso(value: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError("Expire is required. Use mm.dd.yyyy HH:MM in UTC+9.")
    for fmt in EXPIRE_FORMATS:
        try:
            local_dt = datetime.strptime(clean, fmt).replace(tzinfo=KST)
            return local_dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            continue
    raise ValueError("Expire must use mm.dd.yyyy HH:MM or mm.dd.yyyy, HH:MM in UTC+9.")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_expired(value: str | None) -> bool:
    if not value:
        return False
    try:
        return datetime.fromisoformat(value) <= datetime.now(timezone.utc)
    except ValueError:
        return False


def discord_timestamp(value: str | None) -> str | None:
    if not value:
        return None
    try:
        unix = int(datetime.fromisoformat(value).timestamp())
    except ValueError:
        return None
    return f"<t:{unix}:R>"
