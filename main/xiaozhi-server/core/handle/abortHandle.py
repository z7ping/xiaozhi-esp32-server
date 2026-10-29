import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
TAG = __name__


async def handleAbortMessage(conn: "ConnectionHandler"):
    conn.logger.bind(tag=TAG).info("Abort message received")
    # 设置成打断状态，会自动打断llm、tts任务
    conn.close_after_chat = False
    conn.client_abort = True
    # Pause the active HTTP stream while the device collects the next command.
    # The playlist and decoder position are retained for a subsequent resume.
    media_player = getattr(conn, "mcp_media_player", None)
    if media_player:
        await media_player.interrupt()
    conn.clear_queues()
    # 打断客户端说话状态
    await conn.websocket.send(
        json.dumps({"type": "tts", "state": "stop", "session_id": conn.session_id})
    )
    conn.clearSpeakStatus()
    conn.logger.bind(tag=TAG).info("Abort message received-end")
