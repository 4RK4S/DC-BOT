import secrets
import sqlite3

import nextcord

from core.emoji import replace_custom_emoji_keys
from core.embeds import DEFAULT_COLOR


MODULE_NAME = "requests"
ADMIN_VIEW_TYPE = "requests_admin_panel"
PUBLIC_VIEW_TYPE = "requests_public_panel"
REVIEW_VIEW_TYPE = "requests_review_panel"


class RequestsService:
    def __init__(self, bot) -> None:
        self.bot = bot
        self.db = bot.db

    async def save_admin_panel(self, guild_id: int, channel_id: int, message_id: int) -> None:
        await self.db.execute(
            """
            INSERT INTO requests_admin_panels (guild_id, channel_id, message_id, updated_at)
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
        return await self.db.fetchone("SELECT * FROM requests_admin_panels WHERE guild_id = ?", (guild_id,))

    async def create_panel(
        self,
        guild_id: int,
        request_channel_id: int,
        review_channel_id: int,
        role_id: int,
        title: str | None,
        message: str,
        image_url: str | None,
        created_by: int,
    ):
        clean_image_url = self.validate_image_url(image_url)
        panel_key = await self.generate_panel_key()
        cursor = await self.db.execute(
            """
            INSERT INTO request_panels (
                guild_id,
                request_channel_id,
                review_channel_id,
                role_id,
                panel_key,
                title,
                message,
                image_url,
                thumbnail_url,
                enabled,
                created_by,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                guild_id,
                request_channel_id,
                review_channel_id,
                role_id,
                panel_key,
                replace_custom_emoji_keys(title.strip()) if title and title.strip() else None,
                self.normalize_text(message),
                clean_image_url,
                created_by,
            ),
        )
        return await self.get_panel(int(cursor.lastrowid))

    async def set_image_url(self, panel_id: int, image_url: str | None) -> None:
        clean_image_url = self.validate_image_url(image_url)
        await self.db.execute(
            """
            UPDATE request_panels
            SET image_url = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (clean_image_url, panel_id),
        )

    async def update_public_message(self, panel_id: int, message_id: int) -> None:
        await self.db.execute(
            "UPDATE request_panels SET public_message_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (message_id, panel_id),
        )

    async def get_panel(self, panel_id: int):
        return await self.db.fetchone("SELECT * FROM request_panels WHERE id = ?", (panel_id,))

    async def list_panels(self, guild_id: int):
        return await self.db.fetchall(
            """
            SELECT *
            FROM request_panels
            WHERE guild_id = ? AND enabled = 1
            ORDER BY updated_at DESC, id DESC
            """,
            (guild_id,),
        )

    async def disable_panel(self, panel_id: int) -> None:
        await self.db.execute(
            "UPDATE request_panels SET enabled = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (panel_id,),
        )
        await self.db.execute(
            """
            UPDATE request_status
            SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
            WHERE panel_id = ? AND status = 'pending'
            """,
            (panel_id,),
        )

    async def pending_for_user(self, panel_id: int, user_id: int):
        return await self.get_pending_status(panel_id, user_id)

    async def user_has_role(self, guild: nextcord.Guild, user_id: int, role_id: int) -> bool:
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except (nextcord.Forbidden, nextcord.HTTPException, nextcord.NotFound):
                return False
        return any(role.id == role_id for role in member.roles)

    async def get_existing_statuses(self, panel_id: int, user_id: int):
        return await self.db.fetchall(
            """
            SELECT *
            FROM request_status
            WHERE panel_id = ? AND user_id = ?
            ORDER BY id DESC
            """,
            (panel_id, user_id),
        )

    async def get_pending_status(self, panel_id: int, user_id: int):
        return await self.db.fetchone(
            """
            SELECT *
            FROM request_status
            WHERE panel_id = ? AND user_id = ? AND status = 'pending'
            ORDER BY id DESC
            LIMIT 1
            """,
            (panel_id, user_id),
        )

    async def get_approved_status(self, panel_id: int, user_id: int):
        return await self.db.fetchone(
            """
            SELECT *
            FROM request_status
            WHERE panel_id = ? AND user_id = ? AND status = 'approved'
            ORDER BY id DESC
            LIMIT 1
            """,
            (panel_id, user_id),
        )

    async def ensure_user_role(self, guild: nextcord.Guild, user_id: int, role_id: int, reason: str) -> bool:
        role = guild.get_role(role_id)
        if role is None:
            return False
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except (nextcord.Forbidden, nextcord.HTTPException, nextcord.NotFound):
                return False
        if any(member_role.id == role_id for member_role in member.roles):
            return True
        try:
            self.validate_bot_can_manage_role(guild, role)
            await member.add_roles(role, reason=reason)
            return True
        except (ValueError, nextcord.Forbidden, nextcord.HTTPException):
            return False

    async def create_status(self, panel, user_id: int):
        try:
            cursor = await self.db.execute(
                """
                INSERT INTO request_status (
                    guild_id,
                    panel_id,
                    user_id,
                    review_channel_id,
                    status,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, 'pending', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (panel["guild_id"], panel["id"], user_id, panel["review_channel_id"]),
            )
        except sqlite3.IntegrityError:
            return await self.get_pending_status(panel["id"], user_id)
        return await self.get_status(int(cursor.lastrowid))

    async def create_pending_request_if_allowed(self, guild: nextcord.Guild, panel, user_id: int) -> tuple[str, object | None]:
        role_id = int(panel["role_id"])
        if await self.user_has_role(guild, user_id, role_id):
            return "has_access", None
        if await self.get_pending_status(panel["id"], user_id):
            return "pending", None
        if await self.get_approved_status(panel["id"], user_id):
            await self.ensure_user_role(guild, user_id, role_id, "Request access restored")
            return "approved", None
        status = await self.create_status(panel, user_id)
        if status is None:
            if await self.get_pending_status(panel["id"], user_id):
                return "pending", None
            if await self.get_approved_status(panel["id"], user_id):
                await self.ensure_user_role(guild, user_id, role_id, "Request access restored")
                return "approved", None
            return "error", None
        return "created", status

    async def update_review_message(self, status_id: int, review_message_id: int) -> None:
        await self.db.execute(
            "UPDATE request_status SET review_message_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (review_message_id, status_id),
        )

    async def get_status(self, status_id: int):
        return await self.db.fetchone("SELECT * FROM request_status WHERE id = ?", (status_id,))

    async def get_status_by_review_message(self, message_id: int):
        return await self.db.fetchone(
            "SELECT * FROM request_status WHERE review_message_id = ?",
            (message_id,),
        )

    async def set_status(self, status_id: int, status: str) -> None:
        try:
            await self.db.execute(
                "UPDATE request_status SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, status_id),
            )
        except sqlite3.IntegrityError:
            return

    async def delete_status(self, status_id: int) -> None:
        await self.db.execute("DELETE FROM request_status WHERE id = ?", (status_id,))

    async def set_status_safely(self, status_id: int, status: str):
        current = await self.get_status(status_id)
        if current is None:
            return "missing", None
        if current["status"] == status:
            return f"already_{status}", current
        if current["status"] == "approved":
            return "already_approved", current
        if current["status"] == "denied":
            return "already_denied", current
        if status == "approved":
            approved = await self.get_approved_status(current["panel_id"], current["user_id"])
            if approved is not None and approved["id"] != current["id"]:
                await self.delete_status(current["id"])
                if current["review_message_id"]:
                    await self.db.delete_persistent_view(current["review_message_id"])
                return "duplicate_approved", approved
        try:
            await self.db.execute(
                "UPDATE request_status SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, status_id),
            )
        except sqlite3.IntegrityError:
            if status == "approved":
                approved = await self.get_approved_status(current["panel_id"], current["user_id"])
                if approved is not None:
                    await self.delete_status(current["id"])
                    if current["review_message_id"]:
                        await self.db.delete_persistent_view(current["review_message_id"])
                    return "duplicate_approved", approved
            return "integrity_error", current
        return "updated", await self.get_status(status_id)

    async def clear_pending(self, guild_id: int) -> int:
        cursor = await self.db.execute(
            """
            UPDATE request_status
            SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
            WHERE guild_id = ? AND status = 'pending'
            """,
            (guild_id,),
        )
        return cursor.rowcount

    async def counts(self, guild_id: int) -> dict[str, int]:
        panels = await self.db.fetchone(
            "SELECT COUNT(*) AS count FROM request_panels WHERE guild_id = ? AND enabled = 1",
            (guild_id,),
        )
        image_panels = await self.db.fetchone(
            """
            SELECT COUNT(*) AS count
            FROM request_panels
            WHERE guild_id = ? AND enabled = 1 AND image_url IS NOT NULL AND trim(image_url) != ''
            """,
            (guild_id,),
        )
        rows = await self.db.fetchall(
            """
            SELECT status, COUNT(*) AS count
            FROM request_status
            WHERE guild_id = ?
            GROUP BY status
            """,
            (guild_id,),
        )
        data = {
            "active_panels": int(panels["count"] if panels else 0),
            "image_panels": int(image_panels["count"] if image_panels else 0),
            "pending": 0,
            "approved": 0,
            "denied": 0,
        }
        for row in rows:
            if row["status"] in data:
                data[row["status"]] = int(row["count"])
        return data

    def build_admin_embed(self) -> nextcord.Embed:
        embed = nextcord.Embed(
            title="Requests",
            description="Create public request panels and review access requests.",
            color=DEFAULT_COLOR,
        )
        embed.add_field(name="Navigation", value="Navigation edits this panel message.", inline=False)
        return embed

    def build_public_embed(self, panel) -> nextcord.Embed:
        embed = nextcord.Embed(title=panel["title"] or None, description=panel["message"], color=DEFAULT_COLOR)
        if panel["image_url"]:
            embed.set_image(url=panel["image_url"])
        if panel["thumbnail_url"]:
            embed.set_thumbnail(url=panel["thumbnail_url"])
        return embed

    def build_review_embed(self, panel, member: nextcord.Member) -> nextcord.Embed:
        embed = nextcord.Embed(description=panel["message"], color=0xF39C12)
        embed.set_author(name=f"Request from {member}", icon_url=str(member.display_avatar.url))
        embed.add_field(name="Panel", value=f"`{panel['panel_key']}`", inline=True)
        embed.add_field(name="Role", value=f"<@&{panel['role_id']}>", inline=True)
        return embed

    async def build_settings_embed(self, guild_id: int) -> nextcord.Embed:
        counts = await self.counts(guild_id)
        embed = nextcord.Embed(title="Requests Settings", color=DEFAULT_COLOR)
        embed.add_field(name="Active Panels", value=str(counts["active_panels"]), inline=True)
        embed.add_field(name="Pending", value=str(counts["pending"]), inline=True)
        embed.add_field(name="Approved", value=str(counts["approved"]), inline=True)
        embed.add_field(name="Denied", value=str(counts["denied"]), inline=True)
        embed.add_field(name="Panels With Image URL", value=str(counts["image_panels"]), inline=True)
        embed.add_field(name="Duplicate Protection Enabled", value="Yes", inline=True)
        embed.add_field(name="Role Pre-Check Enabled", value="Yes", inline=True)
        return embed

    async def generate_panel_key(self) -> str:
        for _ in range(10):
            key = secrets.token_hex(3).upper()
            row = await self.db.fetchone("SELECT 1 FROM request_panels WHERE panel_key = ?", (key,))
            if row is None:
                return key
        return secrets.token_hex(6).upper()

    def normalize_text(self, value: str) -> str:
        return replace_custom_emoji_keys(replace_single_pipes_with_newlines(value.strip())) or value.strip()

    def validate_image_url(self, value: str | None) -> str | None:
        clean = (value or "").strip()
        if not clean:
            return None
        if not clean.startswith(("http://", "https://")):
            raise ValueError("Image URL must start with http:// or https://")
        return clean

    def validate_bot_can_manage_role(self, guild: nextcord.Guild, role: nextcord.Role) -> None:
        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            raise ValueError("I need Manage Roles permission.")
        if role >= bot_member.top_role:
            raise ValueError("❌ I cannot manage this role. Move my bot role above it.")

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
