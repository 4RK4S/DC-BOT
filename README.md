# DC Bot New

DC Bot New is a modular Discord bot built with Python, nextcord, SQLite, and environment-based configuration. The project is organized around cogs, so each feature area keeps its commands, services, views, and database logic separate.

## Main Features

- Modular Discord bot architecture based on nextcord cogs.
- Slash-command setup with persistent button, select, and modal panels.
- Async SQLite storage in `data/bot.db`.
- Environment-based configuration through `.env`.
- Automatic database schema initialization and migrations.
- Persistent view restoration after bot restarts.
- Guild-scoped slash command registration for development, with global registration support for production.
- Central admin panel for managing modules and checking bot state.

## Requirements

- Python 3.12 or newer is recommended.
- A Discord bot token.
- Discord privileged intents enabled for member and message-related features.

Install dependencies with:

```bash
pip install -r requirements.txt
```

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Create a `.env` file in the project root.
4. Fill in the required values:

```env
DISCORD_TOKEN=
SLAHUB_BASE_URL=
CREATOR_BOT_API_SECRET=
CREATOR_BOT_API_HOST=127.0.0.1
CREATOR_BOT_API_PORT=
CREATOR_BOT_GUILD_ID=
CREATOR_BOT_GUILD_IDS=
SLASH_GUILD_IDS=
DEVELOPMENT_GUILD_IDS=
CLEAR_GUILD_COMMANDS_ON_STARTUP=false
BOT_STATUS_TEXT=Solo Leveling:ARISE
BOT_STATUS_TYPE=playing
BOT_DATABASE_PATH=data/bot.db
```

5. Start the bot:

```bash
python main.py
```

## Environment Variables

`DISCORD_TOKEN` is the Discord bot token used to log in.

`SLAHUB_BASE_URL` is used by integrations that talk to the external SLA hub service.

`CREATOR_BOT_API_SECRET` secures the Creator Codes HTTP API.

`CREATOR_BOT_API_HOST` and `CREATOR_BOT_API_PORT` configure the local Creator Codes API server.

`CREATOR_BOT_GUILD_ID` and `CREATOR_BOT_GUILD_IDS` select guilds used by creator-code integrations and command cleanup.

`SLASH_GUILD_IDS` controls slash command registration. When set, commands are registered directly to those guilds and usually appear faster during development. When empty, commands are registered globally.

`DEVELOPMENT_GUILD_IDS` can be used as an additional development guild list for cleanup-related configuration.

`CLEAR_GUILD_COMMANDS_ON_STARTUP=true` clears configured guild slash commands on startup. Use this carefully, mainly when moving from guild commands to global commands.

`BOT_STATUS_TEXT` sets the default Discord activity text.

`BOT_STATUS_TYPE` supports `playing`, `watching`, `listening`, and `competing`.

`BOT_DATABASE_PATH` changes the SQLite database location.

## Slash Commands

- `/admin-panel` creates or updates the main admin dashboard.
- `/admin-panel-refresh` refreshes the saved admin dashboard.
- `/sync-commands` manually syncs slash commands. Bot owners and server administrators can use it.
- `/creator-code-panel` opens the Creator Codes management panel.
- `/live-code-panel` opens the Live Codes management panel.
- `/messages-panel` opens the message management panel.
- `/send-img` sends uploaded images and image links to a selected channel.
- `/clear` deletes messages from the current channel.
- `/forwarder-panel` opens the Forwarder management panel.
- `/listener-panel` opens the Listener management panel.
- `/welcome-panel` opens the Welcome management panel.
- `/roles-panel` opens the Roles management panel.
- `/requests-panel` opens the Requests management panel.

Most changes are done through panels, buttons, selects, and modals instead of many separate slash commands.

## Feature Modules

### Admin Panel

The Admin Panel module creates a central dashboard for server managers. It shows loaded modules, command registration information, latency, uptime, persistent view counts, database path, module states, and quick controls for module-specific settings. The panel is saved in SQLite and restored after restarts.

### Status

