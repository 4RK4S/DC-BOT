import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


# If Discord shows duplicate slash commands because both global and guild
# commands exist:
# Manual cleanup:
# 1. Stop the bot.
# 2. Run: python scripts/clear_guild_commands.py
# 3. Start the bot.
# 4. Wait for Discord cache/global command refresh.
#
# Startup cleanup:
# 1. Set CLEAR_GUILD_COMMANDS_ON_STARTUP=true in .env.
# 2. Start the bot once.
# 3. Set CLEAR_GUILD_COMMANDS_ON_STARTUP=false again.
# 4. Restart the bot.
# 5. Wait for Discord cache/global command refresh.
API_BASE = "https://discord.com/api/v10"
CLEAR_GLOBAL_COMMANDS = False


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        clean = line.strip()
        if not clean or clean.startswith("#") or "=" not in clean:
            continue
        key, value = clean.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def parse_guild_ids() -> list[int]:
    raw = (
        os.getenv("CREATOR_BOT_GUILD_IDS")
        or os.getenv("DEVELOPMENT_GUILD_IDS")
        or os.getenv("SLASH_GUILD_IDS")
        or ""
    )
    guild_ids: list[int] = []
    for item in raw.replace(";", ",").split(","):
        clean = item.strip()
        if clean:
            guild_ids.append(int(clean))
    single = os.getenv("CREATOR_BOT_GUILD_ID", "").strip()
    if single:
        guild_ids.append(int(single))
    return list(dict.fromkeys(guild_ids))


def discord_request(method: str, path: str, token: str, payload: object | None = None) -> object:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        f"{API_BASE}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "DC_bot_new command cleanup",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Discord API {method} {path} failed: {exc.code} {details}") from exc
    return json.loads(body) if body else None


def main() -> int:
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        print("DISCORD_TOKEN is missing from .env", file=sys.stderr)
        return 1

    guild_ids = parse_guild_ids()
    if not guild_ids:
        print("No guild IDs found in CREATOR_BOT_GUILD_IDS, DEVELOPMENT_GUILD_IDS, SLASH_GUILD_IDS, or CREATOR_BOT_GUILD_ID.")
        return 0

    app = discord_request("GET", "/oauth2/applications/@me", token)
    application_id = app["id"]

    for guild_id in guild_ids:
        discord_request("PUT", f"/applications/{application_id}/guilds/{guild_id}/commands", token, [])
        print(f"Cleared guild commands for guild {guild_id}.")

    if CLEAR_GLOBAL_COMMANDS:
        discord_request("PUT", f"/applications/{application_id}/commands", token, [])
        print("Cleared global commands.")
    else:
        print("Global commands were not cleared.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
