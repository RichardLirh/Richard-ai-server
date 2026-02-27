import os
import uuid
from datetime import datetime

import edge_tts

from core.providers.tts.base import TTSProviderBase


def _coerce_optional_int(value, default_value):
    if value is None:
        return default_value

    text = str(value).strip()
    if not text:
        return default_value

    try:
        return int(text)
    except (TypeError, ValueError):
        return default_value


def _first_non_empty(*values):
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        if config.get("private_voice"):
            self.voice = config.get("private_voice")
        else:
            self.voice = config.get("voice")

        self.audio_file_type = config.get("format", "mp3")
        self.proxy = _first_non_empty(
            config.get("proxy"),
            config.get("https_proxy"),
            config.get("http_proxy"),
            os.getenv("EDGE_TTS_PROXY"),
            os.getenv("HTTPS_PROXY"),
            os.getenv("HTTP_PROXY"),
            os.getenv("ALL_PROXY"),
            os.getenv("https_proxy"),
            os.getenv("http_proxy"),
            os.getenv("all_proxy"),
        )
        self.connect_timeout = _coerce_optional_int(
            config.get("connect_timeout"), 10
        )
        self.receive_timeout = _coerce_optional_int(
            config.get("receive_timeout"), 60
        )

    def generate_filename(self, extension=".mp3"):
        return os.path.join(
            self.output_file,
            f"tts-{datetime.now().date()}@{uuid.uuid4().hex}{extension}",
        )

    async def text_to_speak(self, text, output_file):
        try:
            communicate_kwargs = {
                "voice": self.voice,
                "connect_timeout": self.connect_timeout,
                "receive_timeout": self.receive_timeout,
            }
            if self.proxy:
                communicate_kwargs["proxy"] = self.proxy

            try:
                communicate = edge_tts.Communicate(text, **communicate_kwargs)
            except TypeError:
                # Backward compatibility for older edge-tts versions.
                communicate = edge_tts.Communicate(text, voice=self.voice)

            if output_file:
                # Ensure the directory exists and create an empty file first.
                os.makedirs(os.path.dirname(output_file), exist_ok=True)
                with open(output_file, "wb"):
                    pass

                # Append streamed audio chunks to avoid accidental overwrite.
                with open(output_file, "ab") as f:
                    async for chunk in communicate.stream():
                        if chunk["type"] == "audio":
                            f.write(chunk["data"])
            else:
                audio_bytes = b""
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        audio_bytes += chunk["data"]
                return audio_bytes
        except Exception as e:
            error_msg = f"Edge TTS request failed: {e}"
            raise Exception(error_msg)
