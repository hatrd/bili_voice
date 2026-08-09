from __future__ import annotations

import asyncio
import dataclasses
import enum
import io
import logging
import math
import threading
import time
import wave
import audioop
from collections import deque
from typing import Any, Deque, Dict, Optional, Tuple, Callable

import aiohttp
import winsound

import re
from .models import Settings, ReplacementRule

logger = logging.getLogger("bili_voice.tts_service")
_global_status_listener: Optional[Callable[[Optional[int], Optional[str], str], None]] = None


class Priority(enum.IntEnum):
    HIGH = 0
    NORMAL = 1


_API_V2_LANGUAGE_MAP = {
    "中文": "all_zh",
    "英文": "en",
    "日文": "all_ja",
    "粤语": "all_yue",
    "韩文": "all_ko",
    "中英混合": "zh",
    "日英混合": "ja",
    "粤英混合": "yue",
    "韩英混合": "ko",
    "多语种混合": "auto",
    "多语种混合(粤语)": "auto_yue",
    "all_zh": "all_zh",
    "all_ja": "all_ja",
    "all_yue": "all_yue",
    "all_ko": "all_ko",
}

_API_V2_SPLIT_METHOD_MAP = {
    "不切": "cut0",
    "凑四句一切": "cut1",
    "凑50字一切": "cut2",
    "按中文句号。切": "cut3",
    "按英文句号.切": "cut4",
    "按标点符号切": "cut5",
}


def _normalize_base_url(base_url: str) -> str:
    return (base_url or "").strip().rstrip("/") + "/"


def _api_v2_language(value: str) -> str:
    raw = (value or "").strip()
    return _API_V2_LANGUAGE_MAP.get(raw, raw.lower() or "zh")


def _api_v2_split_method(value: str) -> str:
    raw = (value or "").strip()
    return _API_V2_SPLIT_METHOD_MAP.get(raw, raw or "cut0")


def _build_api_v2_payload(settings: Settings, text: str, ref_text: str) -> Dict[str, Any]:
    prompt_text = "" if bool(settings.ref_text_free) else ref_text
    seed = -1 if bool(settings.keep_random) else int(settings.seed)
    return {
        "text": text,
        "text_lang": _api_v2_language(settings.text_lang),
        "ref_audio_path": (settings.ref_audio_path or "").strip(),
        "prompt_text": prompt_text,
        "prompt_lang": _api_v2_language(settings.text_lang),
        "top_k": int(settings.top_k),
        "top_p": float(settings.top_p),
        "temperature": float(settings.temperature),
        "text_split_method": _api_v2_split_method(settings.text_split_method),
        "batch_size": int(settings.batch_size),
        "speed_factor": float(settings.speed_factor),
        "split_bucket": bool(settings.split_bucket),
        "fragment_interval": float(settings.fragment_interval),
        "seed": seed,
        "media_type": "wav",
        "streaming_mode": False,
        "parallel_infer": bool(settings.parallel_infer),
        "repetition_penalty": float(settings.repetition_penalty),
        "sample_steps": int(settings.sample_steps),
        "super_sampling": bool(settings.super_sampling),
    }


def _adjust_wav_volume(data: bytes, gain_db: float) -> bytes:
    gain_db = max(-60.0, min(24.0, float(gain_db or 0.0)))
    if gain_db == 0.0:
        return data

    source = io.BytesIO(data)
    with wave.open(source, "rb") as reader:
        params = reader.getparams()
        if params.comptype != "NONE":
            raise ValueError(f"Unsupported WAV compression: {params.comptype}")
        frames = reader.readframes(params.nframes)

    adjusted = audioop.mul(frames, params.sampwidth, math.pow(10.0, gain_db / 20.0))
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setparams(params)
        writer.writeframes(adjusted)
    return output.getvalue()


@dataclasses.dataclass
class TtsTask:
    text: str
    priority: Priority = Priority.NORMAL
    key: Optional[str] = None
    room_id: Optional[int] = None


