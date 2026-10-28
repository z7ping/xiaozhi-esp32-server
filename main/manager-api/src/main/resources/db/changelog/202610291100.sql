-- Register Xiaomi MiMo models without overwriting existing administrator settings.
-- LLM uses the existing OpenAI provider; ASR/TTS use the new mimo provider.
INSERT IGNORE INTO ai_model_provider
(id, model_type, provider_code, name, fields, sort, creator, create_date, updater, update_date)
VALUES
('SYSTEM_ASR_MiMoASR', 'ASR', 'mimo', '小米 MiMo ASR', '[{"key":"api_key","label":"MiMo API Key","type":"password"},{"key":"api_url","label":"API 地址","type":"string"},{"key":"model_name","label":"模型 ID","type":"string"},{"key":"language","label":"识别语种 auto/zh/en","type":"string"},{"key":"timeout","label":"超时(秒)","type":"number"},{"key":"output_dir","label":"输出目录","type":"string"}]', 30, 1, NOW(), 1, NOW()),
('SYSTEM_TTS_MiMoTTS', 'TTS', 'mimo', '小米 MiMo TTS', '[{"key":"api_key","label":"MiMo API Key","type":"password"},{"key":"api_url","label":"API 地址","type":"string"},{"key":"model","label":"TTS 模型","type":"select","options":["mimo-v2.5-tts","mimo-v2.5-tts-voicedesign","mimo-v2.5-tts-voiceclone"]},{"key":"voice","label":"预置音色","type":"select","options":["mimo_default","冰糖","茉莉","苏打","白桦","Mia","Chloe","Milo","Dean"]},{"key":"voice_prompt","label":"语音风格/音色设计描述","type":"string"},{"key":"reference_audio","label":"克隆样本服务器本地路径或 data URI","type":"string"},{"key":"output_dir","label":"输出目录","type":"string"},{"key":"tts_timeout","label":"超时(秒)","type":"number"}]', 30, 1, NOW(), 1, NOW());

INSERT IGNORE INTO ai_model_config
VALUES
('ASR_MiMoASR', 'ASR', 'MiMoASR', '小米 MiMo 语音识别', 0, 1,
 '{"type":"mimo","api_key":"","api_url":"https://api.xiaomimimo.com/v1/chat/completions","model_name":"mimo-v2.5-asr","language":"auto","output_dir":"tmp/"}',
 'https://mimo.mi.com/docs/zh-CN/quick-start/usage-guide/multimodal-understanding/Speech-Recognition',
 '使用小米 MiMo API Key，不需要本地 FunASR 模型。', 30, NULL, NULL, NULL, NULL),
('LLM_MiMoLLM', 'LLM', 'MiMoLLM', '小米 MiMo 大模型', 0, 1,
 '{"type":"openai","api_key":"","base_url":"https://api.xiaomimimo.com/v1","model_name":"mimo-v2.6-flash","thinking":"disabled","max_tokens":1024}',
 'https://mimo.mi.com/docs/zh-CN/api/chat/openai-api',
 '默认关闭深度思考，确保 MCP 工具调用多轮历史兼容；启用深度思考需额外适配 reasoning_content 历史回传。', 30, NULL, NULL, NULL, NULL),
('TTS_MiMoTTS', 'TTS', 'MiMoTTS', '小米 MiMo 语音合成', 0, 1,
 '{"type":"mimo","api_key":"","api_url":"https://api.xiaomimimo.com/v1/chat/completions","model":"mimo-v2.5-tts","voice":"冰糖","voice_prompt":"","reference_audio":"","output_dir":"tmp/","tts_timeout":30}',
 'https://mimo.mi.com/docs/zh-CN/quick-start/usage-guide/audio/speech-synthesis-v2.5',
 '预置音色有 冰糖/茉莉/苏打/白桦/Mia/Chloe/Milo/Dean；音色设计需填写 voice_prompt；声音克隆需 reference_audio 路径或 data URI。', 30, NULL, NULL, NULL, NULL);
