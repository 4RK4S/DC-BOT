from datetime import datetime, timezone

import nextcord
from nextcord.ext import commands

from core.embeds import DEFAULT_COLOR, admin_panel_embed, info_embed
from core.permissions import can_manage_guild
from core.utils import chunked


GREEN_MODULES = {
    "Autopublish",
    "Live Codes",
    "Messages",
    "Requests",
    "Roles",
    "Server Boost",
    "Status",
    "Welcome",
}

BLUE_MODULES = {
    "Creator Codes",
    "Forwarder",
    "Listener",
}

GRAY_MODULES = {
    "Custom VC",
    "Giveaway",
    "Leveling",
    "Report/Idea",
    "Slowmode",
    "Supporters",
}

RED_MODULES = {
    "Anti-Nuke",
    "Moderation",
}

SPECIAL_LAST_MODULES = {"Settings"}

IMPLEMENTED_MODULES = GREEN_MODULES | BLUE_MODULES | SPECIAL_LAST_MODULES | {
    "Autopublish",
    "Creator Codes",
    "Forwarder",
    "Listener",
    "Live Codes",
    "Messages",
    "Moderation",
    "Requests",
    "Roles",
    "Server Boost",
    "Welcome",
    "Settings",
}


def module_key(module_name: str) -> str:
    return module_name.lower().replace(" ", "_").replace("/", "_").replace("-", "_")


def get_admin_panel_modules_ordered() -> list[str]:
    ordered: list[str] = []
    ordered.extend(sorted(GREEN_MODULES))
    ordered.extend(sorted(BLUE_MODULES))
    ordered.extend(sorted(GRAY_MODULES))
    ordered.extend(sorted(RED_MODULES))
    ordered.append("Settings")
    return ordered


def get_admin_module_style(module_name: str) -> nextcord.ButtonStyle:
    if module_name in GREEN_MODULES:
        return nextcord.ButtonStyle.success
    if module_name in BLUE_MODULES:
        return nextcord.ButtonStyle.primary
    if module_name in RED_MODULES:
        return nextcord.ButtonStyle.danger
    return nextcord.ButtonStyle.secondary


def format_loaded_cog_name(path: str) -> str:
    parts = path.split(".")
    if len(parts) >= 3 and parts[0] == "cogs":
        return parts[1]
    return path


