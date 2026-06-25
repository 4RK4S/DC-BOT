import io
import re
from datetime import datetime, timedelta, timezone

import nextcord

from core.embeds import DEFAULT_COLOR
from core.emoji import replace_custom_emoji_keys


MODULE_NAME = "messages"
MANAGEMENT_VIEW_TYPE = "messages_management_panel"
DEFAULT_EMBED_COLOR = 0x3498DB


class MessageService:
    def __init__(self, bot) -> None:
        self.bot = bot
        self.db = bot.db

    def build_management_embed(self) -> nextcord.Embed:
        embed = nextcord.Embed(
            title="Messages",
            description="Send, edit, inspect, and export Discord messages from one panel.",
            color=DEFAULT_COLOR,
        )
        embed.add_field(name="Navigation", value="Navigation edits this panel message.", inline=False)
        return embed

    async def build_settings_embed(self, guild_id: int) -> nextcord.Embed:
        row = await self.db.get_module_settings(guild_id, MODULE_NAME)
        enabled = True if row is None else bool(row["enabled"])
        embed = nextcord.Embed(title="Messages Settings", color=DEFAULT_COLOR)
        embed.add_field(name="Enabled", value=str(enabled), inline=True)
        actions = ("Send Message", "Send Multiple Messages", "Send Embed", "Reply Message", "Edit Message", "Show Message", "Export Messages")
        embed.add_field(name="Emoji Map Loaded", value="Yes", inline=True)
        embed.add_field(name="Available Actions Count", value=str(len(actions)), inline=True)
        embed.add_field(
            name="Available Actions",
            value=", ".join(actions),
            inline=False,
        )
        return embed

    def normalize_message_input(self, value: str, spoiler: bool = False) -> str:
        content = replace_single_pipes_with_newlines(value)
        content = replace_custom_emoji_keys(content) or ""
        if spoiler and content:
            return f"||{content}||"
        return content

    def apply_custom_emojis(self, value: str | None) -> str | None:
        return replace_custom_emoji_keys(value)

    def split_messages_input(self, value: str, spoiler: bool = False) -> list[str]:
        messages = [
            self.normalize_message_input(part, spoiler)
            for part in split_by_single_pipe(value)
            if part.strip()
        ]
        return messages[:10]

    def clean_content_for_display(self, content: str) -> str:
        collapsed = collapse_custom_emojis(content)
        return newlines_to_pipes(collapsed)

    def safe_allowed_mentions(self) -> nextcord.AllowedMentions:
        return nextcord.AllowedMentions.none()

    def parse_color(self, value: str | None) -> int:
        if not value or not value.strip():
            return DEFAULT_EMBED_COLOR
        clean = value.strip().removeprefix("#")
        try:
            return int(clean, 16)
        except ValueError as exc:
            raise ValueError("Color must be a hex value like #3498db") from exc

    def parse_bool(self, value: str | None, default: bool = False) -> bool:
        return parse_yes_no(value, default)

    async def get_channel(self, channel_id_text: str):
        try:
            channel_id = int(channel_id_text.strip())
        except ValueError as exc:
            raise ValueError("Channel ID must be a number") from exc

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except nextcord.HTTPException as exc:
                raise ValueError("Could not fetch that channel") from exc

        if not hasattr(channel, "send") or not hasattr(channel, "fetch_message"):
            raise ValueError("Channel must be a text channel or thread")
        return channel

    async def get_message(self, channel_id_text: str, message_id_text: str) -> nextcord.Message:
        channel = await self.get_channel(channel_id_text)
        try:
            message_id = int(message_id_text.strip())
        except ValueError as exc:
            raise ValueError("Message ID must be a number") from exc

        try:
            return await channel.fetch_message(message_id)
        except nextcord.NotFound as exc:
            raise ValueError("Message was not found") from exc
        except nextcord.Forbidden as exc:
            raise ValueError("Missing permission to fetch that message") from exc
        except nextcord.HTTPException as exc:
            raise ValueError("Could not fetch that message") from exc

    async def log_action(
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

    async def export_messages_text(self, channel, hours: int) -> str:
        if hours < 1 or hours > 8760:
            raise ValueError("Hours must be between 1 and 8760")

        after = datetime.now(timezone.utc) - timedelta(hours=hours)
        lines = [
            f"Export from channel {getattr(channel, 'id', 'unknown')}",
            f"Last {hours} hour(s)",
            "",
        ]

        async for message in channel.history(limit=None, after=after, oldest_first=True):
            lines.append(f"[{message.created_at.isoformat()}] {message.author} ({message.author.id})")
            lines.append(f"Message ID: {message.id}")
            if message.content:
                lines.append(f"Content: {message.content}")
            for attachment in message.attachments:
                lines.append(f"Attachment: {attachment.url}")
            for embed in message.embeds:
                if embed.title:
                    lines.append(f"Embed title: {embed.title}")
                if embed.description:
                    lines.append(f"Embed description: {embed.description}")
            if message.reactions:
                reactions = ", ".join(f"{reaction.emoji} x{reaction.count}" for reaction in message.reactions)
                lines.append(f"Reactions: {reactions}")
            lines.append("")

        return "\n".join(lines)

    def export_file(self, text: str, channel_id: int) -> nextcord.File:
        data = io.BytesIO(text.encode("utf-8"))
        return nextcord.File(data, filename=f"messages_export_{channel_id}.txt")

def replace_single_pipes_with_newlines(value: str) -> str:
    return _single_pipe_transform(value, "\n")


def split_by_single_pipe(value: str) -> list[str]:
    sentinel = "\u0000PIPE\u0000"
    protected = value.replace("||", sentinel)
    parts = protected.split("|")
    return [part.replace(sentinel, "||") for part in parts]


def newlines_to_pipes(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "|")


def collapse_custom_emojis(value: str) -> str:
    return re.sub(r"<a?:([A-Za-z0-9_]+):\d+>", r":\1:", value)


def parse_yes_no(value: str | None, default: bool = False) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"yes", "y", "true", "1", "on"}:
        return True
    if normalized in {"no", "n", "false", "0", "off"}:
        return False
    raise ValueError("Use yes or no")


def _single_pipe_transform(value: str, replacement: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(value):
        if value[index : index + 2] == "||":
            output.append("||")
            index += 2
        elif value[index] == "|":
            output.append(replacement)
            index += 1
        else:
            output.append(value[index])
            index += 1
    return "".join(output)
