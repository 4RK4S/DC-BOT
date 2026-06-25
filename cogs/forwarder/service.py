import nextcord

from core.embeds import DEFAULT_COLOR


MODULE_NAME = "forwarder"
MANAGEMENT_VIEW_TYPE = "forwarder_management_panel"
DEFAULT_TYPES = (
    "Notice",
    "Developer Notes",
    "Updates",
    "Events",
    "Packages",
    "CM Notes",
)


class ForwarderService:
    def __init__(self, bot) -> None:
        self.bot = bot
        self.db = bot.db

    async def init_defaults(self) -> None:
        for type_name in DEFAULT_TYPES:
            await self.db.execute(
                """
                INSERT OR IGNORE INTO forwarder_types (name, created_at)
                VALUES (?, CURRENT_TIMESTAMP)
                """,
                (type_name,),
            )

    async def is_enabled(self, guild_id: int) -> bool:
        row = await self.db.get_module_settings(guild_id, MODULE_NAME)
        return True if row is None else bool(row["enabled"])

    async def set_enabled(self, guild_id: int, enabled: bool, user_id: int | None = None) -> None:
        await self.db.upsert_module_settings(guild_id, MODULE_NAME, enabled=enabled, settings={})
        await self.log(
            guild_id,
            "enable_disable",
            user_id=user_id,
            details={"enabled": enabled},
        )

    async def validate_type(self, type_name: str) -> str:
        clean = type_name.strip()
        row = await self.db.fetchone(
            "SELECT name FROM forwarder_types WHERE lower(name) = lower(?)",
            (clean,),
        )
        if row is None:
            raise ValueError(f"Unknown type. Use one of: {', '.join(DEFAULT_TYPES)}")
        return row["name"]

    async def add_source(self, guild_id: int, channel_id: int, type_name: str, user_id: int | None = None) -> None:
        valid_type = await self.validate_type(type_name)
        await self.db.execute(
            """
            INSERT INTO forwarder_sources (
                guild_id,
                channel_id,
                type_name,
                enabled,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id, channel_id) DO UPDATE SET
                type_name = excluded.type_name,
                enabled = 1,
                updated_at = CURRENT_TIMESTAMP
            """,
            (guild_id, channel_id, valid_type),
        )
        await self.log(guild_id, "add_source", user_id=user_id, details={"source_channel_id": channel_id, "type_name": valid_type})

    async def remove_source(self, guild_id: int, channel_id: int, user_id: int | None = None) -> bool:
        cursor = await self.db.execute(
            """
            UPDATE forwarder_sources
            SET enabled = 0, updated_at = CURRENT_TIMESTAMP
            WHERE guild_id = ? AND channel_id = ? AND enabled = 1
            """,
            (guild_id, channel_id),
        )
        removed = cursor.rowcount > 0
        if removed:
            await self.log(guild_id, "remove_source", user_id=user_id, details={"source_channel_id": channel_id})
        return removed

    async def add_target(self, guild_id: int, channel_id: int, type_name: str, user_id: int | None = None) -> None:
        valid_type = await self.validate_type(type_name)
        await self.db.execute(
            """
            INSERT INTO forwarder_targets (
                guild_id,
                channel_id,
                type_name,
                enabled,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id, channel_id, type_name) DO UPDATE SET
                enabled = 1,
                updated_at = CURRENT_TIMESTAMP
            """,
            (guild_id, channel_id, valid_type),
        )
        await self.log(guild_id, "add_target", user_id=user_id, details={"target_channel_id": channel_id, "type_name": valid_type})

    async def remove_target(
        self,
        guild_id: int,
        channel_id: int,
        type_name: str | None = None,
        user_id: int | None = None,
    ) -> bool:
        if type_name and type_name.strip():
            valid_type = await self.validate_type(type_name)
            cursor = await self.db.execute(
                """
                UPDATE forwarder_targets
                SET enabled = 0, updated_at = CURRENT_TIMESTAMP
                WHERE guild_id = ? AND channel_id = ? AND type_name = ? AND enabled = 1
                """,
                (guild_id, channel_id, valid_type),
            )
        else:
            valid_type = None
            cursor = await self.db.execute(
                """
                UPDATE forwarder_targets
                SET enabled = 0, updated_at = CURRENT_TIMESTAMP
                WHERE guild_id = ? AND channel_id = ? AND enabled = 1
                """,
                (guild_id, channel_id),
            )
        removed = cursor.rowcount > 0
        if removed:
            await self.log(
                guild_id,
                "remove_target",
                user_id=user_id,
                details={"target_channel_id": channel_id, "type_name": valid_type},
            )
        return removed

    async def get_source_for_channel(self, guild_id: int, channel_id: int):
        return await self.db.fetchone(
            """
            SELECT *
            FROM forwarder_sources
            WHERE guild_id = ? AND channel_id = ? AND enabled = 1
            """,
            (guild_id, channel_id),
        )

    async def get_targets_for_type(self, type_name: str):
        return await self.db.fetchall(
            """
            SELECT *
            FROM forwarder_targets
            WHERE type_name = ? AND enabled = 1
            """,
            (type_name,),
        )

    async def list_sources(self, guild_id: int):
        return await self.db.fetchall(
            """
            SELECT *
            FROM forwarder_sources
            WHERE guild_id = ? AND enabled = 1
            ORDER BY type_name, channel_id
            """,
            (guild_id,),
        )

    async def list_targets(self, guild_id: int):
        return await self.db.fetchall(
            """
            SELECT *
            FROM forwarder_targets
            WHERE guild_id = ? AND enabled = 1
            ORDER BY type_name, channel_id
            """,
            (guild_id,),
        )

    async def clear_sources(self, guild_id: int, user_id: int | None = None) -> int:
        cursor = await self.db.execute(
            """
            UPDATE forwarder_sources
            SET enabled = 0, updated_at = CURRENT_TIMESTAMP
            WHERE guild_id = ? AND enabled = 1
            """,
            (guild_id,),
        )
        cleared_count = cursor.rowcount
        await self.log(
            guild_id,
            "forwarder_clear_sources",
            user_id=user_id,
            details={
                "guild_id": guild_id,
                "cleared_count_sources": cleared_count,
                "cleared_count_targets": 0,
            },
        )
        return cleared_count

    async def clear_targets(self, guild_id: int, user_id: int | None = None) -> int:
        cursor = await self.db.execute(
            """
            UPDATE forwarder_targets
            SET enabled = 0, updated_at = CURRENT_TIMESTAMP
            WHERE guild_id = ? AND enabled = 1
            """,
            (guild_id,),
        )
        cleared_count = cursor.rowcount
        await self.log(
            guild_id,
            "forwarder_clear_targets",
            user_id=user_id,
            details={
                "guild_id": guild_id,
                "cleared_count_sources": 0,
                "cleared_count_targets": cleared_count,
            },
        )
        return cleared_count

    async def clear_everything(self, guild_id: int, user_id: int | None = None) -> tuple[int, int]:
        source_cursor = await self.db.execute(
            """
            UPDATE forwarder_sources
            SET enabled = 0, updated_at = CURRENT_TIMESTAMP
            WHERE guild_id = ? AND enabled = 1
            """,
            (guild_id,),
        )
        target_cursor = await self.db.execute(
            """
            UPDATE forwarder_targets
            SET enabled = 0, updated_at = CURRENT_TIMESTAMP
            WHERE guild_id = ? AND enabled = 1
            """,
            (guild_id,),
        )
        source_count = source_cursor.rowcount
        target_count = target_cursor.rowcount
        await self.log(
            guild_id,
            "forwarder_clear_everything",
            user_id=user_id,
            details={
                "guild_id": guild_id,
                "cleared_count_sources": source_count,
                "cleared_count_targets": target_count,
            },
        )
        return source_count, target_count

    async def save_panel(self, guild_id: int, channel_id: int, message_id: int) -> None:
        await self.db.execute(
            """
            INSERT INTO forwarder_panels (guild_id, channel_id, message_id, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                message_id = excluded.message_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (guild_id, channel_id, message_id),
        )
        await self.db.save_persistent_view(
            guild_id,
            MODULE_NAME,
            channel_id,
            message_id,
            MANAGEMENT_VIEW_TYPE,
            state={},
        )

    async def get_panel(self, guild_id: int):
        return await self.db.fetchone(
            "SELECT * FROM forwarder_panels WHERE guild_id = ?",
            (guild_id,),
        )

    def build_management_embed(self) -> nextcord.Embed:
        embed = nextcord.Embed(
            title="Forwarder",
            description="Forward messages from source channels to targets by type.",
            color=DEFAULT_COLOR,
        )
        embed.add_field(name="Types", value=", ".join(DEFAULT_TYPES), inline=False)
        embed.add_field(name="Navigation", value="Navigation edits this panel message.", inline=False)
        return embed

    async def build_settings_embed(self, guild_id: int) -> nextcord.Embed:
        enabled = await self.is_enabled(guild_id)
        sources = await self.list_sources(guild_id)
        targets = await self.list_targets(guild_id)
        type_row = await self.db.fetchone("SELECT COUNT(*) AS count FROM forwarder_types")
        embed = nextcord.Embed(title="Forwarder Settings", color=DEFAULT_COLOR)
        embed.add_field(name="Enabled", value=str(enabled), inline=True)
        embed.add_field(name="Sources", value=str(len(sources)), inline=True)
        embed.add_field(name="Targets", value=str(len(targets)), inline=True)
        embed.add_field(name="Types", value=str(type_row["count"] if type_row else 0), inline=True)
        embed.add_field(name="Bot/Webhook Source Messages Allowed", value="Yes", inline=True)
        embed.add_field(name="Own Bot Messages Ignored", value="Yes", inline=True)
        return embed

    def format_mapping_list(self, rows, empty_text: str) -> str:
        if not rows:
            return empty_text
        return "\n".join(f"Type: {row['type_name']} -> <#{row['channel_id']}>" for row in rows)

    def build_forward_payload(self, message: nextcord.Message) -> tuple[str | None, list[nextcord.Embed]]:
        parts: list[str] = []
        if message.content:
            parts.append(message.content)
        if message.attachments:
            parts.extend(attachment.url for attachment in message.attachments)
        content = "\n".join(parts) if parts else None
        return content, list(message.embeds)

    def safe_allowed_mentions(self) -> nextcord.AllowedMentions:
        return nextcord.AllowedMentions.none()

    async def import_old_mappings_later(self) -> None:
        """Placeholder for a future manual migration path from old mapping files."""
        return None

    async def log(
        self,
        guild_id: int,
        action: str,
        user_id: int | None = None,
        target_id: int | None = None,
        details: dict | None = None,
    ) -> None:
        await self.db.log_action(
            guild_id,
            MODULE_NAME,
            action,
            user_id=user_id,
            target_id=target_id,
            details=details or {},
        )
