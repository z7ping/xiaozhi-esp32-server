import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from core.handle.helloHandle import handleHelloMessage
from core.handle.textMessageHandlerRegistry import TextMessageHandlerRegistry
from core.handle.textMessageProcessor import TextMessageProcessor
from core.media.mcp_media_player import MCPMediaPlayer, MediaTrack


def test_touch_media_uses_negotiated_session_and_existing_player():
    async def scenario():
        welcome = {"type": "hello", "features": {"another_feature": True}}
        conn = SimpleNamespace(
            session_id="device-session",
            welcome_msg=welcome,
            media_control_enabled=False,
            mcp_media_player=None,
            websocket=SimpleNamespace(send=AsyncMock()),
            logger=MagicMock(),
            sentence_id="turn",
            stop_event=threading.Event(),
            client_abort=False,
        )
        processor = TextMessageProcessor(TextMessageHandlerRegistry())

        async def send(action, session_id="device-session", **fields):
            await processor.process_message(conn, json.dumps({
                "type": "media", "session_id": session_id, "state": action, **fields,
            }))
            return json.loads(conn.websocket.send.await_args.args[0])

        await processor.process_message(conn, '{"type":"media","state":"get_state"}')
        conn.websocket.send.assert_not_awaited()
        await handleHelloMessage(conn, {"features": {"media_control": True}})
        assert json.loads(conn.websocket.send.await_args.args[0])["features"] == {
            "media_control": True, "media_playlist_start": True,
            "another_feature": True,
        }
        legacy = SimpleNamespace(welcome_msg=welcome, websocket=SimpleNamespace(send=AsyncMock()))
        await handleHelloMessage(legacy, {})
        assert "media_control" not in json.loads(legacy.websocket.send.await_args.args[0])["features"]
        conn.websocket.send.reset_mock()
        await processor.process_message(
            conn, '{"type":"media","state":"get_state","session_id":"other"}'
        )
        conn.websocket.send.assert_not_awaited()
        assert (await send("get_state"))["state"] == "stopped"
        assert (await send("get_playlist", index=6))["playlist"] == []

        player = conn.mcp_media_player = MCPMediaPlayer(conn)

        async def idle(_):
            await asyncio.Event().wait()

        with patch.object(MCPMediaPlayer, "_run", idle), patch(
            "core.media.mcp_media_player.send_tts_message", new_callable=AsyncMock
        ):
            await player.replace([MediaTrack("曲" * 100, "http://jellyfin-mcp/audio/private")] * 8)
            try:
                first = await send("get_playlist", index=0)
                assert len(first["playlist"]) == 6
                page = await send("get_playlist", index=6)
                assert page["page_offset"] == 6
                assert [entry["index"] for entry in page["playlist"]] == [6, 7]
                assert len(page["title"].encode()) == 192
                assert "private" not in json.dumps(page)
                assert (await send("select", index=True))["error"]
                assert (await send("select", index=7))["current_index"] == 7
                assert (await send("prev"))["current_index"] == 6
                assert (await send("next"))["current_index"] == 7
                assert (await send("seek", position_ms=1000))["error"]
                player.duration_ms = 3000
                assert (await send("seek", position_ms=3000))["error"]
                assert (await send("pause"))["state"] == "paused"
                sought = await send("seek", position_ms=1000)
                assert (sought["state"], sought["position_ms"]) == ("paused", 1000)
                assert (await send("resume"))["state"] == "playing"
                assert (await send("stop"))["state"] == "stopped"
            finally:
                await player.close()

    asyncio.run(scenario())
