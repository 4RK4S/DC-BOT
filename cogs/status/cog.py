import json
import logging

import nextcord
from nextcord.ext import commands

from core.embeds import DEFAULT_COLOR
from core.permissions import can_manage_guild


MODULE_NAME = "status"
DEFAULT_STATUS = "dnd"
DEFAULT_ACTIVITY_TYPE = "playing"
DEFAULT_ACTIVITY_TEXT = "Solo Leveling:ARISE"
VALID_STATUSES = ("online", "idle", "dnd", "invisible")
VALID_ACTIVITY_TYPES = ("playing", "watching", "listening", "competing")


class StatusCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.logger = logging.getLogger(__name__)
        self._presence_applied = False

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._presence_applied:
            return

        await self.apply_saved_status()
        self._presence_applied = True

    async def apply_saved_status(self) -> None:
        settings = await self.get_saved_status_settings()
        await self.apply_presence(settings)

    async def get_saved_status_settings(self) -> dict[str, str]:
        guild_id = self.bot.config.creator_bot_guild_id
        if guild_id is not None:
            row = await self.bot.db.get_module_settings(guild_id, MODULE_NAME)
        else:
            row = await self.bot.db.fetchone(
                """
                SELECT settings_json
                FROM module_settings
                WHERE module_name = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (MODULE_NAME,),
            )

        if row is None:
            return self.default_settings()

        try:
            loaded = json.loads(row["settings_json"])
        except (TypeError, json.JSONDecodeError):
            self.logger.warning("Invalid saved status settings for guild %s", guild_id)
            return self.default_settings()

        return {
            "status": self.clean_status(loaded.get("status")),
            "activity_type": self.clean_activity_type(loaded.get("activity_type")),
            "activity_text": str(loaded.get("activity_text") or DEFAULT_ACTIVITY_TEXT),
        }

    async def get_guild_status_settings(self, guild_id: int) -> tuple[dict[str, str], object | None]:
        row = await self.bot.db.get_module_settings(guild_id, MODULE_NAME)
        if row is None:
            return self.default_settings(), None
        try:
            loaded = json.loads(row["settings_json"])
        except (TypeError, json.JSONDecodeError):
            loaded = {}
        return {
            "status": self.clean_status(loaded.get("status")),
            "activity_type": self.clean_activity_type(loaded.get("activity_type")),
            "activity_text": str(loaded.get("activity_text") or DEFAULT_ACTIVITY_TEXT),
        }, row

    async def update_guild_status_settings(self, guild_id: int, user_id: int | None = None, **values: str) -> dict[str, str]:
        settings, _ = await self.get_guild_status_settings(guild_id)
        if "status" in values:
            settings["status"] = self.clean_status(values["status"])
        if "activity_type" in values:
            settings["activity_type"] = self.clean_activity_type(values["activity_type"])
        if "activity_text" in values:
            settings["activity_text"] = str(values["activity_text"] or DEFAULT_ACTIVITY_TEXT)[:128]
        await self.bot.db.upsert_module_settings(guild_id, MODULE_NAME, enabled=True, settings=settings)
        await self.bot.db.log_action(guild_id, MODULE_NAME, "status_settings_updated", user_id=user_id, details=settings)
        return settings

    async def apply_presence(self, settings: dict[str, str]) -> None:
        status = self.build_status(settings["status"])
        activity = self.build_activity(settings["activity_type"], settings["activity_text"])
        await self.bot.change_presence(status=status, activity=activity)
        self._presence_applied = True
        self.logger.info(
            "Presence set to %s: %s %s",
            settings["status"],
            settings["activity_type"],
            settings["activity_text"],
        )

    def default_settings(self) -> dict[str, str]:
        return {
            "status": DEFAULT_STATUS,
            "activity_type": DEFAULT_ACTIVITY_TYPE,
            "activity_text": DEFAULT_ACTIVITY_TEXT,
        }

    def clean_status(self, status: object) -> str:
        value = str(status or "").lower()
        return value if value in VALID_STATUSES else DEFAULT_STATUS

    def clean_activity_type(self, activity_type: object) -> str:
        value = str(activity_type or "").lower()
        return value if value in VALID_ACTIVITY_TYPES else DEFAULT_ACTIVITY_TYPE

    def build_status(self, status: str) -> nextcord.Status:
        if status == "idle":
            return nextcord.Status.idle
        if status == "dnd":
            return nextcord.Status.dnd
        if status == "invisible":
            return nextcord.Status.invisible
        return nextcord.Status.online

    def build_activity(self, activity_type: str, activity_text: str) -> nextcord.BaseActivity:
        if activity_type == "watching":
            return nextcord.Activity(type=nextcord.ActivityType.watching, name=activity_text)
        if activity_type == "listening":
            return nextcord.Activity(type=nextcord.ActivityType.listening, name=activity_text)
        if activity_type == "competing":
            return nextcord.Activity(type=nextcord.ActivityType.competing, name=activity_text)
        return nextcord.Game(name=activity_text)

    def build_panel_embed(self) -> nextcord.Embed:
        embed = nextcord.Embed(
            title="Status",
            description="Manage the bot presence shown in Discord.",
            color=DEFAULT_COLOR,
        )
        embed.add_field(name="Navigation", value="Navigation edits this panel message.", inline=False)
        return embed

    async def build_settings_embed(self, guild_id: int) -> nextcord.Embed:
        settings, row = await self.get_guild_status_settings(guild_id)
        embed = nextcord.Embed(title="Status Settings", color=DEFAULT_COLOR)
        embed.add_field(name="Configured Status", value=settings["status"], inline=True)
        embed.add_field(name="Activity Type", value=settings["activity_type"].title(), inline=True)
        embed.add_field(name="Activity Text", value=settings["activity_text"], inline=False)
        embed.add_field(name="Current Bot Latency", value=f"{round(self.bot.latency * 1000)}ms", inline=True)
        embed.add_field(name="Presence Applied", value=str(self._presence_applied), inline=True)
        embed.add_field(name="Updated At", value=row["updated_at"] if row is not None else "Not stored yet", inline=True)
        return embed

    def create_panel_view(self) -> "StatusPanelView":
        return StatusPanelView(self)


async def ensure_manager(interaction: nextcord.Interaction) -> bool:
    if not isinstance(interaction.user, nextcord.Member):
        await interaction.response.send_message("This panel can only be used in a server.", ephemeral=True)
        return False
    if not can_manage_guild(interaction.user):
        await interaction.response.send_message("You need Administrator or Manage Server permissions to use this.", ephemeral=True)
        return False
    return True


def apply_button_order(view: nextcord.ui.View, custom_ids: list[str]) -> None:
    order = {custom_id: index for index, custom_id in enumerate(custom_ids)}
    view.children.sort(key=lambda child: order.get(getattr(child, "custom_id", ""), len(order)))
    for index, child in enumerate(view.children):
        child.row = min(index // 5, 4)


class BackToAdminPanelButton(nextcord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Back", style=nextcord.ButtonStyle.secondary, custom_id="status:admin_back", row=4)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        from cogs.admin_panel.views import AdminPanelView
        from core.embeds import admin_panel_embed

        await interaction.response.edit_message(content=None, embed=admin_panel_embed(), view=AdminPanelView(interaction.client))


class StatusSelect(nextcord.ui.Select):
    def __init__(self, cog: StatusCog) -> None:
        self.cog = cog
        super().__init__(
            placeholder="Select status",
            options=[nextcord.SelectOption(label=value, value=value) for value in VALID_STATUSES],
            custom_id="status:status_select",
        )

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        settings = await self.cog.update_guild_status_settings(interaction.guild_id, interaction.user.id, status=self.values[0])
        await interaction.response.edit_message(embed=await self.cog.build_settings_embed(interaction.guild_id), view=StatusPanelView(self.cog))
        await interaction.followup.send(f"Status saved as `{settings['status']}`.", ephemeral=True)


class ActivitySelect(nextcord.ui.Select):
    def __init__(self, cog: StatusCog) -> None:
        self.cog = cog
        labels = {
            "playing": "Playing",
            "watching": "Watching",
            "listening": "Listening",
            "competing": "Competing",
        }
        super().__init__(
            placeholder="Select activity type",
            options=[nextcord.SelectOption(label=labels[value], value=value) for value in VALID_ACTIVITY_TYPES],
            custom_id="status:activity_select",
        )

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        settings = await self.cog.update_guild_status_settings(interaction.guild_id, interaction.user.id, activity_type=self.values[0])
        await interaction.response.edit_message(embed=await self.cog.build_settings_embed(interaction.guild_id), view=StatusPanelView(self.cog))
        await interaction.followup.send(f"Activity type saved as `{settings['activity_type']}`.", ephemeral=True)


class StatusSelectView(nextcord.ui.View):
    def __init__(self, cog: StatusCog) -> None:
        super().__init__(timeout=120)
        self.add_item(StatusSelect(cog))


class ActivitySelectView(nextcord.ui.View):
    def __init__(self, cog: StatusCog) -> None:
        super().__init__(timeout=120)
        self.add_item(ActivitySelect(cog))


class ActivityTextModal(nextcord.ui.Modal):
    def __init__(self, cog: StatusCog) -> None:
        super().__init__("Set Activity Text")
        self.cog = cog
        self.activity_text = nextcord.ui.TextInput(
            "Activity Text",
            required=True,
            max_length=128,
            default_value=DEFAULT_ACTIVITY_TEXT,
        )
        self.add_item(self.activity_text)

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        settings = await self.cog.update_guild_status_settings(
            interaction.guild_id,
            interaction.user.id,
            activity_text=str(self.activity_text.value),
        )
        await interaction.response.send_message(f"Activity text saved as `{settings['activity_text']}`.", ephemeral=True)


class StatusPanelView(nextcord.ui.View):
    def __init__(self, cog: StatusCog) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.add_item(BackToAdminPanelButton())
        apply_button_order(
            self,
            [
                "status:set_status",
                "status:set_activity",
                "status:set_activity_text",
                "status:apply",
                "status:settings",
                "status:admin_back",
            ],
        )

    @nextcord.ui.button(label="Set Status", style=nextcord.ButtonStyle.success, custom_id="status:set_status", row=0)
    async def set_status(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_message("Select status.", view=StatusSelectView(self.cog), ephemeral=True)

    @nextcord.ui.button(label="Set Activity", style=nextcord.ButtonStyle.success, custom_id="status:set_activity", row=0)
    async def set_activity(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_message("Select activity type.", view=ActivitySelectView(self.cog), ephemeral=True)

    @nextcord.ui.button(label="Set Activity Text", style=nextcord.ButtonStyle.success, custom_id="status:set_activity_text", row=0)
    async def set_activity_text(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_modal(ActivityTextModal(self.cog))

    @nextcord.ui.button(label="Apply Now", style=nextcord.ButtonStyle.success, custom_id="status:apply", row=0)
    async def apply_now(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if not await ensure_manager(interaction):
            return
        settings, _ = await self.cog.get_guild_status_settings(interaction.guild_id)
        await self.cog.apply_presence(settings)
        await interaction.response.send_message("Presence applied.", ephemeral=True)

    @nextcord.ui.button(label="Show Settings", style=nextcord.ButtonStyle.secondary, custom_id="status:settings", row=0)
    async def show_settings(self, button: nextcord.ui.Button, interaction: nextcord.Interaction) -> None:
        if await ensure_manager(interaction):
            await interaction.response.send_message(embed=await self.cog.build_settings_embed(interaction.guild_id), ephemeral=True)


def setup(bot: commands.Bot) -> None:
    bot.add_cog(StatusCog(bot))