def apply_button_order(view: nextcord.ui.View, custom_ids: list[str]) -> None:
    order = {custom_id: index for index, custom_id in enumerate(custom_ids)}
    view.children.sort(key=lambda child: order.get(getattr(child, "custom_id", ""), len(order)))
    for index, child in enumerate(view.children):
        child.row = min(index // 5, 4)


async def ensure_panel_access(interaction: nextcord.Interaction) -> bool:
    if not isinstance(interaction.user, nextcord.Member):
        await interaction.response.send_message("This panel can only be used in a server.", ephemeral=True)
        return False

    if not can_manage_guild(interaction.user):
        await interaction.response.send_message(
            "You need Administrator or Manage Server permissions to use this panel.",
            ephemeral=True,
        )
        return False

    return True


def build_module_embed(module_name: str, enabled: bool) -> nextcord.Embed:
    status = "Enabled" if enabled else "Disabled"
    embed = info_embed(module_name, f"Status: **{status}**")
    embed.add_field(name="Controls", value="Use the buttons below to enable or disable this module.", inline=False)
    return embed


def build_autopublish_embed(enabled: bool, cache_size: int | None = None) -> nextcord.Embed:
    status = "Enabled" if enabled else "Disabled"
    embed = info_embed("Autopublish", f"Status: **{status}**")
    embed.add_field(
        name="Behavior",
        value="Publishes messages posted in announcement/news channels when enabled.",
        inline=False,
    )
    if cache_size is not None:
        embed.add_field(name="Tracked Message Cache", value=str(cache_size), inline=True)
    return embed


async def build_autopublish_settings_embed(bot, guild_id: int) -> nextcord.Embed:
    autopublish_cog = bot.get_cog("AutopublishCog")
    enabled = await autopublish_cog.is_enabled(guild_id) if autopublish_cog is not None else True
    cache_size = len(autopublish_cog._seen_message_ids) if autopublish_cog is not None else 0
    row = await bot.db.get_module_settings(guild_id, "autopublish")
    embed = nextcord.Embed(title="Autopublish Settings", color=DEFAULT_COLOR)
    embed.add_field(name="Enabled", value=str(enabled), inline=True)
    embed.add_field(name="Tracked Message Cache Size", value=str(cache_size), inline=True)
    embed.add_field(name="Supports Own Bot Messages", value="Yes", inline=True)
    embed.add_field(name="Ignores Other Bots", value="Yes", inline=True)
    embed.add_field(name="Required Channel Type", value="Announcement / News", inline=True)
    embed.add_field(name="Announcement Channels", value="Detected from Discord news channel type at runtime.", inline=False)
    embed.add_field(name="Updated At", value=row["updated_at"] if row is not None else "Not stored yet", inline=True)
    return embed


class ModuleButton(nextcord.ui.Button):
    def __init__(self, module_name: str, row: int) -> None:
        self.module_name = module_name
        super().__init__(
            label=module_name,
            style=get_admin_module_style(module_name),
            custom_id=f"admin_panel:module:{module_key(module_name)}",
            row=row,
        )

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_panel_access(interaction):
            return

        bot = interaction.client
        if self.module_name == "Creator Codes":
            creator_codes_cog = bot.get_cog("CreatorCodesCog")
            if creator_codes_cog is not None:
                await interaction.response.edit_message(
                    content=None,
                    embed=creator_codes_cog.service.build_admin_embed(),
                    view=creator_codes_cog.create_admin_view(show_admin_back=True),
                )
                return
        if self.module_name == "Live Codes":
            live_codes_cog = bot.get_cog("LiveCodesCog")
            if live_codes_cog is not None:
                await interaction.response.edit_message(
                    content=None,
                    embed=live_codes_cog.service.build_management_embed(),
                    view=live_codes_cog.create_management_view(show_admin_back=True),
                )
                return
        if self.module_name == "Autopublish":
            autopublish_cog = bot.get_cog("AutopublishCog")
            if autopublish_cog is not None:
                enabled = await autopublish_cog.is_enabled(interaction.guild_id)
                await interaction.response.edit_message(
                    content=None,
                    embed=build_autopublish_embed(enabled, len(autopublish_cog._seen_message_ids)),
                    view=AutopublishPanelView(bot, enabled),
                )
                return
        if self.module_name == "Status":
            status_cog = bot.get_cog("StatusCog")
            if status_cog is not None:
                await interaction.response.edit_message(
                    content=None,
                    embed=status_cog.build_panel_embed(),
                    view=status_cog.create_panel_view(),
                )
                return
        if self.module_name == "Settings":
            await interaction.response.edit_message(
                content=None,
                embed=await build_settings_overview_embed(bot, interaction),
                view=SettingsOverviewView(bot),
            )
            return
        if self.module_name == "Messages":
            messages_cog = bot.get_cog("MessagesCog")
            if messages_cog is not None:
                await interaction.response.edit_message(
                    content=None,
                    embed=messages_cog.service.build_management_embed(),
                    view=messages_cog.create_management_view(show_admin_back=True),
                )
                return
        if self.module_name == "Moderation":
            moderation_cog = bot.get_cog("ModerationCog")
            if moderation_cog is not None:
                await interaction.response.edit_message(
                    content=None,
                    embed=await moderation_cog.service.build_panel_embed(interaction.guild),
                    view=moderation_cog.create_panel_view(show_admin_back=True),
                )
                return
        if self.module_name == "Forwarder":
            forwarder_cog = bot.get_cog("ForwarderCog")
            if forwarder_cog is not None:
                await interaction.response.edit_message(
                    content=None,
                    embed=forwarder_cog.service.build_management_embed(),
                    view=forwarder_cog.create_management_view(show_admin_back=True),
                )
                return
        if self.module_name == "Listener":
            listener_cog = bot.get_cog("ListenerCog")
            if listener_cog is not None:
                await interaction.response.edit_message(
                    content=None,
                    embed=listener_cog.service.build_management_embed(),
                    view=listener_cog.create_management_view(show_admin_back=True),
                )
                return
        if self.module_name == "Welcome":
            welcome_cog = bot.get_cog("WelcomeCog")
            if welcome_cog is not None:
                await interaction.response.edit_message(
                    content=None,
                    embed=welcome_cog.service.build_management_embed(),
                    view=welcome_cog.create_management_view(show_admin_back=True),
                )
                return
        if self.module_name == "Roles":
            roles_cog = bot.get_cog("RolesCog")
            if roles_cog is not None:
                await interaction.response.edit_message(
                    content=None,
                    embed=roles_cog.service.build_admin_embed(),
                    view=roles_cog.create_admin_view(show_admin_back=True),
                )
                return
        if self.module_name == "Requests":
            requests_cog = bot.get_cog("RequestsCog")
            if requests_cog is not None:
                await interaction.response.edit_message(
                    content=None,
                    embed=requests_cog.service.build_admin_embed(),
                    view=requests_cog.create_admin_view(show_admin_back=True),
                )
                return
        if self.module_name == "Server Boost":
            server_boost_cog = bot.get_cog("ServerBoostCog")
            if server_boost_cog is not None:
                await interaction.response.edit_message(
                    content=None,
                    embed=server_boost_cog.service.build_panel_embed(),
                    view=server_boost_cog.create_panel_view(show_admin_back=True),
                )
                return

        settings = await bot.db.get_module_settings(interaction.guild_id, self.module_name)
        enabled = bool(settings["enabled"]) if settings is not None else True

        await interaction.response.edit_message(
            content=None,
            embed=build_module_embed(self.module_name, enabled),
            view=ModulePanelView(bot, self.module_name, enabled),
        )


class AdminPanelView(nextcord.ui.View):
    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        for row, modules in enumerate(chunked(get_admin_panel_modules_ordered(), 5)):
            for module_name in modules:
                self.add_item(ModuleButton(module_name, row=row))


class ToggleModuleButton(nextcord.ui.Button):
    def __init__(self, module_name: str, enabled: bool) -> None:
        self.module_name = module_name
        self.enabled = enabled
        label = "Disable" if enabled else "Enable"
        style = nextcord.ButtonStyle.danger if enabled else nextcord.ButtonStyle.success
        super().__init__(
            label=label,
            style=style,
            custom_id=f"admin_panel:toggle:{module_key(module_name)}",
            row=0,
        )

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_panel_access(interaction):
            return

        bot = interaction.client
        new_enabled = not self.enabled
        await bot.db.upsert_module_settings(
            interaction.guild_id,
            self.module_name,
            enabled=new_enabled,
            settings={},
        )

        await interaction.response.edit_message(
            content=None,
            embed=build_module_embed(self.module_name, new_enabled),
            view=ModulePanelView(bot, self.module_name, new_enabled),
        )


class BackButton(nextcord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Back",
            style=nextcord.ButtonStyle.secondary,
            custom_id="admin_panel:back",
            row=0,
        )

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_panel_access(interaction):
            return

        await interaction.response.edit_message(
            content=None,
            embed=admin_panel_embed(),
            view=AdminPanelView(interaction.client),
        )


class ModulePanelView(nextcord.ui.View):
    def __init__(self, bot: commands.Bot, module_name: str, enabled: bool) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.module_name = module_name
        self.enabled = enabled
        self.add_item(ToggleModuleButton(module_name, enabled))
        self.add_item(BackButton())


async def build_settings_overview_embed(bot: commands.Bot, interaction: nextcord.Interaction) -> nextcord.Embed:
    now = datetime.now(timezone.utc)
    started_at = getattr(bot, "started_at", now)
    uptime_seconds = int((now - started_at).total_seconds())
    hours, rem = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    persistent = await bot.db.fetchone("SELECT COUNT(*) AS count FROM persistent_views")
    module_rows = await bot.db.fetchall("SELECT module_name, enabled FROM module_settings WHERE guild_id = ? ORDER BY module_name", (interaction.guild_id,))
    status_cog = bot.get_cog("StatusCog")
    status_settings = None
    if status_cog is not None and interaction.guild_id is not None:
        status_settings, _ = await status_cog.get_guild_status_settings(interaction.guild_id)

    embed = nextcord.Embed(title="Settings Overview", color=DEFAULT_COLOR)
    embed.add_field(name="Bot Status", value="Running", inline=True)
    embed.add_field(name="Bot User", value=f"{bot.user} (`{bot.user.id}`)" if bot.user else "Not connected", inline=False)
    embed.add_field(name="Guild ID", value=str(interaction.guild_id or "Unknown"), inline=True)
    embed.add_field(name="Uptime", value=f"{hours}h {minutes}m {seconds}s", inline=True)
    embed.add_field(name="Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
    loaded_cogs = getattr(bot, "loaded_cogs", [])
    embed.add_field(name="Loaded Cogs", value=f"{len(loaded_cogs)} loaded", inline=True)
    embed.add_field(name="Loaded Modules", value=", ".join(format_loaded_cog_name(name) for name in loaded_cogs)[:1000] or "None", inline=False)
    state_by_module = {row["module_name"]: bool(row["enabled"]) for row in module_rows}
    module_lines = []
    for module_name in get_admin_panel_modules_ordered():
        if module_name in SPECIAL_LAST_MODULES:
            continue
        if module_name in state_by_module:
            state = "Enabled" if state_by_module[module_name] else "Disabled"
        else:
            state = "Default: Enabled"
        module_lines.append(f"`{module_name}`: {state}")
    embed.add_field(name="Module States", value="\n".join(module_lines)[:1000], inline=False)
    embed.add_field(name="Database Path", value=str(bot.config.database_path), inline=False)
    embed.add_field(name="Command Registration", value=bot.config.slash_registration_mode, inline=True)
    embed.add_field(name="Persistent Views Saved", value=str(persistent["count"] if persistent else 0), inline=True)
    if status_settings:
        embed.add_field(name="Presence Status", value=status_settings["status"], inline=True)
        embed.add_field(name="Activity Type", value=status_settings["activity_type"], inline=True)
        embed.add_field(name="Activity Text", value=status_settings["activity_text"], inline=False)
    return embed


async def build_module_states_embed(bot: commands.Bot, guild_id: int) -> nextcord.Embed:
    rows = await bot.db.fetchall("SELECT module_name, enabled, updated_at FROM module_settings WHERE guild_id = ? ORDER BY module_name", (guild_id,))
    embed = nextcord.Embed(title="Module States", color=DEFAULT_COLOR)
    state_by_module = {row["module_name"]: bool(row["enabled"]) for row in rows}
    lines = []
    for module_name in get_admin_panel_modules_ordered():
        if module_name in SPECIAL_LAST_MODULES:
            continue
        if module_name in state_by_module:
            state = "Enabled" if state_by_module[module_name] else "Disabled"
        else:
            state = "Default: Enabled"
        lines.append(f"`{module_name}`: {state}")
    embed.description = "\n".join(lines)[:4000]
    return embed


def build_command_info_embed(bot: commands.Bot) -> nextcord.Embed:
    names = get_application_command_names(bot)
    duplicates = sorted({name for name in names if names.count(name) > 1})
    embed = nextcord.Embed(title="Command Info", color=DEFAULT_COLOR)
    embed.add_field(name="Registration Mode", value=bot.config.slash_registration_mode, inline=True)
    embed.add_field(name="Local Command Count", value=str(len(names)), inline=True)
    embed.add_field(name="Duplicate Names", value=", ".join(f"/{name}" for name in duplicates) if duplicates else "None", inline=False)
    embed.add_field(name="Commands", value=", ".join(f"/{name}" for name in sorted(names))[:1000] or "None", inline=False)
    return embed


def get_application_command_names(bot: commands.Bot) -> list[str]:
    getter = getattr(bot, "get_all_application_commands", None)
    if not callable(getter):
        getter = getattr(bot, "get_application_commands", None)
    commands_iterable = getter() if callable(getter) else getattr(bot, "application_commands", [])
    names: list[str] = []
    for command in commands_iterable or []:
        name = command if isinstance(command, str) else getattr(command, "name", None)
        if name:
            names.append(str(name))
    return names


class SettingsOverviewView(nextcord.ui.View):
    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.add_item(BackButton())
        apply_button_order(
            self,
            [
                "settings:refresh_overview",
                "settings:module_states",
                "settings:command_info",
                "admin_panel:back",
            ],
        )

    @nextcord.ui.button(label="Refresh Overview", style=nextcord.ButtonStyle.success, custom_id="settings:refresh_overview", row=0)
    async def refresh_overview(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_panel_access(interaction):
            await interaction.response.edit_message(embed=await build_settings_overview_embed(self.bot, interaction), view=SettingsOverviewView(self.bot))

    @nextcord.ui.button(label="Show Module States", style=nextcord.ButtonStyle.secondary, custom_id="settings:module_states", row=0)
    async def show_module_states(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_panel_access(interaction):
            await interaction.response.send_message(embed=await build_module_states_embed(self.bot, interaction.guild_id), ephemeral=True)

    @nextcord.ui.button(label="Show Command Info", style=nextcord.ButtonStyle.secondary, custom_id="settings:command_info", row=0)
    async def show_command_info(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_panel_access(interaction):
            await interaction.response.send_message(embed=build_command_info_embed(self.bot), ephemeral=True)


class AutopublishSetButton(nextcord.ui.Button):
    def __init__(self, target_enabled: bool, current_enabled: bool) -> None:
        self.target_enabled = target_enabled
        super().__init__(
            label="Enable" if target_enabled else "Disable",
            style=nextcord.ButtonStyle.success if target_enabled else nextcord.ButtonStyle.danger,
            custom_id=f"admin_panel:autopublish:{'enable' if target_enabled else 'disable'}",
            disabled=target_enabled == current_enabled,
            row=0,
        )

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_panel_access(interaction):
            return

        autopublish_cog = interaction.client.get_cog("AutopublishCog")
        if autopublish_cog is None:
            await interaction.response.send_message("Autopublish module is not loaded.", ephemeral=True)
            return

        await autopublish_cog.set_enabled(interaction.guild_id, self.target_enabled, interaction.user.id)
        await interaction.response.edit_message(
            content=None,
            embed=build_autopublish_embed(self.target_enabled, len(autopublish_cog._seen_message_ids)),
            view=AutopublishPanelView(interaction.client, self.target_enabled),
        )


class AutopublishSettingsButton(nextcord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Show Settings",
            style=nextcord.ButtonStyle.secondary,
            custom_id="admin_panel:autopublish:settings",
            row=0,
        )

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_panel_access(interaction):
            return
        await interaction.response.send_message(
            embed=await build_autopublish_settings_embed(interaction.client, interaction.guild_id),
            ephemeral=True,
        )


class AutopublishPanelView(nextcord.ui.View):
    def __init__(self, bot: commands.Bot, enabled: bool) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.enabled = enabled
        self.add_item(AutopublishSetButton(True, enabled))
        self.add_item(AutopublishSetButton(False, enabled))
        self.add_item(AutopublishSettingsButton())
        self.add_item(BackButton())
        apply_button_order(
            self,
            [
                "admin_panel:autopublish:enable",
                "admin_panel:autopublish:disable",
                "admin_panel:autopublish:settings",
                "admin_panel:back",
            ],
        )
