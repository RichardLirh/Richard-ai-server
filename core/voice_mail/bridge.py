"""Per-device voice state, trusted microphone events and transport-only TTS receipts.

Only accept_microphone_final can approve. Model tools are deliberately limited
to create/edit/status/cancel. Persistence and the final submission permit live
in Richard-im, not in conversation history or model output.
"""
import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from .client import DEVICE_MAC, Settings, VoiceClient, VoiceMailError


def iso_now(timestamp=None):
    return datetime.fromtimestamp(time.time() if timestamp is None else timestamp, timezone.utc).isoformat()


def plain_text(text):
    if isinstance(text, dict):
        return str(text.get("content", ""))
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return str(value.get("content", ""))
    except (ValueError, TypeError):
        pass
    return str(text)


def is_confirmation(text):
    return bool(re.fullmatch(r"\s*确认发送[。.!！?？]*\s*", text))


def is_cancel(text):
    return bool(re.fullmatch(r"\s*(取消邮件|取消发送|取消这封邮件)[。.!！?？]*\s*", text))


@dataclass(frozen=True)
class Capture:
    id: str
    started_at: str
    started_monotonic: float
    frames: int
    generation: str


class ConnectionRegistry:
    def __init__(self):
        self.connections = {}

    def add(self, bridge):
        old = self.connections.get(bridge.device_id)
        if old and old is not bridge:
            old.retire()
        self.connections[bridge.device_id] = bridge

    def remove(self, bridge):
        if self.connections.get(bridge.device_id) is bridge:
            del self.connections[bridge.device_id]

    def current(self, bridge):
        return self.connections.get(bridge.device_id) is bridge


REGISTRY = ConnectionRegistry()
EDITABLE_STATES = {"draft", "readback", "awaiting_confirmation"}


