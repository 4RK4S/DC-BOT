import json
import logging
import re
from pathlib import Path
from typing import Any


EMOJI_MAP_PATH = Path("assets") / "emoji_map.json"
EMOJI_KEY_RE = re.compile(r":([A-Za-z0-9_]+):")

_logger = logging.getLogger(__name__)
_cache_mtime: float | None = None
_cache: dict[str, str] = {}


def load_emoji_map() -> dict[str, str]:
    global _cache_mtime, _cache

    try:
        stat = EMOJI_MAP_PATH.stat()
    except FileNotFoundError:
        _cache_mtime = None
        _cache = {}
        return {}
    except OSError as exc:
        _logger.warning("Could not stat emoji map at %s: %s", EMOJI_MAP_PATH, exc)
        return _cache

    if _cache_mtime == stat.st_mtime:
        return _cache

    try:
        data = json.loads(EMOJI_MAP_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _logger.warning("Could not load emoji map at %s: %s", EMOJI_MAP_PATH, exc)
        _cache_mtime = stat.st_mtime
        _cache = {}
        return {}

    _cache = {}
    _flatten_emoji_map(data, _cache)
    _cache_mtime = stat.st_mtime
    return _cache


def replace_custom_emoji_keys(text: str | None) -> str | None:
    if not text:
        return text

    emoji_map = load_emoji_map()
    if not emoji_map:
        return text

    def replace(match: re.Match) -> str:
        start = match.start()
        if start > 0 and text[start - 1] == "<":
            return match.group(0)
        if start > 1 and text[start - 2 : start] == "<a":
            return match.group(0)
        return emoji_map.get(match.group(1), match.group(0))

    return EMOJI_KEY_RE.sub(replace, text)


def _flatten_emoji_map(value: Any, output: dict[str, str], key_hint: str | None = None) -> None:
    if isinstance(value, dict):
        emoji_id = value.get("id")
        if emoji_id is not None:
            name = str(value.get("name") or key_hint or "").strip(":")
            if name:
                animated = bool(value.get("animated"))
                prefix = "a" if animated else ""
                output[name] = f"<{prefix}:{name}:{emoji_id}>"
            return
        for key, child in value.items():
            _flatten_emoji_map(child, output, str(key).strip(":"))
        return

    if isinstance(value, list):
        for child in value:
            _flatten_emoji_map(child, output, key_hint)
        return

    if key_hint and value is not None:
        name = key_hint.strip(":")
        output[name] = f"<:{name}:{value}>"
