"""Direct Jellyfin playlist start for the firmware's home-screen buttons.

The ES3C28P touch UI sends ``play_story`` / ``play_song`` after the hello
negotiates ``media_playlist_start``. The playlist selection comes from the
server environment:

* ``XIAOZHI_STORY_PLAYLIST_ID`` / ``XIAOZHI_SONG_PLAYLIST_ID`` hold one or
  more Jellyfin playlist ids separated by commas, or
* the literal ``auto`` lists every Jellyfin playlist once and keeps the ones
  whose name matches the button's keywords (故事/童话 vs 儿歌/童谣), so new
  playlists need no configuration.

MCP payloads and temporary audio URLs never reach the device; every error
returned here is a fixed device-safe message.
"""

import asyncio
import json
import os
import random
import time

from core.media import playback_store
from core.media.mcp_media_player import (
    MCPMediaPlayer,
    MediaTrack,
    link_mime_type,
    trusted_hosts,
    validate_audio_link,
)

STORY_PLAYLIST_ENV = "XIAOZHI_STORY_PLAYLIST_ID"
SONG_PLAYLIST_ENV = "XIAOZHI_SONG_PLAYLIST_ID"
SHUFFLE_ENV = "XIAOZHI_PLAYLIST_SHUFFLE"
AUTO_VALUE = "auto"


def playlist_shuffle_enabled(action):
    """XIAOZHI_PLAYLIST_SHUFFLE: ``1/true/all`` shuffles every load,
    ``story``/``song`` shuffle only that home-screen button. ``action`` is
    the device media action (``play_story``/``play_song``) or ``None`` for
    generic voice loads, which only shuffle under ``1/true``.
    """
    value = (os.getenv(SHUFFLE_ENV) or "").strip().lower()
    if value in ("1", "true", "yes", "all"):
        return True
    if action == "play_story":
        return value == "story"
    if action == "play_song":
        return value == "song"
    return False

_AUTO_KEYWORDS = {
    "song": ("儿歌", "童谣", "song"),
    "story": ("故事", "童话", "story"),
}
_SEARCH_LIMIT = 100
_MAX_TRACKS = 1000
# A fresh connection builds func_handler in the background (private-config
# fetch + component init) while the device already sends play_story right
# after hello; wait for it instead of failing the first tap.
_TOOLS_READY_TIMEOUT = 12
_LOAD_TIMEOUT_SECONDS = 20

_UNSAFE = "读取 Jellyfin 歌单失败"


async def start_device_playlist(conn, action: str) -> str:
    """Replace the connection's queue with the configured playlist(s).

    Returns an empty string on success or a device-safe Chinese error
    message. Never forwards MCP payloads, URLs or exception text.
    """
    env = SONG_PLAYLIST_ENV if action == "play_song" else STORY_PLAYLIST_ENV
    raw = (os.getenv(env) or "").strip()
    if not raw:
        return "服务端未配置歌单"

    handler = await _wait_tools_ready(conn)
    executor = getattr(handler, "server_mcp_executor", None) if handler is not None else None
    if executor is None:
        return "Jellyfin 音频服务不可用"

    try:
        tracks = await asyncio.wait_for(
            _load_tracks(executor.mcp_manager, action, raw),
            timeout=_LOAD_TIMEOUT_SECONDS,
        )
        if not tracks:
            raise ValueError("歌单没有可播放音频")
        if playlist_shuffle_enabled(action):
            random.shuffle(tracks)
        player = getattr(conn, "mcp_media_player", None)
        if player is None:
            player = MCPMediaPlayer(conn)
            conn.mcp_media_player = player
        player.progress_source = raw
        # Resume where this device left off (title-matched,7-day window).
        start_index, start_position_ms = playback_store.resume_point(
            playback_store.load_progress(playback_store.device_id_of(conn)),
            tracks,
        )
        await player.replace(tracks, start_index, start_position_ms)
    except asyncio.TimeoutError:
        return _UNSAFE
    except ValueError as exc:
        return str(exc)
    except Exception:
        logger = getattr(conn, "logger", None)
        if logger is not None:
            logger.warning("device playlist start failed")
        return _UNSAFE
    return ""


async def _wait_tools_ready(conn, timeout: float = None):
    """Wait for the connection's background tool init (finish_init).

    Returns the UnifiedToolHandler or None when it never becomes ready;
    calling executor.initialize() here would race the background init and
    could connect the MCP clients twice.
    """
    if timeout is None:
        timeout = _TOOLS_READY_TIMEOUT
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        handler = getattr(conn, "func_handler", None)
        if handler is not None and getattr(handler, "finish_init", False):
            return handler
        await asyncio.sleep(0.2)
    return None


async def _load_tracks(manager, action: str, raw: str):
    allowed = trusted_hosts()
    if not allowed:
        raise ValueError("请先配置 MCP_MEDIA_ALLOWED_HOSTS")

    if raw.lower() == AUTO_VALUE:
        playlist_ids = await _discover_playlist_ids(manager, action)
        if not playlist_ids:
            raise ValueError("未找到匹配的歌单")
    else:
        playlist_ids = [part.strip() for part in raw.split(",") if part.strip()]

    tracks = []
    for playlist_id in playlist_ids:
        result = await manager.execute_tool(
            "jellyfin_audio", {"playlist_id": playlist_id}
        )
        if getattr(result, "isError", False):
            raise ValueError(_UNSAFE)
        links = [
            item
            for item in (getattr(result, "content", None) or [])
            if getattr(item, "type", None) == "resource_link"
        ]
        if not links:
            raise ValueError("歌单没有可播放音频")
        for item in links:
            try:
                url = validate_audio_link(
                    item.uri, link_mime_type(item), allowed
                )
            except ValueError:
                # Detail stays server-side: the message goes to a child's screen.
                raise ValueError("媒体链接校验失败") from None
            title = getattr(item, "name", None) or "Jellyfin 音频"
            tracks.append(MediaTrack(title=title[:100], url=url))
        if len(tracks) > _MAX_TRACKS:
            raise ValueError("歌单最多支持 1000 首歌曲")
    return tracks


async def _discover_playlist_ids(manager, action: str):
    """Ask Jellyfin for every playlist, keep the keyword matches, in order."""
    result = await manager.execute_tool(
        "jellyfin_search", {"query": "", "type": "Playlist", "limit": _SEARCH_LIMIT}
    )
    if getattr(result, "isError", False):
        raise ValueError(_UNSAFE)

    keywords = _AUTO_KEYWORDS["song" if action == "play_song" else "story"]
    playlist_ids = []
    seen = set()
    for content in getattr(result, "content", None) or []:
        if getattr(content, "type", None) != "text":
            continue
        try:
            payload = json.loads(content.text)
        except (TypeError, ValueError):
            continue
        items = payload.get("items") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or item.get("type") != "Playlist":
                continue
            item_id = item.get("id")
            name = str(item.get("name") or "").lower()
            if not item_id or item_id in seen:
                continue
            if any(keyword in name for keyword in keywords):
                seen.add(item_id)
                playlist_ids.append(str(item_id))
    return playlist_ids
