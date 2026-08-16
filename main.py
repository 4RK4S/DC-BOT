import asyncio
import json
import logging
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import nextcord
from dotenv import load_dotenv
from nextcord.ext import commands

from core.config import Config, get_development_guild_ids
from core.database import Database
from core.logging_setup import setup_logging


COGS = (
    "cogs.admin_panel.cog",
    "cogs.status.cog",
    "cogs.autopublish.cog",
    "cogs.creator_codes.cog",
    "cogs.live_codes.cog",
    "cogs.messages.cog",
    "cogs.clear.cog",
    "cogs.moderation.cog",
    "cogs.forwarder.cog",
    "cogs.listener.cog",
    "cogs.welcome.cog",
    "cogs.roles.cog",
    "cogs.requests.cog",
    "cogs.server_boost.cog",
)


def build_intents() -> nextcord.Intents:
    intents = nextcord.Intents.default()
    intents.guilds = True
    intents.members = True
    intents.messages = True
    intents.message_content = True
    intents.reactions = True
    return intents


async def create_bot() -> commands.Bot:
    load_dotenv()
    setup_logging()
    logger = logging.getLogger(__name__)

    config = Config.from_env()
    bot_kwargs = {}
    if config.slash_guild_ids is not None:
        bot_kwargs["default_guild_ids"] = config.slash_guild_ids

    bot = commands.Bot(
        command_prefix=commands.when_mentioned_or("!"),
        intents=build_intents(),
        **bot_kwargs,
    )
    bot.config = config
    bot.db = Database(config.database_path)
    bot.loaded_cogs = []
    bot.started_at = datetime.now(timezone.utc)

    logger.info("Slash command guild IDs loaded: %s", config.slash_command_guild_ids or "none")
    logger.info("Using global slash command registration: %s", config.uses_global_slash_commands)
    logger.info("Slash command registration mode: %s", config.slash_registration_mode)

    @bot.listen()
    async def on_ready() -> None:
        logging.getLogger(__name__).info(
            "Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown"
        )

    @bot.slash_command(
        name="sync-commands",
        description="Manually sync slash commands.",
        guild_ids=get_development_guild_ids(),
    )
    async def sync_commands(interaction: nextcord.Interaction) -> None:
        if not await can_sync_commands(bot, interaction):
            await interaction.response.send_message(
                "You need to be the bot owner or a server administrator to use this.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        target_guild_ids = bot.config.slash_guild_ids
        await sync_application_commands(bot, target_guild_ids)
        scope = (
            "guilds " + ", ".join(f"`{guild_id}`" for guild_id in target_guild_ids)
            if target_guild_ids
            else "global commands"
        )
        await interaction.followup.send(f"Slash commands synced for {scope}.", ephemeral=True)

    for cog in COGS:
        logger.info("Loading cog: %s", cog)
        bot.load_extension(cog)
        bot.loaded_cogs.append(cog)

    logger.info("Loaded cogs: %s", ", ".join(bot.loaded_cogs))
    log_registered_application_commands(bot, logger)

    return bot


def log_registered_application_commands(bot: commands.Bot, logger: logging.Logger) -> None:
    command_names = get_registered_application_command_names(bot)
    logger.info("Slash command registration mode: %s", bot.config.slash_registration_mode)
    logger.info("Loaded cogs: %s", ", ".join(getattr(bot, "loaded_cogs", [])) or "none")
    logger.info("Local slash commands registered: %s", ", ".join(f"/{name}" for name in command_names) or "none")

    seen: set[str] = set()
    duplicates: set[str] = set()
    for name in command_names:
        if name in seen:
            duplicates.add(name)
        seen.add(name)
    for name in sorted(duplicates):
        logger.warning("Duplicate slash command registered: /%s", name)


def get_registered_application_command_names(bot: commands.Bot) -> list[str]:
    getter = getattr(bot, "get_all_application_commands", None)
    if not callable(getter):
        getter = getattr(bot, "get_application_commands", None)
    if callable(getter):
        commands_iterable = getter()
    else:
        commands_iterable = getattr(bot, "application_commands", [])
    names: list[str] = []
    for command in commands_iterable or []:
        name = command if isinstance(command, str) else getattr(command, "name", None)
        if name:
            names.append(str(name))
    return names


async def can_sync_commands(bot: commands.Bot, interaction: nextcord.Interaction) -> bool:
    if await bot.is_owner(interaction.user):
        return True

    if isinstance(interaction.user, nextcord.Member):
        return bool(interaction.user.guild_permissions.administrator)

    return False


async def sync_application_commands(bot: commands.Bot, guild_ids: list[int] | None) -> None:
    logger = logging.getLogger(__name__)
    if hasattr(bot, "sync_application_commands"):
        if guild_ids:
            for guild_id in guild_ids:
                try:
                    await bot.sync_application_commands(guild_id=guild_id)
                except TypeError:
                    await bot.sync_application_commands()
        else:
            try:
                await bot.sync_application_commands()
            except TypeError:
                await bot.sync_application_commands()
    elif hasattr(bot, "sync_all_application_commands"):
        await bot.sync_all_application_commands()
    else:
        raise RuntimeError("This nextcord version does not expose an application command sync method")

    logger.info("Slash commands synced for %s", f"guilds {guild_ids}" if guild_ids else "global scope")


async def maybe_clear_guild_commands_on_startup(bot: commands.Bot) -> None:
    logger = logging.getLogger(__name__)
    if not bot.config.clear_guild_commands_on_startup:
        return

    logger.warning("Clearing guild commands on startup is enabled")
    guild_ids = bot.config.guild_command_cleanup_ids
    if not guild_ids:
        logger.warning("CLEAR_GUILD_COMMANDS_ON_STARTUP=true but no guild IDs are configured; skipping cleanup")
        return

    try:
        application = await asyncio.to_thread(discord_api_request, "GET", "/oauth2/applications/@me", bot.config.discord_token)
    except RuntimeError as exc:
        logger.warning("Could not fetch Discord application for guild command cleanup: %s", exc)
        return

    application_id = application["id"]
    for guild_id in guild_ids:
        try:
            await asyncio.to_thread(
                discord_api_request,
                "PUT",
                f"/applications/{application_id}/guilds/{guild_id}/commands",
                bot.config.discord_token,
                [],
            )
        except RuntimeError as exc:
            logger.warning("Failed to clear guild commands for guild %s: %s", guild_id, exc)
            continue
        logger.warning("Cleared guild commands for guild %s", guild_id)


def discord_api_request(method: str, path: str, token: str, payload: object | None = None) -> object:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        f"https://discord.com/api/v10{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "DC_bot_new startup command cleanup",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{exc.code} {details}") from exc
    except URLError as exc:
        raise RuntimeError(str(exc)) from exc
    return json.loads(body) if body else None


async def main() -> None:
    bot = await create_bot()
    logger = logging.getLogger(__name__)

    if not bot.config.discord_token:
        raise RuntimeError("DISCORD_TOKEN is missing from .env")

    try:
        await bot.db.connect()
        await bot.db.init_schema()
        logger.info("Database initialized at %s", bot.config.database_path)
        await maybe_clear_guild_commands_on_startup(bot)
        await bot.start(bot.config.discord_token)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Shutdown requested.")
    finally:
        await bot.close()
        await bot.db.close()
        logger.info("Bot shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
