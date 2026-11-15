"""Offline protocol tests for ES3C28P media control. No MP3 or network access."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core.handle.textHandler.mediaMessageHandler import MediaTextMessageHandler
from core.media.mcp_media_player import MCPMediaPlayer, MediaTrack


def run(coro):
    return asyncio.run(coro)


class Socket:
    def __init__(self):
        self.messages = []

    async def send(self, value):
        self.messages.append(json.loads(value))


class Logger:
    def bind(self, **kwargs):
        return self

    def warning(self, *args, **kwargs):
        pass


def fake_conn(enabled=True):
    return SimpleNamespace(
        media_control_enabled=enabled, session_id="session-a",
        mcp_media_player=None, websocket=Socket(), logger=Logger(),
    )


def test_legacy_session_never_receives_touch_media():
    conn = fake_conn(enabled=False)
    run(MediaTextMessageHandler().handle(
        conn, {"type": "media", "state": "get_state", "session_id": "session-a"}
    ))
    assert conn.websocket.messages == []


def test_idle_reply_and_session_mismatch():
    conn = fake_conn()
    handler = MediaTextMessageHandler()
    run(handler.handle(conn, {"state": "get_state", "session_id": "wrong"}))
    assert conn.websocket.messages == []
    run(handler.handle(conn, {"state": "get_state", "session_id": "session-a"}))
    assert conn.websocket.messages[-1] == {
        "type": "media", "state": "idle", "title": "",
        "position_ms": 0, "duration_ms": 0,
        "current_index": -1, "playlist_length": 0,
    }


def test_bounded_playlist_and_private_audio_urls():
    player = MCPMediaPlayer(SimpleNamespace())
    player.tracks = [
        MediaTrack(title="曲目" + str(i) + "中" * 140,
                   url="http://jellyfin-mcp/audio/VERY_PRIVATE_TOKEN")
        for i in range(14)
    ]
    player.index = 1
    player.active = True
    snapshot = player.snapshot(6)
    assert snapshot["page_offset"] == 6
    assert len(snapshot["playlist"]) == 6
    assert snapshot["playlist"][0]["index"] == 6
    assert snapshot["playlist_length"] == 14
    assert all(len(item["title"].encode("utf-8")) <= 180 for item in snapshot["playlist"])
    assert "VERY_PRIVATE_TOKEN" not in str(snapshot)


def test_missing_seek_position_or_select_index_cannot_become_zero():
    conn = fake_conn()
    player = MCPMediaPlayer(SimpleNamespace())
    player.tracks = [MediaTrack("song", "http://jellyfin-mcp/audio/token", 180000)]
    player.active = True
    player.duration_ms = 180000
    conn.mcp_media_player = player
    handler = MediaTextMessageHandler()
    run(handler.handle(conn, {"state": "select"}))
    assert conn.websocket.messages[-1]["state"] == "error"
    run(handler.handle(conn, {"state": "seek"}))
    assert conn.websocket.messages[-1]["state"] == "error"
    run(handler.handle(conn, {"state": "seek", "position_ms": True}))
    assert conn.websocket.messages[-1]["state"] == "error"
    run(handler.handle(conn, {"state": "seek", "position_ms": 7200001}))
    assert conn.websocket.messages[-1]["state"] == "error"


def test_unknown_duration_refuses_seek_without_mutating_decoder():
    async def scenario():
        player = MCPMediaPlayer(SimpleNamespace())
        player.tracks = [MediaTrack("song", "http://trusted/audio/token")]
        player.active = True
        with pytest.raises(ValueError, match="时长"):
            await player.seek(5000)
        assert player._task is None
        assert player.position_ms == 0

    run(scenario())


def test_real_seek_and_selection_use_existing_decoder_task():
    async def idle(self):
        await asyncio.Event().wait()

    async def scenario():
        conn = SimpleNamespace(
            tts=None, sentence_id="a", client_abort=False,
            mcp_media_suppressed_sentence=None,
        )
        player = MCPMediaPlayer(conn)
        player.tracks = [
            MediaTrack("a", "http://trusted/audio/a", 180000),
            MediaTrack("b", "http://trusted/audio/b", 200000),
        ]
        player.active = True
        player.duration_ms = 180000
        with patch.object(MCPMediaPlayer, "_run", idle):
            await player.seek(60000)
            assert player._seek_frames == 1000
            assert player.position_ms == 60000
            await player.select(1)
            assert player.index == 1
            assert player.duration_ms == 200000
            assert player._seek_frames == 0
            await player.close()

    run(scenario())


def test_hello_advertises_media_only_when_explicitly_supported(monkeypatch):
    from core.handle.helloHandle import handleHelloMessage

    async def check(device_features, host, ffmpeg_path):
        conn = SimpleNamespace(
            websocket=Socket(), logger=Logger(), welcome_msg={
                "type": "hello", "transport": "websocket", "session_id": "session-a"
            }, features=None,
        )
        if host:
            monkeypatch.setenv("MCP_MEDIA_ALLOWED_HOSTS", host)
        else:
            monkeypatch.delenv("MCP_MEDIA_ALLOWED_HOSTS", raising=False)
        with patch("core.handle.helloHandle.shutil.which", return_value=ffmpeg_path):
            await handleHelloMessage(conn, {"features": device_features})
        return conn.websocket.messages[0], conn.media_control_enabled

    message, enabled = run(check({"media_control": True}, "jellyfin-mcp", "/usr/bin/ffmpeg"))
    assert message["features"]["media_control"] is True and enabled
    for features, host, exe in [
        ({}, "jellyfin-mcp", "/usr/bin/ffmpeg"),
        ({"media_control": False}, "jellyfin-mcp", "/usr/bin/ffmpeg"),
        ({"media_control": True}, "", "/usr/bin/ffmpeg"),
        ({"media_control": True}, "jellyfin-mcp", None),
    ]:
        message, enabled = run(check(features, host, exe))
        assert not enabled and "features" not in message


def test_touch_play_button_resumes_stopped_playlist():
    async def idle(self):
        await asyncio.Event().wait()

    async def scenario():
        conn = SimpleNamespace(
            tts=None, sentence_id="a", client_abort=False,
            mcp_media_suppressed_sentence=None,
        )
        player = MCPMediaPlayer(conn)
        player.tracks = [MediaTrack("song", "http://trusted/audio/token", 50000)]
        player.active = False
        player.index = 1  # Natural EOF left the index past the queue.
        conn.mcp_media_player = player
        conn.media_control_enabled = True
        conn.session_id = "session-a"
        conn.websocket = Socket()
        conn.logger = Logger()
        with patch.object(MCPMediaPlayer, "_run", idle):
            await MediaTextMessageHandler().handle(conn, {
                "state": "resume", "session_id": "session-a"
            })
            assert player.active and player.index == 0
            assert conn.websocket.messages[-1]["state"] == "playing"
            await player.close()

    run(scenario())
