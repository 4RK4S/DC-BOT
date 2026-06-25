import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    discord_token: str
    slahub_base_url: str
    creator_bot_api_secret: str
    creator_bot_api_host: str
    creator_bot_api_port: int | None
    creator_bot_guild_id: int | None
    creator_bot_guild_ids: list[int]
    slash_command_guild_ids: list[int]
    development_guild_ids: list[int]
    clear_guild_commands_on_startup: bool
    status_text: str
    status_type: str
    database_path: Path

    @property
    def slash_guild_ids(self) -> list[int] | None:
        return self.slash_command_guild_ids or None

    @property
    def guild_command_cleanup_ids(self) -> list[int]:
        ids = self.creator_bot_guild_ids or self.development_guild_ids or self.slash_command_guild_ids
        if self.creator_bot_guild_id is not None:
            ids = [*ids, self.creator_bot_guild_id]
        return list(dict.fromkeys(ids))

    @property
    def slash_registration_mode(self) -> str:
        return "guild" if self.slash_command_guild_ids else "global"

    @property
    def uses_global_slash_commands(self) -> bool:
        return not self.slash_command_guild_ids

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            discord_token=os.getenv("DISCORD_TOKEN", "").strip(),
            slahub_base_url=os.getenv("SLAHUB_BASE_URL", "").strip(),
            creator_bot_api_secret=os.getenv("CREATOR_BOT_API_SECRET", "").strip(),
            creator_bot_api_host=os.getenv("CREATOR_BOT_API_HOST", "127.0.0.1").strip(),
            creator_bot_api_port=_optional_int("CREATOR_BOT_API_PORT"),
            creator_bot_guild_id=_optional_int("CREATOR_BOT_GUILD_ID"),
            creator_bot_guild_ids=_optional_int_list("CREATOR_BOT_GUILD_IDS"),
            slash_command_guild_ids=_optional_int_list("SLASH_GUILD_IDS"),
            development_guild_ids=_optional_int_list("DEVELOPMENT_GUILD_IDS"),
            clear_guild_commands_on_startup=_optional_bool("CLEAR_GUILD_COMMANDS_ON_STARTUP", False),
            status_text=os.getenv("BOT_STATUS_TEXT", "Solo Leveling:ARISE").strip(),
            status_type=os.getenv("BOT_STATUS_TYPE", "playing").strip().lower(),
            database_path=Path(os.getenv("BOT_DATABASE_PATH", "data/bot.db")),
        )


def get_development_guild_ids() -> list[int] | None:
    return _optional_int_list("SLASH_GUILD_IDS") or None


def _optional_int(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _optional_int_list(name: str) -> list[int]:
    value = os.getenv(name, "").strip()
    if not value:
        return []

    guild_ids: list[int] = []
    for raw_item in value.split(","):
        item = raw_item.strip()
        if not item:
            continue
        try:
            guild_ids.append(int(item))
        except ValueError as exc:
            raise ValueError(f"{name} must contain comma-separated integers") from exc

    return guild_ids


def _optional_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")
