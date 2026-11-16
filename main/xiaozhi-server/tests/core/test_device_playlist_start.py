"""Offline tests for the home-screen 听故事/听儿歌 playlist start (no Jellyfin)."""

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from core.handle.helloHandle import handleHelloMessage
from core.handle.textMessageHandlerRegistry import TextMessageHandlerRegistry
from core.handle.textMessageProcessor import TextMessageProcessor


def _run(coro):
    return asyncio.run(coro)


def _conn():
    return SimpleNamespace(
        session_id="device-session",
        welcome_msg={"type": "hello", "features": {}},
        media_control_enabled=True,
        mcp_media_player=None,
        websocket=SimpleNamespace(send=AsyncMock()),
        logger=MagicMock(),
        sentence_id="turn",
        stop_event=threading.Event(),
        client_abort=False,
        func_handler=None,
        headers={"device-id": "test-device"},
    )


async def _send(conn, action, **fields):
    processor = TextMessageProcessor(TextMessageHandlerRegistry())
    await processor.process_message(conn, json.dumps({
        "type": "media", "session_id": "device-session", "state": action, **fields,
    }))
    return json.loads(conn.websocket.send.await_args.args[0])


def _link(uri, name):
    # Real mcp>=2.x models expose mime_type (snake_case); keep the tests on
    # the field name the live server actually returns.
    return SimpleNamespace(type="resource_link", uri=uri, name=name,
                           mime_type="audio/mpeg")


class FakeManager:
    """Stands in for ServerMCPManager.execute_tool with canned MCP results."""

    def __init__(self, search_items=None, audio_links=None, search_error=False):
        self.calls = []
        self.search_items = search_items or []
        self.audio_links = audio_links or {}
        self.search_error = search_error

    async def execute_tool(self, name, args):
        self.calls.append((name, args))
        if name == "jellyfin_search":
            return SimpleNamespace(
                isError=self.search_error,
                content=[SimpleNamespace(
                    type="text",
                    text=json.dumps({"items": self.search_items}),
                )],
            )
        if name == "jellyfin_audio":
            links = self.audio_links.get(args.get("playlist_id"), [])
            return SimpleNamespace(
                isError=False,
                content=[_link(uri, title) for uri, title in links],
            )
        raise AssertionError(f"unexpected tool {name}")


class FakeExecutor:
    _initialized = True

    def __init__(self, manager):
        self.mcp_manager = manager


class FakePlayer:
    def __init__(self):
        self.tracks = None
        self.start_index = None
        self.start_position_ms = None
        self.progress_source = ""
        self.shuffle = None

    async def replace(self, tracks, start_index=0, start_position_ms=0, *,
                      shuffle=None, restart_random=False, source=None):
        self.tracks = list(tracks)
        self.start_index = start_index
        self.start_position_ms = start_position_ms
        self.shuffle = shuffle
        return len(tracks)

    def device_state(self, page=None):
        return {
            "state": "playing",
            "title": self.tracks[0].title if self.tracks else "",
            "position_ms": 0,
            "duration_ms": 0,
            "current_index": 0,
            "playlist_length": len(self.tracks or []),
        }


def _wire(conn, manager, with_player=True):
    conn.func_handler = SimpleNamespace(
        server_mcp_executor=FakeExecutor(manager), finish_init=True,
    )
    if with_player:
        conn.mcp_media_player = FakePlayer()


def test_hello_advertises_media_playlist_start_only_with_touch_player():
    async def scenario():
        enabled = SimpleNamespace(
            welcome_msg={"type": "hello", "features": {"another_feature": True}},
            media_control_enabled=False,
            websocket=SimpleNamespace(send=AsyncMock()),
            logger=MagicMock(),
        )
        await handleHelloMessage(enabled, {"features": {"media_control": True}})
        features = json.loads(enabled.websocket.send.await_args.args[0])["features"]
        assert features["media_control"] is True
        assert features["media_playlist_start"] is True
        assert features["media_shuffle"] is True

        legacy = SimpleNamespace(
            welcome_msg={"type": "hello", "features": {}},
            media_control_enabled=False,
            websocket=SimpleNamespace(send=AsyncMock()),
            logger=MagicMock(),
        )
        await handleHelloMessage(legacy, {})
        features = json.loads(legacy.websocket.send.await_args.args[0]).get("features", {})
        assert "media_control" not in features
        assert "media_playlist_start" not in features
        assert "media_shuffle" not in features

    _run(scenario())


