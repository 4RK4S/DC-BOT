import asyncio
import logging

import nextcord
from nextcord.ext import commands

from core.config import get_development_guild_ids
from core.permissions import require_guild_manager

from .service import ADMIN_VIEW_TYPE, MODULE_NAME, PUBLIC_VIEW_TYPE, RolesService
from .views import PublicRolePanelView, RolesAdminPanelView


class RolesCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.logger = logging.getLogger(__name__)
        self.service = RolesService(bot)
        self._restored = False

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._restored:
            return
        await self.restore_views()
        self._restored = True

    @nextcord.slash_command(
        name="roles-panel",
        description="Create or update the roles management panel.",
        guild_ids=get_development_guild_ids(),
    )
    async def roles_panel(self, interaction: nextcord.Interaction) -> None:
        if not await require_guild_manager(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        message = await self.create_or_update_admin_panel(interaction)
        await interaction.followup.send(f"Roles panel ready in {message.channel.mention}.", ephemeral=True)

    async def create_or_update_admin_panel(self, interaction: nextcord.Interaction) -> nextcord.Message:
        saved = await self.service.get_admin_panel(interaction.guild_id)
        view = self.create_admin_view()
        if saved is not None:
            message = await self.fetch_message(saved["channel_id"], saved["message_id"])
            if message is not None:
                await message.edit(content=None, embed=self.service.build_admin_embed(), view=view)
                await self.service.save_admin_panel(interaction.guild_id, message.channel.id, message.id)
                return message

        message = await interaction.channel.send(embed=self.service.build_admin_embed(), view=view)
        await self.service.save_admin_panel(interaction.guild_id, message.channel.id, message.id)
        return message

    def create_admin_view(self, show_admin_back: bool = False) -> RolesAdminPanelView:
        return RolesAdminPanelView(self, show_admin_back=show_admin_back)

    async def create_public_view(self, panel_id: int) -> PublicRolePanelView:
        buttons = await self.service.list_buttons(panel_id)
        return PublicRolePanelView(self, buttons)

    async def refresh_public_panel(self, panel_id: int) -> tuple[bool, str]:
        panel = await self.service.get_panel_by_id(panel_id)
        if panel is None or not bool(panel["enabled"]):
            return False, "Role panel not found."
        if panel["message_id"] is None:
            return False, "Role panel message is missing."
        message = await self.fetch_message(panel["channel_id"], panel["message_id"])
        if message is None:
            return False, "Role panel message could not be fetched."
        await message.edit(embed=self.service.build_public_embed(panel), view=await self.create_public_view(panel_id))
        await self.bot.db.save_persistent_view(
            panel["guild_id"],
            MODULE_NAME,
            panel["channel_id"],
            panel["message_id"],
            PUBLIC_VIEW_TYPE,
            state={"panel_id": panel_id},
        )
        return True, "Role panel refreshed."

    async def delete_public_message(self, panel) -> None:
        if panel["message_id"] is None:
            return
        message = await self.fetch_message(panel["channel_id"], panel["message_id"])
        if message is None:
            return
        try:
            await message.delete()
            await self.bot.db.delete_persistent_view(panel["message_id"])
        except nextcord.HTTPException:
            self.logger.warning("Could not delete role panel message %s", panel["message_id"])

    async def restore_views(self) -> None:
        admin_rows = await self.bot.db.fetchall(
            """
            SELECT guild_id, channel_id, message_id
            FROM persistent_views
            WHERE module_name = ? AND view_type = ?
            """,
            (MODULE_NAME, ADMIN_VIEW_TYPE),
        )
        restored_admin = 0
        for row in admin_rows:
            message = await self.fetch_message(row["channel_id"], row["message_id"])
            if message is None:
                self.logger.warning("Roles admin panel message %s could not be found; removing stale database record", row["message_id"])
                await self.bot.db.delete_persistent_view(row["message_id"])
                await self.bot.db.execute(
                    "DELETE FROM roles_admin_panels WHERE guild_id = ? AND message_id = ?",
                    (row["guild_id"], row["message_id"]),
                )
                continue
            self.bot.add_view(self.create_admin_view(), message_id=row["message_id"])
            restored_admin += 1

        panels = await self.bot.db.fetchall(
            """
            SELECT id, channel_id, message_id
            FROM role_panels
            WHERE enabled = 1 AND message_id IS NOT NULL
            """
        )
        restored_public = 0
        for panel in panels:
            message = await self.fetch_message(panel["channel_id"], panel["message_id"])
            if message is None:
                self.logger.warning("Public role panel message %s could not be found; disabling stale panel %s", panel["message_id"], panel["id"])
                await self.bot.db.delete_persistent_view(panel["message_id"])
                await self.bot.db.execute(
                    "UPDATE role_panels SET enabled = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (panel["id"],),
                )
                continue
            self.bot.add_view(await self.create_public_view(panel["id"]), message_id=panel["message_id"])
            restored_public += 1
            await asyncio.sleep(0.05)

        self.logger.info(
            "Restored %s roles admin view(s) and %s public role panel view(s)",
            restored_admin,
            restored_public,
        )

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


def setup(bot: commands.Bot) -> None:
    bot.add_cog(RolesCog(bot))
