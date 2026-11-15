"""待办长对话：多轮、确认、对不上不改。不打真模型。"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from noticehub.application.agent_chat import (
    AGENT_CHAT_PATH,
    _SYSTEM,
    chat_history,
    chat_ready,
    chat_turn,
    optional_get,
    optional_post,
    reset_sessions,
)
from noticehub.infrastructure.sqlite.store import Store
from noticehub.routes import POST_ROUTES
from noticehub.timeutil import now_local

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _Settings:
    def __init__(self, **extra):
        self.llm_enabled = True
        self.llm_backend = "auto"
        self.dashscope_api_key = ""
        self.dashscope_model = "unit"
        self.dashscope_endpoint = "http://127.0.0.1/v1/chat/completions"
        self.llm_timeout = 10
        self.deadline_reminders_enabled = False
        for key, value in extra.items():
            setattr(self, key, value)


def _reply(say, calls=None):
    return json.dumps({"say": say, "calls": calls or []}, ensure_ascii=False)


class AgentChatTurnTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_sessions()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "chat.sqlite3")
        self.settings = _Settings(dashscope_api_key="sk-test")

    def test_second_turn_sees_the_first(self) -> None:
        seen = []

        def poster(messages):
            seen.append(messages)
            return _reply("看到了")

        first = chat_turn(self.store, self.settings, "session1234", "明天那件是什么", poster=poster)
        chat_turn(self.store, self.settings, first["session_id"], "再说具体点", poster=poster)
        self.assertEqual([item["content"] for item in seen[1] if item["role"] == "user"], ["明天那件是什么", "再说具体点"])

    def test_trace_shows_what_it_read_and_called(self) -> None:
        # 界面上要能看见它读了什么、调了哪个工具：第一跳调 create，第二跳收尾
        replies = [
            _reply("先记一条", [{"name": "create", "summary": "交实验报告 图文信息楼B203", "deadline": "2026-11-19 15:00"}]),
            _reply("记好了"),
        ]
        result = chat_turn(
            self.store, self.settings, "session1234", "周四下午去图文信息楼交实验报告",
            poster=lambda messages: replies.pop(0),
        )
        kinds = [item["kind"] for item in result["trace"]]
        self.assertEqual(kinds[0], "read")
        self.assertIn("读了", result["trace"][0]["text"])
        calls = [item for item in result["trace"] if item["kind"] == "call"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["tool"], "create")
        self.assertTrue(calls[0]["ok"])
        self.assertIn("图文信息楼", calls[0]["args"].get("summary", ""))

    def test_fallback_has_empty_trace(self) -> None:
        settings = _Settings(llm_enabled=False, dashscope_api_key="")
        result = chat_turn(self.store, settings, "session1234", "记一件事", poster=lambda m: "")
        self.assertTrue(result["fallback"])
        self.assertEqual(result["trace"], [])

    def test_history_can_be_read_back(self) -> None:
        missing = chat_history("missing-session-id")
        self.assertEqual(missing["messages"], [])
        first = chat_turn(
            self.store, self.settings, "session1234", "明天那件是什么",
            poster=lambda messages: _reply("看到了"),
        )
        loaded = chat_history(first["session_id"])
        self.assertEqual(
            [(item["role"], item["content"]) for item in loaded["messages"]],
            [("user", "明天那件是什么"), ("assistant", "看到了")],
        )
        self.assertEqual(chat_history("missing-session-id")["messages"], [])

    def test_plain_text_does_not_write(self) -> None:
        task_id = self.store.upsert_task(task_key="manual:aaa", summary="交实验报告", source="manual", status="open")
        result = chat_turn(
            self.store, self.settings, "session1234", "帮我看看",
            poster=lambda messages: "先看实验报告",
        )
        self.assertFalse(result["changed"])
        self.assertEqual(result["say"], "先看实验报告")
        self.assertEqual(self.store.get_task(task_id)["status"], "open")

    def test_ambiguous_title_does_not_write(self) -> None:
        left = self.store.upsert_task(task_key="manual:left", summary="劳动课上午", source="manual", status="open")
        right = self.store.upsert_task(task_key="manual:right", summary="劳动课下午", source="manual", status="open")

        def poster(messages):
            if any(str(item.get("content") or "").startswith("工具结果") for item in messages):
                return _reply("有两条")
            return _reply("我来", [{"name": "act", "task_id": "劳动课", "action": "done"}])

        result = chat_turn(self.store, self.settings, "session1234", "完成劳动课", poster=poster)
        self.assertFalse(result["changed"])
        self.assertEqual(self.store.get_task(left)["status"], "open")
        self.assertEqual(self.store.get_task(right)["status"], "open")

    def test_delete_waits_until_the_next_turn(self) -> None:
        task_id = self.store.upsert_task(task_key="manual:gone", summary="要删的待办", source="manual", status="open")

        def first(messages):
            if any(str(item.get("content") or "").startswith("工具结果") for item in messages):
                return _reply("删掉了", [{"name": "act", "task_id": str(task_id), "action": "delete", "confirm": True}])
            return _reply("要删吗", [{"name": "act", "task_id": str(task_id), "action": "delete"}])

        held = chat_turn(self.store, self.settings, "session1234", "删掉要删的待办", poster=first)
        self.assertFalse(held["changed"])
        self.assertIsNotNone(self.store.get_task(task_id))

        def second(messages):
            return _reply("已删", [{"name": "act", "task_id": str(task_id), "action": "delete", "confirm": True}])

        done = chat_turn(self.store, self.settings, held["session_id"], "删", poster=second)
        self.assertTrue(done["changed"])
        self.assertIsNone(self.store.get_task(task_id))

    def test_a_personal_plan_is_created_after_the_model_refuses(self) -> None:
        def poster(messages):
            if any(str(item.get("content") or "").startswith("上一句没有调用") for item in messages):
                return _reply("已新建", [{"name": "create", "summary": "去立功堡见学长", "deadline": "明天上午"}])
            return "好的，记下了。这不在我的待办范围内，我这边只管群待办的处理。"

        result = chat_turn(self.store, self.settings, "session1234", "我明天上午要去立功堡见一个学长", poster=poster)
        self.assertTrue(result["changed"])
        self.assertNotIn("只管群", result["say"])
        tasks = [task for task in self.store.list_tasks() if "立功堡" in str(task.get("summary") or "")]
        self.assertEqual(len(tasks), 1)
        tomorrow = (now_local() + dt.timedelta(days=1)).strftime("%Y-%m-%dT09:00")
        self.assertTrue(str(tasks[0]["deadline"]).startswith(tomorrow))

    def test_read_step_reports_todays_lessons(self) -> None:
        # 排程靠课表算空档，读的那一行就得把课表也说出来，否则用户看不出它读过
        weekday = now_local().isoweekday()
        data_dir = Path(self.tmp.name) / "data"
        data_dir.mkdir()
        (data_dir / "campus_schedule.json").write_text(
            json.dumps(
                {"lessons": [{"weekday": weekday, "start": "00:00", "end": "23:59", "name": "高等数学", "place": "立功楼302"}]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        settings = _Settings(dashscope_api_key="sk-test", data_dir=str(data_dir))
        result = chat_turn(
            self.store, settings, "session1234", "今天还有什么",
            poster=lambda messages: _reply("看完了"),
        )
        self.assertIn("今天还有 1 节课", result["trace"][0]["text"])
        self.assertIn("23:59", result["trace"][0]["text"])

    def test_read_step_says_so_when_there_is_no_lesson(self) -> None:
        result = chat_turn(
            self.store, self.settings, "session1234", "今天还有什么",
            poster=lambda messages: _reply("看完了"),
        )
        self.assertIn("今天没课", result["trace"][0]["text"])

    def test_create_keeps_the_place_words_from_the_user(self) -> None:
        # 模型自己缩写标题会把「图文信息楼」弄丢，那一件事就排不进行程了
        self.assertIn("summary 保留用户原话里的地点和钟点", _SYSTEM)
        self.assertIn("能不能排进行程，靠的就是这几个字", _SYSTEM)

    def test_the_same_call_is_not_run_twice_in_one_turn(self) -> None:        # 本机模型会把上一跳那件事又说一遍；照单执行就是重复建单
        same = _reply("好的，已添加待办。", [{"name": "create", "summary": "去西门打印店取材料", "deadline": "明天上午"}])
        replies = [same, same]
        result = chat_turn(
            self.store, self.settings, "session1234", "帮我记一件：去西门打印店取材料",
            poster=lambda messages: replies.pop(0) if replies else _reply("好了"),
        )
        rows = [task for task in self.store.list_tasks() if "打印店" in str(task.get("summary") or "")]
        self.assertEqual(len(rows), 1)
        calls = [item for item in result["trace"] if item["kind"] == "call"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["tool"], "create")

    def test_saying_noted_without_a_write_is_not_reported_as_saved(self) -> None:
        result = chat_turn(
            self.store, self.settings, "session1234", "我明天上午要去立功堡见一个学长",
            poster=lambda messages: "好的，记下了。只管群待办。",
        )
        self.assertFalse(result["changed"])
        self.assertEqual(result["say"], "还没写进待办。直接说要记的标题和时间。")
        self.assertEqual(self.store.list_tasks(), [])

    def test_rename_changes_the_title(self) -> None:
        task_id = self.store.upsert_task(task_key="manual:rename", summary="见学长", source="manual", status="open")
        result = chat_turn(
            self.store, self.settings, "session1234", "把见学长改成去立功堡",
            poster=lambda messages: _reply("改好了", [{"name": "rename", "task_id": "见学长", "summary": "去立功堡见学长"}]),
        )
        self.assertTrue(result["changed"])
        self.assertEqual(self.store.get_task(task_id)["summary"], "去立功堡见学长")

    def test_open_group_task_is_not_deleted(self) -> None:
        task_id = self.store.upsert_task(task_key="digest:group", summary="劳动课", source="qq", status="open")
        seen = []

        def poster(messages):
            seen.append(messages)
            if any(str(item.get("content") or "").startswith("工具结果") for item in messages):
                return _reply("只能忽略")
            return _reply("删掉", [{"name": "act", "task_id": str(task_id), "action": "delete", "confirm": True}])

        chat_turn(self.store, self.settings, "session1234", "删掉劳动课", poster=poster)
        self.assertEqual(self.store.get_task(task_id)["status"], "open")
        self.assertIn("只能忽略", json.dumps(seen, ensure_ascii=False))

    def test_without_a_key_the_page_keeps_the_old_parser(self) -> None:
        self.assertFalse(chat_ready(_Settings()))
        result = chat_turn(self.store, _Settings(), "session1234", "完成实验")
        self.assertTrue(result["fallback"])
        self.assertFalse(result["changed"])


class AgentChatRouteTest(unittest.TestCase):
    def test_static_table_does_not_grow(self) -> None:
        text = (PROJECT_ROOT / "noticehub" / "routes.py").read_text(encoding="utf-8")
        self.assertNotIn(AGENT_CHAT_PATH, text)
        self.assertNotIn(AGENT_CHAT_PATH, POST_ROUTES)
        previous = os.environ.pop("NOTICEHUB_AGENT_SHELL", None)
        try:
            self.assertIsNone(optional_post(AGENT_CHAT_PATH))
            self.assertIsNone(optional_get(AGENT_CHAT_PATH))
        finally:
            if previous is not None:
                os.environ["NOTICEHUB_AGENT_SHELL"] = previous

    def test_http_falls_back_until_a_key_exists(self) -> None:
        previous = os.environ.pop("NOTICEHUB_AGENT_SHELL", None)
        self.addCleanup(lambda: os.environ.pop("NOTICEHUB_AGENT_SHELL", None) if previous is None else os.environ.__setitem__("NOTICEHUB_AGENT_SHELL", previous))
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        from noticehub.config import Settings
        from noticehub.webapp import TaskWebServer

        store = Store(Path(folder.name) / "api.sqlite3")
        settings = Settings(web_host="127.0.0.1", web_token="secret", web_port=0, data_dir=Path(folder.name) / "data")
        settings.dashscope_api_key = ""
        settings.llm_backend = "auto"
        server = TaskWebServer(settings, store)
        self.assertTrue(server.start())
        self.addCleanup(server.stop)
        base = "http://127.0.0.1:%s" % server.server.server_address[1]

        def post() -> tuple[int, dict]:
            request = urllib.request.Request(
                base + AGENT_CHAT_PATH,
                data=json.dumps({"text": "你好", "session_id": "session1234"}).encode(),
                headers={"Content-Type": "application/json", "X-Token": "secret"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, json.loads(response.read().decode())
            except urllib.error.HTTPError as error:
                body = json.loads(error.read().decode() or "{}")
                error.close()
                return error.code, body

        status, body = post()
        self.assertEqual(status, 404)
        os.environ["NOTICEHUB_AGENT_SHELL"] = "1"
        status, body = post()
        self.assertEqual(status, 200)
        self.assertTrue(body["fallback"])


class AgentPostTest(unittest.TestCase):
    """真正发请求那一步：本机 Ollama 要关掉思考，读超时不能断在 socket 上。"""

    def test_local_ollama_turns_reasoning_off(self) -> None:
        from noticehub.application.agent_chat import _payload

        payload = _payload(_Settings(llm_backend="ollama"), [{"role": "user", "content": "今天有什么"}])
        self.assertEqual(payload["reasoning_effort"], "none")

    def test_cloud_backend_keeps_the_request_plain(self) -> None:
        from noticehub.application.agent_chat import _payload

        payload = _payload(_Settings(llm_backend="auto"), [{"role": "user", "content": "今天有什么"}])
        self.assertNotIn("reasoning_effort", payload)

    def test_read_timeout_becomes_one_sentence(self) -> None:
        from unittest import mock

        from noticehub.application.agent_chat import _post

        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaises(RuntimeError) as caught:
                _post(_Settings(llm_backend="ollama"), [{"role": "user", "content": "今天有什么"}])
        self.assertIn("模型太慢", str(caught.exception))

    def test_local_model_gets_a_longer_read_timeout(self) -> None:
        # 35B 本机模型冷启动（装进显存）实测一分钟上下，云端那 25 秒上限会把第一轮直接掐掉
        from unittest import mock

        from noticehub.application.agent_chat import _post

        seen = []

        def fake_urlopen(request, timeout=None):
            seen.append(timeout)
            raise TimeoutError("timed out")

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            for backend in ("ollama", "auto"):
                with self.assertRaises(RuntimeError):
                    _post(
                        _Settings(llm_backend=backend, llm_timeout=120),
                        [{"role": "user", "content": "在吗"}],
                    )
        self.assertEqual(seen[0], 90)
        self.assertEqual(seen[1], 25)
