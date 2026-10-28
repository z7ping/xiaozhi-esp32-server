"""Offline request-format tests for Xiaomi MiMo ASR/TTS providers."""
import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import patch

from core.providers.asr.mimo import ASRProvider
from core.providers.tts.mimo import TTSProvider, _reference_voice


class StubResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class StubClient:
    def __init__(self, body, captured, **kwargs):
        self.body = body
        self.captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def post(self, url, **kwargs):
        self.captured.append((url, kwargs))
        return StubResponse(self.body)


def test_mimo_asr_uses_input_audio_data_url(tmp_path):
    audio = tmp_path / "input.wav"
    audio.write_bytes(b"RIFFexample")
    provider = ASRProvider(
        {"api_key": "fake", "output_dir": str(tmp_path)}, True
    )
    captured = []
    body = {"choices": [{"message": {"content": "你好，小智。"}}]}
    with patch(
        "core.providers.asr.mimo.httpx.AsyncClient",
        side_effect=lambda **kwargs: StubClient(body, captured),
    ):
        text, path = asyncio.run(
            provider.speech_to_text(
                [], "session",
                SimpleNamespace(temp_path=str(audio), file_path=None),
            )
        )
    assert text == "你好，小智。"
    assert path is None
    url, kwargs = captured[0]
    assert url.endswith("/v1/chat/completions")
    assert kwargs["json"]["model"] == "mimo-v2.5-asr"
    assert kwargs["json"]["messages"][0]["content"][0]["input_audio"]["data"].startswith(
        "data:audio/wav;base64,"
    )


def test_mimo_tts_standard_voice_and_audio_decoding():
    provider = TTSProvider({"api_key": "fake", "voice": "冰糖"}, True)
    captured = []
    wav = b"RIFFfakewav"
    body = {"choices": [{"message": {"audio": {"data": base64.b64encode(wav).decode()}}}]}
    with patch(
        "core.providers.tts.mimo.httpx.AsyncClient",
        side_effect=lambda **kwargs: StubClient(body, captured),
    ):
        result = asyncio.run(provider.text_to_speak("你好", None))
    assert result == wav
    request = captured[0][1]["json"]
    assert request["model"] == "mimo-v2.5-tts"
    assert request["audio"] == {"format": "wav", "voice": "冰糖"}
    assert request["messages"][-1] == {"role": "assistant", "content": "你好"}


def test_mimo_tts_voice_design_does_not_send_voice_id():
    provider = TTSProvider(
        {
            "api_key": "fake",
            "model": "mimo-v2.5-tts-voicedesign",
            "voice_prompt": "温柔的女声",
        },
        True,
    )
    request = provider._request_data("早上好")
    assert request["messages"][0] == {"role": "user", "content": "温柔的女声"}
    assert "voice" not in request["audio"]


def test_mimo_tts_voice_clone_encodes_local_reference(tmp_path):
    sample = tmp_path / "voice.wav"
    sample.write_bytes(b"RIFFfakewav")
    provider = TTSProvider(
        {
            "api_key": "fake",
            "model": "mimo-v2.5-tts-voiceclone",
            "reference_audio": str(sample),
        },
        True,
    )
    request = provider._request_data("早上好")
    assert request["audio"]["voice"].startswith("data:audio/wav;base64,")
    assert _reference_voice(request["audio"]["voice"]) == request["audio"]["voice"]


def test_mimo_llm_maps_max_tokens_for_regular_and_tool_calls():
    from core.providers.llm.openai.openai import LLMProvider

    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return []

    provider = object.__new__(LLMProvider)
    provider.model_name = "mimo-v2.6-flash"
    provider.base_url = "https://api.xiaomimimo.com/v1"
    provider.thinking_mode = "disabled"
    provider.max_tokens = 300
    provider.temperature = None
    provider.top_p = None
    provider.frequency_penalty = None
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    assert list(provider.response("s", [{"role": "user", "content": "hi"}])) == []
    assert list(provider.response_with_functions(
        "s", [{"role": "user", "content": "hi"}], functions=[]
    )) == []
    assert len(calls) == 2
    for request in calls:
        assert request["max_completion_tokens"] == 300
        assert "max_tokens" not in request
        assert request["extra_body"]["thinking"] == {"type": "disabled"}
