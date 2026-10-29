"""Xiaomi MiMo V2.5 TTS: preset voice, voice design, and voice clone.

All modes use Chat Completions (not /audio/speech); the returned audio is
base64-encoded WAV. The standard TTS model is the default for ESP32 playback.
"""

import base64
from pathlib import Path

import httpx

from core.providers.tts.base import TTSProviderBase

DEFAULT_URL = "https://api.xiaomimimo.com/v1/chat/completions"
STANDARD = "mimo-v2.5-tts"
DESIGN = "mimo-v2.5-tts-voicedesign"
CLONE = "mimo-v2.5-tts-voiceclone"
MAX_BASE64_BYTES = 10 * 1024 * 1024


def _reference_voice(path_or_data: str) -> str:
    """Encode a locally provisioned reference sample (or reuse a data URI)."""
    if path_or_data.startswith(("data:audio/wav;base64,", "data:audio/mpeg;base64,", "data:audio/mp3;base64,")):
        if len(path_or_data.split(",", 1)[1]) > MAX_BASE64_BYTES:
            raise ValueError("MiMo reference audio exceeds 10 MB")
        return path_or_data

    path = Path(path_or_data)
    if path.suffix.lower() not in (".wav", ".mp3"):
        raise ValueError("MiMo voice clone reference must be .wav or .mp3")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    if len(encoded) > MAX_BASE64_BYTES:
        raise ValueError("MiMo reference audio exceeds 10 MB")
    mime = "audio/wav" if path.suffix.lower() == ".wav" else "audio/mpeg"
    return f"data:{mime};base64,{encoded}"


class TTSProvider(TTSProviderBase):
    TTS_PARAM_CONFIG = []

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.api_key = config.get("api_key")
        if not self.api_key:
            raise ValueError("MiMo TTS requires api_key")
        self.api_url = config.get("api_url") or DEFAULT_URL
        self.model = config.get("model") or STANDARD
        if self.model not in (STANDARD, DESIGN, CLONE):
            raise ValueError(f"Unsupported MiMo TTS model: {self.model}")
        self.voice = config.get("private_voice") or config.get("voice") or "mimo_default"
        self.voice_prompt = config.get("voice_prompt") or ""
        self.reference_audio = config.get("reference_audio") or ""
        self.audio_file_type = "wav"

    def _request_data(self, text):
        messages = []
        if self.model == DESIGN:
            if not self.voice_prompt:
                raise ValueError("MiMo voice design requires voice_prompt")
            messages.append({"role": "user", "content": self.voice_prompt})
        elif self.voice_prompt:
            messages.append({"role": "user", "content": self.voice_prompt})
        elif self.model == CLONE:
            messages.append({"role": "user", "content": ""})
        messages.append({"role": "assistant", "content": text})

        audio = {"format": "wav"}
        if self.model == STANDARD:
            audio["voice"] = self.voice
        elif self.model == CLONE:
            if not self.reference_audio:
                raise ValueError("MiMo voice clone requires reference_audio")
            audio["voice"] = _reference_voice(self.reference_audio)
        # Voice design generates the voice from the user instruction; no voice ID.
        return {"model": self.model, "messages": messages, "audio": audio}

    async def text_to_speak(self, text, output_file):
        payload = self._request_data(text)
        async with httpx.AsyncClient(timeout=self.tts_timeout) as client:
            response = await client.post(
                self.api_url,
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            response.raise_for_status()
            body = response.json()
        data = body["choices"][0]["message"]["audio"]["data"]
        audio_bytes = base64.b64decode(data, validate=True)
        if not audio_bytes:
            raise ValueError("Empty MiMo TTS audio response")
        if output_file:
            out = Path(output_file)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(audio_bytes)
        else:
            return audio_bytes
