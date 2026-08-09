import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from backend.models import Settings
from backend.tts_service import (
    APIv2Client,
    TTSBackend,
    _build_api_v2_payload,
    check_tts_backend,
    normalize_tts_backend,
)


class _BackendHandler(BaseHTTPRequestHandler):
    backend = TTSBackend.GRADIO

    def do_GET(self):
        if self.backend == TTSBackend.GRADIO and self.path == "/config":
            self._json_response({"dependencies": [], "components": []})
            return
        if self.backend == TTSBackend.API_V2 and self.path == "/openapi.json":
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


class TTSBackendTests(unittest.TestCase):
    def test_backend_is_selected_explicitly(self):
        self.assertEqual(normalize_tts_backend("gradio"), TTSBackend.GRADIO)
        self.assertEqual(normalize_tts_backend("api_v2"), TTSBackend.API_V2)

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

    def test_checks_explicit_gradio_service(self):
        self._check_server(TTSBackend.GRADIO, TTSBackend.GRADIO)

    def test_checks_explicit_api_v2_service(self):
        self._check_server(TTSBackend.API_V2, TTSBackend.API_V2)

    def test_explicit_backend_does_not_fall_back(self):
        with self.assertRaises(RuntimeError):
            self._check_server(TTSBackend.GRADIO, TTSBackend.API_V2)

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

    def _check_server(self, served_backend, configured_backend):
        handler = type("BackendHandler", (_BackendHandler,), {"backend": served_backend})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            asyncio.run(check_tts_backend(url, configured_backend, timeout_seconds=2))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