class PredictQueue:
    def __init__(self, max_size: Optional[int] = None, on_evict: Optional[Callable[[TtsTask], None]] = None):
        self._max_size = max_size
        self._high: Deque[TtsTask] = deque()
        self._normal: Deque[TtsTask] = deque()
        self._cv = threading.Condition()
        self._on_evict = on_evict

    def push(self, task: TtsTask) -> bool:
        with self._cv:
            # capacity check
            cap = self._max_size if isinstance(self._max_size, int) and self._max_size > 0 else None
            size = len(self._high) + len(self._normal)
            if cap is not None and size >= cap:
                if task.priority == Priority.HIGH:
                    # evict from normal; if none, evict oldest high
                    evicted: Optional[TtsTask] = None
                    if self._normal:
                        evicted = self._normal.popleft()
                    elif self._high:
                        evicted = self._high.popleft()
                    else:
                        evicted = None
                    # notify eviction
                    try:
                        if evicted and self._on_evict:
                            self._on_evict(evicted)
                    except Exception:
                        pass
                else:
                    # drop normal task (not enqueued -> no pending emitted)
                    return False
            # enqueue
            if task.priority == Priority.HIGH:
                self._high.append(task)
            else:
                self._normal.append(task)
            self._cv.notify()
            return True

    def pop(self) -> TtsTask:
        with self._cv:
            while True:
                if self._high:
                    return self._high.popleft()
                if self._normal:
                    return self._normal.popleft()
                self._cv.wait()


class AudioQueue:
    def __init__(self, max_size: Optional[int] = None, on_evict: Optional[Callable[[TtsTask], None]] = None):
        self._max_size = max_size
        self._q: Deque[Tuple[bytes, TtsTask]] = deque()
        self._cv = threading.Condition()
        self._on_evict = on_evict

    def push(self, audio: bytes, task: TtsTask):
        with self._cv:
            cap = self._max_size if isinstance(self._max_size, int) and self._max_size > 0 else None
            if cap is not None and len(self._q) >= cap:
                # drop oldest
                evicted: Optional[Tuple[bytes, TtsTask]] = None
                try:
                    evicted = self._q.popleft()
                except Exception:
                    evicted = None
                # notify eviction
                try:
                    if evicted and self._on_evict:
                        self._on_evict(evicted[1])
                except Exception:
                    pass
            self._q.append((audio, task))
            self._cv.notify()

    def pop(self) -> Tuple[bytes, TtsTask]:
        with self._cv:
            while True:
                try:
                    return self._q.popleft()
                except Exception:
                    self._cv.wait()


async def check_tts_backend(
    base_url: str,
    timeout_seconds: float = 5.0,
) -> None:
    base = (base_url or "").strip()
    if not base:
        raise RuntimeError("未配置 TTS 服务地址")
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        path = "openapi.json"
        async with session.get(_normalize_base_url(base) + path) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"api_v2 {path}: HTTP {resp.status} {body[:120]}")
            data = await resp.json(content_type=None)

    paths = data.get("paths") if isinstance(data, dict) else None
    if not isinstance(paths, dict) or "/tts" not in paths:
        raise RuntimeError("api_v2 openapi.json 中未发现 /tts")


class APIv2Client:
    def __init__(self, base_url: str, ssl_verify: bool = False, timeout: int = 300):
        self.base_url = _normalize_base_url(base_url)
        self.ssl_verify = ssl_verify
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    async def ensure(self):
        if self._session is None:
            connector = aiohttp.TCPConnector(ssl=self.ssl_verify)
            self._session = aiohttp.ClientSession(
                timeout=self.timeout,
                connector=connector,
                headers={"User-Agent": "bili_voice/tts_service"},
            )

    async def close(self):
        if self._session is not None:
            session = self._session
            self._session = None
            try:
                await session.close()
            except Exception:
                pass

    async def _set_weights(self, endpoint: str, weights_path: str):
        path = (weights_path or "").strip()
        if not path:
            return
        await self.ensure()
        assert self._session is not None
        async with self._session.get(self.base_url + endpoint, params={"weights_path": path}) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"api_v2 {endpoint} failed: HTTP {resp.status} {body[:200]}")

    async def select_models(self, sovits_model: str, gpt_model: str):
        await self._set_weights("set_sovits_weights", sovits_model)
        await self._set_weights("set_gpt_weights", gpt_model)

    async def synthesize(self, payload: Dict[str, Any]) -> bytes:
        await self.ensure()
        assert self._session is not None
        async with self._session.post(self.base_url + "tts", json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"api_v2 /tts failed: HTTP {resp.status} {body[:300]}")
            data = await resp.read()
            if not data:
                raise RuntimeError("api_v2 /tts returned empty audio")
            return data


