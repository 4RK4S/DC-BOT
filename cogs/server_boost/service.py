from datetime import datetime, timezone

import nextcord

from core.embeds import DEFAULT_COLOR


MODULE_NAME = "server_boost"
DEFAULT_IMAGE_URL = "https://res.cloudinary.com/dmfww0zt8/image/upload/c_scale,w_50/BoosterBadgesRoll.gif"
DEFAULT_MESSAGE_TEMPLATE = (
    "\u2728 **{user}** just boosted the server!\n\n"
    "Thank you {mention} for your support!\n"
    "**Streak:** {streak} month{plural} in a row"
)
BOOST_COLOR = 0xF1C40F


class ServerBoostService:
    def __init__(self, bot) -> None:
        self.bot = bot
        self.db = bot.db

    async def get_settings(self, guild_id: int):
        return await self.db.fetchone("SELECT * FROM server_boost_settings WHERE guild_id = ?", (guild_id,))

    async def ensure_settings(self, guild_id: int):
        await self.db.execute(
            """
            INSERT INTO server_boost_settings (
                guild_id,
                enabled,
                image_url,
                message_template,
                delete_message_on_expire,
                remove_role_on_expire,
                updated_at
            )
            VALUES (?, 1, ?, ?, 1, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id) DO NOTHING
            """,
            (guild_id, DEFAULT_IMAGE_URL, DEFAULT_MESSAGE_TEMPLATE),
        )
        return await self.get_settings(guild_id)

    async def update_settings(self, guild_id: int, **values) -> None:
        await self.ensure_settings(guild_id)
        allowed = {
            "enabled",
            "channel_id",
            "role_id",
            "image_url",
            "message_template",
            "delete_message_on_expire",
            "remove_role_on_expire",
        }
        fields = [key for key in values if key in allowed]
        if not fields:
            return
        assignments = ", ".join(f"{field} = ?" for field in fields)
        params = [values[field] for field in fields]
        params.append(guild_id)
        await self.db.execute(
            f"UPDATE server_boost_settings SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE guild_id = ?",
            params,
        )

    async def set_channel(self, guild_id: int, channel_id: int, user_id: int | None) -> None:
        await self.update_settings(guild_id, channel_id=channel_id)
        await self.log(guild_id, "server_boost_set_channel", user_id=user_id, details={"guild_id": guild_id, "channel_id": channel_id})

    async def set_role(self, guild_id: int, role_id: int | None, user_id: int | None) -> None:
        await self.update_settings(guild_id, role_id=role_id)
        await self.log(guild_id, "server_boost_set_role", user_id=user_id, details={"guild_id": guild_id, "role_id": role_id})

    async def set_image_url(self, guild_id: int, image_url: str | None, user_id: int | None) -> None:
        clean = (image_url or "").strip() or DEFAULT_IMAGE_URL
        if not clean.startswith(("http://", "https://")):
            raise ValueError("Image URL must start with http:// or https://")
        await self.update_settings(guild_id, image_url=clean)
        await self.log(guild_id, "server_boost_set_image", user_id=user_id, details={"guild_id": guild_id, "image_url": clean})

    async def set_message_template(self, guild_id: int, template: str | None, user_id: int | None) -> None:
        clean = (template or "").strip().replace("|", "\n") or DEFAULT_MESSAGE_TEMPLATE
        await self.update_settings(guild_id, message_template=clean)
        await self.log(guild_id, "server_boost_set_message", user_id=user_id, details={"guild_id": guild_id})

    async def toggle_enabled(self, guild_id: int, user_id: int | None) -> bool:
        settings = await self.ensure_settings(guild_id)
        enabled = not bool(settings["enabled"])
        await self.update_settings(guild_id, enabled=int(enabled))
        await self.log(guild_id, "server_boost_toggle", user_id=user_id, details={"guild_id": guild_id, "enabled": enabled})
        return enabled

    async def toggle_delete_on_expire(self, guild_id: int, user_id: int | None) -> bool:
        settings = await self.ensure_settings(guild_id)
        enabled = not bool(settings["delete_message_on_expire"])
        await self.update_settings(guild_id, delete_message_on_expire=int(enabled))
        return enabled

    async def toggle_remove_role_on_expire(self, guild_id: int, user_id: int | None) -> bool:
        settings = await self.ensure_settings(guild_id)
        enabled = not bool(settings["remove_role_on_expire"])
        await self.update_settings(guild_id, remove_role_on_expire=int(enabled))
        return enabled

    async def clear_settings(self, guild_id: int, user_id: int | None) -> None:
        await self.ensure_settings(guild_id)
        await self.db.execute(
            """
            UPDATE server_boost_settings
            SET channel_id = NULL,
                role_id = NULL,
                image_url = ?,
                message_template = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE guild_id = ?
            """,
            (DEFAULT_IMAGE_URL, DEFAULT_MESSAGE_TEMPLATE, guild_id),
        )
        await self.log(guild_id, "server_boost_clear_settings", user_id=user_id, details={"guild_id": guild_id})

    async def handle_boost_started(self, member: nextcord.Member, settings=None, user_id: int | None = None) -> tuple[bool, str]:
        settings = settings or await self.get_settings(member.guild.id)
        if settings is None or not bool(settings["enabled"]):
            return False, "Server Boost module is disabled or not configured."
        channel = await self.resolve_sendable_channel(member.guild, settings["channel_id"])
        if channel is None:
            await self.log_error(member.guild.id, member.id, "boost channel missing", channel_id=settings["channel_id"], role_id=settings["role_id"])
            return False, "Boost channel is not configured or cannot be used."

        boosted_at = member.premium_since or datetime.now(timezone.utc)
        embed = self.build_boost_embed(member, settings, boosted_at)
        message = await self.send_or_update_post(member, channel, embed, boosted_at)
        role_added = await self.add_booster_role(member, settings["role_id"], message.id if message else None)
        await self.log(
            member.guild.id,
            "server_boost_detected",
            user_id=user_id or member.id,
            target_id=message.id if message else None,
            details={"guild_id": member.guild.id, "user_id": member.id, "channel_id": channel.id, "role_id": settings["role_id"], "message_id": message.id if message else None, "role_added": role_added},
        )
        return True, "Boost message sent." if role_added or not settings["role_id"] else "Boost message sent, but role could not be assigned."

    async def handle_boost_expired(self, member: nextcord.Member, settings=None, user_id: int | None = None) -> tuple[bool, str]:
        settings = settings or await self.get_settings(member.guild.id)
        if settings is None:
            return False, "Server Boost module is not configured."
        post = await self.get_post(member.guild.id, member.id)
        deleted = False
        removed = False
        if post is not None and bool(settings["delete_message_on_expire"]):
            deleted = await self.delete_post_message(member.guild, post)
        if bool(settings["remove_role_on_expire"]):
            removed = await self.remove_booster_role(member, settings["role_id"], post["message_id"] if post else None)
        await self.delete_post(member.guild.id, member.id)
        await self.log(
            member.guild.id,
            "server_boost_expired",
            user_id=user_id or member.id,
            details={"guild_id": member.guild.id, "user_id": member.id, "channel_id": post["channel_id"] if post else None, "role_id": settings["role_id"], "message_id": post["message_id"] if post else None, "deleted": deleted, "role_removed": removed},
        )
        return True, "Boost expiration handled."

    async def sync_current_boosters(self, guild: nextcord.Guild, user_id: int | None = None) -> tuple[bool, str]:
        settings = await self.get_settings(guild.id)
        if settings is None or not bool(settings["enabled"]):
            return False, "Server Boost module is disabled or not configured."
        channel = await self.resolve_sendable_channel(guild, settings["channel_id"])
        if channel is None:
            return False, "Boost channel is not configured or cannot be used."

        boosters = self.get_current_boosters(guild)
        if not boosters:
            await self.log(guild.id, "server_boost_sync_current", user_id=user_id, details={"guild_id": guild.id, "synced": 0, "failed": 0})
            return True, "No current boosters found."

        synced = 0
        failed = 0
        for member in boosters:
            ok, _ = await self.handle_boost_started(member, settings=settings, user_id=user_id)
            if ok:
                synced += 1
            else:
                failed += 1

        await self.log(
            guild.id,
            "server_boost_sync_current",
            user_id=user_id,
            details={"guild_id": guild.id, "synced": synced, "failed": failed},
        )
        if failed:
            return True, f"Synced {synced} current booster(s). Failed: {failed}."
        return True, f"Synced {synced} current booster(s)."

    def get_current_boosters(self, guild: nextcord.Guild) -> list[nextcord.Member]:
        boosters = list(getattr(guild, "premium_subscribers", []) or [])
        if boosters:
            return boosters
        return [member for member in guild.members if member.premium_since is not None]

    async def send_or_update_post(self, member: nextcord.Member, channel, embed: nextcord.Embed, boosted_at: datetime):
        post = await self.get_post(member.guild.id, member.id)
        message = None
        if post is not None and post["message_id"]:
            try:
                old_channel = self.bot.get_channel(post["channel_id"]) or await self.bot.fetch_channel(post["channel_id"])
                if hasattr(old_channel, "fetch_message"):
                    message = await old_channel.fetch_message(post["message_id"])
                    await message.edit(embed=embed, allowed_mentions=self.allowed_mentions())
            except (nextcord.NotFound, nextcord.Forbidden, nextcord.HTTPException):
                message = None
        if message is None:
            message = await channel.send(embed=embed, allowed_mentions=self.allowed_mentions())
        await self.save_post(member.guild.id, member.id, channel.id, message.id, boosted_at)
        return message

    async def add_booster_role(self, member: nextcord.Member, role_id: int | None, message_id: int | None = None) -> bool:
        if not role_id:
            return False
        role = member.guild.get_role(role_id)
        if role is None:
            await self.log_error(member.guild.id, member.id, "boost role missing", role_id=role_id, message_id=message_id)
            return False
        try:
            self.validate_bot_can_manage_role(member.guild, role)
            if role not in member.roles:
                await member.add_roles(role, reason="Server boost")
            await self.log(member.guild.id, "server_boost_role_added", target_id=message_id, details={"guild_id": member.guild.id, "user_id": member.id, "role_id": role_id, "message_id": message_id})
            return True
        except (ValueError, nextcord.Forbidden, nextcord.HTTPException) as exc:
            await self.log_error(member.guild.id, member.id, str(exc), role_id=role_id, message_id=message_id)
            return False

    async def remove_booster_role(self, member: nextcord.Member, role_id: int | None, message_id: int | None = None) -> bool:
        if not role_id:
            return False
        role = member.guild.get_role(role_id)
        if role is None or role not in member.roles:
            return False
        try:
            self.validate_bot_can_manage_role(member.guild, role)
            await member.remove_roles(role, reason="Server boost expired")
            await self.log(member.guild.id, "server_boost_role_removed", target_id=message_id, details={"guild_id": member.guild.id, "user_id": member.id, "role_id": role_id, "message_id": message_id})
            return True
        except (ValueError, nextcord.Forbidden, nextcord.HTTPException) as exc:
            await self.log_error(member.guild.id, member.id, str(exc), role_id=role_id, message_id=message_id)
            return False

    async def resolve_sendable_channel(self, guild: nextcord.Guild, channel_id: int | None):
        if not channel_id:
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (nextcord.Forbidden, nextcord.HTTPException, nextcord.NotFound):
                return None
        if getattr(channel, "guild", None) is None or channel.guild.id != guild.id or not hasattr(channel, "send"):
            return None
        bot_member = guild.me
        permissions = channel.permissions_for(bot_member) if bot_member is not None and hasattr(channel, "permissions_for") else None
        if permissions is not None and (not permissions.view_channel or not permissions.send_messages or not permissions.embed_links):
            return None
        return channel

    def validate_bot_can_manage_role(self, guild: nextcord.Guild, role: nextcord.Role) -> None:
        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            raise ValueError("I need Manage Roles permission.")
        if role >= bot_member.top_role:
            raise ValueError("I cannot manage this role. Move my bot role above it.")

    def build_boost_embed(self, member: nextcord.Member, settings, boosted_at: datetime) -> nextcord.Embed:
        streak = consecutive_months(boosted_at)
        plural = "s" if streak != 1 else ""
        template = settings["message_template"] or DEFAULT_MESSAGE_TEMPLATE
        description = template.format(
            user=member.display_name,
            mention=member.mention,
            streak=streak,
            plural=plural,
            guild=member.guild.name,
        )
        embed = nextcord.Embed(title="Server Boosted!", description=description, color=BOOST_COLOR)
        embed.set_author(name=member.display_name, icon_url=str(member.display_avatar.url))
        embed.set_image(url=settings["image_url"] or DEFAULT_IMAGE_URL)
        return embed

    async def build_settings_embed(self, guild: nextcord.Guild) -> nextcord.Embed:
        settings = await self.ensure_settings(guild.id)
        posts = await self.count_posts(guild.id)
        bot_member = guild.me
        has_manage_roles = bool(bot_member and bot_member.guild_permissions.manage_roles)
        channel_ok = "Not set"
        if settings["channel_id"]:
            channel_ok = "Yes" if await self.resolve_sendable_channel(guild, settings["channel_id"]) else "No"
        embed = nextcord.Embed(title="Server Boost Settings", color=DEFAULT_COLOR)
        embed.add_field(name="Enabled", value="Enabled" if settings["enabled"] else "Disabled", inline=True)
        embed.add_field(name="Boost Channel", value=f"<#{settings['channel_id']}>" if settings["channel_id"] else "Not set", inline=True)
        embed.add_field(name="Boost Role", value=f"<@&{settings['role_id']}>" if settings["role_id"] else "Not set", inline=True)
        embed.add_field(name="Image URL", value=settings["image_url"] or DEFAULT_IMAGE_URL, inline=False)
        embed.add_field(name="Delete Message On Expire", value="Enabled" if settings["delete_message_on_expire"] else "Disabled", inline=True)
        embed.add_field(name="Remove Role On Expire", value="Enabled" if settings["remove_role_on_expire"] else "Disabled", inline=True)
        embed.add_field(name="Saved Active Boost Posts", value=str(posts), inline=True)
        embed.add_field(name="Bot Has Manage Roles", value="Yes" if has_manage_roles else "No", inline=True)
        embed.add_field(name="Bot Can Send Embeds To Channel", value=channel_ok, inline=True)
        embed.add_field(name="Default Image", value="Yes" if (settings["image_url"] or DEFAULT_IMAGE_URL) == DEFAULT_IMAGE_URL else "No", inline=True)
        embed.add_field(name="Message Template Preview", value=(settings["message_template"] or DEFAULT_MESSAGE_TEMPLATE)[:1000], inline=False)
        return embed

    def build_panel_embed(self) -> nextcord.Embed:
        embed = nextcord.Embed(
            title="Server Boost",
            description="Configure boost thank-you messages and optional booster roles.",
            color=DEFAULT_COLOR,
        )
        embed.add_field(name="Navigation", value="Navigation edits this panel message.", inline=False)
        return embed

    async def get_post(self, guild_id: int, user_id: int):
        return await self.db.fetchone("SELECT * FROM server_boost_posts WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))

    async def save_post(self, guild_id: int, user_id: int, channel_id: int, message_id: int, boosted_at: datetime) -> None:
        await self.db.execute(
            """
            INSERT INTO server_boost_posts (guild_id, user_id, channel_id, message_id, boosted_at, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                message_id = excluded.message_id,
                boosted_at = excluded.boosted_at,
                updated_at = CURRENT_TIMESTAMP
            """,
            (guild_id, user_id, channel_id, message_id, boosted_at.astimezone(timezone.utc).isoformat()),
        )

    async def delete_post(self, guild_id: int, user_id: int) -> None:
        await self.db.execute("DELETE FROM server_boost_posts WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))

    async def count_posts(self, guild_id: int) -> int:
        row = await self.db.fetchone("SELECT COUNT(*) AS count FROM server_boost_posts WHERE guild_id = ?", (guild_id,))
        return int(row["count"] if row else 0)

    async def delete_post_message(self, guild: nextcord.Guild, post) -> bool:
        try:
            channel = self.bot.get_channel(post["channel_id"]) or await self.bot.fetch_channel(post["channel_id"])
            message = await channel.fetch_message(post["message_id"])
            await message.delete()
            await self.log(guild.id, "server_boost_message_deleted", target_id=post["message_id"], details={"guild_id": guild.id, "user_id": post["user_id"], "channel_id": post["channel_id"], "message_id": post["message_id"]})
            return True
        except (nextcord.NotFound, nextcord.Forbidden, nextcord.HTTPException, AttributeError):
            return False

    def allowed_mentions(self) -> nextcord.AllowedMentions:
        return nextcord.AllowedMentions(everyone=False, users=True, roles=False, replied_user=False)

    async def log_error(self, guild_id: int, user_id: int | None, error: str, channel_id: int | None = None, role_id: int | None = None, message_id: int | None = None) -> None:
        await self.log(
            guild_id,
            "server_boost_error",
            user_id=user_id,
            target_id=message_id,
            details={"guild_id": guild_id, "user_id": user_id, "channel_id": channel_id, "role_id": role_id, "message_id": message_id, "error": error},
        )

    async def log(self, guild_id: int, action: str, user_id: int | None = None, target_id: int | None = None, details: dict | None = None) -> None:
        await self.db.log_action(guild_id, MODULE_NAME, action, user_id=user_id, target_id=target_id, details=details or {})


def consecutive_months(start: datetime, now: datetime | None = None) -> int:
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    start = start.astimezone(timezone.utc)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    months = (now.year - start.year) * 12 + (now.month - start.month)
    if now.day < start.day:
        months -= 1
    return max(1, months + 1)
