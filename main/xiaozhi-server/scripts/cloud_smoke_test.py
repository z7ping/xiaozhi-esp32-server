"""No-network import and CPU ONNX VAD smoke check for Dockerfile.cloud.

This image can be built on main before the MiMo/Jellyfin feature PRs merge.
Their adapters are checked only when present in the source tree.
"""
import sys
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

required = (
    "app",
    "core.websocket_server",
    "core.providers.vad.silero",
    "core.providers.asr.openai",
    "core.providers.llm.openai.openai",
    "core.providers.tts.openai",
    "core.providers.tts.edge",
    "core.providers.tools.server_mcp.mcp_executor",
)
for name in required:
    import_module(name)

for optional in ("core.providers.asr.mimo", "core.providers.tts.mimo"):
    if find_spec(optional) is not None:
        import_module(optional)

from plugins_func.loadplugins import auto_import_modules
auto_import_modules("plugins_func.functions")

model = Path("models/snakers4_silero-vad/src/silero_vad/data/silero_vad.onnx")
if not model.is_file():
    raise RuntimeError(f"ONNX VAD model not found: {model}")

import onnxruntime as ort
session = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
if "CPUExecutionProvider" not in session.get_providers():
    raise RuntimeError("CPUExecutionProvider not available")

for package in ("torch", "funasr", "modelscope", "transformers", "sherpa_onnx", "vosk"):
    if find_spec(package) is not None:
        raise RuntimeError(f"Unexpected heavyweight local-model dependency: {package}")

print("Cloud-only import and ONNX VAD smoke check passed")
