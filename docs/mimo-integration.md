# Xiaomi MiMo 接入（Python 完整版）

以 [MiMo 官方 API](https://mimo.mi.com/docs/zh-CN/api/chat/openai-api) 及
[ASR](https://mimo.mi.com/docs/zh-CN/api/audio/Speech-Recognition)、
[TTS](https://mimo.mi.com/docs/zh-CN/api/audio/tts) 协议为准。

## 智控台（推荐）

更新并重启 manager-api 后，Liquibase 会新增三个**默认未启用**的模型配置：

- **LLM_MiMoLLM**：OpenAI 兼容 Provider，默认 `mimo-v2.6-flash`
- **ASR_MiMoASR**：专用 MiMo ASR Provider，默认 `mimo-v2.5-asr`
- **TTS_MiMoTTS**：专用 MiMo TTS Provider，默认 `mimo-v2.5-tts`

进入模型配置，分别填写 MiMo API Key，按需启用并关联到智能体。

若只需要文本对话，LLM 不需要新 Provider；如果要全链路语音，
ASR 和 TTS 则必须使用新增的 `mimo` Provider，不可直接替换 OpenAI ASR/TTS 的 URL。

MiMo 使用 `/v1/chat/completions`：ASR 传入 Base64 WAV，
TTS 返回 Base64 WAV。不需要下载本地 FunASR / SenseVoice 模型。

## 无智控台模式

在 `main/xiaozhi-server/data/.config.yaml` 中使用：

```yaml
selected_module:
  ASR: MiMoASR
  LLM: MiMoLLM
  TTS: MiMoTTS

ASR:
  MiMoASR:
    type: mimo
    api_key: 你的MiMo_API_Key
    api_url: https://api.xiaomimimo.com/v1/chat/completions
    model_name: mimo-v2.5-asr
    language: auto
    output_dir: tmp/

LLM:
  MiMoLLM:
    type: openai
    api_key: 你的MiMo_API_Key
    base_url: https://api.xiaomimimo.com/v1
    model_name: mimo-v2.6-flash
    thinking: disabled
    max_tokens: 1024

TTS:
  MiMoTTS:
    type: mimo
    api_key: 你的MiMo_API_Key
    api_url: https://api.xiaomimimo.com/v1/chat/completions
    model: mimo-v2.5-tts
    voice: 冰糖
    voice_prompt: ""
    reference_audio: ""
    output_dir: tmp/
    tts_timeout: 30
```

## 音色选择和声音克隆

预置音色支持 `mimo_default`、`冰糖`、`茉莉`、`苏打`、
`白桦`、`Mia`、`Chloe`、`Milo`、`Dean`。

- **预置音色**：`model: mimo-v2.5-tts`，通过 `voice` 选择；
  可使用 `voice_prompt` 调整表达风格。
- **音色设计**：`model: mimo-v2.5-tts-voicedesign`，
  `voice_prompt` 必填，不传预置音色 ID。
- **音色克隆**：`model: mimo-v2.5-tts-voiceclone`，
  `reference_audio` 填写 Python 服务容器可读的 `.wav`/`.mp3` 路径，
  或填写符合官方格式的 Base64 data URI。使用容器时需先挂载音频样本目录。

当前实现采用 WAV **非流式**合成：可直接接入原有音频播放链路；
小米标准 TTS 已支持低延迟 PCM16 流式接口，但需要另行对接项目
`SINGLE_STREAM` 音频发送路径才能获得真实低延迟。音色克隆与音色设计
官方目前没有提供真正低延迟流式输出。请勿把非流式实现误认为流式优化。

MiMo V2.6 默认开启深度思考；项目通用 LLM 适配器不保存多轮工具调用的
`reasoning_content`，所以对 MiMo 默认发送 `thinking: disabled`。
有完整的 `reasoning_content` 历史透传需求时再单独适配。

## 验证

```bash
cd main/xiaozhi-server
python -m pytest tests/core/test_mimo_providers.py -q
```

测试使用模拟 HTTP 响应，不会访问小米云端；真正的联调需要有效 API Key 和
ESP32 设备。若你使用 Docker，需要同时重新构建 server 和 web 镜像，
分别包含 Python Provider 和管理后台 Liquibase 迁移。
