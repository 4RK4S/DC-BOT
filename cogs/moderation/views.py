import nextcord

from .service import DEFAULT_MUTE_TEMPLATE, DEFAULT_UNMUTE_TEMPLATE


async def ensure_moderator(interaction: nextcord.Interaction) -> bool:
    if not isinstance(interaction.user, nextcord.Member):
        await interaction.response.send_message("This can only be used in a server.", ephemeral=True)
        return False
    permissions = interaction.user.guild_permissions
    if not (permissions.administrator or permissions.moderate_members or permissions.manage_guild):
        await interaction.response.send_message(
            "You need Administrator, Moderate Members, or Manage Server permission.", ephemeral=True
        )
        return False
    return True


def parse_yes_no(value: str, default: bool = True) -> bool:
    raw = str(value or "").strip().lower()
    if not raw:
        return default
    if raw in {"yes", "y", "true", "1", "on", "tak", "t"}:
        return True
    if raw in {"no", "n", "false", "0", "off", "nie"}:
        return False
    raise ValueError("Notify must be yes/no (or tak/nie).")


class ModerationActionModal(nextcord.ui.Modal):
    def __init__(self, cog, action: str, default_duration: str, default_notify: bool) -> None:
        titles = {"mute": "Mute Member", "edit": "Edit Mute", "unmute": "Unmute Member"}
        super().__init__(titles[action])
        self.cog = cog
        self.action = action
        self.user_id = nextcord.ui.TextInput("User ID", required=True, max_length=32)
        self.add_item(self.user_id)
        if action != "unmute":
            self.duration = nextcord.ui.TextInput(
                "Duration (30m, 2h, 3d, 1w)", required=True,
                default_value=default_duration, max_length=32,
            )
            self.add_item(self.duration)
        self.reason = nextcord.ui.TextInput(
            "Reason", required=False, style=nextcord.TextInputStyle.paragraph,
            default_value=(
                "Mute removed by a moderator." if action == "unmute"
                else "" if action == "edit"
                else "No reason provided."
            ),
            max_length=500,
        )
        self.notify = nextcord.ui.TextInput(
            "Send DM? (yes/no)", required=True,
            default_value="yes" if default_notify else "no", max_length=5,
        )
        self.add_item(self.reason)
        self.add_item(self.notify)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_moderator(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            user_id = int(str(self.user_id.value).strip())
            member = await self.cog.service.resolve_member(interaction.guild, user_id)
            notify = parse_yes_no(self.notify.value)
            reason = str(self.reason.value or "").strip()
            if self.action == "unmute":
                dm_sent = await self.cog.service.unmute(
                    interaction.user, member, reason, notify, source="admin_panel"
                )
                message = f"✅ {member} was unmuted. DM: {'sent' if dm_sent else 'not sent / unavailable'}."
            else:
                expires, dm_sent = await self.cog.service.mute(
                    interaction.user, member, str(self.duration.value), reason, notify,
                    source="admin_panel", edit=self.action == "edit",
                )
                action_text = "mute was updated" if self.action == "edit" else "was muted"
                message = f"✅ {member} {action_text} until <t:{int(expires.timestamp())}:F>. DM: {'sent' if dm_sent else 'not sent / unavailable'}."
        except (ValueError, TypeError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(message, ephemeral=True)


class ModerationSettingsModal(nextcord.ui.Modal):
    def __init__(self, cog, settings) -> None:
        super().__init__("Moderation Settings")
        self.cog = cog
        self.default_duration = nextcord.ui.TextInput(
            "Default duration", required=True, default_value=settings["default_duration"], max_length=32
        )
        self.default_notify = nextcord.ui.TextInput(
            "Notify by default? (yes/no)", required=True,
            default_value="yes" if settings["notify_by_default"] else "no", max_length=5,
        )
        self.log_channel = nextcord.ui.TextInput(
            "Log channel ID (empty = disabled)", required=False,
            default_value=str(settings["log_channel_id"] or ""), max_length=32,
        )
        self.mute_template = nextcord.ui.TextInput(
            "Mute DM template", required=False, style=nextcord.TextInputStyle.paragraph,
            default_value=settings["mute_dm_template"] or DEFAULT_MUTE_TEMPLATE, max_length=1800,
        )
        self.unmute_template = nextcord.ui.TextInput(
            "Unmute DM template", required=False, style=nextcord.TextInputStyle.paragraph,
            default_value=settings["unmute_dm_template"] or DEFAULT_UNMUTE_TEMPLATE, max_length=1800,
        )
        for item in (self.default_duration, self.default_notify, self.log_channel, self.mute_template, self.unmute_template):
            self.add_item(item)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_moderator(interaction):
            return
        try:
            notify = parse_yes_no(self.default_notify.value)
            raw_channel = str(self.log_channel.value or "").strip()
            channel_id = int(raw_channel) if raw_channel else None
            if channel_id is not None:
                channel = interaction.guild.get_channel(channel_id)
                if channel is None or not hasattr(channel, "send"):
                    raise ValueError("The log channel was not found or cannot receive messages.")
            await self.cog.service.update_settings(
                interaction.guild_id, str(self.default_duration.value), notify, channel_id,
                str(self.mute_template.value or DEFAULT_MUTE_TEMPLATE),
                str(self.unmute_template.value or DEFAULT_UNMUTE_TEMPLATE), interaction.user.id,
            )
        except (ValueError, TypeError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message("Moderation settings saved.", ephemeral=True)


class ExtendedActionModal(nextcord.ui.Modal):
    def __init__(self, cog, action: str, default_notify: bool) -> None:
        titles = {"warn": "Warn Member", "kick": "Kick Member", "ban": "Ban Member", "tempban": "Temporarily Ban Member"}
        super().__init__(titles[action])
        self.cog = cog
        self.action = action
        self.user_id = nextcord.ui.TextInput("User ID", required=True, max_length=32)
        self.reason = nextcord.ui.TextInput("Reason", required=True, style=nextcord.TextInputStyle.paragraph, max_length=500)
        self.notify = nextcord.ui.TextInput("Send DM? (yes/no)", required=True, default_value="yes" if default_notify else "no", max_length=5)
        self.add_item(self.user_id)
        if action == "tempban":
            self.duration = nextcord.ui.TextInput("Duration (2h, 3d, 1w)", required=True, default_value="1d", max_length=32)
            self.add_item(self.duration)
        if action in {"ban", "tempban"}:
            self.delete_days = nextcord.ui.TextInput("Delete message history (0-7 days)", required=True, default_value="0", max_length=1)
            self.add_item(self.delete_days)
        self.add_item(self.reason)
        self.add_item(self.notify)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_moderator(interaction): return
        await interaction.response.defer(ephemeral=True)
        try:
            member = await self.cog.service.resolve_member(interaction.guild, int(str(self.user_id.value).strip()))
            reason = str(self.reason.value).strip()
            notify = parse_yes_no(self.notify.value)
            if self.action == "warn":
                settings = await self.cog.service.get_settings(interaction.guild_id)
                case_id, count, dm = await self.cog.service.warn(interaction.user, member, reason, notify, source="admin_panel")
                auto = await self.cog._apply_warning_automation(interaction.user, member, count, settings)
                message = f"✅ Warning case #{case_id} added. Active warnings: {count}. DM: {'sent' if dm else 'not sent / unavailable'}."
                if auto: message += f" Automatic action: {auto}."
            elif self.action == "kick":
                case_id, dm = await self.cog.service.kick(interaction.user, member, reason, notify, source="admin_panel")
                message = f"✅ Member kicked. Case #{case_id}. DM: {'sent' if dm else 'not sent / unavailable'}."
            else:
                delete_days = int(str(self.delete_days.value).strip())
                if delete_days < 0 or delete_days > 7: raise ValueError("Delete days must be between 0 and 7.")
                duration = str(self.duration.value) if self.action == "tempban" else None
                case_id, expires, dm = await self.cog.service.ban(
                    interaction.user, member, reason, notify, duration, delete_days * 86400, source="admin_panel"
                )
                until = f" Until <t:{int(expires.timestamp())}:F>." if expires else ""
                message = f"✅ Member banned. Case #{case_id}.{until} DM: {'sent' if dm else 'not sent / unavailable'}."
        except (ValueError, TypeError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True); return
        await interaction.followup.send(message, ephemeral=True)


class CaseLookupModal(nextcord.ui.Modal):
    def __init__(self, cog) -> None:
        super().__init__("Find Moderation Case")
        self.cog = cog
        self.case_id = nextcord.ui.TextInput("Case number", required=True, max_length=12)
        self.add_item(self.case_id)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_moderator(interaction): return
        try: embed = await self.cog.service.build_case_embed(interaction.guild_id, int(str(self.case_id.value).strip()))
        except (ValueError, TypeError) as exc: await interaction.response.send_message(str(exc), ephemeral=True); return
        await interaction.response.send_message(embed=embed, ephemeral=True)


class WarningAutomationModal(nextcord.ui.Modal):
    def __init__(self, cog, settings) -> None:
        super().__init__("Warning Automation")
        self.cog = cog
        self.threshold = nextcord.ui.TextInput("Warnings before action (0 = off)", required=True, default_value=str(settings["warn_threshold"]), max_length=3)
        self.action = nextcord.ui.TextInput("Action: mute, kick, or ban", required=True, default_value=settings["warn_action"], max_length=8)
        self.duration = nextcord.ui.TextInput("Mute duration", required=True, default_value=settings["warn_action_duration"], max_length=32)
        for item in (self.threshold, self.action, self.duration): self.add_item(item)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_moderator(interaction): return
        try:
            await self.cog.service.update_warn_automation(
                interaction.guild_id, int(str(self.threshold.value).strip()), str(self.action.value),
                str(self.duration.value), interaction.user.id,
            )
        except (ValueError, TypeError) as exc: await interaction.response.send_message(str(exc), ephemeral=True); return
        await interaction.response.send_message("Warning automation saved.", ephemeral=True)


class AccountProtectionModal(nextcord.ui.Modal):
    def __init__(self, cog, settings) -> None:
        super().__init__("Compromised Account Protection")
        self.cog = cog
        self.enabled = nextcord.ui.TextInput(
            "Enabled? (yes/no)", required=True,
            default_value="yes" if settings["account_protection_enabled"] else "no", max_length=5,
        )
        self.window_seconds = nextcord.ui.TextInput(
            "Detection window in seconds (10-600)", required=True,
            default_value=str(settings["account_protection_window_seconds"]), max_length=3,
        )
        self.min_channels = nextcord.ui.TextInput(
            "Minimum different channels (2-20)", required=True,
            default_value=str(settings["account_protection_min_channels"]), max_length=2,
        )
        self.min_messages = nextcord.ui.TextInput(
            "Minimum suspicious messages (2-20)", required=True,
            default_value=str(settings["account_protection_min_messages"]), max_length=2,
        )
        self.min_attachments = nextcord.ui.TextInput(
            "Minimum images per message (2-10)", required=True,
            default_value=str(settings["account_protection_min_attachments"]), max_length=2,
        )
        for item in (
            self.enabled, self.window_seconds, self.min_channels,
            self.min_messages, self.min_attachments,
        ):
            self.add_item(item)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_moderator(interaction): return
        try:
            await self.cog.service.update_account_protection(
                interaction.guild_id,
                parse_yes_no(self.enabled.value),
                int(str(self.window_seconds.value).strip()),
                int(str(self.min_channels.value).strip()),
                int(str(self.min_messages.value).strip()),
                int(str(self.min_attachments.value).strip()),
                interaction.user.id,
            )
        except (ValueError, TypeError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True); return
        await interaction.response.send_message("Account Protection settings saved.", ephemeral=True)


class AccountProtectionTimeoutModal(nextcord.ui.Modal):
    def __init__(self, cog, settings) -> None:
        super().__init__("Account Protection Timeout")
        self.cog = cog
        self.duration = nextcord.ui.TextInput(
            "Automatic timeout duration", required=True,
            default_value=settings["account_protection_timeout_duration"], max_length=32,
        )
        self.add_item(self.duration)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_moderator(interaction): return
        try:
            await self.cog.service.update_account_protection_timeout(
                interaction.guild_id, str(self.duration.value), interaction.user.id
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True); return
        await interaction.response.send_message("Account Protection timeout saved.", ephemeral=True)


class AccountProtectionAlertsModal(nextcord.ui.Modal):
    def __init__(self, cog, settings) -> None:
        super().__init__("Account Protection Alerts")
        self.cog = cog
        self.channel_id = nextcord.ui.TextInput(
            "Alert channel ID (empty = log/system)",
            required=False,
            default_value=str(settings["account_protection_alert_channel_id"] or ""),
            max_length=32,
        )
        self.role_id = nextcord.ui.TextInput(
            "Role ID to ping (optional)",
            required=False,
            default_value=str(settings["account_protection_alert_role_id"] or ""),
            max_length=32,
        )
        self.user_id = nextcord.ui.TextInput(
            "Admin user ID: channel ping + DM",
            required=False,
            default_value=str(settings["account_protection_alert_user_id"] or ""),
            max_length=32,
        )
        for item in (self.channel_id, self.role_id, self.user_id):
            self.add_item(item)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_moderator(interaction):
            return
        try:
            raw_channel = str(self.channel_id.value or "").strip()
            raw_role = str(self.role_id.value or "").strip()
            raw_user = str(self.user_id.value or "").strip()
            channel_id = int(raw_channel) if raw_channel else None
            role_id = int(raw_role) if raw_role else None
            user_id = int(raw_user) if raw_user else None

            if channel_id is not None:
                channel = interaction.guild.get_channel(channel_id)
                if channel is None or not hasattr(channel, "send"):
                    raise ValueError("The alert channel was not found or cannot receive messages.")
            if role_id is not None and interaction.guild.get_role(role_id) is None:
                raise ValueError("The alert role was not found on this server.")
            if user_id is not None:
                member = interaction.guild.get_member(user_id)
                if member is None:
                    try:
                        member = await interaction.guild.fetch_member(user_id)
                    except (nextcord.NotFound, nextcord.Forbidden, nextcord.HTTPException):
                        raise ValueError("The alert user was not found on this server.") from None

            await self.cog.service.update_account_protection_alerts(
                interaction.guild_id, channel_id, role_id, user_id, interaction.user.id
            )
        except (ValueError, TypeError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message("Account Protection alert settings saved.", ephemeral=True)


class BackToAdminPanelButton(nextcord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Back", style=nextcord.ButtonStyle.secondary, custom_id="moderation:admin_back", row=4)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_moderator(interaction):
            return
        from cogs.admin_panel.views import AdminPanelView
        from core.embeds import admin_panel_embed
        await interaction.response.edit_message(content=None, embed=admin_panel_embed(), view=AdminPanelView(interaction.client))


class ModerationPanelView(nextcord.ui.View):
    def __init__(self, cog, show_admin_back: bool = False) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        if show_admin_back:
            self.add_item(BackToAdminPanelButton())

    async def _open_action(self, interaction, action) -> None:
        if not await ensure_moderator(interaction):
            return
        settings = await self.cog.service.get_settings(interaction.guild_id)
        await interaction.response.send_modal(
            ModerationActionModal(self.cog, action, settings["default_duration"], bool(settings["notify_by_default"]))
        )

    async def _open_extended(self, interaction, action) -> None:
        if not await ensure_moderator(interaction): return
        settings = await self.cog.service.get_settings(interaction.guild_id)
        await interaction.response.send_modal(ExtendedActionModal(self.cog, action, bool(settings["notify_by_default"])))

    @nextcord.ui.button(label="Mute", style=nextcord.ButtonStyle.danger, custom_id="moderation:mute", row=0)
    async def mute(self, button, interaction) -> None:
        await self._open_action(interaction, "mute")

    @nextcord.ui.button(label="Edit Mute", style=nextcord.ButtonStyle.primary, custom_id="moderation:edit", row=0)
    async def edit(self, button, interaction) -> None:
        await self._open_action(interaction, "edit")

    @nextcord.ui.button(label="Unmute", style=nextcord.ButtonStyle.success, custom_id="moderation:unmute", row=0)
    async def unmute(self, button, interaction) -> None:
        await self._open_action(interaction, "unmute")

    @nextcord.ui.button(label="Warn", style=nextcord.ButtonStyle.danger, custom_id="moderation:warn", row=0)
    async def warn(self, button, interaction) -> None: await self._open_extended(interaction, "warn")

    @nextcord.ui.button(label="Kick", style=nextcord.ButtonStyle.danger, custom_id="moderation:kick", row=1)
    async def kick(self, button, interaction) -> None: await self._open_extended(interaction, "kick")

    @nextcord.ui.button(label="Ban", style=nextcord.ButtonStyle.danger, custom_id="moderation:ban", row=1)
    async def ban(self, button, interaction) -> None: await self._open_extended(interaction, "ban")

    @nextcord.ui.button(label="Tempban", style=nextcord.ButtonStyle.danger, custom_id="moderation:tempban", row=1)
    async def tempban(self, button, interaction) -> None: await self._open_extended(interaction, "tempban")

    @nextcord.ui.button(label="Active Mutes", style=nextcord.ButtonStyle.secondary, custom_id="moderation:active", row=2)
    async def active(self, button, interaction) -> None:
        if await ensure_moderator(interaction):
            await interaction.response.send_message(embed=await self.cog.service.build_active_embed(interaction.guild), ephemeral=True)

    @nextcord.ui.button(label="History", style=nextcord.ButtonStyle.secondary, custom_id="moderation:history", row=2)
    async def history(self, button, interaction) -> None:
        if await ensure_moderator(interaction):
            await interaction.response.send_message(embed=await self.cog.service.build_history_embed(interaction.guild_id), ephemeral=True)

    @nextcord.ui.button(label="Find Case", style=nextcord.ButtonStyle.secondary, custom_id="moderation:case", row=2)
    async def find_case(self, button, interaction) -> None:
        if await ensure_moderator(interaction): await interaction.response.send_modal(CaseLookupModal(self.cog))

    @nextcord.ui.button(label="Settings", style=nextcord.ButtonStyle.secondary, custom_id="moderation:settings", row=2)
    async def settings(self, button, interaction) -> None:
        if not await ensure_moderator(interaction):
            return
        settings = await self.cog.service.get_settings(interaction.guild_id)
        await interaction.response.send_modal(ModerationSettingsModal(self.cog, settings))

    @nextcord.ui.button(label="Warning Automation", style=nextcord.ButtonStyle.secondary, custom_id="moderation:warn_auto", row=2)
    async def warn_auto(self, button, interaction) -> None:
        if not await ensure_moderator(interaction): return
        settings = await self.cog.service.get_settings(interaction.guild_id)
        await interaction.response.send_modal(WarningAutomationModal(self.cog, settings))

    @nextcord.ui.button(label="Enable / Disable", style=nextcord.ButtonStyle.secondary, custom_id="moderation:toggle", row=3)
    async def toggle(self, button, interaction) -> None:
        if not await ensure_moderator(interaction):
            return
        enabled = not await self.cog.service.is_enabled(interaction.guild_id)
        await self.cog.service.set_enabled(interaction.guild_id, enabled, interaction.user.id)
        await interaction.response.edit_message(
            embed=await self.cog.service.build_panel_embed(interaction.guild),
            view=ModerationPanelView(self.cog, show_admin_back=any(getattr(item, "custom_id", "") == "moderation:admin_back" for item in self.children)),
        )

    @nextcord.ui.button(label="Account Protection", style=nextcord.ButtonStyle.primary, custom_id="moderation:account_protection", row=3)
    async def account_protection(self, button, interaction) -> None:
        if not await ensure_moderator(interaction): return
        settings = await self.cog.service.get_settings(interaction.guild_id)
        await interaction.response.send_modal(AccountProtectionModal(self.cog, settings))

    @nextcord.ui.button(label="Protection Timeout", style=nextcord.ButtonStyle.secondary, custom_id="moderation:protection_timeout", row=3)
    async def protection_timeout(self, button, interaction) -> None:
        if not await ensure_moderator(interaction): return
        settings = await self.cog.service.get_settings(interaction.guild_id)
        await interaction.response.send_modal(AccountProtectionTimeoutModal(self.cog, settings))

    @nextcord.ui.button(label="Protection Alerts", style=nextcord.ButtonStyle.secondary, custom_id="moderation:protection_alerts", row=3)
    async def protection_alerts(self, button, interaction) -> None:
        if not await ensure_moderator(interaction): return
        settings = await self.cog.service.get_settings(interaction.guild_id)
        await interaction.response.send_modal(AccountProtectionAlertsModal(self.cog, settings))
