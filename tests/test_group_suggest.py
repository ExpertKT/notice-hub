from __future__ import annotations
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock
from qq_live_digest.config import Settings
from qq_live_digest.group_suggest import _clear_caches, suggest
from qq_live_digest.store import Store
from qq_live_digest.webapp import TaskWebServer

GROUPS = [{"group_id": "1", "group_name": "计算机课程群"}, {"group_id": "2", "group_name": "二手交易群"}]

class GroupSuggestTest(unittest.TestCase):
    def setUp(self):
        _clear_caches()
        self.settings = Settings(llm_enabled=True, llm_backend="codebuddy", llm_timeout=1)

    def test_llm_valid_json(self):
        with mock.patch("qq_digest.run_codebuddy", return_value={"groups": [{"group_id":"1","category":"course","reason":"课程"},{"group_id":"2","category":"market","reason":"交易"}]}):
            result = suggest(self.settings, GROUPS)
        self.assertEqual(result["source"], "llm"); self.assertTrue(result["groups"][0]["suggested"]); self.assertEqual(result["counts"]["market"], 1)

    def test_codebuddy_schema_is_json_string(self):
        captured = {}
        def fake(prompt, schema, *, cli="", timeout=60):
            captured["schema"] = schema
            return {"groups": [{"group_id":"1","category":"course","reason":"课程"},{"group_id":"2","category":"market","reason":"交易"}]}
        with mock.patch("qq_digest.run_codebuddy", side_effect=fake):
            result = suggest(self.settings, GROUPS)
        self.assertEqual(result["source"], "llm")
        self.assertIsInstance(captured["schema"], str)
        self.assertIsInstance(json.loads(captured["schema"]), dict)

    def test_auto_falls_back_to_local_model(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps({"choices": [{"message": {"content": json.dumps({"groups": [{"group_id":"1","category":"course","reason":"课程"},{"group_id":"2","category":"market","reason":"交易"}]})}}]}).encode()
        settings = Settings(llm_enabled=True, llm_backend="auto", llm_timeout=1)
        with mock.patch("qq_digest.run_codebuddy", side_effect=RuntimeError("429")), mock.patch("urllib.request.urlopen", return_value=Response()):
            result = suggest(settings, GROUPS)
        self.assertEqual(result["source"], "llm")
        self.assertEqual(result["counts"]["course"], 1)

    def test_ollama_json_fence_is_parsed(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return '{"choices":[{"message":{"content":"```json\\n{\\"groups\\":[{\\"group_id\\":\\"1\\",\\"category\\":\\"course\\",\\"reason\\":\\"课程\\"},{\\"group_id\\":\\"2\\",\\"category\\":\\"market\\",\\"reason\\":\\"交易\\"}]}\\n```"}}]}'.encode()
        settings = Settings(llm_enabled=True, llm_backend="ollama", llm_timeout=1)
        with mock.patch("urllib.request.urlopen", return_value=Response()):
            result = suggest(settings, GROUPS)
        self.assertEqual(result["source"], "llm"); self.assertEqual(result["counts"]["course"], 1)

    def test_ollama_requests_are_batched(self):
        calls = []
        class Response:
            def __init__(self, ids): self.ids = ids
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps({"choices":[{"message":{"content":json.dumps({"groups":[{"group_id":gid,"category":"course","reason":"课程"} for gid in self.ids]})}}]}).encode()
        def fake_urlopen(request, timeout=0):
            body = json.loads(request.data.decode()); calls.append(body)
            ids = [item["group_id"] for item in json.loads(body["messages"][0]["content"].split("输入：", 1)[1])["groups"]]
            return Response(ids)
        groups = [{"group_id": str(10001 + i), "group_name": "课程群" + str(i)} for i in range(13)]
        settings = Settings(llm_enabled=True, llm_backend="ollama", llm_timeout=1)
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = suggest(settings, groups)
        self.assertGreaterEqual(len(calls), 2); self.assertEqual(len(result["groups"]), 13)

    def test_local_prefers_small_model_from_tags(self):
        calls = []
        class Response:
            def __init__(self, payload): self.payload = payload
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps(self.payload).encode()
        def fake_urlopen(request, timeout=0):
            if request.data is None:
                return Response({"models": [{"name": "qwen3.5:9b"}, {"name": "other"}]})
            body = json.loads(request.data.decode()); calls.append(body)
            return Response({"choices": [{"message": {"content": json.dumps({"groups": [{"group_id": "1", "category": "course", "reason": "课程"}, {"group_id": "2", "category": "market", "reason": "交易"}]})}}]})
        settings = Settings(llm_enabled=True, llm_backend="ollama", llm_timeout=1)
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = suggest(settings, GROUPS)
        self.assertEqual(result["source"], "llm")
        self.assertEqual(calls[0]["model"], "qwen3.5:9b")

    def test_local_tags_failure_uses_configured_model(self):
        calls = []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps({"choices": [{"message": {"content": json.dumps({"groups": [{"group_id": "1", "category": "course", "reason": "课程"}, {"group_id": "2", "category": "market", "reason": "交易"}]})}}]}).encode()
        def fake_urlopen(request, timeout=0):
            if request.data is None:
                raise OSError("tags unavailable")
            calls.append(json.loads(request.data.decode()))
            return Response()
        settings = Settings(llm_enabled=True, llm_backend="ollama", llm_timeout=1, dashscope_model="configured-model")
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = suggest(settings, GROUPS)
        self.assertEqual(result["source"], "llm")
        self.assertEqual(calls[0]["model"], "configured-model")

    def test_openai_backend_uses_configured_model_without_local_probe(self):
        probes = []
        calls = []
        class Response:
            def __init__(self, payload): self.payload = payload
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps(self.payload).encode()
        def fake_urlopen(request, timeout=0):
            if request.data is None:
                probes.append(request.full_url)
                return Response({"models": [{"name": "qwen3.5:9b"}]})
            calls.append(json.loads(request.data.decode()))
            return Response({"choices": [{"message": {"content": json.dumps({"groups": [{"group_id": "1", "category": "course", "reason": "课程"}, {"group_id": "2", "category": "market", "reason": "交易"}]})}}]})
        settings = Settings(llm_enabled=True, llm_backend="openai", llm_timeout=1, dashscope_model="cloud-model")
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = suggest(settings, GROUPS)
        self.assertEqual(result["source"], "llm")
        self.assertEqual(calls[0]["model"], "cloud-model")
        self.assertEqual(probes, [], "openai 后端不应探测本机 Ollama 的 /api/tags")

    def test_result_cache_skips_second_llm_request(self):
        calls = []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps({"choices": [{"message": {"content": json.dumps({"groups": [{"group_id": "1", "category": "course", "reason": "课程"}, {"group_id": "2", "category": "market", "reason": "交易"}]})}}]}).encode()
        def fake_urlopen(request, timeout=0):
            if request.data is None:
                return Response({"models": []})
            calls.append(request)
            return Response()
        settings = Settings(llm_enabled=True, llm_backend="ollama", llm_timeout=1)
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            first = suggest(settings, GROUPS)
            second = suggest(settings, GROUPS)
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)

    def test_llm_exception_falls_back(self):
        with mock.patch("qq_digest.run_codebuddy", side_effect=TimeoutError("timeout")):
            result = suggest(self.settings, GROUPS)
        self.assertEqual(result["source"], "heuristic"); self.assertEqual(result["groups"][0]["category"], "course"); self.assertTrue(result["groups"][0]["suggested"])

    def test_llm_garbage_falls_back(self):
        with mock.patch("qq_digest.run_codebuddy", return_value="garbage"):
            result = suggest(self.settings, GROUPS)
        self.assertEqual(result["source"], "heuristic"); self.assertEqual(len(result["groups"]), 2)

    def test_http_requires_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "x.sqlite3")
            server = TaskWebServer(Settings(web_host="127.0.0.1", web_port=0, web_token="secret"), store)
            self.assertTrue(server.start()); self.addCleanup(server.stop)
            port = server.server.server_address[1]
            request = urllib.request.Request(f"http://127.0.0.1:{port}/api/groups/suggest")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(caught.exception.code, 401)

if __name__ == "__main__":
    unittest.main()
