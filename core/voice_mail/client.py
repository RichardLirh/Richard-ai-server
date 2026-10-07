"""Small private-network client; never returns credentials or response bodies in errors."""
import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib import error, request
from urllib.parse import urlsplit


DEVICE_MAC = "34:cd:b0:0b:7a:5c"


class VoiceMailError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    api_url: str = "http://task-api:8010"
    token_file: str = ""
    tail_guard: float = 2.0
    poll_seconds: float = 3.0
    allowlist_file: str = ""

    @classmethod
    def load(cls, config=None, environ=None):
        env = os.environ if environ is None else environ
        cfg = (config or {}).get("voice_mail", {})
        enabled = str(env.get("RAIOT_VOICE_MAIL_ENABLED", cfg.get("enabled", False))).lower() in {"1", "true"}
        if not enabled:
            return cls()
        return cls(True,
                   str(env.get("RAIOT_VOICE_MAIL_API_URL", cfg.get("api_url", cls.api_url))).rstrip("/"),
                   str(env.get("RAIOT_VOICE_MAIL_TOKEN_FILE", cfg.get("token_file", ""))),
                   max(2.0, min(10.0, float(env.get("RAIOT_VOICE_MAIL_TAIL_GUARD_SECONDS", cfg.get("tail_guard", 2))))),
                   max(1.0, min(30.0, float(env.get("RAIOT_VOICE_MAIL_POLL_SECONDS", cfg.get("poll_seconds", 3))))),
                   str(env.get("RAIOT_VOICE_MAIL_DEVICE_ALLOWLIST_FILE", cfg.get("allowlist_file", ""))))

    def allows_device(self, device_id):
        normalized = str(device_id).lower().replace("_", ":")
        if normalized != DEVICE_MAC:
            return False
        try:
            values = json.loads(Path(self.allowlist_file).read_text(encoding="utf-8-sig"))
            return isinstance(values, list) and normalized in {str(v).lower().replace("_", ":") for v in values}
        except (OSError, ValueError):
            return False


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise VoiceMailError("语音邮件接口拒绝重定向")


class VoiceClient:
    def __init__(self, settings):
        parsed = urlsplit(settings.api_url)
        if (parsed.scheme != "http" or parsed.hostname not in {"task-api", "localhost", "127.0.0.1"}
                or parsed.port != 8010 or parsed.path not in {"", "/"}
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise VoiceMailError("语音邮件接口必须为本地任务服务的 8010 端口")
        try:
            raw = Path(settings.token_file).read_text(encoding="utf-8-sig").strip()
            token = json.loads(raw).get("voice", "") if raw.startswith("{") else raw
            if not isinstance(token, str) or len(token) < 32 or any(c.isspace() for c in token):
                raise ValueError()
        except (OSError, ValueError, AttributeError):
            raise VoiceMailError("语音邮件受限令牌文件不可用") from None
        self._token = token
        self._base = settings.api_url.rstrip("/")
        self._opener = request.build_opener(request.ProxyHandler({}), NoRedirect())

    async def post(self, path, payload):
        if not path.startswith("/internal/voice/") or ".." in path or "?" in path:
            raise VoiceMailError("语音邮件接口路径无效")
        return await asyncio.to_thread(self._post, path, payload)

    def _post(self, path, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(body) > 32768:
            raise VoiceMailError("语音邮件请求过长")
        req = request.Request(self._base + path, data=body, method="POST",
                              headers={"Content-Type": "application/json", "Authorization": "Bearer " + self._token})
        try:
            with self._opener.open(req, timeout=6) as response:
                data = response.read(131073)
                if len(data) > 131072:
                    raise VoiceMailError("语音邮件响应过长")
                value = json.loads(data)
                if not isinstance(value, dict):
                    raise VoiceMailError("语音邮件响应格式无效")
                return value
        except error.HTTPError as exc:
            raise VoiceMailError("语音邮件状态已变化，请重新查询或复述" if exc.code == 409
                                 else "语音邮件接口暂不可用") from None
        except (error.URLError, TimeoutError, OSError, ValueError):
            raise VoiceMailError("语音邮件接口暂不可用") from None
