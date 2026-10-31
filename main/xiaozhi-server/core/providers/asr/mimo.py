"""Xiaomi MiMo V2.5 ASR via the official Chat Completions audio input protocol."""

import base64
import os

import httpx

from config.logger import setup_logging
from core.providers.asr.base import ASRProviderBase
from core.providers.asr.dto.dto import InterfaceType

logger = setup_logging()
TAG = __name__

DEFAULT_URL = "https://api.xiaomimimo.com/v1/chat/completions"
MAX_BASE64_BYTES = 10 * 1024 * 1024


class ASRProvider(ASRProviderBase):
    def __init__(self, config: dict, delete_audio_file: bool):
        super().__init__()
        self.interface_type = InterfaceType.NON_STREAM
        self.api_key = config.get("api_key")
        if not self.api_key:
            raise ValueError("MiMo ASR requires api_key")
        self.api_url = config.get("api_url") or DEFAULT_URL
        self.model = config.get("model_name") or "mimo-v2.5-asr"
        self.language = config.get("language") or "auto"
        self.timeout = float(config.get("timeout") or 30)
        self.output_dir = config.get("output_dir") or "tmp/"
        self.delete_audio_file = delete_audio_file
        os.makedirs(self.output_dir, exist_ok=True)

    def requires_file(self) -> bool:
        return True

    def prefers_temp_file(self) -> bool:
        return True

    async def speech_to_text(self, opus_data, session_id: str, artifacts=None):
        # Base ASR wrapper generates 16 kHz mono WAV from the decoded PCM.
        if artifacts is None or not artifacts.temp_path:
            return "", None
        try:
            with open(artifacts.temp_path, "rb") as file:
                encoded = base64.b64encode(file.read()).decode("ascii")
            if len(encoded) > MAX_BASE64_BYTES:
                raise ValueError("MiMo ASR base64 audio exceeds 10 MB")

            payload = {
                "model": self.model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "data": "data:audio/wav;base64," + encoded,
                                },
                            }
                        ],
                    }
                ],
                "asr_options": {"language": self.language},
            }
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self.api_url,
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                response.raise_for_status()
                result = response.json()
            message = result["choices"][0]["message"]
            text = message.get("content") or ""
            if isinstance(text, list):
                text = "".join(
                    part.get("text", "") for part in text if isinstance(part, dict)
                )
            return str(text).strip(), artifacts.file_path
        except Exception as exc:
            logger.bind(tag=TAG).error(f"MiMo ASR failed: {exc}")
            return "", artifacts.file_path