class TTSService:
    def __init__(self) -> None:
        self._cfg: Optional[Settings] = None
        self._predict_q = PredictQueue(on_evict=lambda t: self._emit_status(getattr(t, "room_id", None), getattr(t, "key", None), "cancelled"))
        self._audio_q = AudioQueue(on_evict=lambda t: self._emit_status(getattr(t, "room_id", None), getattr(t, "key", None), "cancelled"))
        self._predict_thread = threading.Thread(target=self._predict_worker, daemon=True)
        self._play_thread = threading.Thread(target=self._play_worker, daemon=True)
        self._threads_started = False
        self._status_listener: Optional[Callable[[Optional[int], Optional[str], str], None]] = None

    def init(self, settings: Settings):
        self._cfg = settings
        # start threads once
        if not self._threads_started:
            self._threads_started = True
            self._predict_thread.start()
            self._play_thread.start()

    def update_settings(self, settings: Settings):
        self._cfg = settings

    def set_status_listener(self, fn: Optional[Callable[[Optional[int], Optional[str], str], None]]):
        self._status_listener = fn

    def _emit_status(self, room_id: Optional[int], key: Optional[str], status: str):
        try:
            logger.debug("TTS_STATUS room=%s key=%s status=%s", room_id, key, status)
        except Exception:
            pass
        try:
            if self._status_listener:
                self._status_listener(room_id, key, status)
        except Exception:
            pass

    def enqueue_text(self, text: str, priority: Priority = Priority.NORMAL, key: Optional[str] = None, room_id: Optional[int] = None) -> bool:
        if not self._cfg or not getattr(self._cfg, "tts_enabled", False):
            return False
        # replacement rules（仅使用有序列表 replacement_rules）
        t = text or ""
        try:
            text_to_process = t
            rep_list = getattr(self._cfg, "replacement_rules", None) or []
            if isinstance(rep_list, list) and len(rep_list) > 0:
                for raw in rep_list:
                    try:
                        rule: ReplacementRule
                        if isinstance(raw, ReplacementRule):
                            rule = raw
                        else:
                            # tolerate dict input
                            rule = ReplacementRule(**(raw or {}))
                    except Exception:
                        continue
                    if not rule.key:
                        continue

                    # Build pattern/replacement according to flags
                    flags = 0 if rule.match_case else re.IGNORECASE
                    pattern: str
                    if rule.use_regex:
                        pattern = rule.key
                    else:
                        # escape literal
                        pattern = re.escape(rule.key)
                    if rule.whole_word:
                        # Use word boundaries; for CJK this may not be perfect, but acceptable
                        pattern = r"\b" + pattern + r"\b"
                    try:
                        text_to_process = re.sub(pattern, rule.value, text_to_process, flags=flags)
                    except re.error:
                        # invalid regex -> skip
                        continue
            t = text_to_process
        except Exception:
            pass

        max_q = getattr(self._cfg, "max_tts_queue_size", None) or getattr(self._cfg, "tts_max_queue_size", None)
        try:
            cap = int(max_q) if max_q is not None else None
        except Exception:
            cap = None
        # reconfigure capacity
        self._predict_q._max_size = cap
        self._audio_q._max_size = cap
        ok = self._predict_q.push(TtsTask(text=t, priority=priority, key=key, room_id=room_id))
        if ok:
            try:
                self._emit_status(room_id, key, "pending")
            except Exception:
                pass
        else:
            # Not enqueued due to capacity and normal priority drop -> mark as cancelled
            try:
                if key is not None:
                    self._emit_status(room_id, key, "cancelled")
            except Exception:
                pass
        return ok

    # ---------- workers ----------

    def _predict_worker(self):
        logger.info("TTS predict worker started")
        api_v2_client: Optional[APIv2Client] = None
        active_base: Optional[str] = None
        selected_sig: Optional[Tuple[str, str, str, str]] = None

        async def _close_client():
            nonlocal api_v2_client
            if api_v2_client is not None:
                await api_v2_client.close()
                api_v2_client = None

        async def _ensure_and_select_models():
            nonlocal api_v2_client, active_base, selected_sig
            cfg = self._cfg
            if not cfg:
                return False
            base = (cfg.api_v2_url or "").strip()
            if not base:
                logger.warning("api_v2 server URL not set; waiting...")
                return False
            try:
                normalized_base = _normalize_base_url(base)
                if active_base != normalized_base:
                    await _close_client()
                    active_base = normalized_base
                    selected_sig = None
                    api_v2_client = APIv2Client(base, ssl_verify=False)
                    logger.info("Configured api_v2 backend at %s", base)

                sig = (
                    normalized_base,
                    str(cfg.sovits_model),
                    str(cfg.gpt_model),
                    str(cfg.text_lang),
                )
                if selected_sig != sig:
                    assert api_v2_client is not None
                    await api_v2_client.select_models(cfg.sovits_model, cfg.gpt_model)
                    logger.info("Applied api_v2 model settings")
                    selected_sig = sig
                return True
            except Exception as e:
                logger.warning("Failed to initialize TTS client: %s", e)
                await _close_client()
                active_base = None
                selected_sig = None
                return False

        def _new_loop():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            return loop

        loop = _new_loop()
        while True:
            try:
                task = self._predict_q.pop()
                # ensure client ready
                ok = loop.run_until_complete(_ensure_and_select_models())
                if not ok:
                    # backoff and skip this task to avoid blocking queue forever
                    time.sleep(2.0)
                    continue

                cfg = self._cfg
                assert cfg is not None

                ref_text = ""
                if isinstance(cfg.ref_text_path, str) and cfg.ref_text_path.strip():
                    try:
                        with open(cfg.ref_text_path.strip(), "r", encoding="utf-8") as f:
                            ref_text = f.read().strip()
                    except Exception:
                        ref_text = ""

                # call inference
                logger.info("Generating TTS: %s", task.text)
                start = time.time()
                assert api_v2_client is not None
                payload = _build_api_v2_payload(cfg, task.text, ref_text)
                buf = loop.run_until_complete(api_v2_client.synthesize(payload))

                logger.info("Received audio %.1f KB in %.2fs", len(buf) / 1024, time.time() - start)

                # api_v2 is configured to return PCM WAV; adjust it without FFmpeg.
                vol_db = float(getattr(cfg, "tts_volume", 0.0) or 0.0)
                audio = _adjust_wav_volume(buf, vol_db)

                # enqueue to play queue (server-side playback)
                self._audio_q.push(audio, task)
                logger.info("Enqueued audio: %s", task.text)
            except Exception as e:
                logger.error("Predict worker error: %s", e, exc_info=True)
                try:
                    loop.run_until_complete(_close_client())
                except Exception:
                    pass
                active_base = None
                selected_sig = None
                time.sleep(1.0)

    def _play_worker(self):
        logger.info("TTS play worker started")
        while True:
            try:
                data, task = self._audio_q.pop()
                try:
                    self._emit_status(getattr(task, "room_id", None), getattr(task, "key", None), "playing")
                except Exception:
                    pass
                logger.info("Playing: %s", task.text)
                try:
                    winsound.PlaySound(data, winsound.SND_MEMORY)
                except Exception as we:
                    logger.error("winsound playback failed: %s", we)
                finally:
                    try:
                        self._emit_status(getattr(task, "room_id", None), getattr(task, "key", None), "done")
                    except Exception:
                        pass
            except Exception as e:
                logger.error("Play worker error: %s", e, exc_info=True)


