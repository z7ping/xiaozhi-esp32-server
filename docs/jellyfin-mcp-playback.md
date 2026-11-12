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

## Current limitations

- This is server-side streaming; it does **not** add firmware-level wake-word
  or full-duplex acoustic echo cancellation. The device must still be able to
  capture or send the next instruction during playback.
- Seeking and persisted playlists across device reconnections are not included.
- MP3 transcoding must be available in the Jellyfin backend.
- Expired temporary audio URLs cannot be refreshed transparently. The MCP branch
  issues links with an eight-hour lifetime.
- One device connection has one media playlist; voice playback and music use
  the same speaker, so music is paused while handling a new voice command.

## Tests

```sh
cd main/xiaozhi-server
python -m pytest tests/core/test_mcp_media_playback.py -q
```

The offline tests do not call Jellyfin or open audio URLs. For hardware
acceptance, test a single MP3, a FLAC-origin track transcoded by Jellyfin,
a multi-song playlist, pause/resume, next/previous, and a wake-word interrupt.
