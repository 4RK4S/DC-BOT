import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import aiosqlite


CURRENT_SCHEMA_VERSION = 1


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(self.path)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.execute("PRAGMA foreign_keys = ON")
        await self.connection.commit()

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.close()
            self.connection = None

    async def execute(self, query: str, params: Iterable[Any] = ()) -> aiosqlite.Cursor:
        connection = self._connection()
        cursor = await connection.execute(query, tuple(params))
        await connection.commit()
        return cursor

    async def fetchone(self, query: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        connection = self._connection()
        async with connection.execute(query, tuple(params)) as cursor:
            return await cursor.fetchone()

    async def fetchall(self, query: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        connection = self._connection()
        async with connection.execute(query, tuple(params)) as cursor:
            rows = await cursor.fetchall()
            return list(rows)

    async def init_schema(self) -> None:
        connection = self._connection()
        await connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                admin_panel_channel_id INTEGER,
                admin_panel_message_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS module_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                module_name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                settings_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(guild_id, module_name)
            );

            CREATE TABLE IF NOT EXISTS persistent_views (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                module_name TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                view_type TEXT NOT NULL,
                state_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(message_id)
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                module_name TEXT NOT NULL,
                action TEXT NOT NULL,
                user_id INTEGER,
                target_id INTEGER,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS creator_code_pools (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                keywords TEXT NOT NULL,
                key_words TEXT,
                expires_at TEXT,
                expire_at TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                enabled INTEGER NOT NULL DEFAULT 1,
                announcement_message_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS creator_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                pool_id INTEGER,
                button_id INTEGER,
                code TEXT NOT NULL,
                description TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                used INTEGER NOT NULL DEFAULT 0,
                user_id INTEGER,
                nick TEXT,
                claimed_at TEXT,
                used_by_user_id INTEGER,
                used_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (pool_id) REFERENCES creator_code_pools(id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS creator_code_settings (
                guild_id INTEGER PRIMARY KEY,
                public_channel_id INTEGER,
                public_message_id INTEGER,
                announcement_channel_id INTEGER,
                ping_role_id INTEGER,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS creator_codes_admin_panels (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS live_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                source TEXT,
                reward TEXT,
                note TEXT,
                source_url TEXT,
                expires_at TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS live_code_panels (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                message_id INTEGER,
                role_id INTEGER,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS live_code_settings (
                guild_id INTEGER PRIMARY KEY,
                announcement_channel_id INTEGER,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS forwarder_types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS forwarder_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                type_name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(guild_id, channel_id)
            );

            CREATE TABLE IF NOT EXISTS forwarder_targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                type_name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(guild_id, channel_id, type_name)
            );

            CREATE TABLE IF NOT EXISTS forwarder_panels (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS listener_types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL UNIQUE,
                code TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS listener_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(guild_id, channel_id),
                UNIQUE(guild_id, code)
            );

            CREATE TABLE IF NOT EXISTS listener_targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                message_location TEXT NOT NULL DEFAULT 'before',
                message TEXT NOT NULL DEFAULT '',
                message_link TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS listener_panels (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS welcome_settings (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER,
                enabled INTEGER NOT NULL DEFAULT 1,
                background_url TEXT,
                message_text TEXT NOT NULL DEFAULT '<@{user_id}>|Welcome to **{guild_name}**!',
                image_enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS welcome_panels (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS role_panels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER,
                panel_key TEXT,
                title TEXT,
                description TEXT NOT NULL DEFAULT '',
                image_url TEXT,
                thumbnail_url TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_by INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS role_buttons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                panel_id INTEGER NOT NULL,
                label TEXT NOT NULL,
                style TEXT NOT NULL DEFAULT 'Primary (Blue)',
                nick_change INTEGER NOT NULL DEFAULT 0,
                position INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (panel_id) REFERENCES role_panels(id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS role_button_roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                button_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                UNIQUE(button_id, role_id),
                FOREIGN KEY (button_id) REFERENCES role_buttons(id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS roles_admin_panels (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS request_panels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                request_channel_id INTEGER NOT NULL,
                review_channel_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                public_message_id INTEGER,
                panel_key TEXT NOT NULL UNIQUE,
                title TEXT,
                message TEXT NOT NULL,
                image_url TEXT,
                thumbnail_url TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_by INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS request_status (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                panel_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                review_channel_id INTEGER NOT NULL,
                review_message_id INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(panel_id, user_id, status)
            );

            CREATE TABLE IF NOT EXISTS requests_admin_panels (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS server_boost_settings (
                guild_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                channel_id INTEGER,
                role_id INTEGER,
                image_url TEXT,
                message_template TEXT,
                delete_message_on_expire INTEGER NOT NULL DEFAULT 1,
                remove_role_on_expire INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS server_boost_posts (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                message_id INTEGER,
                channel_id INTEGER,
                boosted_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_module_settings_guild_id
                ON module_settings(guild_id);
            CREATE INDEX IF NOT EXISTS idx_persistent_views_guild_id
                ON persistent_views(guild_id);
            CREATE INDEX IF NOT EXISTS idx_audit_logs_guild_id
                ON audit_logs(guild_id);
            CREATE INDEX IF NOT EXISTS idx_creator_code_pools_guild_id
                ON creator_code_pools(guild_id);
            CREATE INDEX IF NOT EXISTS idx_creator_codes_pool_id
                ON creator_codes(pool_id);
            CREATE INDEX IF NOT EXISTS idx_live_codes_guild_id
                ON live_codes(guild_id);
            CREATE INDEX IF NOT EXISTS idx_forwarder_sources_channel
                ON forwarder_sources(channel_id);
            CREATE INDEX IF NOT EXISTS idx_forwarder_targets_type
                ON forwarder_targets(type_name);
            CREATE INDEX IF NOT EXISTS idx_listener_sources_channel
                ON listener_sources(channel_id);
            CREATE INDEX IF NOT EXISTS idx_listener_sources_code
                ON listener_sources(code);
            CREATE INDEX IF NOT EXISTS idx_listener_targets_code
                ON listener_targets(code);
            CREATE INDEX IF NOT EXISTS idx_listener_targets_guild_id
                ON listener_targets(guild_id);
            CREATE INDEX IF NOT EXISTS idx_welcome_settings_channel_id
                ON welcome_settings(channel_id);
            CREATE INDEX IF NOT EXISTS idx_role_panels_guild_id
                ON role_panels(guild_id);
            CREATE INDEX IF NOT EXISTS idx_role_buttons_panel_id
                ON role_buttons(panel_id);
            CREATE INDEX IF NOT EXISTS idx_role_button_roles_button_id
                ON role_button_roles(button_id);
            CREATE INDEX IF NOT EXISTS idx_request_panels_guild_id
                ON request_panels(guild_id);
            CREATE INDEX IF NOT EXISTS idx_request_status_panel_user
                ON request_status(panel_id, user_id);
            CREATE INDEX IF NOT EXISTS idx_request_status_review_message
                ON request_status(review_message_id);
            CREATE INDEX IF NOT EXISTS idx_server_boost_posts_guild_id
                ON server_boost_posts(guild_id);
            """
        )
        await connection.execute(
            """
            INSERT OR IGNORE INTO schema_version (version)
            VALUES (?)
            """,
            (CURRENT_SCHEMA_VERSION,),
        )
        await self._ensure_columns(
            "creator_code_settings",
            {
                "public_channel_id": "INTEGER",
                "public_message_id": "INTEGER",
                "announcement_channel_id": "INTEGER",
                "ping_role_id": "INTEGER",
                "enabled": "INTEGER NOT NULL DEFAULT 1",
                "updated_at": "TEXT",
            },
        )
        await self._ensure_columns(
            "creator_code_pools",
            {
                "guild_id": "INTEGER",
                "name": "TEXT",
                "keywords": "TEXT",
                "key_words": "TEXT",
                "expires_at": "TEXT",
                "expire_at": "TEXT",
                "active": "INTEGER NOT NULL DEFAULT 1",
                "enabled": "INTEGER NOT NULL DEFAULT 1",
                "announcement_message_id": "INTEGER",
                "created_at": "TEXT",
                "updated_at": "TEXT",
            },
        )
        await self._ensure_columns(
            "creator_codes",
            {
                "guild_id": "INTEGER",
                "pool_id": "INTEGER",
                "button_id": "INTEGER",
                "code": "TEXT",
                "description": "TEXT",
                "enabled": "INTEGER NOT NULL DEFAULT 1",
                "used": "INTEGER NOT NULL DEFAULT 0",
                "user_id": "INTEGER",
                "nick": "TEXT",
                "claimed_at": "TEXT",
                "used_by_user_id": "INTEGER",
                "used_at": "TEXT",
                "created_at": "TEXT",
                "updated_at": "TEXT",
            },
        )
        await self._ensure_columns(
            "creator_codes_admin_panels",
            {
                "channel_id": "INTEGER",
                "message_id": "INTEGER",
                "updated_at": "TEXT",
            },
        )
        await self._migrate_creator_codes_schema()
        await self._ensure_columns(
            "live_codes",
            {
                "guild_id": "INTEGER",
                "code": "TEXT",
                "source": "TEXT",
                "reward": "TEXT",
                "note": "TEXT",
                "source_url": "TEXT",
                "expires_at": "TEXT",
                "active": "INTEGER NOT NULL DEFAULT 1",
                "created_at": "TEXT",
                "updated_at": "TEXT",
            },
        )
        await self._ensure_columns(
            "live_code_settings",
            {
                "announcement_channel_id": "INTEGER",
                "updated_at": "TEXT",
            },
        )
        await self._migrate_live_codes_schema()
        await self._ensure_columns(
            "role_panels",
            {
                "panel_key": "TEXT",
                "title": "TEXT",
                "description": "TEXT NOT NULL DEFAULT ''",
                "image_url": "TEXT",
                "thumbnail_url": "TEXT",
                "enabled": "INTEGER NOT NULL DEFAULT 1",
                "created_by": "INTEGER",
                "created_at": "TEXT",
                "updated_at": "TEXT",
            },
        )
        await self._ensure_columns(
            "role_buttons",
            {
                "style": "TEXT NOT NULL DEFAULT 'Primary (Blue)'",
                "nick_change": "INTEGER NOT NULL DEFAULT 0",
                "position": "INTEGER NOT NULL DEFAULT 0",
                "enabled": "INTEGER NOT NULL DEFAULT 1",
                "created_at": "TEXT",
                "updated_at": "TEXT",
            },
        )
        await self._ensure_columns(
            "role_button_roles",
            {
                "position": "INTEGER NOT NULL DEFAULT 0",
            },
        )
        await self._ensure_columns(
            "roles_admin_panels",
            {
                "channel_id": "INTEGER",
                "message_id": "INTEGER",
                "updated_at": "TEXT",
            },
        )
        await self._migrate_role_schema()
        await connection.commit()

    async def get_existing_columns(self, connection: aiosqlite.Connection, table_name: str) -> set[str]:
        async with connection.execute(f"PRAGMA table_info({table_name})") as cursor:
            return {row[1] for row in await cursor.fetchall()}

    async def add_column_if_missing(
        self,
        connection: aiosqlite.Connection,
        table_name: str,
        column_sql: str,
    ) -> None:
        column_name = column_sql.strip().split()[0]
        existing = await self.get_existing_columns(connection, table_name)
        if column_name not in existing:
            await connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}")

    async def _ensure_columns(self, table: str, columns: Mapping[str, str]) -> None:
        connection = self._connection()
        for name, definition in columns.items():
            await self.add_column_if_missing(connection, table, f"{name} {definition}")

    async def _migrate_creator_codes_schema(self) -> None:
        connection = self._connection()
        now = datetime.now(timezone.utc).isoformat()
        await connection.execute(
            """
            UPDATE creator_code_pools
            SET key_words = COALESCE(NULLIF(trim(key_words), ''), NULLIF(trim(keywords), ''), NULLIF(trim(name), ''), 'Creator Code Pool ' || id)
            WHERE key_words IS NULL OR trim(key_words) = ''
            """
        )
        await connection.execute(
            """
            UPDATE creator_code_pools
            SET expire_at = COALESCE(NULLIF(trim(expire_at), ''), NULLIF(trim(expires_at), ''))
            WHERE expire_at IS NULL OR trim(expire_at) = ''
            """
        )
        await connection.execute(
            """
            UPDATE creator_code_pools
            SET enabled = COALESCE(enabled, active, 1)
            """
        )
        await connection.execute(
            """
            UPDATE creator_code_pools
            SET name = COALESCE(NULLIF(trim(name), ''), key_words),
                keywords = COALESCE(NULLIF(trim(keywords), ''), key_words)
            """
        )
        await connection.execute(
            """
            UPDATE creator_codes
            SET guild_id = (
                    SELECT guild_id
                    FROM creator_code_pools
                    WHERE creator_code_pools.id = creator_codes.pool_id
                )
            WHERE guild_id IS NULL AND pool_id IS NOT NULL
            """
        )
        await connection.execute(
            """
            UPDATE creator_codes
            SET used = CASE
                    WHEN used_by_user_id IS NOT NULL OR used_at IS NOT NULL THEN 1
                    ELSE COALESCE(used, 0)
                END,
                user_id = COALESCE(user_id, used_by_user_id),
                claimed_at = COALESCE(NULLIF(trim(claimed_at), ''), used_at)
            """
        )
        for table in ("creator_code_pools", "creator_codes"):
            await connection.execute(
                f"UPDATE {table} SET created_at = ? WHERE created_at IS NULL OR trim(created_at) = ''",
                (now,),
            )
            await connection.execute(
                f"UPDATE {table} SET updated_at = ? WHERE updated_at IS NULL OR trim(updated_at) = ''",
                (now,),
            )
        await connection.execute(
            "UPDATE creator_codes_admin_panels SET updated_at = ? WHERE updated_at IS NULL OR trim(updated_at) = ''",
            (now,),
        )
        await connection.execute(
            "UPDATE creator_code_settings SET updated_at = ? WHERE updated_at IS NULL OR trim(updated_at) = ''",
            (now,),
        )

        await connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_creator_codes_button_id
            ON creator_codes(button_id)
            """
        )
        await connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_creator_codes_pool_used
            ON creator_codes(pool_id, used)
            """
        )
        await connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_creator_code_pools_guild_enabled
            ON creator_code_pools(guild_id, enabled)
            """
        )

    async def _migrate_live_codes_schema(self) -> None:
        connection = self._connection()
        now = datetime.now(timezone.utc).isoformat()
        await connection.execute(
            "UPDATE live_code_settings SET updated_at = ? WHERE updated_at IS NULL OR trim(updated_at) = ''",
            (now,),
        )

    async def _migrate_role_schema(self) -> None:
        connection = self._connection()
        now = datetime.now(timezone.utc).isoformat()
        await connection.execute(
            """
            UPDATE role_panels
            SET panel_key = 'role-' || guild_id || '-' || id
            WHERE panel_key IS NULL OR trim(panel_key) = ''
            """
        )
        await connection.execute(
            "UPDATE role_panels SET created_at = ? WHERE created_at IS NULL OR trim(created_at) = ''",
            (now,),
        )
        await connection.execute(
            "UPDATE role_panels SET updated_at = ? WHERE updated_at IS NULL OR trim(updated_at) = ''",
            (now,),
        )
        await connection.execute(
            "UPDATE role_buttons SET created_at = ? WHERE created_at IS NULL OR trim(created_at) = ''",
            (now,),
        )
        await connection.execute(
            "UPDATE role_buttons SET updated_at = ? WHERE updated_at IS NULL OR trim(updated_at) = ''",
            (now,),
        )

        rows = await self.fetchall("SELECT id, guild_id, panel_key FROM role_panels ORDER BY id")
        seen: set[str] = set()
        for row in rows:
            panel_key = row["panel_key"] or f"role-{row['guild_id']}-{row['id']}"
            if panel_key in seen:
                base_key = panel_key
                suffix = row["id"]
                panel_key = f"{base_key}-{suffix}"
                while panel_key in seen:
                    suffix += 1
                    panel_key = f"{base_key}-{suffix}"
                await connection.execute(
                    "UPDATE role_panels SET panel_key = ? WHERE id = ?",
                    (panel_key, row["id"]),
                )
            seen.add(panel_key)

        await connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_role_panels_panel_key
            ON role_panels(panel_key)
            """
        )

    async def get_schema_version(self) -> int:
        row = await self.fetchone("SELECT MAX(version) AS version FROM schema_version")
        if row is None or row["version"] is None:
            return 0
        return int(row["version"])

    async def upsert_module_settings(
        self,
        guild_id: int,
        module_name: str,
        enabled: bool = True,
        settings: Mapping[str, Any] | None = None,
    ) -> aiosqlite.Row | None:
        settings_json = _to_json(settings or {})
        await self.execute(
            """
            INSERT INTO module_settings (
                guild_id,
                module_name,
                enabled,
                settings_json,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id, module_name) DO UPDATE SET
                enabled = excluded.enabled,
                settings_json = excluded.settings_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (guild_id, module_name, int(enabled), settings_json),
        )
        return await self.get_module_settings(guild_id, module_name)

    async def get_module_settings(self, guild_id: int, module_name: str) -> aiosqlite.Row | None:
        return await self.fetchone(
            """
            SELECT *
            FROM module_settings
            WHERE guild_id = ? AND module_name = ?
            """,
            (guild_id, module_name),
        )

    async def save_persistent_view(
        self,
        guild_id: int,
        module_name: str,
        channel_id: int,
        message_id: int,
        view_type: str,
        state: Mapping[str, Any] | None = None,
    ) -> aiosqlite.Row | None:
        state_json = _to_json(state or {})
        await self.execute(
            """
            INSERT INTO persistent_views (
                guild_id,
                module_name,
                channel_id,
                message_id,
                view_type,
                state_json,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(message_id) DO UPDATE SET
                guild_id = excluded.guild_id,
                module_name = excluded.module_name,
                channel_id = excluded.channel_id,
                view_type = excluded.view_type,
                state_json = excluded.state_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (guild_id, module_name, channel_id, message_id, view_type, state_json),
        )
        return await self.fetchone(
            "SELECT * FROM persistent_views WHERE message_id = ?",
            (message_id,),
        )

    async def delete_persistent_view(self, message_id: int) -> None:
        await self.execute(
            "DELETE FROM persistent_views WHERE message_id = ?",
            (message_id,),
        )

    async def log_action(
        self,
        guild_id: int,
        module_name: str,
        action: str,
        user_id: int | None = None,
        target_id: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> int:
        cursor = await self.execute(
            """
            INSERT INTO audit_logs (
                guild_id,
                module_name,
                action,
                user_id,
                target_id,
                details_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (guild_id, module_name, action, user_id, target_id, _to_json(details or {})),
        )
        return int(cursor.lastrowid)

    def _connection(self) -> aiosqlite.Connection:
        if self.connection is None:
            raise RuntimeError("Database is not connected")
        return self.connection


def _to_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
