import json

from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textMessageType import TextMessageType
from core.media.device_playlist import start_device_playlist


class MediaTextMessageHandler(TextMessageHandler):
    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.MEDIA

    async def handle(self, conn, msg_json):
        if not conn.media_control_enabled or msg_json.get("session_id") != conn.session_id:
            return
        action = msg_json.get("state")
        index = msg_json.get("index")
        position = msg_json.get("position_ms")
        shuffle = msg_json.get("shuffle")
        page = None
        error = ""
        player = conn.mcp_media_player
        if action == "get_playlist":
            if type(index) is int and index >= 0:
                page = index
            else:
                error = "无效的列表位置"
        elif action == "select" and (type(index) is not int or index < 0):
            error = "无效的曲目序号"
        elif action == "seek" and (type(position) is not int or position < 0):
            error = "无效的播放进度"
        elif action in ("pause", "resume", "stop", "next", "prev", "select", "seek"):
            if player is None:
                error = "请先语音播放"
            else:
                error = await player.control_from_device(action, index, position)
        elif action == "set_shuffle":
            if type(shuffle) is not bool:
                error = "无效的播放模式"
            elif player is None:
                error = "当前歌单不支持切换顺序"
            else:
                error = await player.set_shuffle(shuffle)
        elif action in ("play_story", "play_song"):
            if "shuffle" in msg_json and type(shuffle) is not bool:
                error = "无效的播放模式"
            else:
                error = await start_device_playlist(conn, action, shuffle)
            # Starting a playlist may have created the player; report its
            # real state instead of the pre-start snapshot below.
            player = conn.mcp_media_player
        elif action != "get_state":
            error = "不支持的播放操作"

        if player is None:
            state = {
                "state": "stopped", "title": "", "position_ms": 0,
                "duration_ms": 0, "current_index": -1, "playlist_length": 0,
            }
            if page is not None:
                state.update(page_offset=0, playlist=[])
        else:
            state = player.device_state(page)
        if error:
            state["error"] = error
        await conn.websocket.send(json.dumps({
            "type": "media", "session_id": conn.session_id, **state,
        }, ensure_ascii=False))
