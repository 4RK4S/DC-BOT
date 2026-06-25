import logging

import nextcord
from nextcord.ext import commands

from core.config import get_development_guild_ids
from core.embeds import admin_panel_embed
from core.permissions import require_guild_manager

from .views import AdminPanelView


ADMIN_PANEL_MODULE = "admin_panel"
ADMIN_PANEL_VIEW_TYPE = "admin_panel"


class AdminPanelCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.logger = logging.getLogger(__name__)
        self._views_restored = False

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._views_restored:
            return

        await self.restore_persistent_views()
        self._views_restored = True

    @nextcord.slash_command(
        name="admin-panel",
        description="Create or update the admin module dashboard.",
        guild_ids=get_development_guild_ids(),
    )
    async def admin_panel(self, interaction: nextcord.Interaction) -> None:
        if not await require_guild_manager(interaction):
            return

        await interaction.response.defer(ephemeral=True)
        message = await self.create_or_update_panel(interaction)
        await interaction.followup.send(
            f"Admin panel is ready in {message.channel.mention}.",
            ephemeral=True,
        )

    @nextcord.slash_command(
        name="admin-panel-refresh",
        description="Refresh the saved admin panel.",
        guild_ids=get_development_guild_ids(),
    )
    async def admin_panel_refresh(self, interaction: nextcord.Interaction) -> None:
        if not await require_guild_manager(interaction):
            return

        await interaction.response.defer(ephemeral=True)
        message = await self.create_or_update_panel(interaction)
        await interaction.followup.send(
            f"Admin panel refreshed in {message.channel.mention}.",
            ephemeral=True,
        )

    async def create_or_update_panel(self, interaction: nextcord.Interaction) -> nextcord.Message:
        if interaction.guild_id is None:
            raise RuntimeError("Admin panels can only be created in a guild")

        saved_message = await self.fetch_saved_panel_message(interaction.guild_id)
        view = AdminPanelView(self.bot)

        if saved_message is not None:
            await saved_message.edit(embed=admin_panel_embed(), view=view)
            message = saved_message
        else:
            if interaction.channel is None or not hasattr(interaction.channel, "send"):
                raise RuntimeError("This command must be used in a message channel")
            message = await interaction.channel.send(embed=admin_panel_embed(), view=view)

        await self.save_panel_records(interaction.guild_id, message.channel.id, message.id)
        await self.bot.db.log_action(
            interaction.guild_id,
            ADMIN_PANEL_MODULE,
            "panel_upserted",
            user_id=interaction.user.id,
            target_id=message.id,
            details={"channel_id": message.channel.id},
        )
        return message

    async def remove_panel(self, guild_id: int | None) -> bool:
        if guild_id is None:
            return False

        saved = await self.get_saved_panel(guild_id)
        if saved is None:
            return False

        message_id = saved["admin_panel_message_id"]
        message = await self.fetch_saved_panel_message(guild_id)
        if message is not None:
            try:
                await message.delete()
            except nextcord.HTTPException:
                self.logger.exception("Failed to delete admin panel message %s", message_id)

        await self.bot.db.execute(
            """
            UPDATE guild_settings
            SET admin_panel_channel_id = NULL,
                admin_panel_message_id = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE guild_id = ?
            """,
            (guild_id,),
        )
        if message_id is not None:
            await self.bot.db.delete_persistent_view(message_id)
        await self.bot.db.log_action(guild_id, ADMIN_PANEL_MODULE, "panel_removed", target_id=message_id)
        return True

    async def restore_persistent_views(self) -> None:
        rows = await self.bot.db.fetchall(
            """
            SELECT guild_id, admin_panel_channel_id, admin_panel_message_id
            FROM guild_settings
            WHERE admin_panel_channel_id IS NOT NULL
                AND admin_panel_message_id IS NOT NULL
            """,
        )

        for row in rows:
            message = await self.fetch_saved_panel_message(row["guild_id"])
            if message is None:
                self.logger.warning(
                    "Saved admin panel message %s for guild %s could not be restored; clearing stale database record",
                    row["admin_panel_message_id"],
                    row["guild_id"],
                )
                await self.remove_panel_records(row["guild_id"], row["admin_panel_message_id"])
                continue
            view = AdminPanelView(self.bot)
            try:
                await message.edit(content=None, embed=admin_panel_embed(), view=view)
                await self.save_panel_records(row["guild_id"], message.channel.id, message.id)
            except nextcord.HTTPException:
                self.logger.exception("Failed to reset admin panel message %s", message.id)
                continue
            self.bot.add_view(view, message_id=message.id)

        self.logger.info("Restored %s admin panel persistent view(s)", len(rows))

    async def remove_panel_records(self, guild_id: int, message_id: int | None) -> None:
        await self.bot.db.execute(
            """
            UPDATE guild_settings
            SET admin_panel_channel_id = NULL,
                admin_panel_message_id = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE guild_id = ?
            """,
            (guild_id,),
        )
        if message_id is not None:
            await self.bot.db.delete_persistent_view(message_id)

    async def save_panel_records(self, guild_id: int, channel_id: int, message_id: int) -> None:
        await self.bot.db.execute(
            """
            INSERT INTO guild_settings (
                guild_id,
                admin_panel_channel_id,
                admin_panel_message_id,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id) DO UPDATE SET
                admin_panel_channel_id = excluded.admin_panel_channel_id,
                admin_panel_message_id = excluded.admin_panel_message_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (guild_id, channel_id, message_id),
        )
        await self.bot.db.save_persistent_view(
            guild_id=guild_id,
            module_name=ADMIN_PANEL_MODULE,
            channel_id=channel_id,
            message_id=message_id,
            view_type=ADMIN_PANEL_VIEW_TYPE,
            state={},
        )

    async def get_saved_panel(self, guild_id: int):
        return await self.bot.db.fetchone(
            """
            SELECT admin_panel_channel_id, admin_panel_message_id
            FROM guild_settings
            WHERE guild_id = ?
                AND admin_panel_channel_id IS NOT NULL
                AND admin_panel_message_id IS NOT NULL
            """,
            (guild_id,),
        )

    async def fetch_saved_panel_message(self, guild_id: int) -> nextcord.Message | None:
        saved = await self.get_saved_panel(guild_id)
        if saved is None:
            return None

        channel_id = saved["admin_panel_channel_id"]
        message_id = saved["admin_panel_message_id"]
        channel = self.bot.get_channel(channel_id)

        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except nextcord.HTTPException:
                self.logger.warning("Saved admin panel channel %s could not be fetched", channel_id)
                return None

        if not hasattr(channel, "fetch_message"):
            return None

        try:
            return await channel.fetch_message(message_id)
        except nextcord.NotFound:
            self.logger.info("Saved admin panel message %s was not found", message_id)
        except nextcord.Forbidden:
            self.logger.warning("Missing permissions to fetch admin panel message %s", message_id)
        except nextcord.HTTPException:
            self.logger.exception("Failed to fetch admin panel message %s", message_id)
        return None


def setup(bot: commands.Bot) -> None:
    bot.add_cog(AdminPanelCog(bot))
