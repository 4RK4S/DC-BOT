import hmac
import logging
from typing import Any

from aiohttp import web


class CreatorCodeApiServer:
    """Shared local HTTP API used by SLAHUB for creator and live codes."""

    def __init__(self, cog) -> None:
        self.cog = cog
        self.logger = logging.getLogger(__name__)
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None

    async def start(self) -> None:
        config = self.cog.bot.config
        if not config.creator_bot_api_port:
            self.logger.info("SLAHUB HTTP API disabled; CREATOR_BOT_API_PORT is not configured.")
            return
        if not config.creator_bot_api_secret and not config.live_code_bot_api_secret:
            self.logger.info("SLAHUB HTTP API disabled; no API secret is configured.")
            return

        app = web.Application(client_max_size=512 * 1024)
        app.router.add_post("/creator-code-add", self.handle_creator_code_add)
        app.router.add_post("/live-code-add", self.handle_live_code_add)
        app.router.add_get("/health", self.handle_health)

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
                "SLAHUB HTTP API could not bind to %s:%s",
                config.creator_bot_api_host,
                config.creator_bot_api_port,
            )
            return
        self.logger.info(
            "SLAHUB HTTP API listening on %s:%s (creator-code-add, live-code-add)",
            config.creator_bot_api_host,
            config.creator_bot_api_port,
        )

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
            self.site = None

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "service": "slahub-discord-bot-api"})

    async def handle_creator_code_add(self, request: web.Request) -> web.Response:
        config = self.cog.bot.config
        if not self._is_authorized(request, config.creator_bot_api_secret):
            return web.json_response({"error": "unauthorized"}, status=401)
        if not config.creator_bot_guild_id:
            return web.json_response({"error": "CREATOR_BOT_GUILD_ID is not configured"}, status=400)

        payload = await self._read_json_object(request)
        if isinstance(payload, web.Response):
            return payload

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

    # Backward-compatible method name used by older code/tests.
    handle_add = handle_creator_code_add

    async def handle_live_code_add(self, request: web.Request) -> web.Response:
        config = self.cog.bot.config
        if not self._is_authorized(request, config.live_code_bot_api_secret):
            return web.json_response({"error": "unauthorized"}, status=401)
        if not config.live_code_bot_guild_id:
            return web.json_response({"error": "LIVE_CODE_BOT_GUILD_ID is not configured"}, status=400)

        payload = await self._read_json_object(request)
        if isinstance(payload, web.Response):
            return payload

        try:
            items = self._normalize_live_code_payload(payload)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        live_cog = self.cog.bot.get_cog("LiveCodesCog")
        if live_cog is None:
            return web.json_response({"error": "LiveCodesCog is not loaded"}, status=503)

        try:
            result = await live_cog.add_live_code_items(
                config.live_code_bot_guild_id,
                items,
                user_id=None,
                announce=True,
                background_sync=True,
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except Exception as exc:
            self.logger.exception("Live code API add failed")
            return web.json_response({"error": str(exc)}, status=500)

        return web.json_response(
            {
                "ok": True,
                "added": result.added,
                "updated": result.updated,
                "skipped": result.skipped_duplicates,
                "codes": result.codes,
            }
        )

    @staticmethod
    async def _read_json_object(request: web.Request) -> dict[str, Any] | web.Response:
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"error": "json body must be an object"}, status=400)
        return payload

    @staticmethod
    def _is_authorized(request: web.Request, expected_secret: str) -> bool:
        if not expected_secret:
            return False
        supplied = request.headers.get("X-SLAHub-Secret") or request.headers.get("X-Creator-Secret") or ""
        return bool(supplied) and hmac.compare_digest(supplied, expected_secret)

    @staticmethod
    def _normalize_live_code_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
        raw_items = payload.get("items")
        if raw_items is None:
            raw_codes = payload.get("codes")
            if isinstance(raw_codes, str):
                raw_items = [part for part in raw_codes.replace(",", " ").split() if part]
            elif isinstance(raw_codes, list):
                raw_items = raw_codes
            else:
                raise ValueError("items or codes must be provided")

        if not isinstance(raw_items, list):
            raise ValueError("items must be an array")
        if not raw_items:
            raise ValueError("items must include at least one live code")
        if len(raw_items) > 100:
            raise ValueError("a maximum of 100 live codes can be sent at once")

        normalized: list[dict[str, Any]] = []
        for raw in raw_items:
            item = raw if isinstance(raw, dict) else {"code": raw}
            code = str(item.get("code") or "").strip()
            if not code:
                continue
            if len(code) > 128:
                raise ValueError("live code cannot be longer than 128 characters")
            expires_at = item.get("expires_at")
            if expires_at is not None and not isinstance(expires_at, str):
                raise ValueError("expires_at must be an ISO date string or null")
            normalized.append(
                {
                    "code": code,
                    "expires_at": expires_at.strip() if isinstance(expires_at, str) else None,
                    "source": str(item.get("source") or payload.get("source") or "SLAHUB").strip()[:200],
                    "reward": str(item.get("reward") or "").strip()[:500],
                    "note": str(item.get("note") or "").strip()[:500],
                    "source_url": str(item.get("source_url") or "").strip()[:1000],
                }
            )

        if not normalized:
            raise ValueError("items must include at least one non-empty live code")
        return normalized
