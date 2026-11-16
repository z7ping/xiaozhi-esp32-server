# Jellyfin MCP music playback (Python XiaoZhi)

This integration extends the **same PR #1 branch as MiMo support**.
It reuses the existing `z7ping/jellyfin-mcp` branch
[`feature/mcp-audio-stream`](https://github.com/z7ping/jellyfin-mcp/tree/feature/mcp-audio-stream).
Do not deploy the unmodified upstream MCP server for ESP32 playback: its
`jellyfin_play` tool only controls external Jellyfin client sessions.

## Prerequisites

- Jellyfin MCP runs with its optional HTTP MP3 audio proxy enabled (not stdio-only).
- Python XiaoZhi server can connect to the MCP endpoint and the `/audio/` endpoint.
- Python XiaoZhi server has `ffmpeg` installed/in PATH.
- MCP exposes `jellyfin_audio` under the `media` toolset.
- The LLM/agent is configured for `function_call` to use server MCP tools.

Run the MCP process with the same setup as the Go implementation:

```sh
JELLYFIN_URL=http://jellyfin:8096 \
JELLYFIN_API_KEY=YOUR_JELLYFIN_KEY \
jellyfin-mcp --http --addr 0.0.0.0:8080 \
  --http-token YOUR_MCP_TOKEN \
  --audio-public-url http://jellyfin-mcp:8080 \
  --toolsets discovery,media,user,playback
```

Note that `--audio-public-url` must be reachable **from the Python XiaoZhi
container/host**, not the ESP32. Only Jellyfin MCP needs the Jellyfin API Key.

On Python XiaoZhi server create `data/.mcp_server_settings.json`:

```json
{
  "mcpServers": {
    "jellyfin": {
      "url": "http://jellyfin-mcp:8080/mcp",
      "transport": "streamable-http",
      "headers": {"Authorization": "Bearer YOUR_MCP_TOKEN"}
    }
  }
}
```

On the Python XiaoZhi container, set `MCP_MEDIA_ALLOWED_HOSTS=jellyfin-mcp`.
For example in the Compose **server** service:

```yaml
environment:
  MCP_MEDIA_ALLOWED_HOSTS: jellyfin-mcp
```

Use the hostname (without scheme or port) from the MCP `ResourceLink` URL,
not the hostname of the Jellyfin REST service. Multiple hosts are comma
separated. This is required; playback is disabled if unset to prevent
arbitrary SSRF against HTTP addresses provided by tool results.

## Supported voice workflow

1. `jellyfin_search` / `jellyfin_playlists` finds the requested item or playlist.
2. `jellyfin_audio` returns one or more ordered `audio/mpeg` ResourceLinks.
3. Server MCP executor validates them, consumes links without echoing temporary
   bearer tokens to the LLM or logs, and replaces the per-device playlist.
4. An async FFmpeg decoder converts HTTP MP3 to 16/24 kHz 16-bit mono PCM,
   Opus-encodes 60 ms frames and uses the existing XiaoZhi audio sender.
5. `control_mcp_media_playback` with `pause`, `resume`, `next`,
   `previous`, `stop` controls the current ESP32 playlist.
6. A device abort / new wake pauses the current track and releases the speaker;
   resume continues from the paused decoder as long as the connection survives.

The audio is streamed with bounded in-memory buffers, not downloaded in full.
On disconnect, the background process is closed. The feature is restricted to
Jellyfin's own `jellyfin_audio` MCP tool, leaving ordinary MCP calls alone.

## Touch-screen playlist start (听故事 / 听儿歌)

When the device hello declares `media_control`, the server hello also
advertises `media_playlist_start` (`core/handle/helloHandle.py`). The
ES3C28P home screen then sends `{"type":"media","state":"play_story"}` or
`"play_song"` and expects a media state reply, exactly like the touch player
commands.

- Set `XIAOZHI_STORY_PLAYLIST_ID` and `XIAOZHI_SONG_PLAYLIST_ID` on the
  Python XiaoZhi server. Each value is either one Jellyfin playlist id, a
  comma-separated list of ids (played back-to-back in the configured
  order), or the literal `auto`: the server then lists all Jellyfin
  playlists once (`jellyfin_search` with an empty query) and keeps the
  names matching that button's keywords (故事/童话/story or 儿歌/童谣/song),
  so new playlists need no configuration.
- `core/media/device_playlist.py` calls the existing `jellyfin_audio` MCP
  tool for each selected playlist and replaces the device queue starting at
  the saved resume point (or item 0 when there is none); the reply carries
  the real post-start player state.
- Every failure returns a fixed device-safe Chinese message
  (`服务端未配置歌单`, `未找到匹配的歌单`, `读取 Jellyfin 歌单失败`, …);
  raw exception text, MCP payloads and temporary audio URLs are never put
  on the wire.

## Playback progress (resume) and shuffle

- **Resume**: the player persists the current title/index/position to
  `data/playback_progress.json` (keyed by device id, atomic write,
  throttled to one write per 10 s plus track change, pause, stop and
  disconnect). The next `play_story` / `play_song` **or voice playlist
  load** resumes at the saved position when the record is younger than
  7 days and its track title still exists in the queue — matching by
  title keeps resume correct across shuffles and playlist edits;
  otherwise playback starts from the head. Single-item voice plays of the
  same title also resume.
- **随机播放**：点播故事或儿歌默认随机。设备可在 `play_story` 或
  `play_song` 中传入布尔字段 `shuffle`，也可在播放中发送
  `set_shuffle`。切换会保留当前曲目及进度，播放列表显示实际顺序。
  设备未指定时，服务端环境变量 `XIAOZHI_PLAYLIST_SHUFFLE` 可覆盖默认值。
  `1`、`true` 或 `all` 也对语音入口启用随机，`story` 或 `song`
  仅对对应首页入口启用随机，`0` 使用 Jellyfin 顺序。

## Current limitations

- This is server-side streaming; it does **not** add firmware-level wake-word
  or full-duplex acoustic echo cancellation. The device must still be able to
  capture or send the next instruction during playback.
- MP3 transcoding must be available in the Jellyfin backend.
- Expired temporary audio URLs cannot be refreshed transparently. The MCP branch
  issues links with an eight-hour lifetime.
- One device connection has one media playlist; voice playback and music use
  the same speaker, so music is paused while handling a new voice command.

## Tests

```sh
cd main/xiaozhi-server
python -m pytest tests/core/test_mcp_media_playback.py -q
python -m pytest tests/core/test_device_playlist_start.py -q
```

The offline tests do not call Jellyfin or open audio URLs. For hardware
acceptance, test a single MP3, a FLAC-origin track transcoded by Jellyfin,
a multi-song playlist, pause/resume, next/previous, and a wake-word interrupt.