class VoiceMailBridge:
    def __init__(self, conn, settings, client, registry=REGISTRY, clock=time.time, monotonic=time.monotonic):
        self.conn, self.settings, self.client = conn, settings, client
        self.registry, self.clock, self.monotonic = registry, clock, monotonic
        self.device_id = DEVICE_MAC
        self.generation = uuid.uuid4().hex
        self.context = {"device_id": self.device_id, "conversation_id": conn.session_id,
                        "connection_generation": self.generation}
        self.closed = False
        self.capture = None
        self.capture_frames = 0
        self.latest_utterance = None
        self._issued_captures = {}
        self.draft = None
        self.readback = None
        self.readback_seen = None
        self.block_until = 0.0
        self.speaking_id = None
        self.speech_future = None
        self.normal_outputs = set()
        self.poll_task = None
        self.readback_task = None
        self.speech_lock = asyncio.Lock()
        self.operation_lock = asyncio.Lock()
        self._readback_abort = False
        self.audio_frames_sent = 0

    def live(self):
        return not self.closed and not self.conn.stop_event.is_set() and self.registry.current(self)

    def start(self):
        self.registry.add(self)
        self.poll_task = asyncio.create_task(self._poll(), name="voice-mail-outbox")

    def retire(self):
        self.closed = True
        self.capture = None
        self.readback = None
        if self.speech_future and not self.speech_future.done():
            self.speech_future.set_result(False)
        for task in (self.poll_task, self.readback_task):
            if task and not task.done():
                task.cancel()
        self.registry.remove(self)

    async def close(self):
        tasks = [t for t in (self.poll_task, self.readback_task) if t and t is not asyncio.current_task()]
        self.retire()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def begin_capture(self, timestamp=None, monotonic=None):
        self.capture = Capture(uuid.uuid4().hex, iso_now(self.clock() if timestamp is None else timestamp),
                               self.monotonic() if monotonic is None else monotonic, 0, self.generation)
        self.capture_frames = 0

    def audio_frame(self, received_at, received_monotonic, have_voice):
        if not self.live():
            return
        if self.capture is None and (have_voice or self.conn.client_listen_mode == "manual"):
            self.begin_capture(received_at, received_monotonic)
        if self.capture:
            self.capture_frames += 1

    def take_capture(self):
        value = self.capture
        self.capture = None
        if value is None:
            return None
        value = Capture(value.id, value.started_at, value.started_monotonic, self.capture_frames, value.generation)
        self.capture_frames = 0
        self._issued_captures[value.id] = value
        while len(self._issued_captures) > 20:
            self._issued_captures.pop(next(iter(self._issued_captures)))
        return value

    def clear_capture(self):
        self.capture = None
        self.capture_frames = 0

    async def api(self, path, **payload):
        if not self.live():
            raise VoiceMailError("语音会话已断开，请重新对话")
        result = await self.client.post("/internal/voice/" + path, {**self.context, **payload})
        if not self.live():
            raise VoiceMailError("语音会话已更新，请重新对话")
        return result

    @staticmethod
    def unwrap_draft(value):
        return value.get("draft", value)

    def _set_draft(self, value):
        draft = self.unwrap_draft(value)
        if not isinstance(draft, dict) or not draft.get("id"):
            raise VoiceMailError("邮件草稿尚未就绪")
        self.draft = draft
        return draft

    def _editable(self, draft):
        return bool(draft and draft.get("state") in EDITABLE_STATES)

    async def refresh(self):
        result = await self.api("state")
        draft = result.get("draft")
        if draft:
            old = (self.draft or {}).get("content_hash")
            self.draft = draft
            if old != draft.get("content_hash"):
                self.readback = None
        else:
            self.draft = None
            self.readback = None
        return result

    async def tool(self, action, subject=None, body=None):
        """Model-visible operations. There is intentionally no confirmation action."""
        if action not in {"create", "edit", "status", "cancel"}:
            raise VoiceMailError("模型不能确认或发送邮件")
        async with self.operation_lock:
            if action in {"create", "edit"}:
                if not self.latest_utterance:
                    raise VoiceMailError("请通过当前设备语音口述邮件")
                if not isinstance(subject, str) or not 1 <= len(subject.strip()) <= 160:
                    raise VoiceMailError("邮件主题须为 1 至 160 字")
                if not isinstance(body, str) or not 1 <= len(body.strip()) <= 2000:
                    raise VoiceMailError("首版邮件正文须为 1 至 2000 字")
                self.readback = None
                if action == "create":
                    value = await self.api("drafts", subject=subject.strip(), body=body.strip(),
                                           utterance_id=self.latest_utterance)
                else:
                    await self.refresh()
                    if not self._editable(self.draft):
                        raise VoiceMailError("当前没有可修改的邮件草稿")
                    value = await self.api(f"drafts/{self.draft['id']}/edit", expected_version=self.draft["version"],
                                           subject=subject.strip(), body=body.strip())
                self._set_draft(value)
                self.schedule_readback()
                return "草稿已保存。系统将逐字复述，尚未发送。"
            if action == "cancel":
                await self.refresh()
                if not self.draft:
                    return "当前没有待处理的邮件。"
                self.readback = None
                self.interrupt()
                value = await self.api(f"drafts/{self.draft['id']}/cancel")
                self._set_draft(value)
                return "已提交取消请求。已经发送的邮件不会撤回；最终状态以任务记录为准。"
            state = await self.refresh()
            if self._editable(self.draft):
                self.schedule_readback()
                return "邮件尚未发送。系统将复述当前版本，请听完后再确认。"
            run = state.get("run") or {}
            return "邮件任务状态：" + str(run.get("status", "暂无任务")) + "。"

    async def accept_microphone_final(self, text, capture):
        text = plain_text(text)
        trusted = isinstance(capture, Capture) and self._issued_captures.pop(capture.id, None) is capture
        if (not self.live() or not trusted
                or capture.generation != self.generation or capture.frames < 5):
            return is_confirmation(text) or is_cancel(text)
        # Fail closed for any recording overlapping readback or its speaker tail.
        if self.speaking_id or capture.started_monotonic < self.block_until:
            return True
        self.latest_utterance = capture.id
        if is_cancel(text):
            try:
                message = await self.tool("cancel")
            except VoiceMailError:
                message = "取消请求未确认，请在 App 查看当前任务状态。"
            await self.speak(message)
            return True
        if not is_confirmation(text):
            return False
        async with self.operation_lock:
            proof = self.readback
            if not proof or capture.started_monotonic < proof["allowed_monotonic"]:
                if self._editable(self.draft):
                    self.schedule_readback()
                else:
                    await self.speak("没有已经完整复述的待发送草稿，请先口述邮件。")
                return True
            # Clear locally before I/O: a lost response cannot cause a second approval.
            self.readback = None
            try:
                await self.api(f"drafts/{proof['draft_id']}/confirm", expected_version=proof["version"],
                               content_hash=proof["content_hash"], readback_id=proof["readback_id"],
                               utterance_id=capture.id, capture_started_at=capture.started_at,
                               source="mic_asr_final", text=text)
                await self.speak("已确认这版邮件，正在交给云端浏览器发送，结果稍后播报。")
            except VoiceMailError:
                await self.speak("发送确认未完成，请查询任务状态；我不会自动重复发送。")
            return True

    def normal_started(self, sentence_id):
        self.normal_outputs.add(sentence_id)

    def output_finished(self, sentence_id):
        self.normal_outputs.discard(sentence_id)
        if sentence_id == self.speaking_id and self.speech_future and not self.speech_future.done():
            self.speech_future.set_result(not self._readback_abort and not self.conn.client_abort and self.audio_frames_sent > 0)

    def output_audio(self, audio):
        if self.speaking_id and audio:
            self.audio_frames_sent += 1

    def synthesis_failed(self, sentence_id):
        if sentence_id == self.speaking_id:
            self.conn.client_abort = True
            self.interrupt()
            self.conn.clear_queues()

    def interrupt(self):
        self.readback = None
        self._readback_abort = True
        self.normal_outputs.clear()
        self.block_until = max(self.block_until, self.monotonic() + self.settings.tail_guard)
        if self.speech_future and not self.speech_future.done():
            self.speech_future.set_result(False)

    async def _idle(self):
        deadline = self.monotonic() + 90
        while self.live():
            tts = self.conn.tts
            if (tts and hasattr(tts, "tts_audio_queue") and hasattr(tts, "tts_text_queue")
                    and not self.normal_outputs and not self.conn.client_is_speaking
                    and tts.tts_text_queue.empty() and tts.tts_audio_queue.empty()):
                return
            if self.monotonic() > deadline:
                raise VoiceMailError("语音输出仍忙，请重新对话")
            await asyncio.sleep(0.1)
        raise VoiceMailError("语音会话已断开")

    async def speak(self, text):
        from core.providers.tts.dto.dto import TTSMessageDTO, ContentType, SentenceType
        async with self.speech_lock:
            await self._idle()
            self._readback_abort = False
            self.speaking_id = uuid.uuid4().hex
            self.speech_future = asyncio.get_running_loop().create_future()
            self.audio_frames_sent = 0
            self.conn.sentence_id = self.speaking_id
            self.conn.client_abort = False
            try:
                for stage, detail in ((SentenceType.FIRST, None), (SentenceType.MIDDLE, text), (SentenceType.LAST, None)):
                    self.conn.tts.tts_text_queue.put(TTSMessageDTO(self.speaking_id, stage,
                        ContentType.TEXT if stage == SentenceType.MIDDLE else ContentType.ACTION, content_detail=detail))
                # This is a transport completion, never proof of physical playback.
                completed = await asyncio.wait_for(self.speech_future, timeout=180)
                if not completed:
                    raise VoiceMailError("语音复述被打断")
                return self.clock(), self.monotonic()
            except asyncio.TimeoutError:
                raise VoiceMailError("语音输出未完成") from None
            finally:
                self.block_until = max(self.block_until, self.monotonic() + self.settings.tail_guard)
                self.speaking_id = None
                self.speech_future = None

    def schedule_readback(self):
        self.readback = None
        self.readback_seen = None
        if not self.readback_task or self.readback_task.done():
            self.readback_task = asyncio.create_task(self._readback(), name="voice-mail-readback")

    async def _readback(self):
        try:
            await self._idle()
            async with self.operation_lock:
                await self.refresh()
                if not self._editable(self.draft):
                    return
                draft = dict(self.draft)
                readback_id = uuid.uuid4().hex
                common = {"expected_version": draft["version"], "content_hash": draft["content_hash"], "readback_id": readback_id}
                path = f"drafts/{draft['id']}/readback"
                await self.api(path, status="started", **common)
                self.readback_seen = (draft["id"], draft["version"])
                text = ("准备发送给" + draft.get("recipient_label", "当前登录的本人163邮箱") + "。主题："
                        + draft["subject"] + "。正文开始。" + draft["body"]
                        + "。正文结束。请等我说完，再说确认发送；也可以修改或取消邮件。")
                try:
                    await self.speak(text)
                    returned = self._set_draft(await self.api(path, status="completed", **common))
                    server_after = returned.get("confirm_after") or (returned.get("readback") or {}).get("confirm_after")
                    server_delay = max(0.0, datetime.fromisoformat(server_after.replace("Z", "+00:00")).timestamp() - self.clock()) if server_after else self.settings.tail_guard
                    self.readback = {"draft_id": draft["id"], "version": draft["version"], "content_hash": draft["content_hash"],
                                     "readback_id": readback_id, "allowed_monotonic": self.monotonic() + max(self.settings.tail_guard, server_delay)}
                    self.block_until = max(self.block_until, self.readback["allowed_monotonic"])
                except (VoiceMailError, asyncio.CancelledError):
                    self.readback = None
                    if self.live():
                        try:
                            await self.api(path, status="interrupted", **common)
                        except VoiceMailError:
                            pass
                    raise
        except VoiceMailError:
            self.readback = None

    async def _poll(self):
        while self.live():
            try:
                await self._idle()
                if not self.operation_lock.locked() and not self.speaking_id:
                    async with self.operation_lock:
                        state = await self.refresh()
                    if self._editable(self.draft) and (self.draft["id"], self.draft["version"]) != self.readback_seen:
                        self.schedule_readback()
                    elif not self.readback_task or self.readback_task.done():
                        for notification in state.get("notifications", []):
                            message = notification.get("message", "")
                            if not isinstance(message, str) or not message or len(message) > 500:
                                continue
                            try:
                                await self.speak(message)
                            except VoiceMailError:
                                if self.live():
                                    await self.api(f"notifications/{notification['id']}/ack", delivery="interrupted")
                                break
                            await self.api(f"notifications/{notification['id']}/ack", delivery="transport_sent")
            except VoiceMailError:
                pass  # Durable outbox remains pending; no automatic mail retry.
            await asyncio.sleep(self.settings.poll_seconds)


async def attach_bridge(conn):
    try:
        settings = Settings.load(conn.config)
        if not settings.enabled:
            return
        if (not settings.allows_device(conn.device_id) or conn.need_bind
                or not conn.read_config_from_api or not getattr(conn, "voice_mail_authenticated", False)):
            return
        bridge = VoiceMailBridge(conn, settings, VoiceClient(settings))
        conn.voice_mail = bridge
        bridge.start()
    except (VoiceMailError, ValueError):
        conn.logger.warning("语音邮件功能未启用：受限配置不可用")
