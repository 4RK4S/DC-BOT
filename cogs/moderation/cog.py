import logging
from datetime import datetime, timezone

import nextcord
from nextcord.ext import commands, tasks

from core.config import get_development_guild_ids

from .service import ModerationService, parse_duration
from .protection import AccountProtection
from .views import ModerationPanelView, ensure_moderator


GUILD_IDS = get_development_guild_ids()


class ModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.service = ModerationService(bot)
        self.account_protection = AccountProtection(self)
        self.logger = logging.getLogger(__name__)
        self.tempban_expiry_loop.start()

    def cog_unload(self) -> None:
        self.tempban_expiry_loop.cancel()

    def create_panel_view(self, show_admin_back: bool = False) -> ModerationPanelView:
        return ModerationPanelView(self, show_admin_back=show_admin_back)

    @commands.Cog.listener()
    async def on_message(self, message: nextcord.Message) -> None:
        await self.account_protection.handle_message(message)

    @tasks.loop(seconds=60)
    async def tempban_expiry_loop(self) -> None:
        try:
            completed = await self.service.process_expired_tempbans()
            if completed:
                self.logger.info("Expired %s temporary ban(s)", completed)
        except Exception:
            self.logger.exception("Temporary ban expiry task failed")

    @tempban_expiry_loop.before_loop
    async def before_tempban_expiry_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _defaults(self, interaction, notify):
        settings = await self.service.get_settings(interaction.guild_id)
        return settings, bool(settings["notify_by_default"]) if notify is None else notify

    async def _run_mute(self, interaction, member, duration, reason, notify, edit=False) -> None:
        if not await ensure_moderator(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        settings, notify = await self._defaults(interaction, notify)
        duration = duration or settings["default_duration"]
        try:
            expires_at, dm_sent = await self.service.mute(
                interaction.user, member, duration,
                reason or ("" if edit else "No reason provided."), notify, edit=edit,
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        verb = "Mute updated" if edit else "Member muted"
        await interaction.followup.send(
            f"✅ {verb}: {member.mention} until <t:{int(expires_at.timestamp())}:F> "
            f"(<t:{int(expires_at.timestamp())}:R>). DM: {'sent' if dm_sent else 'not sent / unavailable'}.",
            ephemeral=True,
        )

    @nextcord.slash_command(name="mute", description="Temporarily mute a server member.", guild_ids=GUILD_IDS)
    async def mute(
        self, interaction: nextcord.Interaction,
        member: nextcord.Member = nextcord.SlashOption(description="Member to mute"),
        duration: str | None = nextcord.SlashOption(description="30m, 2h, 3d, 1w; empty uses default", required=False, default=None),
        reason: str | None = nextcord.SlashOption(description="Reason", required=False, default=None, max_length=500),
        notify: bool | None = nextcord.SlashOption(description="Send a private notification", required=False, default=None),
    ) -> None:
        await self._run_mute(interaction, member, duration, reason, notify)

    @nextcord.slash_command(name="mute-edit", description="Edit an active member mute.", guild_ids=GUILD_IDS)
    async def mute_edit(
        self, interaction: nextcord.Interaction,
        member: nextcord.Member = nextcord.SlashOption(description="Muted member"),
        duration: str = nextcord.SlashOption(description="New duration counted from now"),
        reason: str | None = nextcord.SlashOption(description="Updated reason; empty keeps current", required=False, default=None, max_length=500),
        notify: bool | None = nextcord.SlashOption(description="Send an updated private notification", required=False, default=None),
    ) -> None:
        await self._run_mute(interaction, member, duration, reason, notify, edit=True)

    @nextcord.slash_command(name="unmute", description="Remove a member's active mute.", guild_ids=GUILD_IDS)
    async def unmute(
        self, interaction: nextcord.Interaction,
        member: nextcord.Member = nextcord.SlashOption(description="Member to unmute"),
        reason: str | None = nextcord.SlashOption(description="Reason", required=False, default=None, max_length=500),
        notify: bool | None = nextcord.SlashOption(description="Send a private notification", required=False, default=None),
    ) -> None:
        if not await ensure_moderator(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        _, notify = await self._defaults(interaction, notify)
        try:
            dm_sent = await self.service.unmute(interaction.user, member, reason or "Mute removed by a moderator.", notify)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(f"✅ Member unmuted: {member.mention}. DM: {'sent' if dm_sent else 'not sent / unavailable'}.", ephemeral=True)

    @nextcord.slash_command(name="warn", description="Give a member a warning.", guild_ids=GUILD_IDS)
    async def warn(
        self, interaction: nextcord.Interaction,
        member: nextcord.Member = nextcord.SlashOption(description="Member to warn"),
        reason: str = nextcord.SlashOption(description="Warning reason", max_length=500),
        notify: bool | None = nextcord.SlashOption(description="Send a private notification", required=False, default=None),
    ) -> None:
        if not await ensure_moderator(interaction): return
        await interaction.response.defer(ephemeral=True)
        settings, notify = await self._defaults(interaction, notify)
        try:
            case_id, count, dm_sent = await self.service.warn(interaction.user, member, reason, notify)
            auto = await self._apply_warning_automation(interaction.user, member, count, settings)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True); return
        suffix = f" Automatic action: {auto}." if auto else ""
        await interaction.followup.send(f"✅ Warning case `#{case_id}` added. Active warnings: **{count}**. DM: {'sent' if dm_sent else 'not sent / unavailable'}.{suffix}", ephemeral=True)

    async def _apply_warning_automation(self, actor, member, count, settings) -> str | None:
        threshold = int(settings["warn_threshold"] or 0)
        if not threshold or count != threshold:
            return None
        reason = f"Automatic action after reaching {count} active warnings."
        action = settings["warn_action"]
        try:
            if action == "mute":
                await self.service.mute(actor, member, settings["warn_action_duration"], reason, True, source="warn_automation")
            elif action == "kick":
                await self.service.kick(actor, member, reason, True, source="warn_automation")
            elif action == "ban":
                await self.service.ban(actor, member, reason, True, source="warn_automation")
            return action
        except ValueError as exc:
            return f"failed ({exc})"

    @nextcord.slash_command(name="warnings", description="Show a member's warnings.", guild_ids=GUILD_IDS)
    async def warnings(self, interaction: nextcord.Interaction, member: nextcord.Member = nextcord.SlashOption(description="Member")) -> None:
        if await ensure_moderator(interaction):
            await interaction.response.send_message(embed=await self.service.build_member_cases_embed(interaction.guild_id, member.id, True), ephemeral=True)

    @nextcord.slash_command(name="warn-edit", description="Edit a warning reason.", guild_ids=GUILD_IDS)
    async def warn_edit(self, interaction: nextcord.Interaction, case_id: int = nextcord.SlashOption(description="Warning case number", min_value=1), reason: str = nextcord.SlashOption(description="New reason", max_length=500)) -> None:
        if not await ensure_moderator(interaction): return
        try: await self.service.edit_case_reason(interaction.user, case_id, reason, warning_only=True)
        except ValueError as exc: await interaction.response.send_message(str(exc), ephemeral=True); return
        await interaction.response.send_message(f"✅ Warning case `#{case_id}` updated.", ephemeral=True)

    @nextcord.slash_command(name="unwarn", description="Remove an active warning.", guild_ids=GUILD_IDS)
    async def unwarn(self, interaction: nextcord.Interaction, case_id: int = nextcord.SlashOption(description="Warning case number", min_value=1), reason: str | None = nextcord.SlashOption(description="Removal reason", required=False, default=None, max_length=500)) -> None:
        if not await ensure_moderator(interaction): return
        try: await self.service.remove_warning(interaction.user, case_id, reason or "Warning removed by a moderator.")
        except ValueError as exc: await interaction.response.send_message(str(exc), ephemeral=True); return
        await interaction.response.send_message(f"✅ Warning case `#{case_id}` removed.", ephemeral=True)

    @nextcord.slash_command(name="kick", description="Kick a member from the server.", guild_ids=GUILD_IDS)
    async def kick(self, interaction: nextcord.Interaction, member: nextcord.Member = nextcord.SlashOption(description="Member to kick"), reason: str | None = nextcord.SlashOption(description="Reason", required=False, default=None, max_length=500), notify: bool | None = nextcord.SlashOption(description="Send DM before kicking", required=False, default=None)) -> None:
        if not await ensure_moderator(interaction): return
        await interaction.response.defer(ephemeral=True); _, notify = await self._defaults(interaction, notify)
        try: case_id, dm = await self.service.kick(interaction.user, member, reason or "No reason provided.", notify)
        except ValueError as exc: await interaction.followup.send(str(exc), ephemeral=True); return
        await interaction.followup.send(f"✅ {member} kicked. Case `#{case_id}`. DM: {'sent' if dm else 'not sent / unavailable'}.", ephemeral=True)

    async def _run_ban(self, interaction, member, reason, notify, duration=None, delete_days=0) -> None:
        if not await ensure_moderator(interaction): return
        await interaction.response.defer(ephemeral=True); _, notify = await self._defaults(interaction, notify)
        try:
            case_id, expires, dm = await self.service.ban(interaction.user, member, reason or "No reason provided.", notify, duration, delete_days * 86400)
        except ValueError as exc: await interaction.followup.send(str(exc), ephemeral=True); return
        until = f" Until <t:{int(expires.timestamp())}:F>." if expires else ""
        await interaction.followup.send(f"✅ {member} banned. Case `#{case_id}`.{until} DM: {'sent' if dm else 'not sent / unavailable'}.", ephemeral=True)

    @nextcord.slash_command(name="ban", description="Permanently ban a member.", guild_ids=GUILD_IDS)
    async def ban(self, interaction: nextcord.Interaction, member: nextcord.Member = nextcord.SlashOption(description="Member to ban"), reason: str | None = nextcord.SlashOption(description="Reason", required=False, default=None, max_length=500), notify: bool | None = nextcord.SlashOption(description="Send DM before banning", required=False, default=None), delete_days: int = nextcord.SlashOption(description="Delete message history: 0-7 days", min_value=0, max_value=7, required=False, default=0)) -> None:
        await self._run_ban(interaction, member, reason, notify, delete_days=delete_days)

    @nextcord.slash_command(name="tempban", description="Temporarily ban a member.", guild_ids=GUILD_IDS)
    async def tempban(self, interaction: nextcord.Interaction, member: nextcord.Member = nextcord.SlashOption(description="Member to ban"), duration: str = nextcord.SlashOption(description="Duration, for example 2h, 3d, or 1w"), reason: str | None = nextcord.SlashOption(description="Reason", required=False, default=None, max_length=500), notify: bool | None = nextcord.SlashOption(description="Send DM before banning", required=False, default=None), delete_days: int = nextcord.SlashOption(description="Delete message history: 0-7 days", min_value=0, max_value=7, required=False, default=0)) -> None:
        await self._run_ban(interaction, member, reason, notify, duration, delete_days)

    @nextcord.slash_command(name="unban", description="Unban a user by ID.", guild_ids=GUILD_IDS)
    async def unban(self, interaction: nextcord.Interaction, user_id: str = nextcord.SlashOption(description="Discord user ID"), reason: str | None = nextcord.SlashOption(description="Reason", required=False, default=None, max_length=500), notify: bool | None = nextcord.SlashOption(description="Send DM after unbanning", required=False, default=None)) -> None:
        if not await ensure_moderator(interaction): return
        await interaction.response.defer(ephemeral=True); _, notify = await self._defaults(interaction, notify)
        try: case_id, dm = await self.service.unban(interaction.user, int(user_id), reason or "Ban removed by a moderator.", notify)
        except (ValueError, TypeError):
            await interaction.followup.send("Invalid user ID or the user is not banned.", ephemeral=True); return
        await interaction.followup.send(f"✅ User unbanned. Case `#{case_id}`. DM: {'sent' if dm else 'not sent / unavailable'}.", ephemeral=True)

    @nextcord.slash_command(name="case", description="Show one moderation case.", guild_ids=GUILD_IDS)
    async def case(self, interaction: nextcord.Interaction, case_id: int = nextcord.SlashOption(description="Case number", min_value=1)) -> None:
        if not await ensure_moderator(interaction): return
        try: embed = await self.service.build_case_embed(interaction.guild_id, case_id)
        except ValueError as exc: await interaction.response.send_message(str(exc), ephemeral=True); return
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @nextcord.slash_command(name="cases", description="Show a member's moderation history.", guild_ids=GUILD_IDS)
    async def cases(self, interaction: nextcord.Interaction, member: nextcord.Member = nextcord.SlashOption(description="Member")) -> None:
        if await ensure_moderator(interaction):
            await interaction.response.send_message(embed=await self.service.build_member_cases_embed(interaction.guild_id, member.id), ephemeral=True)

    @nextcord.slash_command(name="case-edit", description="Edit a moderation case reason.", guild_ids=GUILD_IDS)
    async def case_edit(self, interaction: nextcord.Interaction, case_id: int = nextcord.SlashOption(description="Case number", min_value=1), reason: str = nextcord.SlashOption(description="New reason", max_length=500)) -> None:
        if not await ensure_moderator(interaction): return
        try: await self.service.edit_case_reason(interaction.user, case_id, reason)
        except ValueError as exc: await interaction.response.send_message(str(exc), ephemeral=True); return
        await interaction.response.send_message(f"✅ Case `#{case_id}` updated.", ephemeral=True)

    @nextcord.slash_command(name="slowmode", description="Set slowmode in the current channel.", guild_ids=GUILD_IDS)
    async def slowmode(self, interaction: nextcord.Interaction, seconds: int = nextcord.SlashOption(description="0 disables slowmode", min_value=0, max_value=21600)) -> None:
        if not await self._require_channel_permission(interaction, "manage_channels", "Manage Channels"): return
        try: await interaction.channel.edit(slowmode_delay=seconds, reason=f"Changed by {interaction.user}")
        except (nextcord.Forbidden, nextcord.HTTPException): await interaction.response.send_message("Discord rejected the slowmode change.", ephemeral=True); return
        await self.bot.db.log_action(interaction.guild_id, "Moderation", "slowmode", user_id=interaction.user.id, details={"channel_id": interaction.channel_id, "seconds": seconds})
        await interaction.response.send_message(f"✅ Slowmode set to **{seconds} seconds**.", ephemeral=True)

    async def _set_lock(self, interaction, locked: bool) -> None:
        if not await self._require_channel_permission(interaction, "manage_channels", "Manage Channels"): return
        channel = interaction.channel
        if not isinstance(channel, nextcord.TextChannel):
            await interaction.response.send_message("This command requires a text channel.", ephemeral=True); return
        try: await channel.set_permissions(interaction.guild.default_role, send_messages=False if locked else None, reason=f"{'Locked' if locked else 'Unlocked'} by {interaction.user}")
        except (nextcord.Forbidden, nextcord.HTTPException): await interaction.response.send_message("Discord rejected the channel permission change.", ephemeral=True); return
        await self.bot.db.log_action(interaction.guild_id, "Moderation", "channel_locked" if locked else "channel_unlocked", user_id=interaction.user.id, details={"channel_id": channel.id})
        await interaction.response.send_message(f"✅ Channel {'locked' if locked else 'unlocked'}.", ephemeral=True)

    @nextcord.slash_command(name="lock", description="Lock the current text channel.", guild_ids=GUILD_IDS)
    async def lock(self, interaction: nextcord.Interaction) -> None: await self._set_lock(interaction, True)

    @nextcord.slash_command(name="unlock", description="Unlock the current text channel.", guild_ids=GUILD_IDS)
    async def unlock(self, interaction: nextcord.Interaction) -> None: await self._set_lock(interaction, False)

    @nextcord.slash_command(name="nickname", description="Set or clear a member nickname.", guild_ids=GUILD_IDS)
    async def nickname(self, interaction: nextcord.Interaction, member: nextcord.Member = nextcord.SlashOption(description="Member"), nickname: str | None = nextcord.SlashOption(description="Empty clears the nickname", required=False, default=None, max_length=32)) -> None:
        if not await self._require_channel_permission(interaction, "manage_nicknames", "Manage Nicknames"): return
        try:
            self.service.validate_target(interaction.user, member, require_bot_moderate=False)
            self.service.validate_bot_permission(interaction.guild, "manage_nicknames", "Manage Nicknames")
            await member.edit(nick=nickname or None, reason=f"Changed by {interaction.user}")
        except ValueError as exc: await interaction.response.send_message(str(exc), ephemeral=True); return
        except (nextcord.Forbidden, nextcord.HTTPException): await interaction.response.send_message("Discord rejected the nickname change.", ephemeral=True); return
        await self.bot.db.log_action(interaction.guild_id, "Moderation", "nickname_changed", user_id=interaction.user.id, target_id=member.id, details={"nickname": nickname})
        await interaction.response.send_message(f"✅ Nickname {'changed' if nickname else 'cleared'}.", ephemeral=True)

    @nextcord.slash_command(name="purge-user", description="Delete a member's messages by amount or time.", guild_ids=GUILD_IDS)
    async def purge_user(
        self,
        interaction: nextcord.Interaction,
        member: nextcord.Member = nextcord.SlashOption(description="Message author"),
        amount: int | None = nextcord.SlashOption(
            description="Number of newest messages; empty with no time deletes 1",
            min_value=1, max_value=500, required=False, default=None,
        ),
        time: str | None = nextcord.SlashOption(
            description="Delete messages from the last time period, e.g. 5m, 2h, 3d",
            required=False, default=None,
        ),
        all_channels: bool = nextcord.SlashOption(
            description="True = all text channels; False = current channel",
            required=False, default=False,
        ),
    ) -> None:
        if not await self._require_channel_permission(interaction, "manage_messages", "Manage Messages"):
            return
        if amount is not None and time:
            await interaction.response.send_message(
                "Choose either `amount` or `time`, not both.", ephemeral=True
            )
            return

        duration = None
        if time:
            try:
                duration = parse_duration(time)
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        elif amount is None:
            amount = 1

        current_channel = interaction.channel
        if not all_channels and not isinstance(current_channel, (nextcord.TextChannel, nextcord.Thread)):
            await interaction.response.send_message(
                "This command requires a text channel or thread.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        channels = self._purge_channels(interaction) if all_channels else [current_channel]
        try:
            if duration is not None:
                cutoff = datetime.now(timezone.utc) - duration
                deleted_count, affected_channels, skipped_channels = await self._purge_by_time(
                    interaction, channels, member.id, cutoff
                )
                mode_details = {"mode": "time", "time": time, "cutoff": cutoff.isoformat()}
                mode_text = f"from the last **{time}**"
            else:
                deleted_count, affected_channels, skipped_channels = await self._purge_by_amount(
                    interaction, channels, member.id, amount or 1
                )
                mode_details = {"mode": "amount", "amount": amount or 1}
                mode_text = f"(newest requested: **{amount or 1}**)"
        except nextcord.HTTPException:
            self.logger.exception("Failed to purge messages for user %s", member.id)
            await interaction.followup.send(
                "Discord rejected part of the message deletion. Some messages may already have been deleted.",
                ephemeral=True,
            )
            return

        await self.bot.db.log_action(
            interaction.guild_id, "Moderation", "purge_user",
            user_id=interaction.user.id, target_id=member.id,
            details={
                **mode_details,
                "all_channels": all_channels,
                "source_channel_id": current_channel.id,
                "affected_channels": affected_channels,
                "skipped_channels": skipped_channels,
                "deleted": deleted_count,
            },
        )
        scope = "all accessible channels" if all_channels else current_channel.mention
        skipped = f" Skipped inaccessible channels: **{skipped_channels}**." if skipped_channels else ""
        await interaction.followup.send(
            f"✅ Deleted **{deleted_count}** message(s) from {member.mention} {mode_text} in {scope}.{skipped}",
            ephemeral=True,
        )

    def _purge_channels(self, interaction: nextcord.Interaction) -> list:
        channels = list(interaction.guild.text_channels)
        known_ids = {channel.id for channel in channels}
        for thread in interaction.guild.threads:
            if thread.id not in known_ids:
                channels.append(thread)
                known_ids.add(thread.id)
        return channels

    def _can_purge_channel(self, guild: nextcord.Guild, channel) -> bool:
        me = guild.me
        if me is None or not hasattr(channel, "permissions_for"):
            return False
        permissions = channel.permissions_for(me)
        return bool(
            permissions.view_channel
            and permissions.read_message_history
            and permissions.manage_messages
        )

    async def _purge_by_time(self, interaction, channels, target_id: int, cutoff: datetime) -> tuple[int, int, int]:
        deleted_count = 0
        affected_channels = 0
        skipped_channels = 0
        for channel in channels:
            if not self._can_purge_channel(interaction.guild, channel):
                skipped_channels += 1
                continue
            try:
                deleted = await channel.purge(
                    limit=None,
                    after=cutoff,
                    check=lambda message: message.author.id == target_id,
                )
            except (nextcord.Forbidden, nextcord.NotFound):
                skipped_channels += 1
                continue
            deleted_count += len(deleted)
            if deleted:
                affected_channels += 1
        return deleted_count, affected_channels, skipped_channels

    async def _purge_by_amount(self, interaction, channels, target_id: int, amount: int) -> tuple[int, int, int]:
        candidates = []
        skipped_channels = 0
        for channel in channels:
            if not self._can_purge_channel(interaction.guild, channel):
                skipped_channels += 1
                continue
            try:
                found_in_channel = 0
                async for message in channel.history(limit=None):
                    if message.author.id == target_id:
                        candidates.append(message)
                        found_in_channel += 1
                        if found_in_channel >= amount:
                            break
            except (nextcord.Forbidden, nextcord.NotFound):
                skipped_channels += 1

        candidates.sort(key=lambda message: message.created_at, reverse=True)
        selected = candidates[:amount]
        affected_ids = set()
        deleted_count = 0
        for message in selected:
            try:
                await message.delete(reason=f"User purge by {interaction.user}")
            except (nextcord.Forbidden, nextcord.NotFound):
                continue
            deleted_count += 1
            affected_ids.add(message.channel.id)
        return deleted_count, len(affected_ids), skipped_channels

    async def _require_channel_permission(self, interaction, permission, label) -> bool:
        if not isinstance(interaction.user, nextcord.Member):
            await interaction.response.send_message("This can only be used in a server.", ephemeral=True); return False
        if not (interaction.user.guild_permissions.administrator or getattr(interaction.user.guild_permissions, permission, False)):
            await interaction.response.send_message(f"You need {label} permission.", ephemeral=True); return False
        me = interaction.guild.me
        if me is None or not (me.guild_permissions.administrator or getattr(me.guild_permissions, permission, False)):
            await interaction.response.send_message(f"The bot needs {label} permission.", ephemeral=True); return False
        return True

    @nextcord.slash_command(name="moderation-panel", description="Open moderation controls.", guild_ids=GUILD_IDS)
    async def moderation_panel(self, interaction: nextcord.Interaction) -> None:
        if await ensure_moderator(interaction):
            await interaction.response.send_message(embed=await self.service.build_panel_embed(interaction.guild), view=self.create_panel_view(), ephemeral=True)


def setup(bot: commands.Bot) -> None:
    bot.add_cog(ModerationCog(bot))
