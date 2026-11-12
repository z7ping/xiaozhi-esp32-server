"""Bounded HTTP audio playback for MCP ResourceLinks.

The MCP server owns Jellyfin authentication. Only temporary links from the
explicitly configured trusted audio host are ever fetched. No media URL is
logged or placed into the LLM dialogue. Each ConnectionHandler has one player.
"""

import asyncio
import math
import os
import queue
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
    duration_ms: int = 0  # Only verified MCP metadata or ffprobe duration


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
    if "/audio/" not in parsed.path or parsed.path.endswith("/audio/"):
        raise ValueError("MCP media URL must target the trusted audio proxy")
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
        self.position_ms = 0  # Encoded frames sent, not a device ACK clock
        self.duration_ms = 0
        self.error = ""
        self._seek_frames = 0
        self._probe_task = None
        self._run_has_started = False

    def owns_sentence(self, sentence_id):
        return self.active and self.owns_audio and self.sentence_id == sentence_id

    def snapshot(self, page_offset=None):
        """Serialize only bounded, safe metadata; never return MCP audio URLs."""
        title = self.tracks[self.index].title if 0 <= self.index < len(self.tracks) else ""
        state = ("error" if self.error and not self.active else
                 "paused" if self.active and self.paused else
                 "playing" if self.active else
                 "stopped" if self.tracks else "idle")
        result = {
            "type": "media", "state": state,
            "title": str(title or "").encode("utf-8")[:180].decode("utf-8", "ignore"),
            "position_ms": max(0, int(self.position_ms)),
            "duration_ms": max(0, int(self.duration_ms)),
            "current_index": self.index if 0 <= self.index < len(self.tracks) else -1,
            "playlist_length": len(self.tracks),
        }
        if self.error:
            # All player errors are fixed messages, never FFmpeg/HTTP diagnostics.
            result["error"] = self.error
        if page_offset is not None:
            offset = min(max(0, page_offset), len(self.tracks))
            result["page_offset"] = offset
            result["playlist"] = [
                {"index": n, "title": str(self.tracks[n].title or "").encode("utf-8")[:180].decode("utf-8", "ignore")}
                for n in range(offset, min(offset + 6, len(self.tracks)))
            ]
        return result

    async def _probe_duration(self, track, index):
        """Read a real MP3 duration without making it a required dependency."""
        if track.duration_ms > 0 or not shutil.which("ffprobe"):
            return
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "error", "-rw_timeout", "5000000",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", track.url,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            data, _ = await asyncio.wait_for(process.communicate(), timeout=8)
            if process.returncode != 0 or len(data) > 64:
                return
            seconds = float(data.strip())
            if not math.isfinite(seconds) or seconds <= 0:
                return
            if (self.active and self.index == index
                    and self.tracks[index] is track):
                self.duration_ms = int(seconds * 1000)
        except (OSError, ValueError, asyncio.TimeoutError):
            return
        finally:
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()

    async def replace(self, tracks):
        if not tracks or len(tracks) > MAX_TRACKS:
            raise ValueError("MCP playlist must contain 1-1000 audio tracks")
        async with self._lock:
            await self._cancel_current()
            self.tracks = list(tracks)
            self.index = 0
            self.position_ms = 0
            self.duration_ms = max(0, self.tracks[0].duration_ms)
            self.error = ""
            self._seek_frames = 0
            self.active = True
            self.paused = False
            self._gate.set()
            self._take_audio()
            self._task = asyncio.create_task(self._run(), name="mcp-media-playback")
        return len(self.tracks)

    def _clear_pending_audio(self):
        # Do not clear report_queue: switching music must not lose tool reports.
        tts = getattr(self.conn, "tts", None)
        if tts:
            for pending in (tts.tts_text_queue, tts.tts_audio_queue):
                while True:
                    try:
                        pending.get_nowait()
                    except queue.Empty:
                        break
        controller = getattr(self.conn, "audio_rate_controller", None)
        if controller:
            controller.reset()
            # reset() cancels the old sender without clearing its task field.
            # The next sendAudio() must install a *new* sender even in the
            # same sentence; otherwise frames remain queued indefinitely.
            controller.pending_send_task = None

    def _take_audio(self):
        self._clear_pending_audio()
        self.conn.client_abort = False
        self.owns_audio = True
        self.sentence_id = self.conn.sentence_id
        # Silence any in-flight TTS frames from the tool-invoking turn.
        self.conn.mcp_media_suppressed_sentence = self.sentence_id

    async def _cancel_current(self):
        self._run_has_started = False
        if self._probe_task is not None:
            self._probe_task.cancel()
            self._probe_task = None
        task = self._task
        self._task = None
        # Cancellation enters _play_track's finally, which closes FFmpeg.
        # Do not kill here too: double-kill races with subprocess teardown.
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._process = None

    async def _run(self):
        try:
            await self._gate.wait()
            if not self.active:
                return
            await send_tts_message(self.conn, "start")
            self._run_has_started = True
            self.conn.client_is_speaking = True
            while self.active and self.index < len(self.tracks) and not self.conn.stop_event.is_set():
                await self._gate.wait()
                if not self.active:
                    break
                track = self.tracks[self.index]
                if self.duration_ms == 0:
                    self.duration_ms = max(0, track.duration_ms)
                if self.duration_ms == 0:
                    self._probe_task = asyncio.create_task(
                        self._probe_duration(track, self.index), name="mcp-media-duration"
                    )
                await send_display_message(self.conn, f"正在播放：{track.title}")
                await send_tts_message(self.conn, "sentence_start", track.title)
                await self._play_track(track)
                if self._probe_task is not None:
                    self._probe_task.cancel()
                    self._probe_task = None
                if self.active:
                    self.index += 1
                    self.position_ms = 0
                    self._seek_frames = 0
                    self.duration_ms = (max(0, self.tracks[self.index].duration_ms)
                                        if self.index < len(self.tracks) else 0)
                controller = getattr(self.conn, "audio_rate_controller", None)
                if controller:
                    # Only a few frames are queued at any time. Do not hang
                    # forever if the sender task died unexpectedly.
                    await asyncio.wait_for(controller.queue_empty_event.wait(), timeout=30)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = "音乐播放失败，请重试"
            # Do not surface FFmpeg's URL-bearing stderr or opaque token.
            self.conn.logger.warning(f"MCP media playback stopped ({type(exc).__name__})")
            if self.owns_audio and not self.conn.client_abort:
                await send_display_message(self.conn, "音乐播放失败，请重试")
        finally:
            if self._process and self._process.returncode is None:
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass
                await self._process.wait()
            self._process = None
            if self._probe_task is not None:
                self._probe_task.cancel()
                self._probe_task = None
            if asyncio.current_task() is self._task and self.active:
                was_owner = self.owns_audio
                self.active = False
                self.owns_audio = False
                if was_owner and not self.conn.client_abort and not self.conn.stop_event.is_set():
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
                if self._seek_frames > 0:
                    self._seek_frames -= 1
                    continue
                packet = encoder._encode(encoder._convert_bytes_to_shorts(pcm))
                if packet:
                    await sendAudio(self.conn, packet)
                    self.position_ms += 60
                    controller = getattr(self.conn, "audio_rate_controller", None)
                    # Backpressure: never buffer an entire song in RAM.
                    while controller and len(controller.queue) >= 5 and self.active:
                        await self._gate.wait()
                        await asyncio.sleep(0.02)
            # If FFmpeg exited because the proxy URL expired or decoding failed,
            # report an error rather than silently advancing to the next track.
            await process.wait()
            if self._seek_frames > 0 and self.active:
                raise RuntimeError("seek beyond actual decoded audio")
            if process.returncode != 0 and self.active:
                raise RuntimeError("ffmpeg decoder failed")
        finally:
            encoder.close()
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
            if self._process is process:
                self._process = None

    async def select(self, index):
        if type(index) is not int or not 0 <= index < len(self.tracks):
            raise ValueError("无效的歌曲索引")
        async with self._lock:
            await self._cancel_current()
            self.index = index
            self.position_ms = 0
            self.duration_ms = max(0, self.tracks[index].duration_ms)
            self.error = ""
            self._seek_frames = 0
            self.active = True
            self.paused = False
            self._gate.set()
            self._take_audio()
            self._task = asyncio.create_task(self._run(), name="mcp-media-playback")

    async def seek(self, position_ms):
        if type(position_ms) is not int or not 0 <= position_ms <= 7200000:
            raise ValueError("跳转位置超出支持范围")
        async with self._lock:
            if not self.active or not 0 <= self.index < len(self.tracks):
                raise ValueError("当前没有播放内容")
            if self.duration_ms <= 0 or position_ms > self.duration_ms:
                raise ValueError("当前曲目尚无可用时长或跳转位置越界")
            paused = self.paused
            await self._cancel_current()
            self.position_ms = position_ms
            self.error = ""
            self._seek_frames = position_ms // 60
            if paused:
                self.owns_audio = False
                self._gate.clear()
            else:
                self._gate.set()
                self._take_audio()
            self._task = asyncio.create_task(self._run(), name="mcp-media-playback")

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
                had_speaker = self.owns_audio
                self.active = False
                self.owns_audio = False
                self._gate.set()
                await self._cancel_current()
                self._clear_pending_audio()
                if had_speaker and not self.conn.client_abort:
                    await send_tts_message(self.conn, "stop")
                return "已停止播放"
            if not self.active:
                # The firmware's play button sends resume after a deliberate stop.
                if action == "resume" and self.tracks:
                    if not 0 <= self.index < len(self.tracks):
                        self.index = 0
                    self.position_ms = 0
                    self.duration_ms = max(0, self.tracks[self.index].duration_ms)
                    self.error = ""
                    self._seek_frames = 0
                    self._run_has_started = False
                    self.active = True
                    self.paused = False
                    self._gate.set()
                    self._take_audio()
                    self._task = asyncio.create_task(self._run(), name="mcp-media-playback")
                    return "开始播放"
                return "当前没有播放内容"
            if action == "pause":
                had_speaker = self.owns_audio
                self.paused = True
                self.owns_audio = False
                self._gate.clear()
                self._clear_pending_audio()
                if had_speaker and not self.conn.client_abort:
                    await send_tts_message(self.conn, "stop")
                return "已暂停播放"
            if action == "resume":
                if not self.paused:
                    return "正在播放"
                self.paused = False
                self._take_audio()
                self._gate.set()
                # When seeking a paused track the replacement _run() has not
                # sent its initial start. Let that task send it exactly once.
                if self._run_has_started:
                    await send_tts_message(self.conn, "start")
                    self.conn.client_is_speaking = True
                    await send_tts_message(self.conn, "sentence_start", self.tracks[self.index].title)
                return "已继续播放"
            if action in ("next", "previous"):
                next_index = self.index + (1 if action == "next" else -1)
                if not 0 <= next_index < len(self.tracks):
                    return "没有更多歌曲"
                await self._cancel_current()
                self.index = next_index
                self.position_ms = 0
                self.duration_ms = max(0, self.tracks[next_index].duration_ms)
                self.error = ""
                self._seek_frames = 0
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
            self.position_ms = 0
            self.duration_ms = 0
            self._seek_frames = 0
            self.error = ""
