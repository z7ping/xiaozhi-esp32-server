"""Offline tests for MCP ResourceLink audio playback (no Jellyfin/FFmpeg required)."""

import asyncio
import queue
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.media.mcp_media_player import (
    MAX_TRACKS,
    MCPMediaPlayer,
    MediaTrack,
    validate_audio_link,
)
from core.providers.tools.server_mcp.mcp_executor import ServerMCPExecutor
from plugins_func.register import Action


def _run(coro):
    return asyncio.run(coro)


def test_resource_link_requires_audio_mime_and_trusted_host():
    allowed = {"mcp-host", "jellyfin-mcp"}
    url = "http://mcp-host:8080/audio/SECRET_LINK_TOKEN"
    assert validate_audio_link(url, "audio/mpeg", allowed) == url
    for invalid, mime in [
        ("http://untrusted:8080/audio/token", "audio/mpeg"),
        ("file:///etc/passwd", "audio/mpeg"),
        ("http://user:password@mcp-host:8080/audio/t", "audio/mpeg"),
        ("http://mcp-host:8080/audio/t", "text/html"),
        ("http://mcp-host:8080/audio/t#fragment", "audio/mpeg"),
    ]:
        with pytest.raises(ValueError):
            validate_audio_link(invalid, mime, allowed)


def test_audio_mcp_result_starts_player_and_never_echoes_token(monkeypatch):
    monkeypatch.setenv("MCP_MEDIA_ALLOWED_HOSTS", "mcp-host")
    token = "DO_NOT_LOG_OR_SEND_TO_LLM"
    class FakeManager:
        async def execute_tool(self, tool_name, args):
            assert tool_name == "jellyfin_audio"
            return SimpleNamespace(
                isError=False,
                content=[
                    SimpleNamespace(type="resource_link", uri=f"http://mcp-host:8080/audio/{token}",
                                    name="曲目一", mimeType="audio/mpeg"),
                    SimpleNamespace(type="resource_link", uri="http://mcp-host:8080/audio/safe",
                                    name="曲目二", mimeType="audio/mpeg"),
                ],
            )

    class FakePlayer:
        def __init__(self, conn):
            self.conn = conn
            self.tracks = None

        async def replace(self, tracks):
            self.tracks = tracks
            return len(tracks)

    conn = SimpleNamespace(mcp_media_player=None)
    executor = ServerMCPExecutor(conn)
    executor.mcp_manager = FakeManager()
    executor._initialized = True
    with patch("core.providers.tools.server_mcp.mcp_executor.MCPMediaPlayer", FakePlayer):
        response = _run(executor.execute(conn, "jellyfin_audio", {"item_id": "song"}))
    assert response.action is Action.RECORD
    assert "2" in response.result
    assert token not in str(response.result) + str(response.response)
    assert [t.title for t in conn.mcp_media_player.tracks] == ["曲目一", "曲目二"]


def test_untrusted_link_stops_whole_playlist(monkeypatch):
    monkeypatch.setenv("MCP_MEDIA_ALLOWED_HOSTS", "mcp-host")
    class FakeManager:
        async def execute_tool(self, name, args):
            return SimpleNamespace(
                isError=False,
                content=[SimpleNamespace(type="resource_link", uri="http://other/audio/token",
                                        name="bad", mimeType="audio/mpeg")],
            )
    conn = SimpleNamespace(mcp_media_player=None)
    executor = ServerMCPExecutor(conn)
    executor.mcp_manager = FakeManager()
    executor._initialized = True
    result = _run(executor.execute(conn, "jellyfin_audio", {}))
    assert result.action is Action.NOTFOUND
    assert conn.mcp_media_player is None


def test_playback_control_is_visible_only_when_jellyfin_audio_exists():
    class FakeManager:
        def __init__(self, on):
            self.on = on

        def get_all_tools(self):
            if not self.on:
                return []
            return [{"function": {"name": "jellyfin_audio",
                    "description": "play resources", "parameters": {"type": "object"}}}]

    conn = SimpleNamespace(mcp_media_player=None)
    executor = ServerMCPExecutor(conn)
    executor.mcp_manager = FakeManager(False)
    executor._initialized = True
    assert "control_mcp_media_playback" not in executor.get_tools()
    executor.mcp_manager = FakeManager(True)
    assert "control_mcp_media_playback" in executor.get_tools()


def test_media_player_pause_resume_preserves_playlist_without_just_clearing_reports():
    async def scenario():
        text_q = queue.Queue()
        audio_q = queue.Queue()
        report_q = queue.Queue()
        text_q.put("old speech")
        report_q.put("retain this report")
        conn = SimpleNamespace(
            tts=SimpleNamespace(tts_text_queue=text_q, tts_audio_queue=audio_q),
            report_queue=report_q,
            sentence_id="turn1",
            client_abort=False,
            stop_event=threading.Event(),
            client_is_speaking=False,
        )
        player = MCPMediaPlayer(conn)

        async def idle(self):
            await asyncio.Event().wait()

        with patch.object(MCPMediaPlayer, "_run", idle), patch(
            "core.media.mcp_media_player.send_tts_message", return_value=None
        ):
            # send_tts_message is an async function; patch via AsyncMock below.
            pass
        from unittest.mock import AsyncMock
        with patch.object(MCPMediaPlayer, "_run", idle), patch(
            "core.media.mcp_media_player.send_tts_message", new_callable=AsyncMock
        ):
            await player.replace([MediaTrack("song", "http://mcp-host/audio/t")])
            assert player.owns_sentence("turn1")
            assert text_q.empty()
            assert report_q.get_nowait() == "retain this report"
            await player.interrupt()
            assert player.paused and not player.owns_audio
            conn.sentence_id = "turn2"
            assert await player.control("resume") == "已继续播放"
            assert player.owns_sentence("turn2")
            await player.close()
            assert not player.active

    _run(scenario())
