# Local speech verification

`verify_voice_local.py` checks the CPU speech stack independently of the model
API, database, MQTT broker, and physical hardware. Run from the AI server root
inside its built image with the existing `models` directory mounted read only.

```sh
python scripts/verify_voice_local.py --mode dependencies
python scripts/verify_voice_local.py --mode asr
python scripts/verify_voice_local.py --mode roundtrip
```

- `dependencies`: offline FFmpeg, local Silero VAD, and Opus encode/decode.
- `asr`: the same checks plus CPU SenseVoice transcription of the bundled
  `models/SenseVoiceSmall/example/zh.mp3` sample. It does not contact a model API.
- `roundtrip`: synthesize the fixed public test sentence with Edge TTS, then
  transcribe the returned audio locally. Normalized text must match exactly.
  This mode needs internet access but never calls an LLM or loads an API key.

Reports and test audio are written under `tmp/voice-verification` (ignored by
Git). Override with `--output-dir` to retain them in a mounted local directory.
An optional `--proxy http://host.docker.internal:7897` routes only the Edge TTS
request through an existing host proxy. Do not use `127.0.0.1` for a host proxy
from inside a container.

Passing these checks confirms the speech dependencies, not device binding,
LLM responses, microphone input, speaker output, or the hardware network path.
The AI process in manager API mode initially loads only VAD and ASR. A real
device session obtains its LLM and TTS from its assigned backend agent.

References: [Edge TTS upstream](https://github.com/rany2/edge-tts) and
[SenseVoice upstream](https://github.com/QwenAudio/SenseVoice).