The Status module manages the bot presence shown in Discord. It supports different activity types such as playing, watching, listening, and competing. Settings can be stored per guild and applied when the bot becomes ready.

### Autopublish

The Autopublish module listens for messages in announcement/news channels and automatically publishes them when enabled. It keeps a tracked message cache to avoid repeated work and can be controlled from the admin UI.

### Creator Codes

The Creator Codes module manages pools of claimable codes. Admins can add pools, set expiry dates, publish a public claim embed, configure announcement channels, configure ping roles, remove pools, clear expired or used codes, and view stats. It also starts a small HTTP API server so external services can send creator codes into the bot when configured.

### Live Codes

The Live Codes module manages temporary public redeem codes for live streams, social posts, and announcements. It supports adding multiple codes, expiry parsing with timezone choices, public code-list embeds, announcement messages, ping roles, and scheduled cleanup of expired codes.

### Messages

The Messages module provides tools for sending, replying to, editing, inspecting, and exporting Discord messages. It supports plain messages, embeds, multiple message sends, custom emoji replacement from `assets/emoji_map.json`, safe allowed mentions, image uploads, image links, and text exports from recent channel history.

### Clear

The Clear module provides the `/clear` command for deleting messages from the current channel. It is intended for administrators or managers who need quick moderation cleanup.

### Forwarder

The Forwarder module forwards messages from configured source channels to configured target channels. It supports message type filtering, saved source/target/type settings, bot/webhook handling rules, and persistent management panels.

### Listener

The Listener module is another forwarding-style module built around editable listener codes. It can connect source channels, target channels, and content types through a management panel. It preserves attachments where possible and restores configured views after restart.

### Welcome

The Welcome module sends configurable welcome messages when members join. It supports a target channel, custom text, optional welcome images, a fixed image layout, background image configuration, and member leave/listener behavior where configured.

### Roles

The Roles module creates public role button panels. Admins can create panels, add buttons, map one or more roles to a button, remove buttons, clear role mappings, delete panels, refresh public messages, and restore public button views after restart.

### Requests

The Requests module creates public request panels for access or role requests. Users can submit requests from a public panel, and staff can approve or deny them through review messages. It includes duplicate protection, role pre-checks, request status tracking, public panel restoration, and review view restoration.

### Server Boost

The Server Boost module detects when members start or stop boosting the server. It can send thank-you embeds, assign a booster role, remove the role when boosting expires, optionally delete expired boost posts, sync current boosters, test boost/expire flows, and validate bot role/channel permissions.

## Project Structure

```text
assets/                 Static assets such as emoji maps and fonts.
cogs/                   Feature modules loaded by `main.py`.
core/                   Shared configuration, database, permissions, embeds, emoji, logging, and utilities.
data/                   Local SQLite database location.
scripts/                Maintenance scripts.
main.py                 Bot entry point and cog loader.
requirements.txt        Python dependencies.
```

## Database

The bot uses SQLite through `aiosqlite`. By default, the database file is stored at:

```text
data/bot.db
```

The database is treated as local runtime state and is ignored by Git.

## Slash Command Registration

During development, set `SLASH_GUILD_IDS` to one or more guild IDs:

```env
SLASH_GUILD_IDS=1245449718285209743
```

Multiple guilds can be separated with commas:

```env
SLASH_GUILD_IDS=1245449718285209743,123456789012345678
```

For global commands, leave it empty:

```env
SLASH_GUILD_IDS=
```

Global commands can take longer to appear in Discord. Guild commands are usually much faster for development.

## Maintenance

Use the helper script below when guild commands need to be cleared manually:

```bash
python scripts/clear_guild_commands.py
```

Use `/sync-commands` in Discord to trigger a manual command sync after code changes.

## Development Notes

Slash commands should usually open panels or run safe diagnostics. Add, remove, edit, toggle, and configuration actions should live inside panel UI with buttons, selects, and modals.

Do not commit `.env`, local databases, Python bytecode, or local IDE files. These are covered by `.gitignore`.
