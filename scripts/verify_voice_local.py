"""Verify local speech dependencies without a device, database, or LLM request.

The default mode is offline. ``roundtrip`` sends only the fixed test sentence to
Edge TTS and transcribes its returned audio with the local SenseVoice weights.
"""

import argparse
import asyncio
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


TEST_TEXT = "你好小智，这是本地语音测试。"


def normalized(text):
    return "".join(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", text)).lower()


def dependencies(model_root):
    import opuslib_next
    import torch

    subprocess.run(["ffmpeg", "-version"], check=True, stdout=subprocess.DEVNULL)
    model, _ = torch.hub.load(
        repo_or_dir=str(model_root / "snakers4_silero-vad"),
        source="local",
        model="silero_vad",
        force_reload=False,
    )
    probability = float(model(torch.zeros(512), 16000).item())
    if not 0 <= probability <= 1:
        raise RuntimeError("Silero returned an invalid probability")
    encoder = opuslib_next.Encoder(16000, 1, opuslib_next.APPLICATION_AUDIO)
    decoder = opuslib_next.Decoder(16000, 1)
    packet = encoder.encode(bytes(960 * 2), 960)
    decoded = decoder.decode(packet, 960)
    if len(decoded) != 960 * 2:
        raise RuntimeError("Opus encode/decode did not return a 60 ms PCM frame")
    return {
        "torch": torch.__version__,
        "cuda_available": torch.cuda.is_available(),
        "ffmpeg": "passed",
        "silero_silence_probability": probability,
        "opus_roundtrip_pcm_bytes": len(decoded),
    }


def transcribe(audio_path, model_root):
    from funasr import AutoModel

    started = time.monotonic()
    model = AutoModel(
        model=str(model_root / "SenseVoiceSmall"),
        device="cpu",
        disable_update=True,
        hub="hf",
    )
    result = model.generate(
        input=str(audio_path), cache={}, language="auto", use_itn=True,
        batch_size_s=60,
    )
    text = re.sub(r"<\|[^|]+\|>", "", result[0]["text"]).strip()
    if not text:
        raise RuntimeError("SenseVoice returned an empty transcript")
    return {"transcript": text, "seconds": round(time.monotonic() - started, 3)}


async def synthesize(audio_path, proxy):
    import edge_tts

    request = edge_tts.Communicate(
        TEST_TEXT, voice="zh-CN-XiaoxiaoNeural", proxy=proxy,
        connect_timeout=10, receive_timeout=30,
    )
    await asyncio.wait_for(request.save(str(audio_path)), timeout=60)
    if audio_path.stat().st_size < 1000:
        raise RuntimeError("Edge TTS did not return a usable audio file")
    return {"sentence": TEST_TEXT, "audio_bytes": audio_path.stat().st_size}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["dependencies", "asr", "roundtrip"], default="dependencies")
    parser.add_argument("--model-root", type=Path, default=Path("models"))
    parser.add_argument("--output-dir", type=Path, default=Path("tmp/voice-verification"))
    parser.add_argument("--proxy", help="Optional Edge TTS HTTP proxy; never written to report")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "mode": args.mode,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "llm_called": False,
        "device_required": False,
        "paid_model_called": False,
        "status": "failed",
    }
    try:
        report["dependencies"] = dependencies(args.model_root)
        if args.mode == "asr":
            report["asr"] = transcribe(args.model_root / "SenseVoiceSmall/example/zh.mp3", args.model_root)
        elif args.mode == "roundtrip":
            audio_path = args.output_dir / "edge-fixed-sentence.mp3"
            report["tts"] = asyncio.run(synthesize(audio_path, args.proxy))
            report["asr"] = transcribe(audio_path, args.model_root)
            report["transcript_matches"] = normalized(report["asr"]["transcript"]) == normalized(TEST_TEXT)
            if not report["transcript_matches"]:
                raise RuntimeError("Transcript differed from the fixed test sentence")
        report["status"] = "passed"
    except Exception as error:
        # This test never loads service credentials. Redact any proxy URL anyway.
        report["error_type"] = type(error).__name__
        error_text = str(error)
        if args.proxy:
            error_text = error_text.replace(args.proxy, "<proxy>")
        report["error"] = re.sub(r"https?://[^\s'\"]+", "<url>", error_text)[:500]
    report_path = args.output_dir / (args.mode + ".json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
