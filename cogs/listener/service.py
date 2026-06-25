import re
from io import BytesIO

import nextcord

from core.embeds import DEFAULT_COLOR
from core.emoji import replace_custom_emoji_keys


MODULE_NAME = "listener"
MANAGEMENT_VIEW_TYPE = "listener_management_panel"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
URL_RE = re.compile(r"https?://[^\s<>)]+")
MARKDOWN_URL_RE = re.compile(r"\[[^\]]+\]\((https?://[^)\s]+)\)")


DEFAULT_TYPES = (
    ("Solo Leveling:ARISE | Official Page on X", "SLAGX"),
    ("나 혼자만 레벨업:어라이즈 - 공식 계정 on X", "SLAKX"),
    ("【公式】俺だけレベルアップな件: ARISE (俺アラ) on X", "SLAJX"),
    ("Solo Leveling:ARISE | Leaks", "SLALEAK"),
    ("ARKAS on X", "ARKAS"),
    ("Shiney YT", "Shiney"),
    ("Announcement", "Announcement"),
    ("Test", "Tests"),
    ("Solo Leveling:ARISE Live Stream YouTube", "SLAYLG"),
    ("Solo Leveling:ARISE Premiere YouTube", "SLAYPG"),
    ("Solo Leveling:ARISE Video Upload YouTube", "SLAYVG"),
    ("나 혼자만 레벨업:어라이즈 라이브 스트림 YouTube", "SLAYLK"),
    ("나 혼자만 레벨업:어라이즈 프리미어 YouTube", "SLAYPK"),
    ("나 혼자만 레벨업:어라이즈 비디오 업로드 YouTube", "SLAYVK"),
    ("俺だけレベルアップな件:ARISE ライブストリーム YouTube", "SLAYLJ"),
    ("俺だけレベルアップな件:ARISE プレミア YouTube", "SLAYPJ"),
    ("俺だけレベルアップな件:ARISE ビデオアップロード YouTube", "SLAYVJ"),
    ("Solo Leveling:ARISE | Official X", "SLAOFFICIAL"),
)


