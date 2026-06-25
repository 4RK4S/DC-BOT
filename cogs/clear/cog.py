import logging

import nextcord
from nextcord.ext import commands

from core.config import get_development_guild_ids
from core.permissions import require_guild_manager


MODULE_NAME = "clear"


class ClearCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.logger = logging.getLogger(__name__)

    @nextcord.slash_command(
        name="clear",
        description="Delete messages from this channel.",
        guild_ids=get_development_guild_ids(),
    )
    async def clear(
        self,
        interaction: nextcord.Interaction,
        amount: int | None = nextcord.SlashOption(
            description="Optional number of messages to delete. Empty = max 1000.",
            min_value=1,
            max_value=1000,
            required=False,
            default=None,
        ),
    ) -> None:
        if not await require_guild_manager(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        channel = interaction.channel
        if not isinstance(channel, (nextcord.TextChannel, nextcord.Thread)):
            await interaction.followup.send("This command can only be used in text channels or threads.", ephemeral=True)
            return

        if not isinstance(interaction.user, nextcord.Member):
            await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
            return

        user_permissions = channel.permissions_for(interaction.user)
        if not user_permissions.manage_messages:
            await interaction.followup.send("You need Manage Messages to use this command.", ephemeral=True)
            return

        me = interaction.guild.me if interaction.guild else None
        if me is None:
            await interaction.followup.send("Could not check bot permissions in this server.", ephemeral=True)
            return

        bot_permissions = channel.permissions_for(me)
        if not bot_permissions.manage_messages:
            await interaction.followup.send("I need Manage Messages to clear messages here.", ephemeral=True)
            return

        requested_amount = amount or 1000

        try:
            deleted = await channel.purge(limit=requested_amount)
        except nextcord.Forbidden:
            await interaction.followup.send("I do not have permission to delete messages here.", ephemeral=True)
            return
        except nextcord.NotFound:
            await interaction.followup.send("This channel was not found while clearing messages.", ephemeral=True)
            return
        except nextcord.HTTPException:
            self.logger.exception("Failed to clear messages in channel %s", channel.id)
            await interaction.followup.send(
                "Discord rejected the clear request. Some messages may be too old to bulk delete.",
                ephemeral=True,
            )
            return

        deleted_count = len(deleted)
        await self.bot.db.log_action(
            interaction.guild_id,
            MODULE_NAME,
            "clear_messages",
            user_id=interaction.user.id,
            details={
                "channel_id": channel.id,
                "requested_amount": requested_amount,
                "amount_option_provided": amount is not None,
                "deleted_count": deleted_count,
            },
        )

        if deleted_count < requested_amount:
            await interaction.followup.send(
                f"✅ Deleted {deleted_count} message(s). Some older messages may have been skipped by Discord.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(f"✅ Deleted {deleted_count} message(s).", ephemeral=True)


def setup(bot: commands.Bot) -> None:
    bot.add_cog(ClearCog(bot))
