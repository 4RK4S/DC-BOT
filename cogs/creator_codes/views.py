import nextcord

from core.permissions import can_manage_guild


async def ensure_manager(interaction: nextcord.Interaction) -> bool:
    if not isinstance(interaction.user, nextcord.Member):
        await interaction.response.send_message("This can only be used in a server.", ephemeral=True)
        return False
    if not can_manage_guild(interaction.user):
        await interaction.response.send_message("You need Administrator or Manage Server permissions to use this.", ephemeral=True)
        return False
    return True


async def resolve_text_channel(interaction: nextcord.Interaction, channel_id: int):
    channel = interaction.client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await interaction.client.fetch_channel(channel_id)
        except (nextcord.Forbidden, nextcord.HTTPException, nextcord.NotFound):
            raise ValueError("Channel not found. Make sure the bot can see this channel.") from None
    if not isinstance(channel, nextcord.TextChannel):
        raise ValueError("Channel must be a text channel.")
    if channel.guild.id != interaction.guild_id:
        raise ValueError("This channel is not from this server.")
    bot_member = interaction.guild.me if interaction.guild else None
    perms = channel.permissions_for(bot_member) if bot_member else None
    if perms is not None and (not perms.view_channel or not perms.send_messages or not perms.embed_links):
        raise ValueError("I need View Channel, Send Messages, and Embed Links in that channel.")
    return channel


