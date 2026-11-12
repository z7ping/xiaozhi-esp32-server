"""Device-bound touch player control over the existing XiaoZhi text channel.

No URL, path, token, or arbitrary process argument is accepted from the device.
Legacy devices and non-media sessions never enter this handler.
"""

import json
from typing import Any, Dict

from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textMessageType import TextMessageType

MAX_SEEK_MS = 2 * 60 * 60 * 1000
ACTIONS = frozenset((
    "get_state", "get_playlist", "select", "seek",
    "pause", "resume", "stop", "prev", "next",
))


def _idle_state():
    return {
        "type": "media", "state": "idle", "title": "",
        "position_ms": 0, "duration_ms": 0,
        "current_index": -1, "playlist_length": 0,
    }


class MediaTextMessageHandler(TextMessageHandler):
    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.MEDIA

    async def handle(self, conn, msg_json: Dict[str, Any]) -> None:
        # Session-specific opt-in: old or unconfigured servers never receive media
        # requests from the new firmware. Also protect this handler from other clients.
        if not getattr(conn, "media_control_enabled", False):
            return
        session_id = msg_json.get("session_id")
        if session_id and session_id != conn.session_id:
            return

        action = msg_json.get("state")
        player = getattr(conn, "mcp_media_player", None)
        try:
            if action not in ACTIONS:
                raise ValueError("不支持的播放操作")
            if action == "get_state":
                result = player.snapshot() if player else _idle_state()
            elif action == "get_playlist":
                offset = msg_json.get("index")
                if type(offset) is not int or not 0 <= offset <= 1000:
                    raise ValueError("无效的歌单分页位置")
                result = player.snapshot(offset) if player else {
                    **_idle_state(), "page_offset": offset, "playlist": [],
                }
            else:
                if not player or (not player.active and
                                  not (action == "resume" and player.tracks)):
                    raise ValueError("当前没有播放内容")
                if action == "select":
                    index = msg_json.get("index")
                    if type(index) is not int or not 0 <= index < len(player.tracks):
                        raise ValueError("无效的歌曲索引")
                    await player.select(index)
                elif action == "seek":
                    position = msg_json.get("position_ms")
                    if type(position) is not int or not 0 <= position <= MAX_SEEK_MS:
                        raise ValueError("跳转位置超出支持范围")
                    await player.seek(position)
                else:
                    await player.control("previous" if action == "prev" else action)
                result = player.snapshot()
        except ValueError as exc:
            result = {**(player.snapshot() if player else _idle_state()),
                      "state": "error", "error": str(exc)[:90]}
        except Exception as exc:
            # Never leak FFmpeg URL, internal paths, or Jellyfin credentials.
            conn.logger.bind(tag=__name__).warning(
                f"Media command failed: {type(exc).__name__}"
            )
            result = {**_idle_state(), "state": "error", "error": "播放控制失败，请重试"}

        await conn.websocket.send(json.dumps(result, ensure_ascii=False))