# ---- Singleton API ----

_service: Optional[TTSService] = None

def init(settings: Settings):
    global _service
    if _service is None:
        _service = TTSService()
        _service.init(settings)
        try:
            if _global_status_listener:
                _service.set_status_listener(_global_status_listener)
        except Exception:
            pass

def update_settings(settings: Settings):
    if _service is None:
        init(settings)
    else:
        _service.update_settings(settings)

def set_status_listener(fn: Optional[Callable[[Optional[int], Optional[str], str], None]]):
    global _global_status_listener, _service
    _global_status_listener = fn
    if _service is not None:
        try:
            _service.set_status_listener(fn)
        except Exception:
            pass

def enqueue_text(text: str, priority: Priority = Priority.NORMAL, key: Optional[str] = None, room_id: Optional[int] = None) -> bool:
    if _service is None:
        return False
    return _service.enqueue_text(text, priority, key=key, room_id=room_id)

def priority_from_event_type(event_type: str) -> Priority:
    t = (event_type or "").upper()
    if "SUPER_CHAT" in t or t in ("SEND_GIFT", "COMBO_SEND", "GUARD_BUY"):
        return Priority.HIGH
    return Priority.NORMAL


async def tts_health(settings: Settings) -> Dict[str, Any]:
    """Check the configured GPT-SoVITS api_v2 service."""
    base = (settings.api_v2_url or "").strip()
    if not base:
        return {"ok": False, "ready": False, "url": base, "message": "未配置 TTS 服务地址"}
    try:
        await check_tts_backend(base)
        return {"ok": True, "ready": True, "url": base, "backend": "api_v2"}
    except Exception as e:
        return {"ok": False, "ready": False, "url": base, "backend": "api_v2", "message": str(e)}
