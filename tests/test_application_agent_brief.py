"""今况句子。不经过 HTTP。"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from noticehub.application.agent_brief import AGENT_BRIEF_PATH, agent_brief  # noqa: E402
from noticehub.domain.time import iso  # noqa: E402
from noticehub.routes import GET_ROUTES  # noqa: E402
from noticehub.store import Store  # noqa: E402

NOW = dt.datetime(2026, 11, 11, 9, 0, 0)


def _task(**overrides: object) -> dict:
    item = {
        "id": 1,
        "summary": "交实验报告",
        "deadline": "2026-11-11T18:00:00",
        "source": "qq_message",
        "status": "open",
        "urgent": None,
        "category": "info",
    }
    item.update(overrides)
    return item


class AgentBriefTest(unittest.TestCase):
    def test_today_and_unconfirmed_share_one_sentence(self) -> None:
        brief = agent_brief(
            [
                _task(id=1, summary="交实验报告"),
                _task(id=2, summary="社团报名", deadline="2026-11-11T12:00:00"),
                _task(id=3, summary="改课件", deadline="2026-11-12T09:00:00"),
                _task(id=4, status="candidate", summary="要不要报名", deadline="", source="wechat"),
            ],
            now=NOW,
        )
        self.assertEqual(brief["sentence"], "今天 2 件待办，1 条还要你确认")
        self.assertEqual(brief["action"], "confirm")
        self.assertEqual(brief["count"], 1)
        self.assertEqual([item["id"] for item in brief["items"]], [4])
        self.assertEqual(set(brief["items"][0]), {"id", "summary", "deadline", "source"})
        self.assertEqual(brief["items"][0]["source"], "wechat")
        self.assertEqual(brief["updated_at"], iso(NOW))

    def test_overdue_is_part_of_the_sentence(self) -> None:
        brief = agent_brief(
            [_task(id=7, deadline="2026-11-10T18:00:00", summary="补交")],
            now=NOW,
        )
        self.assertEqual(brief["sentence"], "今天 1 件待办，逾期 1 件")
        self.assertEqual(brief["action"], "open")
        self.assertEqual(brief["count"], 1)

    def test_open_tasks_without_a_due_date_today(self) -> None:
        brief = agent_brief(
            [_task(id=8, deadline="", summary="有空再看")],
            now=NOW,
        )
        self.assertEqual(brief["sentence"], "今天没有到期的事")
        self.assertEqual(brief["action"], "open")
        self.assertEqual(brief["count"], 0)
        self.assertEqual(brief["items"][0]["summary"], "有空再看")

    def test_finished_tasks_do_not_count(self) -> None:
        brief = agent_brief(
            [
                _task(id=1, status="done"),
                _task(id=2, status="dismissed"),
                _task(id=3, status="expired"),
            ],
            now=NOW,
        )
        self.assertEqual(brief["sentence"], "所有待办都清空了")
        self.assertEqual(brief["action"], "none")
        self.assertEqual(brief["count"], 0)
        self.assertEqual(brief["items"], [])

    def test_latest_digest_is_used_only_when_nothing_is_open(self) -> None:
        empty = agent_brief([], now=NOW, latest_digest={"item_count": 4})
        self.assertEqual(empty["sentence"], "刚整理了 4 条重点")
        self.assertEqual(empty["action"], "none")
        self.assertEqual(empty["count"], 0)
        busy = agent_brief(
            [_task(id=9, status="candidate", summary="确认一下", deadline="")],
            now=NOW,
            latest_digest={"item_count": 4},
        )
        self.assertEqual(busy["sentence"], "1 条还要你确认")

    def test_running_job_prefixes_the_same_sentence(self) -> None:
        tasks = [_task(id=5, status="candidate", summary="这场要不要去", deadline="")]
        history = agent_brief(tasks, now=NOW, job={"kind": "history", "running": True})
        self.assertEqual(history["sentence"], "正在回溯群消息。1 条还要你确认")
        self.assertEqual(history["action"], "confirm")
        classify = agent_brief(tasks, now=NOW, job={"kind": "classify", "running": False})
        self.assertEqual(classify["sentence"], "1 条还要你确认")
        idle = agent_brief([], now=NOW, job={"kind": "classify", "running": True})
        self.assertEqual(idle["sentence"], "正在提取通知。")
        self.assertEqual(idle["action"], "none")

    def test_items_stop_at_three_and_urgent_comes_first(self) -> None:
        brief = agent_brief(
            [
                _task(id=1, status="candidate", summary="甲", deadline="2026-11-11T10:00:00"),
                _task(id=2, status="candidate", summary="乙", deadline="2026-11-11T08:00:00", category="urgent"),
                _task(id=3, status="candidate", summary="丙", deadline="2026-11-11T09:00:00"),
                _task(id=4, status="candidate", summary="丁", deadline="2026-11-11T07:00:00"),
            ],
            now=NOW,
        )
        self.assertEqual(brief["count"], 4)
        self.assertEqual([item["summary"] for item in brief["items"]], ["乙", "丁", "丙"])

    def test_store_rows_use_the_same_sentence(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "brief.sqlite3")
        store.upsert_task(
            task_key="manual:aaaaaaaaaaaaaaaa",
            summary="交实验报告",
            deadline="2026-11-11T18:00:00",
            source="qq_message",
            status="open",
        )
        store.upsert_task(
            task_key="rule:1:m1",
            summary="要不要报名",
            source="wechat",
            status="candidate",
        )
        brief = agent_brief(store.list_tasks(), now=NOW)
        self.assertEqual(brief["sentence"], "今天 1 件待办，1 条还要你确认")
        self.assertEqual(brief["action"], "confirm")
        self.assertEqual(brief["items"][0]["source"], "wechat")


class AgentShellSwitchTest(unittest.TestCase):
    """默认开：没设、空串、1 都算开；只有 0 关。"""

    def _enabled(self, value: str | None) -> bool:
        from noticehub.application.agent_brief import agent_shell_enabled

        if value is None:
            env = {key: item for key, item in os.environ.items() if key != "NOTICEHUB_AGENT_SHELL"}
        else:
            env = {**os.environ, "NOTICEHUB_AGENT_SHELL": value}
        with patch.dict(os.environ, env, clear=True):
            return agent_shell_enabled()

    def test_unset_means_on(self) -> None:
        self.assertTrue(self._enabled(None))

    def test_empty_means_on(self) -> None:
        self.assertTrue(self._enabled(""))

    def test_one_means_on(self) -> None:
        self.assertTrue(self._enabled("1"))

    def test_zero_means_off(self) -> None:
        self.assertFalse(self._enabled("0"))

    def test_zero_with_spaces_still_off(self) -> None:
        self.assertFalse(self._enabled("  0  "))

    def test_route_is_served_without_the_switch(self) -> None:
        from noticehub.application.agent_brief import optional_get

        env = {key: item for key, item in os.environ.items() if key != "NOTICEHUB_AGENT_SHELL"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(optional_get(AGENT_BRIEF_PATH), ("_route_get_api_agent_brief", False))


class AgentBriefRouteTest(unittest.TestCase):
    def test_static_table_does_not_grow(self) -> None:
        text = (PROJECT_ROOT / "noticehub" / "routes.py").read_text(encoding="utf-8")
        self.assertNotIn(AGENT_BRIEF_PATH, text)
        self.assertNotIn(AGENT_BRIEF_PATH, GET_ROUTES)
        # 63 条是加位图图标之前的数；PWA 装了 4 个图标路由（/icon-192.png 等），现在 67。
        self.assertEqual(len(GET_ROUTES), 67)


class AgentBriefHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.previous = os.environ.get("NOTICEHUB_AGENT_SHELL")
        os.environ["NOTICEHUB_AGENT_SHELL"] = "0"  # 这一组验的是关掉时的表现；默认是开的
        self.addCleanup(self._restore_env)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from noticehub.config import Settings
        from noticehub.webapp import TaskWebServer, _JOB_STATE

        self.store = Store(Path(self.tmp.name) / "api.sqlite3")
        self.store.upsert_task(
            task_key="manual:bbbbbbbbbbbbbbbb",
            summary="交实验报告",
            deadline="2026-11-11T18:00:00",
            source="qq_message",
            status="open",
        )
        self.settings = Settings(
            web_host="127.0.0.1",
            web_token="secret",
            web_port=0,
            data_dir=Path(self.tmp.name) / "data",
        )
        self.server = TaskWebServer(self.settings, self.store)
        self.assertTrue(self.server.start())
        self.addCleanup(self.server.stop)
        assert self.server.server is not None
        self.base = f"http://127.0.0.1:{self.server.server.server_address[1]}"
        self.job_state = _JOB_STATE
        self.saved_job = {
            "active": _JOB_STATE.get("active"),
            "history": dict(_JOB_STATE["history"]),
            "classify": dict(_JOB_STATE["classify"]),
        }
        self.addCleanup(self._restore_job)

    def _restore_env(self) -> None:
        if self.previous is None:
            os.environ.pop("NOTICEHUB_AGENT_SHELL", None)
        else:
            os.environ["NOTICEHUB_AGENT_SHELL"] = self.previous

    def _restore_job(self) -> None:
        self.job_state["active"] = self.saved_job["active"]
        self.job_state["history"].update(self.saved_job["history"])
        self.job_state["classify"].update(self.saved_job["classify"])

    def _get(self, token: str = "secret") -> tuple[int, dict]:
        request = urllib.request.Request(
            self.base + AGENT_BRIEF_PATH,
            headers={"X-Token": token} if token else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8")
            error.close()
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = {"error": raw}
            return error.code, body

    def test_switch_off_is_not_found_after_auth(self) -> None:
        status, body = self._get(token="")
        self.assertEqual(status, 401)
        status, body = self._get()
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not found")

    def test_route_is_there_without_any_setting(self) -> None:
        del os.environ["NOTICEHUB_AGENT_SHELL"]
        from noticehub import webapp

        with patch.object(webapp, "now_local", return_value=NOW):
            status, body = self._get()
        self.assertEqual(status, 200)
        self.assertEqual(body["sentence"], "今天 1 件待办")

    def test_switch_returns_the_same_sentence_as_the_function(self) -> None:
        os.environ["NOTICEHUB_AGENT_SHELL"] = "1"
        from noticehub import webapp

        with patch.object(webapp, "now_local", return_value=NOW):
            status, body = self._get()
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"sentence", "action", "count", "items", "updated_at"})
        self.assertEqual(body["sentence"], "今天 1 件待办")
        self.assertEqual(body["action"], "open")
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["updated_at"], iso(NOW))
        denied, _ = self._get(token="")
        self.assertEqual(denied, 401)

    def test_running_history_job_is_visible_on_the_route(self) -> None:
        os.environ["NOTICEHUB_AGENT_SHELL"] = "1"
        self.job_state["active"] = "history"
        self.job_state["history"]["running"] = True
        from noticehub import webapp

        with patch.object(webapp, "now_local", return_value=NOW):
            status, body = self._get()
        self.assertEqual(status, 200)
        self.assertEqual(body["sentence"], "正在回溯群消息。今天 1 件待办")

    def _page(self) -> str:
        request = urllib.request.Request(self.base + "/", headers={"X-Token": "secret"})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            return response.read().decode("utf-8")

    def test_homepage_omits_the_strip_until_the_switch_is_on(self) -> None:
        quiet = self._page()
        self.assertNotIn('id="agent-brief"', quiet)
        self.assertNotIn("loadAgentBrief", quiet)
        os.environ["NOTICEHUB_AGENT_SHELL"] = "1"
        shown = self._page()
        self.assertIn('id="agent-brief"', shown)
        self.assertIn("loadAgentBrief();", shown)
        self.assertNotIn("AGENT_BRIEF_START", shown)

    def _floatball(self) -> str:
        request = urllib.request.Request(self.base + "/floatball", headers={"X-Token": "secret"})
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.read().decode("utf-8")

    def test_floatball_reads_the_brief_only_when_the_switch_is_on(self) -> None:
        quiet = self._floatball()
        self.assertNotIn('id="agent-brief"', quiet)
        self.assertNotIn("loadBrief", quiet)
        self.assertNotIn("/api/agent/brief", quiet)
        self.assertIn("badgeTotal", quiet)
        os.environ["NOTICEHUB_AGENT_SHELL"] = "1"
        shown = self._floatball()
        self.assertIn('id="agent-brief"', shown)
        self.assertIn("loadBrief();", shown)
        self.assertIn("/api/agent/brief", shown)
        self.assertIn("badge.textContent=String(count)", shown)
        self.assertIn("-webkit-line-clamp:2", shown)
        self.assertNotIn("AGENT_BRIEF_START", shown)

