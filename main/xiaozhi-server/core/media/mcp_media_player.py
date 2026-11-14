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
PAGE_SIZE = 6
MAX_SEEK_MS = 2 * 60 * 60 * 1000


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
        self.position_ms = 0
        self.duration_ms = 0
        self.error = ""
        self._seek_ms = 0
        self.active = False
        self.paused = False
        self.owns_audio = False
        self.sentence_id = None
        self._task = None
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
            self._start_track(0)
        return len(self.tracks)

    def _set_track(self, index, position_ms=0, duration_ms=0):
        self.index = index
        self.position_ms = position_ms
        self.duration_ms = duration_ms
        self._seek_ms = position_ms
        self.error = ""

    def device_state(self, page=None):
        track = self.tracks[self.index] if 0 <= self.index < len(self.tracks) else None
        state = "stopped"
        if self.error:
            state = "error"
        elif self.active:
            state = "paused" if self.paused else "playing"
        position = min(self.position_ms, self.duration_ms) if self.duration_ms else self.position_ms
        result = {
            "state": state,
            "title": self._device_title(track) if track else "",
            "position_ms": position if track else 0,
            "duration_ms": self.duration_ms if track else 0,
            "current_index": self.index if track else -1,
            "playlist_length": len(self.tracks),
        }
        if self.error:
            result["error"] = self.error
        if page is not None:
            last_page = max(0, len(self.tracks) - 1) // PAGE_SIZE * PAGE_SIZE
            page = min(page, last_page)
            result["page_offset"] = page
            result["playlist"] = [
                {"index": i, "title": self._device_title(self.tracks[i])}
                for i in range(page, min(page + PAGE_SIZE, len(self.tracks)))
            ]
        return result

    @staticmethod
    def _device_title(track):
        # Match the firmware's bounded UTF-8 buffer without splitting a glyph.
        return track.title.replace("\x00", "").encode("utf-8")[:192].decode("utf-8", "ignore")

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

    async def _run(self):
        try:
            await send_tts_message(self.conn, "start")
            self.conn.client_is_speaking = True
            while self.active and self.index < len(self.tracks) and not self.conn.stop_event.is_set():
                await self._gate.wait()
                if not self.active:
                    break
                track = self.tracks[self.index]
                await send_display_message(self.conn, f"正在播放：{track.title}")
                await send_tts_message(self.conn, "sentence_start", track.title)
                await self._play_track(track)
                controller = getattr(self.conn, "audio_rate_controller", None)
                if controller:
                    # Only a few frames are queued at any time. Do not hang
                    # forever if the sender task died unexpectedly.
                    await asyncio.wait_for(controller.queue_empty_event.wait(), timeout=30)
                await self._gate.wait()
                self._set_track(self.index + 1)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Do not surface FFmpeg's URL-bearing stderr or opaque token.
            self.conn.logger.warning(f"MCP media playback stopped ({type(exc).__name__})")
            self.error = "媒体播放失败，请重试"
            if self.owns_audio and not self.conn.client_abort:
                await send_display_message(self.conn, "音乐播放失败，请重试")
        finally:
            if asyncio.current_task() is self._task and self.active:
                was_owner = self.owns_audio
                self.active = False
                self.owns_audio = False
                if was_owner and not self.conn.client_abort and not self.conn.stop_event.is_set():
                    await send_tts_message(self.conn, "stop")

    async def _probe_duration(self, url):
        if not shutil.which("ffprobe"):
            return 0
        try:
            process = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "error", "-rw_timeout", "3000000",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", url,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                output, _ = await asyncio.wait_for(process.communicate(), timeout=3)
            finally:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.communicate()
            if process.returncode != 0:
                return 0
            duration = float(output.strip())
            return int(duration * 1000) if math.isfinite(duration) and 0 < duration < 1e9 else 0
        except (OSError, ValueError, asyncio.TimeoutError):
            return 0

    async def _play_track(self, track):
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg executable not found")
        # No shell, no stderr logging (FFmpeg errors may include bearer URLs).
        # Input is trusted MCP's temporary MP3 URL; output is bounded PCM frames.
        sample_rate = self.conn.sample_rate
        frame_bytes = sample_rate * 60 // 1000 * 2
        seek = ["-ss", f"{self._seek_ms / 1000:.3f}"] if self._seek_ms else []
        probe = None
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-rw_timeout", "15000000", "-i", track.url, *seek,
            "-vn", "-f", "s16le", "-acodec", "pcm_s16le",
            "-ac", "1", "-ar", str(sample_rate), "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        encoder = None
        decoded_ms = self._seek_ms
        try:
            encoder = OpusEncoderUtils(sample_rate=sample_rate, channels=1, frame_size_ms=60)
            if getattr(self.conn, "media_control_enabled", False) and not self.duration_ms:
                probe = asyncio.create_task(self._probe_duration(track.url), name="mcp-media-duration")
            while self.active and not self.conn.stop_event.is_set():
                await self._gate.wait()
                try:
                    pcm = await asyncio.wait_for(process.stdout.readexactly(frame_bytes), timeout=20)
                except asyncio.IncompleteReadError as exc:
                    pcm = exc.partial
                    if not pcm:
                        break
                samples = len(pcm) // 2
                pcm += bytes(frame_bytes - len(pcm))
                # The abort handler can pause us while stdout is being read.
                await self._gate.wait()
                if not self.active or not self.owns_audio or self.conn.client_abort:
                    continue
                packet = encoder._encode(encoder._convert_bytes_to_shorts(pcm))
                if packet:
                    await sendAudio(self.conn, packet)
                    controller = getattr(self.conn, "audio_rate_controller", None)
                    decoded_ms += samples * 1000 // sample_rate
                    queued_frames = len(controller.queue) if controller else 0
                    self.position_ms = max(self.position_ms, decoded_ms - queued_frames * 60)
                    if probe is not None and probe.done():
                        self.duration_ms = probe.result()
                        probe = None
                    # Backpressure: never buffer an entire song in RAM.
                    while controller and len(controller.queue) >= 5 and self.active:
                        sender = controller.pending_send_task
                        if self.owns_audio and (sender is None or sender.done()):
                            raise RuntimeError("audio sender stopped")
                        await self._gate.wait()
                        await asyncio.sleep(0.02)
            # If FFmpeg exited because the proxy URL expired or decoding failed,
            # report an error rather than silently advancing to the next track.
            await process.wait()
            if process.returncode != 0 and self.active:
                raise RuntimeError("ffmpeg decoder failed")
        finally:
            if probe is not None:
                probe.cancel()
            if encoder is not None:
                encoder.close()
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            # A killed decoder can still have buffered PCM in its pipe.
            await process.communicate()
            if probe is not None:
                await asyncio.gather(probe, return_exceptions=True)

    async def interrupt(self):
        """Device wake/barge-in: silence audio but preserve the current playlist."""
        if not self.active or self.paused:
            return
        self.paused = True
        self.owns_audio = False
        self._gate.clear()

    def _start_track(self, index, position_ms=0, duration_ms=0, paused=False):
        self._set_track(index, position_ms, duration_ms)
        self.active = True
        self.paused = paused
        if paused:
            self._clear_pending_audio()
            self.owns_audio = False
            self._gate.clear()
        else:
            self._gate.set()
            self._take_audio()
            self._task = asyncio.create_task(self._run(), name="mcp-media-playback")

    async def control_from_device(self, action, index, position_ms):
        async with self._lock:
            if action == "select":
                if not 0 <= index < len(self.tracks):
                    return "无效的曲目序号"
                await self._cancel_current()
                self._start_track(index)
                return ""
            if action == "seek":
                if not self.active or not self.duration_ms:
                    return "当前曲目无法跳转"
                if position_ms >= self.duration_ms or position_ms > MAX_SEEK_MS:
                    return "播放进度超出范围"
                await self._cancel_current()
                self._start_track(self.index, position_ms, self.duration_ms, self.paused)
                return ""
            response = await self._control("previous" if action == "prev" else action)
            return response if response in ("当前没有播放内容", "没有更多歌曲") else ""

    async def control(self, action):
        async with self._lock:
            return await self._control(action)

    async def _control(self, action):
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
            if self._task is None:
                self._gate.set()
                self._task = asyncio.create_task(self._run(), name="mcp-media-playback")
                return "已继续播放"
            await send_tts_message(self.conn, "start")
            self.conn.client_is_speaking = True
            await send_tts_message(self.conn, "sentence_start", self.tracks[self.index].title)
            self._gate.set()
            return "已继续播放"
        if action in ("next", "previous"):
            next_index = self.index + (1 if action == "next" else -1)
            if not 0 <= next_index < len(self.tracks):
                return "没有更多歌曲"
            await self._cancel_current()
            self._start_track(next_index)
            return "正在切换歌曲"
        return "不支持的播放操作"

    async def close(self):
        async with self._lock:
            self.active = False
            self.owns_audio = False
            self._gate.set()
            await self._cancel_current()
            self.tracks.clear()
