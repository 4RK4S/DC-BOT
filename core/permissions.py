import nextcord


def is_guild_admin(member: nextcord.Member) -> bool:
    return bool(member.guild_permissions.administrator)


def can_manage_guild(member: nextcord.Member) -> bool:
    permissions = member.guild_permissions
    return bool(permissions.administrator or permissions.manage_guild)


async def require_admin(interaction: nextcord.Interaction) -> bool:
    if not isinstance(interaction.user, nextcord.Member):
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return False

    if not is_guild_admin(interaction.user):
        await interaction.response.send_message("You need administrator permissions to use this.", ephemeral=True)
        return False

    return True


async def require_guild_manager(interaction: nextcord.Interaction) -> bool:
    if not isinstance(interaction.user, nextcord.Member):
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return False

    if not can_manage_guild(interaction.user):
        await interaction.response.send_message(
            "You need Administrator or Manage Server permissions to use this.",
            ephemeral=True,
        )
        return False

    return True