def test_play_story_uses_single_configured_playlist(monkeypatch):
    async def scenario():
        monkeypatch.setenv("MCP_MEDIA_ALLOWED_HOSTS", "mcp-host")
        monkeypatch.setenv("XIAOZHI_STORY_PLAYLIST_ID", "story-1")
        conn = _conn()
        manager = FakeManager(audio_links={
            "story-1": [("http://mcp-host/audio/t1", "故事一"),
                        ("http://mcp-host/audio/t2", "故事二")],
        })
        _wire(conn, manager)

        reply = await _send(conn, "play_story", shuffle=False)
        assert conn.mcp_media_player.shuffle is False
        assert manager.calls == [("jellyfin_audio", {"playlist_id": "story-1"})]
        assert [t.title for t in conn.mcp_media_player.tracks] == ["故事一", "故事二"]
        assert reply["state"] == "playing"
        assert reply["title"] == "故事一"
        assert "error" not in reply

    _run(scenario())


def test_comma_separated_ids_are_loaded_in_order(monkeypatch):
    async def scenario():
        monkeypatch.setenv("MCP_MEDIA_ALLOWED_HOSTS", "mcp-host")
        monkeypatch.setenv("XIAOZHI_STORY_PLAYLIST_ID", "id-1, id-2")
        conn = _conn()
        manager = FakeManager(audio_links={
            "id-1": [("http://mcp-host/audio/a", "甲")],
            "id-2": [("http://mcp-host/audio/b", "乙"),
                     ("http://mcp-host/audio/c", "丙")],
        })
        _wire(conn, manager)

        reply = await _send(conn, "play_story")
        assert manager.calls == [
            ("jellyfin_audio", {"playlist_id": "id-1"}),
            ("jellyfin_audio", {"playlist_id": "id-2"}),
        ]
        assert [t.title for t in conn.mcp_media_player.tracks] == ["甲", "乙", "丙"]
        assert reply["state"] == "playing"
        assert reply["playlist_length"] == 3

    _run(scenario())


def test_auto_keeps_only_keyword_matching_story_playlists(monkeypatch):
    async def scenario():
        monkeypatch.setenv("MCP_MEDIA_ALLOWED_HOSTS", "mcp-host")
        monkeypatch.setenv("XIAOZHI_STORY_PLAYLIST_ID", "auto")
        conn = _conn()
        manager = FakeManager(
            search_items=[
                {"id": "qinqiang", "name": "秦腔 · 全本", "type": "Playlist"},
                {"id": "songs", "name": "少儿音频 · 宝宝巴士儿歌", "type": "Playlist"},
                {"id": "bedtime", "name": "少儿音频 · 宝宝巴士睡前故事", "type": "Playlist"},
                {"id": "grimm", "name": "少儿音频 · 格林童话", "type": "Playlist"},
                {"id": "classic", "name": "少儿音频 · 经典儿童故事", "type": "Playlist"},
                {"id": "not-a-playlist", "name": "故事合集", "type": "BoxSet"},
            ],
            audio_links={
                "bedtime": [("http://mcp-host/audio/b", "睡前")],
                "grimm": [("http://mcp-host/audio/g", "童话")],
                "classic": [("http://mcp-host/audio/c", "经典")],
            },
        )
        _wire(conn, manager)

        reply = await _send(conn, "play_story")
        assert manager.calls[0] == (
            "jellyfin_search", {"query": "", "type": "Playlist", "limit": 100},
        )
        loaded = [args["playlist_id"] for name, args in manager.calls[1:]]
        assert loaded == ["bedtime", "grimm", "classic"]  # 秦腔/儿歌/BoxSet excluded
        assert reply["state"] == "playing"
        assert reply["playlist_length"] == 3

    _run(scenario())


