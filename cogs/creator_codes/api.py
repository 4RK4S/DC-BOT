import logging

from aiohttp import web


class CreatorCodeApiServer:
    def __init__(self, cog) -> None:
        self.cog = cog
        self.logger = logging.getLogger(__name__)
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None

    async def start(self) -> None:
        config = self.cog.bot.config
        if not config.creator_bot_api_secret or not config.creator_bot_api_port:
            self.logger.info("Creator Codes HTTP API disabled; secret or port is not configured.")
            return
        app = web.Application()
        app.router.add_post("/creator-code-add", self.handle_add)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, config.creator_bot_api_host, config.creator_bot_api_port)
        try:
            await self.site.start()
        except OSError:
            await self.runner.cleanup()
            self.runner = None
            self.site = None
            self.logger.exception(
                "Creator Codes HTTP API could not bind to %s:%s",
                config.creator_bot_api_host,
                config.creator_bot_api_port,
            )
            return
        self.logger.info("Creator Codes HTTP API listening on %s:%s", config.creator_bot_api_host, config.creator_bot_api_port)

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
            self.site = None

    async def handle_add(self, request: web.Request) -> web.Response:
        config = self.cog.bot.config
        if request.headers.get("X-Creator-Secret") != config.creator_bot_api_secret:
            return web.json_response({"error": "unauthorized"}, status=401)
        if not config.creator_bot_guild_id:
            return web.json_response({"error": "CREATOR_BOT_GUILD_ID is not configured"}, status=400)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"error": "json body must be an object"}, status=400)

        codes = payload.get("codes")
        key_words = payload.get("key_words")
        expire = payload.get("expire")
        if not isinstance(codes, (list, str)):
            return web.json_response({"error": "codes must be a string or array"}, status=400)
        if not isinstance(key_words, str) or not key_words.strip():
            return web.json_response({"error": "key_words must be a non-empty string"}, status=400)
        if not isinstance(expire, str) or not expire.strip():
            return web.json_response({"error": "expire must be a non-empty string"}, status=400)

        try:
            pool, added, skipped, became_active = await self.cog.service.add_codes(
                config.creator_bot_guild_id,
                codes,
                key_words,
                expire,
                source="api",
            )
            await self.cog.refresh_public_embed(config.creator_bot_guild_id)
            if became_active:
                await self.cog.send_announcement(config.creator_bot_guild_id, pool)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except Exception as exc:
            self.logger.exception("Creator code API add failed")
            return web.json_response({"error": str(exc)}, status=500)
        return web.json_response({"pool_id": pool["id"], "added": added, "skipped": skipped})
