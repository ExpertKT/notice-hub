from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest.config import Settings  # noqa: E402
from qq_live_digest.store import Store  # noqa: E402
from qq_live_digest.timeutil import iso  # noqa: E402
from qq_live_digest.webapp import TaskWebServer, group_tasks, overview  # noqa: E402

NOW = dt.datetime(2026, 9, 30, 9, 0, 0)


class TaskStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "tasks.sqlite3")

    def test_upsert_keeps_done_status(self) -> None:
        task_id = self.store.upsert_task(
            task_key="m1",
            summary="提交实验报告",
            category="action",
            deadline=iso(NOW + dt.timedelta(hours=9)),
        )
        self.assertGreater(task_id, 0)
        self.assertTrue(self.store.set_task_status(task_id, True))
        self.store.upsert_task(task_key="m1", summary="提交实验报告（更新）", category="action")
        tasks = self.store.list_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "done")
        self.assertEqual(tasks[0]["summary"], "提交实验报告（更新）")
        self.assertEqual(self.store.task_stats()["done"], 1)

    def test_group_tasks_splits_today_week_later(self) -> None:
        self.store.upsert_task(task_key="a", summary="today", deadline=iso(NOW + dt.timedelta(hours=2)))
        self.store.upsert_task(task_key="b", summary="week", deadline=iso(NOW + dt.timedelta(days=3)))
        self.store.upsert_task(task_key="c", summary="later")
        grouped = group_tasks(self.store.list_tasks(), NOW)
        self.assertEqual([task["summary"] for task in grouped["today"]], ["today"])
        self.assertEqual([task["summary"] for task in grouped["week"]], ["week"])
        self.assertEqual([task["summary"] for task in grouped["later"]], ["later"])
        result = overview(self.store.list_tasks(), grouped, NOW)
        self.assertIn("今天", result["headline"])
        self.assertEqual(result["progress"], 0)


    def test_candidate_is_separate_from_today(self) -> None:
        self.store.upsert_task(
            task_key="candidate",
            summary="可能要交报名表",
            category="action",
            deadline=iso(NOW + dt.timedelta(hours=1)),
            status="candidate",
            confidence=0.6,
        )
        grouped = group_tasks(self.store.list_tasks(), NOW)
        self.assertEqual(len(grouped["candidates"]), 1)
        self.assertEqual(grouped["today"], [])


class TaskApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "api.sqlite3")
        self.task_id = self.store.upsert_task(
            task_key="m1",
            summary="提交实验报告",
            category="action",
            deadline=iso(dt.datetime(2026, 9, 30, 18, 0)),
            groups=["测仪2602班群"],
            evidence="请各班班长今天18:00前提交实验报告",
        )
        self.settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "学院通知群"},
            web_host="127.0.0.1",
            web_port=0,
            web_token="secret",
        )
        self.server = TaskWebServer(self.settings, self.store)
        self.assertTrue(self.server.start())
        self.addCleanup(self.server.stop)
        assert self.server.server is not None
        self.base = f"http://127.0.0.1:{self.server.server.server_address[1]}"

    def _get(self, path: str, token: str = "") -> dict:
        headers = {"X-Token": token} if token else {}
        request = urllib.request.Request(self.base + path, headers=headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_requires_token(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as context:
            self._get("/api/tasks")
        self.assertEqual(context.exception.code, 401)

    def test_list_tasks_and_complete(self) -> None:
        with mock.patch("qq_live_digest.webapp.now_local", return_value=NOW):
            data = self._get("/api/tasks", token="secret")
            self.assertEqual(len(data["today"]), 1)
            self.assertEqual(data["today"][0]["summary"], "提交实验报告")
            self.assertIn("今天", data["today"][0]["deadline_text"])

            payload = json.dumps({"done": True}).encode("utf-8")
            request = urllib.request.Request(
                f"{self.base}/api/tasks/{self.task_id}",
                data=payload,
                headers={"X-Token": "secret", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                result = json.loads(response.read().decode("utf-8"))
            self.assertTrue(result["ok"])

            data = self._get("/api/tasks", token="secret")
            self.assertEqual(len(data["done"]), 1)
            self.assertEqual(len(data["today"]), 0)

    def test_candidate_group_and_actions(self) -> None:
        candidate_id = self.store.upsert_task(
            task_key="candidate-1",
            summary="可能要交报名表",
            category="action",
            deadline=iso(NOW + dt.timedelta(hours=3)),
            groups=["班级群"],
            evidence="记得看一下报名表",
            status="candidate",
            confidence=0.66,
            classification_reason="可能出现弱行动词",
        )
        with mock.patch("qq_live_digest.webapp.now_local", return_value=NOW):
            data = self._get("/api/tasks", token="secret")
        self.assertEqual(len(data["candidates"]), 1)
        self.assertEqual(data["candidates"][0]["id"], candidate_id)
        self.assertIn("把握", data["candidates"][0]["confidence_text"])
        self.assertIn("弱行动词", data["candidates"][0]["confidence_reason"])

        payload = json.dumps({"action": "confirm"}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base}/api/tasks/{candidate_id}",
            data=payload,
            headers={"X-Token": "secret", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            result = json.loads(response.read().decode("utf-8"))
        self.assertTrue(result["ok"])
        self.assertEqual(self.store.get_task(candidate_id)["status"], "open")

    def test_calendar_page_and_ics_are_served(self) -> None:
        request = urllib.request.Request(f"{self.base}/calendar?token=secret")
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            page = response.read().decode("utf-8")
        self.assertIn("月历", page)
        # 月历页必须像主页一样把 token 带进 /api/tasks，否则页面永远空白
        self.assertIn("X-Token", page)
        self.store.upsert_task(
            task_key="all-day", summary="逗号,分号;反斜\\线\n长文本" * 12,
            deadline="2026-10-01",
        )
        request = urllib.request.Request(f"{self.base}/calendar.ics?token=secret")
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.headers.get_content_type(), "text/calendar")
            raw = response.read()
        self.assertIn(b"\r\n", raw)
        text = raw.decode("utf-8")
        self.assertEqual(text.count("BEGIN:VEVENT"), 2)
        self.assertIn("DTSTART;VALUE=DATE:20261001", text)
        self.assertIn("DTEND;VALUE=DATE:20261002", text)
        self.assertIn("SUMMARY:", text)
        self.assertIn("STATUS:NEEDS-ACTION", text)
        self.assertIn("\\\\", text)
        self.assertIn("\\,", text)
        self.assertIn("\\;", text)
        self.assertTrue(all(len(line.encode("utf-8")) <= 75 for line in text.split("\r\n") if line))

    def test_page_is_served(self) -> None:
        request = urllib.request.Request(f"{self.base}/?token=secret")
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read().decode("utf-8")
        self.assertIn("群消息待办", body)
        self.assertIn("/api/tasks", body)

    def test_pwa_assets_are_public(self) -> None:
        for path, content_type in (
            ("/manifest.webmanifest", "application/manifest+json"),
            ("/icon.svg", "image/svg+xml"),
            ("/sw.js", "text/javascript"),
        ):
            with urllib.request.urlopen(self.base + path, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(content_type, response.headers.get_content_type())
                self.assertTrue(response.read())


    def test_task_correction_records_feedback(self) -> None:
        payload = json.dumps({
            "action": "correct", "correction": "category", "value": "academic"
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base}/api/tasks/{self.task_id}",
            data=payload,
            headers={"X-Token": "secret", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            result = json.loads(response.read().decode("utf-8"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "correct")
        task = self.store.get_task(self.task_id)
        assert task is not None
        self.assertEqual(task["category"], "academic")
        events = self.store.list_task_events(self.task_id)
        self.assertIn("corrected", [item["event"] for item in events])


    def test_snooze_defaults_to_next_morning(self) -> None:
        payload = json.dumps({"action": "snooze"}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base}/api/tasks/{self.task_id}",
            data=payload,
            headers={"X-Token": "secret", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            result = json.loads(response.read().decode("utf-8"))
        self.assertTrue(result["ok"])
        task = self.store.get_task(self.task_id)
        assert task is not None
        parsed = dt.datetime.fromisoformat(task["snooze_until"])
        self.assertEqual((parsed.hour, parsed.minute), (7, 30))
        self.assertEqual(parsed.date(), dt.date.today() + dt.timedelta(days=1))

    def test_duplicate_correction_merges_source_groups(self) -> None:
        copy_id = self.store.upsert_task(
            task_key="m2",
            summary="提交实验报告",
            category="action",
            groups=["班级闲聊群"],
        )
        payload = json.dumps({"action": "correct", "correction": "duplicate"}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base}/api/tasks/{copy_id}",
            data=payload,
            headers={"X-Token": "secret", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertTrue(json.loads(response.read().decode("utf-8"))["ok"])
        original = self.store.get_task(self.task_id)
        assert original is not None
        self.assertIn("班级闲聊群", original["groups"])
        copy = self.store.get_task(copy_id)
        assert copy is not None
        self.assertEqual(copy["duplicate_of"], self.task_id)
        self.assertEqual(copy["duplicate_summary"], "提交实验报告")

    def test_napcat_status_unreachable_returns_error_json(self) -> None:
        with mock.patch("qq_live_digest.webapp._Handler._napcat", return_value={"ok": False, "error": "connection refused"}):
            data = self._get("/api/napcat/status", token="secret")
        self.assertFalse(data["ok"])
        self.assertIn("connection refused", data["error"])

    def test_napcat_groups_maps_group_name(self) -> None:
        result = {"ok": True, "data": [{"group_id": 123, "group_name": "通知群", "member_count": 8}]}
        with mock.patch("qq_live_digest.webapp._Handler._napcat", return_value=result):
            data = self._get("/api/napcat/groups", token="secret")
        self.assertTrue(data["ok"])
        self.assertEqual(data["groups"][0]["group_id"], "123")
        self.assertEqual(data["groups"][0]["name"], "通知群")

    def test_subscriptions_only_rewrite_target_env_lines(self) -> None:
        env_path = Path(self.tmp.name) / ".env"
        original = b"OTHER=keep\r\nQQ_DIGEST_GROUPS=old\nQQ_DIGEST_GROUP_ALIASES=old-name\r\nTAIL=\xe4\xb8\xad\n"
        env_path.write_bytes(original)
        self.server.settings.env_file = env_path
        payload = json.dumps({"groups": ["123"], "aliases": {"123": "通知群"}}).encode("utf-8")
        request = urllib.request.Request(f"{self.base}/api/subscriptions", data=payload, headers={"X-Token": "secret", "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
        self.assertTrue(data["applied"])
        expected = b"OTHER=keep\r\nQQ_DIGEST_GROUPS=123\nQQ_DIGEST_GROUP_ALIASES=123=" + "通知群".encode("utf-8") + b"\r\nTAIL=\xe4\xb8\xad\n"
        self.assertEqual(env_path.read_bytes(), expected)
        self.assertEqual(self.server.settings.group_whitelist, ("123",))
        self.assertEqual(self.server.settings.group_aliases, {"123": "通知群"})

    def test_setup_page_contains_qrcode_endpoint(self) -> None:
        request = urllib.request.Request(f"{self.base}/setup?token=secret")
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            page = response.read().decode("utf-8")
        self.assertIn("/api/napcat/qrcode", page)

    def test_setup_polling_does_not_rebuild_loaded_groups(self) -> None:
        request = urllib.request.Request(f"{self.base}/setup?token=secret")
        with urllib.request.urlopen(request, timeout=5) as response:
            page = response.read().decode("utf-8")
        self.assertIn("loaded=false", page)
        self.assertIn("if(ok&&!loaded)loadGroups();", page)
        interval = page.split("setInterval(function(){", 1)[1].split("},5000)", 1)[0]
        self.assertNotIn("loadGroups", interval)

    def test_setup_save_reports_subscription_errors(self) -> None:
        request = urllib.request.Request(f"{self.base}/setup?token=secret")
        with urllib.request.urlopen(request, timeout=5) as response:
            page = response.read().decode("utf-8")
        save_path = page.split("api('/api/subscriptions'", 1)[1].split("setInterval", 1)[0]
        self.assertIn(".catch(function(e)", save_path)
        self.assertIn("保存失败", save_path)

if __name__ == "__main__":
    unittest.main()