def test_auto_without_matches_reports_safe_error(monkeypatch):
    async def scenario():
        monkeypatch.setenv("MCP_MEDIA_ALLOWED_HOSTS", "mcp-host")
        monkeypatch.setenv("XIAOZHI_STORY_PLAYLIST_ID", "auto")
        conn = _conn()
        manager = FakeManager(search_items=[
            {"id": "qinqiang", "name": "秦腔 · 全本", "type": "Playlist"},
        ])
        _wire(conn, manager, with_player=False)

        reply = await _send(conn, "play_story")
        assert [name for name, _ in manager.calls] == ["jellyfin_search"]
        assert reply["state"] == "stopped"
        assert reply["error"] == "未找到匹配的歌单"

    _run(scenario())


def test_resume_restores_saved_position(monkeypatch):
    async def scenario():
        import time as _time
        from core.media import playback_store

        monkeypatch.setenv("MCP_MEDIA_ALLOWED_HOSTS", "mcp-host")
        monkeypatch.setenv("XIAOZHI_STORY_PLAYLIST_ID", "story-1")
        monkeypatch.setattr(playback_store, "load_progress", lambda device_id: {
            "title": "故事二", "index": 1, "position_ms": 5000,
            "duration_ms": 60000, "updated_at": _time.time(),
        })
        conn = _conn()
        manager = FakeManager(audio_links={
            "story-1": [("http://mcp-host/audio/t1", "故事一"),
                        ("http://mcp-host/audio/t2", "故事二")],
        })
        _wire(conn, manager)

        reply = await _send(conn, "play_story")
        assert conn.mcp_media_player.start_index == 1
        assert conn.mcp_media_player.start_position_ms == 5000
        assert reply["state"] == "playing"

    _run(scenario())


def test_unknown_saved_title_starts_from_head(monkeypatch):
    async def scenario():
        from core.media import playback_store

        monkeypatch.setenv("MCP_MEDIA_ALLOWED_HOSTS", "mcp-host")
        monkeypatch.setenv("XIAOZHI_STORY_PLAYLIST_ID", "story-1")
        monkeypatch.setattr(playback_store, "load_progress", lambda device_id: {
            "title": "不存在的标题", "position_ms": 9000,
            "duration_ms": 60000, "updated_at": 1,
        })
        conn = _conn()
        manager = FakeManager(audio_links={
            "story-1": [("http://mcp-host/audio/t1", "故事一")],
        })
        _wire(conn, manager)

        await _send(conn, "play_story")
        assert conn.mcp_media_player.start_index == 0
        assert conn.mcp_media_player.start_position_ms == 0

    _run(scenario())


def test_shuffle_env_semantics(monkeypatch):
    from core.media.device_playlist import playlist_shuffle_enabled

    monkeypatch.delenv("XIAOZHI_PLAYLIST_SHUFFLE", raising=False)
    assert playlist_shuffle_enabled("play_story")
    assert playlist_shuffle_enabled("play_song")
    assert not playlist_shuffle_enabled(None)

    monkeypatch.setenv("XIAOZHI_PLAYLIST_SHUFFLE", "story")
    assert playlist_shuffle_enabled("play_story")
    assert not playlist_shuffle_enabled("play_song")
    assert not playlist_shuffle_enabled(None)

    monkeypatch.setenv("XIAOZHI_PLAYLIST_SHUFFLE", "1")
    assert playlist_shuffle_enabled(None)
    assert playlist_shuffle_enabled("play_song")

    monkeypatch.setenv("XIAOZHI_PLAYLIST_SHUFFLE", "0")
    assert not playlist_shuffle_enabled("play_story")


