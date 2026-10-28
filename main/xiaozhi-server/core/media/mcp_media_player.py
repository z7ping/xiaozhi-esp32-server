"""Bounded HTTP audio playback for MCP ResourceLinks.

The MCP server owns Jellyfin authentication. Only temporary links from the
explicitly configured trusted audio host are ever fetched. No media URL is
logged or placed into the LLM dialogue. Each ConnectionHandler has one player.
"""

import asyncio
import ipaddress
import os
import shutil
from dataclasses import dataclass
from urllib.parse import urlsplit

from core.handle.sendAudioHandle import sendAudio, send_tts_message, send_display_message
from core.utils.opus_encoder_utils import OpusEncoderUtils

MAX_TRACKS = 1000
MAX_URL_LENGTH = 4096


@dataclass(frozen=True)
class MediaTrack:
    title: str
    url: str


def validate_audio_link(uri, mime_type, allowed_hosts):
    """Only accept audio/mpeg HTTP links on a configured trusted host."""
    url = str(uri)
    if not isinstance(mime_type, str) or mime_type.lower().split(";", 1)[0] != "audio/mpeg":
        raise ValueError("MCP media playback requires audio/mpeg ResourceLinks")
    if not url or len(url) > MAX_URL_LENGTH or any(ch in url for ch in "\r\n\t"):
        raise ValueError("Invalid MCP media URL")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Invalid MCP media URL")
    if parsed.hostname.lower().rstrip(".") not in allowed_hosts:
        raise ValueError("Untrusted MCP media host (set MCP_MEDIA_ALLOWED_HOSTS)")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Invalid MCP media port")
    return url


def trusted_hosts():
    """No implicit trust in arbitrary endpoints returned by an LLM/MCP tool."""
    return {v.strip().lower().rstrip(".") for v in
            os.getenv("MCP_MEDIA_ALLOWED_HOSTS", "").split(",") if v.strip()}


class MCPMediaPlayer:
    def __init__(self, conn):
        self.conn = conn
        self.tracks = []
        self.index = 0
        self.active = False
        self.paused = False
        self.owns_audio = False
        self.sentence_id = None
        self._task = None
        self._process = None
        self._gate = asyncio.Event()
        self._gate.set()
        self._lock = asyncio.Lock()

    def owns_sentence(self, sentence_id):
        return self.active and self.owns_audio and self.sentence_id == sentence_id

    async def replace(self, tracks):
        if not tracks or len(tracks) > MAX_TRACKS:
            raise ValueError("MCP playlist must contain 1-1000 audio tracks")
        async with self._lock:
            await self._cancel_current()
            self.tracks = list(tracks)
            self.index = 0
            self.active = True
            self.paused = False
            self._gate.set()
            self._take_audio()
            self._task = asyncio.create_task(self._run(), name="mcp-media-playback")
        return len(self.tracks)

    def _take_audio(self):
        self.conn.clear_queues()
        self.conn.client_abort = False
        self.owns_audio = True
        self.sentence_id = self.conn.sentence_id

    async def _cancel_current(self):
        task = self._task
        self._task = None
        if self._process and self._process.returncode is None:
            self._process.kill()
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._process = None

    async def _run(self):
        try:
            await send_tts_message(self.conn, "start")
            while self.active and self.index < len(self.tracks) and not self.conn.stop_event.is_set():
                track = self.tracks[self.index]
                await send_display_message(self.conn, f"正在播放：{track.title}")
                await self._play_track(track)
                if self.active:
                    self.index += 1
                controller = getattr(self.conn, "audio_rate_controller", None)
                if controller:
                    await controller.queue_empty_event.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Do not surface FFmpeg's URL-bearing stderr or opaque token.
            self.conn.logger.warning(f"MCP media playback stopped ({type(exc).__name__})")
        finally:
            if self._process and self._process.returncode is None:
                self._process.kill()
                await self._process.wait()
            self._process = None
            if self.active and self.owns_audio:
                self.active = False
                self.owns_audio = False
                if not self.conn.client_abort and not self.conn.stop_event.is_set():
                    await send_tts_message(self.conn, "stop")

    async def _play_track(self, track):
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg executable not found")
        # No shell, no stderr logging (FFmpeg errors may include bearer URLs).
        # Input is trusted MCP's temporary MP3 URL; output is bounded PCM frames.
        sample_rate = self.conn.sample_rate
        frame_bytes = sample_rate * 60 // 1000 * 2
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-rw_timeout", "15000000", "-i", track.url,
            "-vn", "-f", "s16le", "-acodec", "pcm_s16le",
            "-ac", "1", "-ar", str(sample_rate), "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        self._process = process
        encoder = OpusEncoderUtils(sample_rate=sample_rate, channels=1, frame_size_ms=60)
        try:
            while self.active and not self.conn.stop_event.is_set():
                await self._gate.wait()
                try:
                    pcm = await process.stdout.readexactly(frame_bytes)
                except asyncio.IncompleteReadError as exc:
                    pcm = exc.partial
                    if not pcm:
                        break
                    pcm += bytes(frame_bytes - len(pcm))
                # The abort handler can pause us while stdout is being read.
                await self._gate.wait()
                if not self.active or not self.owns_audio or self.conn.client_abort:
                    continue
                packet = encoder._encode(encoder._convert_bytes_to_shorts(pcm))
                if packet:
                    await sendAudio(self.conn, packet)
                    controller = getattr(self.conn, "audio_rate_controller", None)
                    # Backpressure: never buffer an entire song in RAM.
                    while controller and len(controller.queue) >= 5 and self.active:
                        await self._gate.wait()
                        await asyncio.sleep(0.02)
            if process.returncode not in (None, 0) and self.active:
                raise RuntimeError("ffmpeg decoder failed")
        finally:
            encoder.close()
            if process.returncode is None:
                process.kill()
            await process.wait()
            if self._process is process:
                self._process = None

    async def interrupt(self):
        """Device wake/barge-in: silence audio but preserve the current playlist."""
        if not self.active or self.paused:
            return
        self.paused = True
        self.owns_audio = False
        self._gate.clear()

    async def control(self, action):
        async with self._lock:
            if action == "stop":
                if not self.active:
                    return "当前没有播放内容"
                self.active = False
                self.owns_audio = False
                self._gate.set()
                await self._cancel_current()
                self.conn.clear_queues()
                return "已停止播放"
            if not self.active:
                return "当前没有播放内容"
            if action == "pause":
                self.paused = True
                self.owns_audio = False
                self._gate.clear()
                self.conn.clear_queues()
                return "已暂停播放"
            if action == "resume":
                if not self.paused:
                    return "正在播放"
                self.paused = False
                self._take_audio()
                self._gate.set()
                await send_tts_message(self.conn, "start")
                return "已继续播放"
            if action in ("next", "previous"):
                next_index = self.index + (1 if action == "next" else -1)
                if not 0 <= next_index < len(self.tracks):
                    return "没有更多歌曲"
                await self._cancel_current()
                self.index = next_index
                self.paused = False
                self._gate.set()
                self._take_audio()
                self._task = asyncio.create_task(self._run(), name="mcp-media-playback")
                return "正在切换歌曲"
            return "不支持的播放操作"

    async def close(self):
        async with self._lock:
            self.active = False
            self.owns_audio = False
            self._gate.set()
            await self._cancel_current()
            self.tracks.clear()
