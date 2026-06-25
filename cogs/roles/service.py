import secrets

import nextcord

from core.emoji import replace_custom_emoji_keys
from core.embeds import DEFAULT_COLOR


MODULE_NAME = "roles"
ADMIN_VIEW_TYPE = "roles_admin_panel"
PUBLIC_VIEW_TYPE = "roles_public_panel"
STYLE_MAP = {
    "Primary (Blue)": nextcord.ButtonStyle.primary,
    "Secondary (Gray)": nextcord.ButtonStyle.secondary,
    "Success (Green)": nextcord.ButtonStyle.success,
    "Danger (Red)": nextcord.ButtonStyle.danger,
}


class RolesService:
    def __init__(self, bot) -> None:
        self.bot = bot
        self.db = bot.db

    async def save_admin_panel(self, guild_id: int, channel_id: int, message_id: int) -> None:
        await self.db.execute(
            """
            INSERT INTO roles_admin_panels (guild_id, channel_id, message_id, updated_at)
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
        return await self.db.fetchone("SELECT * FROM roles_admin_panels WHERE guild_id = ?", (guild_id,))

    async def create_panel(
        self,
        guild_id: int,
        channel_id: int,
        description: str,
        title: str | None,
        image_url: str | None,
        thumbnail_url: str | None,
        created_by: int,
    ):
        panel_key = await self.generate_panel_key()
        clean_description = self.normalize_text(description)
        clean_title = replace_custom_emoji_keys(title.strip()) if title and title.strip() else None
        clean_image = image_url.strip() if image_url and image_url.strip() else None
        clean_thumbnail = thumbnail_url.strip() if thumbnail_url and thumbnail_url.strip() else None
        cursor = await self.db.execute(
            """
            INSERT INTO role_panels (
                guild_id,
                channel_id,
                message_id,
                panel_key,
                title,
                description,
                image_url,
                thumbnail_url,
                enabled,
                created_by,
                created_at,
                updated_at
            )
            VALUES (?, ?, 0, ?, ?, ?, ?, ?, 1, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (guild_id, channel_id, panel_key, clean_title, clean_description, clean_image, clean_thumbnail, created_by),
        )
        return await self.get_panel_by_id(int(cursor.lastrowid))

    async def update_panel_message(self, panel_id: int, message_id: int) -> None:
        await self.db.execute(
            "UPDATE role_panels SET message_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (message_id, panel_id),
        )

    async def get_panel_by_id(self, panel_id: int):
        return await self.db.fetchone("SELECT * FROM role_panels WHERE id = ?", (panel_id,))

    async def get_panel_by_message(self, message_id: int):
        return await self.db.fetchone(
            "SELECT * FROM role_panels WHERE message_id = ? AND enabled = 1",
            (message_id,),
        )

    async def list_panels(self, guild_id: int):
        return await self.db.fetchall(
            """
            SELECT p.*,
                (SELECT COUNT(*) FROM role_buttons b WHERE b.panel_id = p.id AND b.enabled = 1) AS button_count
            FROM role_panels p
            WHERE p.guild_id = ? AND p.enabled = 1
            ORDER BY p.updated_at DESC, p.id DESC
            """,
            (guild_id,),
        )

    async def add_button(
        self,
        panel_id: int,
        label: str,
        role_ids: list[int],
        style: str,
        position: int | None,
        nick_change: bool,
    ):
        if style not in STYLE_MAP:
            raise ValueError("Style must be one of: " + ", ".join(STYLE_MAP))
        clean_label = replace_custom_emoji_keys(label.strip()) or label.strip()
        if not clean_label:
            raise ValueError("Button Label is required.")
        if position is None:
            row = await self.db.fetchone(
                "SELECT COALESCE(MAX(position), -1) + 1 AS next_position FROM role_buttons WHERE panel_id = ?",
                (panel_id,),
            )
            position = int(row["next_position"] or 0)
        existing = await self.db.fetchone(
            "SELECT * FROM role_buttons WHERE panel_id = ? AND lower(label) = lower(?)",
            (panel_id, clean_label),
        )
        if existing is None:
            cursor = await self.db.execute(
                """
                INSERT INTO role_buttons (
                    panel_id,
                    label,
                    style,
                    nick_change,
                    position,
                    enabled,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (panel_id, clean_label, style, int(nick_change), position),
            )
            button_id = int(cursor.lastrowid)
        else:
            button_id = existing["id"]
            await self.db.execute(
                """
                UPDATE role_buttons
                SET label = ?, style = ?, nick_change = ?, position = ?, enabled = 1, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (clean_label, style, int(nick_change), position, button_id),
            )
        await self.add_roles_to_button(button_id, role_ids)
        return await self.get_button(button_id)

    async def add_roles_to_button(self, button_id: int, role_ids: list[int]) -> int:
        added = 0
        for index, role_id in enumerate(role_ids):
            cursor = await self.db.execute(
                """
                INSERT OR IGNORE INTO role_button_roles (button_id, role_id, position)
                VALUES (?, ?, ?)
                """,
                (button_id, role_id, index),
            )
            added += max(cursor.rowcount, 0)
        return added

    async def list_buttons(self, panel_id: int):
        return await self.db.fetchall(
            "SELECT * FROM role_buttons WHERE panel_id = ? AND enabled = 1 ORDER BY position, id",
            (panel_id,),
        )

    async def get_button(self, button_id: int):
        return await self.db.fetchone("SELECT * FROM role_buttons WHERE id = ?", (button_id,))

    async def get_button_roles(self, button_id: int):
        return await self.db.fetchall(
            "SELECT * FROM role_button_roles WHERE button_id = ? ORDER BY position, id",
            (button_id,),
        )

    async def disable_button(self, button_id: int) -> None:
        await self.db.execute(
            "UPDATE role_buttons SET enabled = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (button_id,),
        )

    async def clear_button_roles(self, button_id: int) -> tuple[int, bool]:
        rows = await self.get_button_roles(button_id)
        if not rows:
            await self.disable_button(button_id)
            return 0, False
        keep_id = rows[0]["id"]
        cursor = await self.db.execute(
            "DELETE FROM role_button_roles WHERE button_id = ? AND id != ?",
            (button_id, keep_id),
        )
        return cursor.rowcount, True

    async def disable_panel(self, panel_id: int) -> None:
        await self.db.execute(
            "UPDATE role_panels SET enabled = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (panel_id,),
        )
        await self.db.execute(
            "UPDATE role_buttons SET enabled = 0, updated_at = CURRENT_TIMESTAMP WHERE panel_id = ?",
            (panel_id,),
        )

    def build_admin_embed(self) -> nextcord.Embed:
        embed = nextcord.Embed(
            title="Roles",
            description="Create public role button panels and manage button role bundles.",
            color=DEFAULT_COLOR,
        )
        embed.add_field(name="Navigation", value="Navigation edits this panel message.", inline=False)
        return embed

    async def build_settings_embed(self, guild_id: int) -> nextcord.Embed:
        panels = await self.list_panels(guild_id)
        buttons = await self.db.fetchone(
            """
            SELECT COUNT(*) AS count
            FROM role_buttons b
            JOIN role_panels p ON p.id = b.panel_id
            WHERE p.guild_id = ? AND p.enabled = 1 AND b.enabled = 1
            """,
            (guild_id,),
        )
        mappings = await self.db.fetchone(
            """
            SELECT COUNT(*) AS count
            FROM role_button_roles r
            JOIN role_buttons b ON b.id = r.button_id
            JOIN role_panels p ON p.id = b.panel_id
            WHERE p.guild_id = ? AND p.enabled = 1 AND b.enabled = 1
            """,
            (guild_id,),
        )
        restored = await self.db.fetchone(
            "SELECT COUNT(*) AS count FROM persistent_views WHERE guild_id = ? AND module_name = ? AND view_type = ?",
            (guild_id, MODULE_NAME, PUBLIC_VIEW_TYPE),
        )
        nick_change = await self.db.fetchone(
            """
            SELECT COUNT(*) AS count
            FROM role_buttons b
            JOIN role_panels p ON p.id = b.panel_id
            WHERE p.guild_id = ? AND p.enabled = 1 AND b.enabled = 1 AND b.nick_change = 1
            """,
            (guild_id,),
        )
        embed = nextcord.Embed(title="Roles Settings", color=DEFAULT_COLOR)
        embed.add_field(name="Status", value="Enabled", inline=True)
        embed.add_field(name="Role Panels", value=str(len(panels)), inline=True)
        embed.add_field(name="Buttons", value=str(buttons["count"] if buttons else 0), inline=True)
        embed.add_field(name="Assigned Role Mappings", value=str(mappings["count"] if mappings else 0), inline=True)
        embed.add_field(name="Public Views Stored", value=str(restored["count"] if restored else 0), inline=True)
        embed.add_field(name="Nick Change Buttons", value=str(nick_change["count"] if nick_change else 0), inline=True)
        return embed

    def build_public_embed(self, panel) -> nextcord.Embed:
        embed = nextcord.Embed(
            title=panel["title"] or None,
            description=panel["description"],
            color=DEFAULT_COLOR,
        )
        if panel["image_url"]:
            embed.set_image(url=panel["image_url"])
        if panel["thumbnail_url"]:
            embed.set_thumbnail(url=panel["thumbnail_url"])
        return embed

    async def generate_panel_key(self) -> str:
        for _ in range(10):
            key = secrets.token_hex(3).upper()
            row = await self.db.fetchone("SELECT 1 FROM role_panels WHERE panel_key = ?", (key,))
            if row is None:
                return key
        return secrets.token_hex(6).upper()

    def normalize_text(self, value: str) -> str:
        return replace_custom_emoji_keys(replace_single_pipes_with_newlines(value.strip())) or value.strip()

    def parse_role_ids(self, value: str) -> list[int]:
        role_ids: list[int] = []
        for part in value.replace(",", " ").split():
            if not part.strip():
                continue
            role_ids.append(int(part.strip()))
        deduped = list(dict.fromkeys(role_ids))
        if not deduped:
            raise ValueError("Provide at least one role ID.")
        return deduped

    def style_to_button_style(self, style: str) -> nextcord.ButtonStyle:
        return STYLE_MAP.get(style, nextcord.ButtonStyle.primary)

    def validate_roles(self, guild: nextcord.Guild, role_ids: list[int]) -> list[nextcord.Role]:
        roles: list[nextcord.Role] = []
        for role_id in role_ids:
            role = guild.get_role(role_id)
            if role is None:
                raise ValueError(f"Role `{role_id}` was not found in this server.")
            roles.append(role)
        return roles

    def validate_bot_can_manage_roles(self, guild: nextcord.Guild, roles: list[nextcord.Role]) -> None:
        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            raise ValueError("I need Manage Roles permission.")
        if any(role >= bot_member.top_role for role in roles):
            raise ValueError("❌ I cannot manage one or more of these roles. Move my bot role above them.")

    def format_panel_option(self, guild: nextcord.Guild, panel) -> tuple[str, str]:
        channel = guild.get_channel(panel["channel_id"])
        channel_name = f"#{channel.name}" if channel else f"#{panel['channel_id']}"
        preview = (panel["description"] or "").replace("\n", " ")[:45]
        return f"{panel['panel_key']} {channel_name}", preview

    async def log(
        self,
        guild_id: int,
        action: str,
        user_id: int | None = None,
        target_id: int | None = None,
        details: dict | None = None,
    ) -> None:
        await self.db.log_action(guild_id, MODULE_NAME, action, user_id=user_id, target_id=target_id, details=details or {})


def replace_single_pipes_with_newlines(value: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(value):
        if value[index : index + 2] == "||":
            output.append("||")
            index += 2
        elif value[index] == "|":
            output.append("\n")
            index += 1
        else:
            output.append(value[index])
            index += 1
    return "".join(output)