def test_shuffle_reorders_button_queue(monkeypatch):
    async def scenario():
        from core.media import mcp_media_player, playback_store

        monkeypatch.setenv("MCP_MEDIA_ALLOWED_HOSTS", "mcp-host")
        monkeypatch.setenv("XIAOZHI_STORY_PLAYLIST_ID", "story-1")
        monkeypatch.delenv("XIAOZHI_PLAYLIST_SHUFFLE", raising=False)
        monkeypatch.setattr(playback_store, "load_progress", lambda _: None)
        monkeypatch.setattr(playback_store, "save_progress", lambda *args: None)
        monkeypatch.setattr(mcp_media_player, "random",
                            SimpleNamespace(shuffle=lambda tracks: tracks.reverse()))
        monkeypatch.setattr(mcp_media_player.MCPMediaPlayer, "_run", AsyncMock())
        conn = _conn()
        manager = FakeManager(audio_links={
            "story-1": [("http://mcp-host/audio/a", "甲"),
                        ("http://mcp-host/audio/b", "乙"),
                        ("http://mcp-host/audio/c", "丙")],
        })
        _wire(conn, manager, with_player=False)

        reply = await _send(conn, "play_story")
        try:
            assert reply["shuffle"] is True
            assert [t.title for t in conn.mcp_media_player.tracks] == ["丙", "乙", "甲"]
        finally:
            await conn.mcp_media_player.close()

    _run(scenario())


def test_unready_tool_handler_replies_safe_error(monkeypatch):
    async def scenario():
        monkeypatch.setenv("MCP_MEDIA_ALLOWED_HOSTS", "mcp-host")
        monkeypatch.setenv("XIAOZHI_STORY_PLAYLIST_ID", "story-1")
        monkeypatch.setattr("core.media.device_playlist._TOOLS_READY_TIMEOUT", 0.05)
        conn = _conn()  # func_handler still None, like a brand-new connection
        reply = await _send(conn, "play_story")
        assert reply["state"] == "stopped"
        assert reply["error"] == "Jellyfin 音频服务不可用"

    _run(scenario())


def test_song_uses_song_env_and_safe_error_when_missing(monkeypatch):
    async def scenario():
        monkeypatch.setenv("MCP_MEDIA_ALLOWED_HOSTS", "mcp-host")
        monkeypatch.delenv("XIAOZHI_SONG_PLAYLIST_ID", raising=False)
        conn = _conn()
        manager = FakeManager()
        _wire(conn, manager, with_player=False)

        reply = await _send(conn, "play_song")
        assert manager.calls == []
        assert reply["state"] == "stopped"
        assert reply["error"] == "服务端未配置歌单"

        monkeypatch.setenv("XIAOZHI_SONG_PLAYLIST_ID", "song-list-9")
        conn.websocket.send.reset_mock()
        manager = FakeManager(audio_links={
            "song-list-9": [("http://mcp-host/audio/s", "儿歌一")],
        })
        _wire(conn, manager)

        reply = await _send(conn, "play_song")
        assert manager.calls == [("jellyfin_audio", {"playlist_id": "song-list-9"})]
        assert reply["state"] == "playing"
        assert "error" not in reply

    _run(scenario())


def test_failures_never_leak_exception_text(monkeypatch):
    async def scenario():
        monkeypatch.setenv("MCP_MEDIA_ALLOWED_HOSTS", "mcp-host")
        monkeypatch.setenv("XIAOZHI_STORY_PLAYLIST_ID", "story-1")

        class ExplodingManager(FakeManager):
            async def execute_tool(self, name, args):
                self.calls.append((name, args))
                raise RuntimeError("boom http://secret/token")

        conn = _conn()
        _wire(conn, ExplodingManager(), with_player=False)
        reply = await _send(conn, "play_story")
        assert reply["state"] == "stopped"
        assert reply["error"] == "读取 Jellyfin 歌单失败"
        assert "secret" not in json.dumps(reply)

        class UntrustedManager(FakeManager):
            async def execute_tool(self, name, args):
                self.calls.append((name, args))
                return SimpleNamespace(isError=False, content=[
                    _link("http://evil/audio/t", "坏链接"),
                ])

        # With prior playback the old queue keeps running: the reply reports
        # the real (still playing) state plus the start error.
        conn = _conn()
        _wire(conn, UntrustedManager(), with_player=False)
        prior = FakePlayer()
        prior.tracks = [SimpleNamespace(title="旧曲", url="http://mcp-host/audio/old")]
        conn.mcp_media_player = prior
        conn.websocket.send.reset_mock()
        reply = await _send(conn, "play_story")
        assert reply["state"] == "playing"
        assert reply["error"] == "媒体链接校验失败"
        assert "evil" not in json.dumps(reply)
        assert [t.title for t in conn.mcp_media_player.tracks] == ["旧曲"]

    _run(scenario())
