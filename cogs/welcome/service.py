import logging
from io import BytesIO
from pathlib import Path

import aiohttp
import nextcord
from PIL import Image, ImageDraw, ImageFont

from core.emoji import replace_custom_emoji_keys
from core.embeds import DEFAULT_COLOR


MODULE_NAME = "welcome"
MANAGEMENT_VIEW_TYPE = "welcome_management_panel"
DEFAULT_BACKGROUND_URL = "https://res.cloudinary.com/dmfww0zt8/image/upload/Discord_basic.png"
DEFAULT_MESSAGE_TEXT = "<@{user_id}>|Welcome to **{guild_name}**!"

AVATAR_SIZE = 280
AVATAR_CENTER_X = 455
AVATAR_CENTER_Y = 237
AVATAR_X = 315
AVATAR_Y = 97
WELCOME_FONT_SIZE = 80
USERNAME_FONT_SIZE = 50
WELCOME_Y = -3
USERNAME_Y = 422
SHADOW_OFFSET = 5


class WelcomeService:
    def __init__(self, bot) -> None:
        self.bot = bot
        self.db = bot.db
        self.logger = logging.getLogger(__name__)
        self.font_path = Path("assets") / "fonts" / "Lexend-ExtraBold.ttf"

    async def get_settings(self, guild_id: int):
        return await self.db.fetchone("SELECT * FROM welcome_settings WHERE guild_id = ?", (guild_id,))

    async def get_or_create_settings(self, guild_id: int):
        row = await self.get_settings(guild_id)
        if row is not None:
            return row
        await self.db.execute(
            """
            INSERT INTO welcome_settings (
                guild_id,
                enabled,
                background_url,
                message_text,
                image_enabled,
                updated_at
            )
            VALUES (?, 1, ?, ?, 1, CURRENT_TIMESTAMP)
            """,
            (guild_id, DEFAULT_BACKGROUND_URL, DEFAULT_MESSAGE_TEXT),
        )
        return await self.get_settings(guild_id)

    async def set_channel(self, guild_id: int, channel_id: int, user_id: int | None = None) -> None:
        await self.get_or_create_settings(guild_id)
        await self.db.execute(
            """
            UPDATE welcome_settings
            SET channel_id = ?, enabled = 1, updated_at = CURRENT_TIMESTAMP
            WHERE guild_id = ?
            """,
            (channel_id, guild_id),
        )
        await self.log(guild_id, "welcome_set_channel", user_id=user_id, details={"guild_id": guild_id, "channel_id": channel_id})

    async def set_background(self, guild_id: int, background_url: str, user_id: int | None = None) -> None:
        clean = background_url.strip()
        if not clean.startswith(("http://", "https://")):
            raise ValueError("Background Image URL must start with http:// or https://")
        await self.get_or_create_settings(guild_id)
        await self.db.execute(
            """
            UPDATE welcome_settings
            SET background_url = ?, updated_at = CURRENT_TIMESTAMP
            WHERE guild_id = ?
            """,
            (clean, guild_id),
        )
        await self.log(guild_id, "welcome_set_background", user_id=user_id, details={"guild_id": guild_id, "background_url": clean})

    async def reset_background(self, guild_id: int, user_id: int | None = None) -> None:
        await self.set_background(guild_id, DEFAULT_BACKGROUND_URL, user_id)
        await self.log(guild_id, "welcome_reset_background", user_id=user_id, details={"guild_id": guild_id, "background_url": DEFAULT_BACKGROUND_URL})

    async def set_message(self, guild_id: int, message_text: str, user_id: int | None = None) -> None:
        clean = message_text.strip() or DEFAULT_MESSAGE_TEXT
        await self.get_or_create_settings(guild_id)
        await self.db.execute(
            """
            UPDATE welcome_settings
            SET message_text = ?, updated_at = CURRENT_TIMESTAMP
            WHERE guild_id = ?
            """,
            (clean, guild_id),
        )
        await self.log(guild_id, "welcome_set_message", user_id=user_id, details={"guild_id": guild_id})

    async def toggle_enabled(self, guild_id: int, user_id: int | None = None) -> bool:
        row = await self.get_or_create_settings(guild_id)
        enabled = not bool(row["enabled"])
        await self.db.execute(
            "UPDATE welcome_settings SET enabled = ?, updated_at = CURRENT_TIMESTAMP WHERE guild_id = ?",
            (int(enabled), guild_id),
        )
        await self.log(guild_id, "welcome_toggle", user_id=user_id, details={"guild_id": guild_id, "enabled": enabled})
        return enabled

    async def toggle_image(self, guild_id: int, user_id: int | None = None) -> bool:
        row = await self.get_or_create_settings(guild_id)
        image_enabled = not bool(row["image_enabled"])
        await self.db.execute(
            "UPDATE welcome_settings SET image_enabled = ?, updated_at = CURRENT_TIMESTAMP WHERE guild_id = ?",
            (int(image_enabled), guild_id),
        )
        await self.log(guild_id, "welcome_toggle_image", user_id=user_id, details={"guild_id": guild_id, "image_enabled": image_enabled})
        return image_enabled

    async def clear_settings(self, guild_id: int, user_id: int | None = None) -> None:
        await self.get_or_create_settings(guild_id)
        await self.db.execute(
            """
            UPDATE welcome_settings
            SET channel_id = NULL,
                enabled = 0,
                background_url = ?,
                message_text = ?,
                image_enabled = 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE guild_id = ?
            """,
            (DEFAULT_BACKGROUND_URL, DEFAULT_MESSAGE_TEXT, guild_id),
        )
        await self.log(guild_id, "welcome_clear", user_id=user_id, details={"guild_id": guild_id})

    async def save_panel(self, guild_id: int, channel_id: int, message_id: int) -> None:
        await self.db.execute(
            """
            INSERT INTO welcome_panels (guild_id, channel_id, message_id, updated_at)
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
        return await self.db.fetchone("SELECT * FROM welcome_panels WHERE guild_id = ?", (guild_id,))

    def build_management_embed(self) -> nextcord.Embed:
        embed = nextcord.Embed(
            title="Welcome",
            description="Configure join messages and fixed-layout welcome images.",
            color=DEFAULT_COLOR,
        )
        embed.add_field(name="Navigation", value="Navigation edits this panel message.", inline=False)
        return embed

    def build_settings_embed(self, guild: nextcord.Guild, settings) -> nextcord.Embed:
        configured = settings is not None and settings["channel_id"] is not None
        embed = nextcord.Embed(title="Welcome Settings", color=DEFAULT_COLOR)
        if not configured:
            embed.description = "Welcome is not configured for this server."
        if settings is None:
            enabled = True
            image_enabled = True
            channel = "Not configured"
            background_url = DEFAULT_BACKGROUND_URL
            message_text = DEFAULT_MESSAGE_TEXT
        else:
            enabled = bool(settings["enabled"])
            image_enabled = bool(settings["image_enabled"])
            channel = f"<#{settings['channel_id']}>" if settings["channel_id"] else "Not configured"
            background_url = settings["background_url"] or DEFAULT_BACKGROUND_URL
            message_text = settings["message_text"] or DEFAULT_MESSAGE_TEXT
        embed.add_field(name="Enabled", value=str(enabled), inline=True)
        embed.add_field(name="Image Enabled", value=str(image_enabled), inline=True)
        embed.add_field(name="Channel", value=channel, inline=False)
        embed.add_field(name="Custom Background Set", value=str(background_url != DEFAULT_BACKGROUND_URL), inline=True)
        embed.add_field(name="Custom Message Set", value=str(message_text != DEFAULT_MESSAGE_TEXT), inline=True)
        embed.add_field(name="Background URL", value=background_url, inline=False)
        embed.add_field(name="Message Text", value=message_text, inline=False)
        embed.add_field(
            name="Default Background URL",
            value=DEFAULT_BACKGROUND_URL,
            inline=False,
        )
        embed.add_field(
            name="Fixed Layout Info",
            value=(
                "avatar size: 280x280\n"
                "avatar center: x=455, y=237\n"
                "avatar top-left: x=315, y=97\n"
                "WELCOME font size: 80\n"
                "username font size: 50"
            ),
            inline=False,
        )
        embed.set_footer(text=guild.name)
        return embed

    def format_message(self, member: nextcord.Member, message_text: str) -> str:
        guild = member.guild
        values = {
            "user": member.mention,
            "user_id": str(member.id),
            "user_name": member.name,
            "guild_name": guild.name,
            "member_count": str(guild.member_count or 0),
        }
        content = message_text or DEFAULT_MESSAGE_TEXT
        for key, value in values.items():
            content = content.replace("{" + key + "}", value)
        content = replace_single_pipes_with_newlines(content)
        return replace_custom_emoji_keys(content) or content

    async def generate_welcome_file(self, member: nextcord.Member, background_url: str | None) -> nextcord.File:
        background_bytes = await self.fetch_bytes(background_url or DEFAULT_BACKGROUND_URL)
        avatar_url = str(member.display_avatar.replace(format="png", size=512).url)
        avatar_bytes = await self.fetch_bytes(avatar_url)

        image = Image.open(BytesIO(background_bytes)).convert("RGBA")
        draw = ImageDraw.Draw(image)
        avatar = Image.open(BytesIO(avatar_bytes)).convert("RGBA").resize((AVATAR_SIZE, AVATAR_SIZE), Image.LANCZOS)
        mask = Image.new("L", (AVATAR_SIZE, AVATAR_SIZE), 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.ellipse((0, 0, AVATAR_SIZE, AVATAR_SIZE), fill=255)
        image.paste(avatar, (AVATAR_X, AVATAR_Y), mask)

        welcome_font = self.load_font(WELCOME_FONT_SIZE)
        username_font = self.load_font(USERNAME_FONT_SIZE)
        self.draw_centered_text(draw, image.width, WELCOME_Y, "WELCOME", welcome_font)
        self.draw_centered_text(draw, image.width, USERNAME_Y, member.name, username_font)

        output = BytesIO()
        image.save(output, format="PNG")
        output.seek(0)
        return nextcord.File(output, filename="welcome.png")

    async def fetch_bytes(self, url: str) -> bytes:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                response.raise_for_status()
                return await response.read()

    def load_font(self, size: int):
        try:
            return ImageFont.truetype(str(self.font_path), size)
        except OSError:
            self.logger.warning("Welcome font missing at %s; using Pillow default font", self.font_path)
            return ImageFont.load_default()

    def draw_centered_text(self, draw: ImageDraw.ImageDraw, width: int, y: int, text: str, font) -> None:
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        x = (width - text_width) // 2
        draw.text((x + SHADOW_OFFSET, y + SHADOW_OFFSET), text, font=font, fill="black")
        draw.text((x, y), text, font=font, fill="white")

    def allowed_mentions(self) -> nextcord.AllowedMentions:
        return nextcord.AllowedMentions(everyone=False, users=True, roles=False, replied_user=False)

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
