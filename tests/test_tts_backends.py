import asyncio
import io
import json
import math
import struct
import threading
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from backend.models import Settings
from backend.tts_service import (
    APIv2Client,
    _adjust_wav_volume,
    _build_api_v2_payload,
    check_tts_backend,
)


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/openapi.json":
            self._json_response({"paths": {"/tts": {"post": {}}}})
            return
        self.send_response(404)
        self.end_headers()

    def _json_response(self, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


class _APIClientHandler(BaseHTTPRequestHandler):
    requests = []

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path in ("/set_sovits_weights", "/set_gpt_weights"):
            self.requests.append((parsed.path, parse_qs(parsed.query)))
            self._response(b'{"message":"success"}', "application/json")
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path != "/tts":
            self.send_response(404)
            self.end_headers()
            return
        size = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(size).decode("utf-8"))
        self.requests.append((self.path, payload))
        self._response(b"RIFF-test-wave", "audio/wav")

    def _response(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


class APIv2Tests(unittest.TestCase):
    def test_api_v2_payload_maps_ui_values(self):
        settings = Settings(
            text_lang="中文",
            text_split_method="按标点符号切",
            ref_audio_path=r"C:\voice\ref.wav",
            ref_text_free=False,
            keep_random=True,
            sample_steps="32",
        )

        payload = _build_api_v2_payload(settings, "测试文本", "参考文本")

        self.assertEqual(payload["text_lang"], "all_zh")
        self.assertEqual(payload["prompt_lang"], "all_zh")
        self.assertEqual(payload["text_split_method"], "cut5")
        self.assertEqual(payload["prompt_text"], "参考文本")
        self.assertEqual(payload["seed"], -1)
        self.assertEqual(payload["media_type"], "wav")
        self.assertFalse(payload["streaming_mode"])

    def test_api_v2_payload_honors_reference_free_and_fixed_seed(self):
        settings = Settings(text_lang="中英混合", ref_text_free=True, keep_random=False, seed=42)
        payload = _build_api_v2_payload(settings, "测试文本", "不会发送")
        self.assertEqual(payload["text_lang"], "zh")
        self.assertEqual(payload["prompt_text"], "")
        self.assertEqual(payload["seed"], 42)

    def test_migrates_legacy_api_v2_url(self):
        settings = Settings(tts_backend="api_v2", gradio_server_url="http://127.0.0.1:9999/")
        self.assertEqual(settings.api_v2_url, "http://127.0.0.1:9999/")
        self.assertNotIn("tts_backend", settings.model_dump())
        self.assertNotIn("gradio_server_url", settings.model_dump())

    def test_checks_api_v2_service(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            asyncio.run(check_tts_backend(url, timeout_seconds=2))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_api_v2_client_selects_models_and_returns_audio(self):
        handler = type("APIClientHandler", (_APIClientHandler,), {"requests": []})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        async def exercise_client():
            client = APIv2Client(f"http://127.0.0.1:{server.server_port}")
            try:
                await client.select_models("voice model.pth", "voice model.ckpt")
                return await client.synthesize({"text": "测试", "text_lang": "all_zh"})
            finally:
                await client.close()

        try:
            audio = asyncio.run(exercise_client())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(audio, b"RIFF-test-wave")
        self.assertEqual(handler.requests[0][1]["weights_path"], ["voice model.pth"])
        self.assertEqual(handler.requests[1][1]["weights_path"], ["voice model.ckpt"])
        self.assertEqual(handler.requests[2][1]["text"], "测试")

    def test_adjusts_pcm_wav_volume_without_ffmpeg(self):
        source = io.BytesIO()
        samples = (1000, -1000, 2000, -2000)
        with wave.open(source, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16000)
            writer.writeframes(struct.pack("<4h", *samples))

        adjusted = _adjust_wav_volume(source.getvalue(), -6.0206)
        with wave.open(io.BytesIO(adjusted), "rb") as reader:
            actual = struct.unpack("<4h", reader.readframes(4))

        for value, expected in zip(actual, samples):
            self.assertLessEqual(abs(value - math.trunc(expected * 0.5)), 1)


if __name__ == "__main__":
    unittest.main()