class ListenerService:
    def __init__(self, bot) -> None:
        self.bot = bot
        self.db = bot.db

    async def init_defaults(self) -> None:
        for label, code in DEFAULT_TYPES:
            await self.db.execute(
                """
                INSERT OR IGNORE INTO listener_types (label, code, created_at, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (label, code),
            )

    async def is_enabled(self, guild_id: int) -> bool:
        row = await self.db.get_module_settings(guild_id, MODULE_NAME)
        return True if row is None else bool(row["enabled"])

    async def set_enabled(self, guild_id: int, enabled: bool, user_id: int | None = None) -> None:
        await self.db.upsert_module_settings(guild_id, MODULE_NAME, enabled=enabled, settings={})
        await self.log(
            guild_id,
            "listener_enable_disable",
            user_id=user_id,
            details={"enabled": enabled},
        )

    async def list_types(self):
        return await self.db.fetchall("SELECT * FROM listener_types ORDER BY label COLLATE NOCASE")

    async def get_type(self, code: str):
        return await self.db.fetchone("SELECT * FROM listener_types WHERE code = ?", (code,))

    async def upsert_type(self, guild_id: int, label: str, code: str, user_id: int | None = None) -> None:
        label = label.strip()
        code = code.strip()
        if not label or not code:
            raise ValueError("Label and Code are required.")

        existing_label = await self.db.fetchone("SELECT * FROM listener_types WHERE label = ?", (label,))
        existing_code = await self.db.fetchone("SELECT * FROM listener_types WHERE code = ?", (code,))
        if existing_code is not None and existing_code["label"] != label:
            raise ValueError("❌ This code already exists with another label.")

        await self.db.execute(
            """
            INSERT INTO listener_types (label, code, created_at, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(label) DO UPDATE SET
                code = excluded.code,
                updated_at = CURRENT_TIMESTAMP
            """,
            (label, code),
        )
        if existing_label is not None and existing_label["code"] != code:
            await self.db.execute(
                "UPDATE listener_sources SET code = ?, updated_at = CURRENT_TIMESTAMP WHERE code = ?",
                (code, existing_label["code"]),
            )
            await self.db.execute(
                "UPDATE listener_targets SET code = ?, updated_at = CURRENT_TIMESTAMP WHERE code = ?",
                (code, existing_label["code"]),
            )
        await self.log(guild_id, "listener_add_type", user_id=user_id, details={"label": label, "code": code})

    async def remove_type(self, guild_id: int, code: str, user_id: int | None = None) -> None:
        used = await self.db.fetchone(
            """
            SELECT 1
            FROM listener_sources
            WHERE code = ? AND enabled = 1
            UNION
            SELECT 1
            FROM listener_targets
            WHERE code = ? AND enabled = 1
            LIMIT 1
            """,
            (code, code),
        )
        if used is not None:
            raise ValueError("❌ This type is still used by active sources or targets.")

        row = await self.get_type(code)
        await self.db.execute("DELETE FROM listener_types WHERE code = ?", (code,))
        await self.log(guild_id, "listener_remove_type", user_id=user_id, details={"code": code, "label": row["label"] if row else None})

    async def add_source(self, guild_id: int, channel_id: int, code: str, user_id: int | None = None) -> None:
        await self.require_type(code)
        code_row = await self.db.fetchone(
            "SELECT * FROM listener_sources WHERE guild_id = ? AND code = ?",
            (guild_id, code),
        )
        if code_row is not None:
            channel_row = await self.db.fetchone(
                "SELECT * FROM listener_sources WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id),
            )
            if channel_row is not None and channel_row["id"] != code_row["id"]:
                raise ValueError("source conflicts with another configured source")
            await self.db.execute(
                """
                UPDATE listener_sources
                SET channel_id = ?, enabled = 1, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (channel_id, code_row["id"]),
            )
        else:
            await self.db.execute(
                """
                INSERT INTO listener_sources (guild_id, channel_id, code, enabled, created_at, updated_at)
                VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(guild_id, channel_id) DO UPDATE SET
                    code = excluded.code,
                    enabled = 1,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (guild_id, channel_id, code),
            )
        await self.log(
            guild_id,
            "listener_add_source",
            user_id=user_id,
            details={"source_guild_id": guild_id, "source_channel_id": channel_id, "code": code},
        )

    async def remove_source_by_id(self, guild_id: int, source_id: int, user_id: int | None = None) -> bool:
        row = await self.db.fetchone(
            "SELECT * FROM listener_sources WHERE id = ? AND guild_id = ? AND enabled = 1",
            (source_id, guild_id),
        )
        cursor = await self.db.execute(
            """
            UPDATE listener_sources
            SET enabled = 0, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND guild_id = ? AND enabled = 1
            """,
            (source_id, guild_id),
        )
        removed = cursor.rowcount > 0
        if removed:
            await self.log(
                guild_id,
                "listener_remove_source",
                user_id=user_id,
                details={"source_guild_id": guild_id, "source_channel_id": row["channel_id"], "code": row["code"]},
            )
        return removed

    async def add_target(
        self,
        guild_id: int,
        channel_id: int,
        code: str,
        message_location: str,
        message: str,
        message_link: str,
        user_id: int | None = None,
    ) -> None:
        await self.require_type(code)
        location = message_location.strip().lower() or "before"
        if location not in {"before", "after"}:
            raise ValueError("Message Location must be `before` or `after`.")

        message = self.normalize_config_text(message)
        message_link = self.normalize_config_text(message_link)
        await self.db.execute(
            """
            INSERT INTO listener_targets (
                guild_id,
                channel_id,
                code,
                message_location,
                message,
                message_link,
                enabled,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (guild_id, channel_id, code, location, message, message_link),
        )
        await self.log(
            guild_id,
            "listener_add_target",
            user_id=user_id,
            details={"target_guild_id": guild_id, "target_channel_id": channel_id, "code": code},
        )

    async def remove_target_by_id(self, guild_id: int, target_id: int, user_id: int | None = None) -> bool:
        row = await self.db.fetchone(
            "SELECT * FROM listener_targets WHERE id = ? AND guild_id = ? AND enabled = 1",
            (target_id, guild_id),
        )
        cursor = await self.db.execute(
            """
            UPDATE listener_targets
            SET enabled = 0, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND guild_id = ? AND enabled = 1
            """,
            (target_id, guild_id),
        )
        removed = cursor.rowcount > 0
        if removed:
            await self.log(
                guild_id,
                "listener_remove_target",
                user_id=user_id,
                details={"target_guild_id": guild_id, "target_channel_id": row["channel_id"], "code": row["code"]},
            )
        return removed

    async def get_source_for_channel(self, guild_id: int, channel_id: int):
        return await self.db.fetchone(
            """
            SELECT *
            FROM listener_sources
            WHERE guild_id = ? AND channel_id = ? AND enabled = 1
            """,
            (guild_id, channel_id),
        )

    async def get_targets_for_code(self, code: str):
        return await self.db.fetchall(
            """
            SELECT *
            FROM listener_targets
            WHERE code = ? AND enabled = 1
            ORDER BY guild_id, channel_id, id
            """,
            (code,),
        )

    async def list_sources(self, guild_id: int):
        return await self.db.fetchall(
            """
            SELECT s.*, t.label
            FROM listener_sources s
            LEFT JOIN listener_types t ON t.code = s.code
            WHERE s.guild_id = ? AND s.enabled = 1
            ORDER BY s.code COLLATE NOCASE, s.channel_id
            """,
            (guild_id,),
        )

    async def list_targets(self, guild_id: int):
        return await self.db.fetchall(
            """
            SELECT tg.*, ty.label
            FROM listener_targets tg
            LEFT JOIN listener_types ty ON ty.code = tg.code
            WHERE tg.guild_id = ? AND tg.enabled = 1
            ORDER BY tg.code COLLATE NOCASE, tg.channel_id, tg.id
            """,
            (guild_id,),
        )

    async def clear_sources(self, guild_id: int, user_id: int | None = None) -> int:
        cursor = await self.db.execute(
            """
            UPDATE listener_sources
            SET enabled = 0, updated_at = CURRENT_TIMESTAMP
            WHERE guild_id = ? AND enabled = 1
            """,
            (guild_id,),
        )
        count = cursor.rowcount
        await self.log(guild_id, "listener_clear_sources", user_id=user_id, details={"cleared_count_sources": count, "cleared_count_targets": 0})
        return count

    async def clear_targets(self, guild_id: int, user_id: int | None = None) -> int:
        cursor = await self.db.execute(
            """
            UPDATE listener_targets
            SET enabled = 0, updated_at = CURRENT_TIMESTAMP
            WHERE guild_id = ? AND enabled = 1
            """,
            (guild_id,),
        )
        count = cursor.rowcount
        await self.log(guild_id, "listener_clear_targets", user_id=user_id, details={"cleared_count_sources": 0, "cleared_count_targets": count})
        return count

    async def clear_everything(self, guild_id: int, user_id: int | None = None) -> tuple[int, int]:
        source_cursor = await self.db.execute(
            """
            UPDATE listener_sources
            SET enabled = 0, updated_at = CURRENT_TIMESTAMP
            WHERE guild_id = ? AND enabled = 1
            """,
            (guild_id,),
        )
        target_cursor = await self.db.execute(
            """
            UPDATE listener_targets
            SET enabled = 0, updated_at = CURRENT_TIMESTAMP
            WHERE guild_id = ? AND enabled = 1
            """,
            (guild_id,),
        )
        source_count = source_cursor.rowcount
        target_count = target_cursor.rowcount
        await self.log(
            guild_id,
            "listener_clear_everything",
            user_id=user_id,
            details={"cleared_count_sources": source_count, "cleared_count_targets": target_count},
        )
        return source_count, target_count

    async def save_panel(self, guild_id: int, channel_id: int, message_id: int) -> None:
        await self.db.execute(
            """
            INSERT INTO listener_panels (guild_id, channel_id, message_id, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                message_id = excluded.message_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (guild_id, channel_id, message_id),
        )
        await self.db.save_persistent_view(guild_id, MODULE_NAME, channel_id, message_id, MANAGEMENT_VIEW_TYPE, state={})

    async def get_panel(self, guild_id: int):
        return await self.db.fetchone("SELECT * FROM listener_panels WHERE guild_id = ?", (guild_id,))

    async def require_type(self, code: str) -> None:
        if await self.get_type(code) is None:
            raise ValueError("Unknown listener type.")

    def build_management_embed(self) -> nextcord.Embed:
        embed = nextcord.Embed(
            title="Listener",
            description="Forward configured source channels to listener targets by editable code.",
            color=DEFAULT_COLOR,
        )
        embed.add_field(name="Navigation", value="Navigation edits this panel message.", inline=False)
        return embed

    async def build_settings_embed(self, guild_id: int) -> nextcord.Embed:
        enabled = await self.is_enabled(guild_id)
        sources = await self.list_sources(guild_id)
        targets = await self.list_targets(guild_id)
        types = await self.list_types()
        embed = nextcord.Embed(title="Listener Settings", color=DEFAULT_COLOR)
        embed.add_field(name="Enabled", value=str(enabled), inline=True)
        embed.add_field(name="Sources", value=str(len(sources)), inline=True)
        embed.add_field(name="Targets", value=str(len(targets)), inline=True)
        embed.add_field(name="Types", value=str(len(types)), inline=True)
        embed.add_field(name="Bot/Webhook Source Messages Allowed", value="Yes", inline=True)
        embed.add_field(name="Own Bot Messages Ignored", value="Yes", inline=True)
        return embed

    def normalize_config_text(self, value: str | None) -> str:
        if value is None:
            return ""
        clean = value.strip()
        if clean.lower() == "nothing":
            return ""
        return clean.replace("|", "\n")

    def first_url_from_message(self, message: nextcord.Message) -> str | None:
        values = [message.content or ""]
        for embed in message.embeds:
            values.append(embed.description or "")
            for field in embed.fields:
                values.append(str(field.value or ""))
        values.extend(attachment.url for attachment in message.attachments)
        combined = "\n".join(values)
        markdown = MARKDOWN_URL_RE.search(combined)
        if markdown:
            return markdown.group(1)
        plain = URL_RE.search(combined)
        return plain.group(0) if plain else None

    async def build_forward_payload(self, message: nextcord.Message, target) -> tuple[str | None, list[nextcord.Embed], list[nextcord.File]]:
        raw_text = replace_custom_emoji_keys(message.content or "") or ""
        files: list[nextcord.File] = []
        oversized_urls: list[str] = []
        for attachment in message.attachments:
            if attachment.size > MAX_UPLOAD_BYTES:
                oversized_urls.append(attachment.url)
                continue
            try:
                data = await attachment.read()
                files.append(nextcord.File(BytesIO(data), filename=attachment.filename, spoiler=attachment.is_spoiler()))
            except nextcord.HTTPException:
                oversized_urls.append(attachment.url)

        if oversized_urls:
            raw_text = "\n".join(part for part in [raw_text, "\n".join(oversized_urls)] if part)

        extra = replace_custom_emoji_keys(target["message"] or "") or ""
        link_label = replace_custom_emoji_keys(target["message_link"] or "") or ""
        location = target["message_location"]

        if link_label:
            url = self.first_url_from_message(message)
            if url is None:
                return None, [], []
            link = f"[{link_label}]({url})"
            content = self.join_parts(extra, link, location)
            return content, [], files

        if extra:
            content = self.join_parts(extra, raw_text, location)
            return content, [], files

        return raw_text or None, list(message.embeds)[:10], files

    def join_parts(self, extra: str, raw: str, location: str) -> str:
        parts = [extra, raw] if location == "before" else [raw, extra]
        return "\n".join(part for part in parts if part)

    def allowed_mentions(self) -> nextcord.AllowedMentions:
        return nextcord.AllowedMentions(everyone=False, users=True, roles=True, replied_user=False)

    async def log(
        self,
        guild_id: int,
        action: str,
        user_id: int | None = None,
        target_id: int | None = None,
        details: dict | None = None,
    ) -> None:
        await self.db.log_action(guild_id, MODULE_NAME, action, user_id=user_id, target_id=target_id, details=details or {})