def apply_button_order(view: nextcord.ui.View, custom_ids: list[str]) -> None:
    order = {custom_id: index for index, custom_id in enumerate(custom_ids)}
    view.children.sort(key=lambda child: order.get(getattr(child, "custom_id", ""), len(order)))
    for index, child in enumerate(view.children):
        child.row = min(index // 5, 4)


class CreatorCodeClaimButton(nextcord.ui.Button):
    def __init__(self, cog, pool_id: int, label: str) -> None:
        super().__init__(label=label, style=nextcord.ButtonStyle.primary, custom_id=f"creator_codes:claim:{pool_id}")
        self.cog = cog
        self.pool_id = pool_id

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("This can only be used in a server.", ephemeral=True)
            return
        try:
            result = await self.cog.service.claim(interaction.guild_id, self.pool_id, interaction.user)
            if result.status == "empty":
                await self.cog.refresh_public_embed(interaction.guild_id)
            elif result.status == "claimed":
                await self.cog.refresh_public_embed(interaction.guild_id)
        except Exception as exc:
            await self.cog.service.log(
                interaction.guild_id,
                "creator_codes_error",
                user_id=interaction.user.id,
                details={"pool_id": self.pool_id, "error": str(exc)},
            )
            await interaction.response.send_message("Could not claim this code right now.", ephemeral=True)
            return

        if result.status == "inactive":
            await interaction.response.send_message("No active codes are available anymore.", ephemeral=True)
            return
        if result.status == "empty":
            await interaction.response.send_message("No active codes are available anymore.", ephemeral=True)
            return
        if result.code is None:
            await interaction.response.send_message("No active codes are available anymore.", ephemeral=True)
            return

        already = result.status == "already"
        dm_text = build_claim_message(result.key_words or "Creator Code", result.code, already=already)
        try:
            await interaction.user.send(dm_text)
            await interaction.response.send_message("**Code sent in a private message.**", ephemeral=True)
        except nextcord.Forbidden:
            await interaction.response.send_message(dm_text, ephemeral=True)


class CreatorCodePublicView(nextcord.ui.View):
    def __init__(self, cog, pools) -> None:
        super().__init__(timeout=None)
        for index, pool in enumerate(pools[:25], start=1):
            self.add_item(CreatorCodeClaimButton(cog, pool.id, f"Code {index}"))


class ChannelModal(nextcord.ui.Modal):
    def __init__(self, cog, action: str) -> None:
        title = "Set Public Channel" if action == "public" else "Set Announcement Channel"
        super().__init__(title)
        self.cog = cog
        self.action = action
        self.channel_id_input = nextcord.ui.TextInput("Channel ID", required=True, max_length=32)
        self.add_item(self.channel_id_input)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        try:
            channel = await resolve_text_channel(interaction, int(str(self.channel_id_input.value).strip()))
            if self.action == "public":
                await self.cog.service.set_public_channel(interaction.guild_id, channel.id, interaction.user.id)
                message = await self.cog.refresh_public_embed(interaction.guild_id)
                response = f"Public channel set to {channel.mention}."
                if message is not None:
                    response += f" Public embed ready: `{message.id}`."
            else:
                await self.cog.service.set_announcement_channel(interaction.guild_id, channel.id, interaction.user.id)
                response = f"Announcement channel set to {channel.mention}."
        except (ValueError, nextcord.HTTPException) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(response, ephemeral=True)


class PingRoleModal(nextcord.ui.Modal):
    def __init__(self, cog) -> None:
        super().__init__("Set Ping Role")
        self.cog = cog
        self.role_id_input = nextcord.ui.TextInput("Role ID", required=False, max_length=32)
        self.add_item(self.role_id_input)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        try:
            clean = str(self.role_id_input.value or "").strip()
            role_id = None
            if clean:
                role_id = int(clean)
                role = interaction.guild.get_role(role_id) if interaction.guild else None
                if role is None:
                    raise ValueError("Role not found in this server.")
            await self.cog.service.set_ping_role(interaction.guild_id, role_id, interaction.user.id)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message("Ping role updated.", ephemeral=True)


class AddCodesModal(nextcord.ui.Modal):
    def __init__(self, cog) -> None:
        super().__init__("Add Creator Codes")
        self.cog = cog
        self.codes_input = nextcord.ui.TextInput("Code / Codes", required=True, style=nextcord.TextInputStyle.paragraph)
        self.key_words_input = nextcord.ui.TextInput("Key Words", required=True, style=nextcord.TextInputStyle.paragraph)
        self.expire_input = nextcord.ui.TextInput("Expire", placeholder="mm.dd.yyyy HH:MM UTC+9", required=True)
        self.add_item(self.codes_input)
        self.add_item(self.key_words_input)
        self.add_item(self.expire_input)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        try:
            pool, added, skipped, became_active = await self.cog.service.add_codes(
                interaction.guild_id,
                str(self.codes_input.value),
                str(self.key_words_input.value),
                str(self.expire_input.value),
                user_id=interaction.user.id,
            )
            await self.cog.refresh_public_embed(interaction.guild_id)
            if became_active:
                await self.cog.send_announcement(interaction.guild_id, pool)
        except (ValueError, nextcord.HTTPException) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(f"Added {added} creator code(s). Skipped {skipped} duplicate(s).", ephemeral=True)


class RemovePoolSelect(nextcord.ui.Select):
    def __init__(self, cog, pools) -> None:
        self.cog = cog
        options = [
            nextcord.SelectOption(label=f"#{pool['id']} - {pool['key_words']}"[:100], value=str(pool["id"]))
            for pool in pools[:25]
        ]
        super().__init__(placeholder="Select pool to remove", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        pool_id = int(self.values[0])
        removed = await self.cog.service.remove_pool(interaction.guild_id, pool_id, interaction.user.id)
        await self.cog.refresh_public_embed(interaction.guild_id)
        await interaction.response.edit_message(content="Pool removed." if removed else "Pool was already inactive or missing.", view=None)


class RemovePoolView(nextcord.ui.View):
    def __init__(self, cog, pools) -> None:
        super().__init__(timeout=120)
        self.add_item(RemovePoolSelect(cog, pools))


class BackToAdminPanelButton(nextcord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Back", style=nextcord.ButtonStyle.secondary, custom_id="creator_codes:admin_back", row=4)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        from cogs.admin_panel.views import AdminPanelView
        from core.embeds import admin_panel_embed

        await interaction.response.edit_message(content=None, embed=admin_panel_embed(), view=AdminPanelView(interaction.client))


class CreatorCodeAdminPanelView(nextcord.ui.View):
    def __init__(self, cog, show_admin_back: bool = False) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        if show_admin_back:
            self.add_item(BackToAdminPanelButton())
        apply_button_order(
            self,
            [
                "creator_codes:add_codes",
                "creator_codes:set_public",
                "creator_codes:set_announcement",
                "creator_codes:set_ping",
                "creator_codes:refresh_public",
                "creator_codes:list_pools",
                "creator_codes:remove_pool",
                "creator_codes:clear_expired",
                "creator_codes:settings",
                "creator_codes:admin_back",
            ],
        )

    @nextcord.ui.button(label="Set Public Channel", style=nextcord.ButtonStyle.success, custom_id="creator_codes:set_public", row=0)
    async def set_public_channel(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_modal(ChannelModal(self.cog, "public"))

    @nextcord.ui.button(label="Set Announcement Channel", style=nextcord.ButtonStyle.success, custom_id="creator_codes:set_announcement", row=0)
    async def set_announcement_channel(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_modal(ChannelModal(self.cog, "announcement"))

    @nextcord.ui.button(label="Set Ping Role", style=nextcord.ButtonStyle.success, custom_id="creator_codes:set_ping", row=0)
    async def set_ping_role(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_modal(PingRoleModal(self.cog))

    @nextcord.ui.button(label="Add Codes", style=nextcord.ButtonStyle.primary, custom_id="creator_codes:add_codes", row=0)
    async def add_codes(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_modal(AddCodesModal(self.cog))

    @nextcord.ui.button(label="Remove Pool", style=nextcord.ButtonStyle.danger, custom_id="creator_codes:remove_pool", row=1)
    async def remove_pool(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        pools = [pool for pool in await self.cog.service.list_pools(interaction.guild_id) if bool(pool["enabled"])]
        if not pools:
            await interaction.response.send_message("No active creator code pools configured.", ephemeral=True)
            return
        await interaction.response.send_message("Select pool to remove.", view=RemovePoolView(self.cog, pools), ephemeral=True)

    @nextcord.ui.button(label="Refresh Public Embed", style=nextcord.ButtonStyle.success, custom_id="creator_codes:refresh_public", row=0)
    async def refresh_public(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        message = await self.cog.refresh_public_embed(interaction.guild_id)
        await self.cog.service.log(interaction.guild_id, "creator_codes_refresh_public", user_id=interaction.user.id)
        await interaction.response.send_message(
            f"Public embed refreshed in {message.channel.mention}." if message else "Set a public channel first.",
            ephemeral=True,
        )

    @nextcord.ui.button(label="List Pools", style=nextcord.ButtonStyle.secondary, custom_id="creator_codes:list_pools", row=1)
    async def list_pools(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_message(format_pool_list(await self.cog.service.list_pools(interaction.guild_id)), ephemeral=True)

    @nextcord.ui.button(label="Clear Expired / Used Pools", style=nextcord.ButtonStyle.danger, custom_id="creator_codes:clear_expired", row=1)
    async def clear_expired(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        count = await self.cog.service.clear_expired_or_used(interaction.guild_id, interaction.user.id)
        await self.cog.refresh_public_embed(interaction.guild_id)
        await interaction.response.send_message(f"Disabled {count} expired or used pool(s).", ephemeral=True)

    @nextcord.ui.button(label="Show Settings", style=nextcord.ButtonStyle.secondary, custom_id="creator_codes:settings", row=2)
    async def show_settings(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_message(embed=await self.cog.service.build_settings_embed(interaction.guild_id), ephemeral=True)


def build_claim_message(key_words: str, code: str, already: bool = False) -> str:
    prefix = "You already received this code earlier.\n\n" if already else ""
    return f"{prefix}**{key_words}**\n\n🔐 Your code:\n```{code}```"


def format_pool_list(rows) -> str:
    if not rows:
        return "No creator code pools configured."
    lines = []
    for index, row in enumerate(rows[:25], start=1):
        lines.append(
            f"Code {index} | pool `{row['id']}` | enabled `{row['enabled']}` | unused `{row['left_count'] or 0}` | used `{row['used_count'] or 0}`\n"
            f"{row['key_words']}\nExpire: `{row['expire_at'] or 'None'}`"
        )
    return "\n\n".join(lines)[:1900]
