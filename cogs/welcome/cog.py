import logging

import nextcord
from nextcord.ext import commands

from core.config import get_development_guild_ids
from core.permissions import require_guild_manager

from .service import DEFAULT_BACKGROUND_URL, MANAGEMENT_VIEW_TYPE, MODULE_NAME, WelcomeService
from .views import WelcomePanelView


class WelcomeCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.logger = logging.getLogger(__name__)
        self.service = WelcomeService(bot)
        self._restored = False

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._restored:
            return
        await self.restore_management_panels()
        self._restored = True

    @commands.Cog.listener()
    async def on_member_join(self, member: nextcord.Member) -> None:
        await self.send_welcome(member, test=False)

    @nextcord.slash_command(
        name="welcome-panel",
        description="Create or update the welcome management panel.",
        guild_ids=get_development_guild_ids(),
    )
    async def welcome_panel(self, interaction: nextcord.Interaction) -> None:
        if not await require_guild_manager(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        message = await self.create_or_update_management_panel(interaction)
        await interaction.followup.send(f"Welcome panel ready in {message.channel.mention}.", ephemeral=True)

    async def create_or_update_management_panel(self, interaction: nextcord.Interaction) -> nextcord.Message:
        saved = await self.service.get_panel(interaction.guild_id)
        view = self.create_management_view()
        if saved is not None:
            message = await self.fetch_message(saved["channel_id"], saved["message_id"])
            if message is not None:
                await message.edit(content=None, embed=self.service.build_management_embed(), view=view)
                await self.service.save_panel(interaction.guild_id, message.channel.id, message.id)
                return message

        message = await interaction.channel.send(embed=self.service.build_management_embed(), view=view)
        await self.service.save_panel(interaction.guild_id, message.channel.id, message.id)
        return message

    def create_management_view(self, show_admin_back: bool = False) -> WelcomePanelView:
        return WelcomePanelView(self, show_admin_back=show_admin_back)

    async def restore_management_panels(self) -> None:
        rows = await self.bot.db.fetchall(
            """
            SELECT guild_id, channel_id, message_id
            FROM persistent_views
            WHERE module_name = ? AND view_type = ?
            """,
            (MODULE_NAME, MANAGEMENT_VIEW_TYPE),
        )
        restored = 0
        for row in rows:
            message = await self.fetch_message(row["channel_id"], row["message_id"])
            if message is None:
                self.logger.warning("Welcome panel message %s could not be found; removing stale database record", row["message_id"])
                await self.bot.db.delete_persistent_view(row["message_id"])
                await self.bot.db.execute(
                    "DELETE FROM welcome_panels WHERE guild_id = ? AND message_id = ?",
                    (row["guild_id"], row["message_id"]),
                )
                continue
            self.bot.add_view(self.create_management_view(), message_id=row["message_id"])
            restored += 1
        self.logger.info("Restored %s welcome management panel view(s)", restored)

    async def send_welcome(self, member: nextcord.Member, test: bool = False) -> tuple[bool, str]:
        settings = await self.service.get_settings(member.guild.id)
        if settings is None:
            return False, "Welcome is not configured."
        if not bool(settings["enabled"]):
            return False, "Welcome is disabled."
        if settings["channel_id"] is None:
            return False, "No welcome channel configured."

        channel = await self.fetch_sendable_channel(settings["channel_id"])
        if channel is None:
            await self.log_error(member.guild.id, settings["channel_id"], member.id, "welcome channel not found or not sendable")
            return False, "Welcome channel is missing or I cannot send there."

        content = self.service.format_message(member, settings["message_text"])
        background_url = settings["background_url"] or DEFAULT_BACKGROUND_URL
        file = None
        if bool(settings["image_enabled"]):
            try:
                file = await self.service.generate_welcome_file(member, background_url)
            except Exception as exc:
                self.logger.exception("Failed to generate welcome image for guild %s", member.guild.id)
                await self.log_error(member.guild.id, channel.id, member.id, str(exc))

        try:
            permissions = channel.permissions_for(member.guild.me) if member.guild.me is not None else None
            if permissions is not None:
                if not permissions.send_messages:
                    await self.log_error(member.guild.id, channel.id, member.id, "missing Send Messages permission")
                    return False, "I do not have permission to send welcome messages there."
                if file is not None and not permissions.attach_files:
                    await self.log_error(member.guild.id, channel.id, member.id, "missing Attach Files permission")
                    file = None

            sent = await channel.send(
                content=content,
                file=file,
                allowed_mentions=self.service.allowed_mentions(),
            )
            await self.service.log(
                member.guild.id,
                "welcome_test" if test else "welcome_sent",
                target_id=sent.id,
                details={
                    "guild_id": member.guild.id,
                    "channel_id": channel.id,
                    "user_id": member.id,
                    "image_enabled": bool(settings["image_enabled"]),
                },
            )
        except nextcord.Forbidden:
            await self.log_error(member.guild.id, channel.id, member.id, "missing permission to send welcome")
            return False, "I do not have permission to send welcome messages there."
        except nextcord.HTTPException as exc:
            await self.log_error(member.guild.id, channel.id, member.id, str(exc))
            return False, "Discord rejected the welcome message."

        return True, f"Welcome message sent to {channel.mention}."

    async def fetch_sendable_channel(self, channel_id: int):
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except nextcord.HTTPException:
                return None
        return channel if hasattr(channel, "send") else None

    async def fetch_message(self, channel_id: int, message_id: int) -> nextcord.Message | None:
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except nextcord.HTTPException:
                return None
        if not hasattr(channel, "fetch_message"):
            return None
        try:
            return await channel.fetch_message(message_id)
        except (nextcord.NotFound, nextcord.Forbidden, nextcord.HTTPException):
            return None

    async def log_error(self, guild_id: int, channel_id: int | None, user_id: int | None, error: str) -> None:
        await self.service.log(
            guild_id,
            "welcome_error",
            details={"guild_id": guild_id, "channel_id": channel_id, "user_id": user_id, "error": error},
        )


def setup(bot: commands.Bot) -> None:
    bot.add_cog(WelcomeCog(bot))
