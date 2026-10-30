"""盘点出的零覆盖接口：真起 TaskWebServer、真发 HTTP，不 mock handler。

覆盖：/api/notices、/api/insights、/api/chaoxing/cookie/status、
/api/onboarding/status、/api/pair/deny（**POST**，路线表 routes.py:100），
以及本批新增的 /api/inbox/delete、/api/tasks/clear-finished、
/api/inbox/classify/cancel 的未鉴权与非法入参边界。
"""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from qq_live_digest import pairing, webapp
from qq_live_digest.config import Settings
from qq_live_digest.store import Store
from qq_live_digest.webapp import TaskWebServer


class _ServerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "routes.sqlite3")
        self.settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "学院通知群"},
            web_host="127.0.0.1",
            web_port=0,
            web_token="secret",
            data_dir=Path(self.tmp.name) / "data",
        )
        self.server = TaskWebServer(self.settings, self.store)
        self.assertTrue(self.server.start())
        self.addCleanup(self.server.stop)
        assert self.server.server is not None
        self.base = f"http://127.0.0.1:{self.server.server.server_address[1]}"

    def _get(self, path: str, token: str = "secret") -> tuple[int, dict]:
        request = urllib.request.Request(self.base + path, headers={"X-Token": token} if token else {})
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def _post(self, path: str, payload: dict, token: str = "secret") -> tuple[int, dict]:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"X-Token": token, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def _insert_digest(self, body: str = "正文", items: int = 1) -> int:
        payload = [{"summary": "条目 %d" % i} for i in range(items)]
        return self.store.insert_digest(
            kind="window",
            window_start="2026-10-01T08:00:00",
            window_end="2026-10-01T08:10:00",
            body=body,
            payload=payload,
            message_count=items,
            llm_used=False,
        )


class NoticesTests(_ServerCase):
    def test_empty_store_returns_empty_items_list(self) -> None:
        status, body = self._get("/api/notices")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"items": []})

    def test_items_shape_limit_and_newest_first(self) -> None:
        for index in range(9):
            self._insert_digest(body="正文 %d" % index, items=3)
        status, body = self._get("/api/notices")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["items"]), 8, "handler 固定 limit=8")
        ids = [item["id"] for item in body["items"]]
        self.assertEqual(ids, sorted(ids, reverse=True), "最新的在前（ORDER BY id DESC）")
        first = body["items"][0]
        self.assertEqual(set(first), {"id", "kind", "created_at", "summary", "body"})
        self.assertEqual(first["summary"], "3 条重点")
        self.assertEqual(first["kind"], "window")

    def test_body_is_truncated_to_600_chars(self) -> None:
        self._insert_digest(body="长" * 1000)
        _status, body = self._get("/api/notices")
        self.assertEqual(len(body["items"][0]["body"]), 600)

    def test_broken_store_degrades_to_200_with_error_text(self) -> None:
        """读库失败不能断连接：回 200 + 空列表 + 给用户看的原因，前端才能显示。"""
        def boom(*_args, **_kwargs):
            raise OSError("database is locked")

        original = self.store.recent_digests
        self.store.recent_digests = boom  # type: ignore[assignment]
        self.addCleanup(setattr, self.store, "recent_digests", original)
        status, body = self._get("/api/notices")
        self.assertEqual(status, 200, "不能断连接，也不能回 500")
        self.assertEqual(body["items"], [])
        self.assertIn("读取推送记录失败", body["error"])
        self.assertIn("database is locked", body["error"], "要把库的原话带出来")


class InsightsTests(_ServerCase):
    def test_empty_data_returns_zeroed_stats(self) -> None:
        status, body = self._get("/api/insights")
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"stats", "insights"})
        self.assertEqual(body["stats"]["days"], 30)
        self.assertEqual(body["stats"]["total"], 0)
        self.assertEqual(body["stats"]["by_type"], {})
        self.assertEqual(body["stats"]["by_group"], {})
        self.assertEqual(body["insights"], [])

    def test_days_parameter_is_clamped_and_bad_values_default(self) -> None:
        for query, expected in (("?days=0", 1), ("?days=9999", 365), ("?days=abc", 30), ("?days=7", 7)):
            with self.subTest(query=query):
                status, body = self._get("/api/insights" + query)
                self.assertEqual(status, 200)
                self.assertEqual(body["stats"]["days"], expected)


class ChaoxingCookieStatusTests(_ServerCase):
    def test_unconfigured_cookie(self) -> None:
        status, body = self._get("/api/chaoxing/cookie/status")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertFalse(body["has_cookie"])
        self.assertEqual(body["result"], "未配置")
        self.assertEqual(body["last_checked_at"], "")

    def test_configured_but_unverified_cookie(self) -> None:
        self.settings.chaoxing_cookie = "sessionid=abc123"
        status, body = self._get("/api/chaoxing/cookie/status")
        self.assertEqual(status, 200)
        self.assertTrue(body["has_cookie"])
        self.assertEqual(body["result"], "未校验", "换了 Cookie 但没自检过")
        self.assertEqual(body["error"], "")


class OnboardingStatusTests(_ServerCase):
    def test_first_visit_is_idle(self) -> None:
        with webapp._onboarding_lock:
            saved = dict(webapp._onboarding_run)
            webapp._onboarding_run.update(action="", state="idle", message="")
        self.addCleanup(lambda: webapp._onboarding_run.update(saved))
        status, body = self._get("/api/onboarding/status")
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"ok", "state", "action", "message", "error"})
        self.assertEqual(body["state"], "idle")
        self.assertEqual(body["action"], "")
        self.assertEqual(body["error"], "")

    def test_failed_state_exposes_message_as_error(self) -> None:
        with webapp._onboarding_lock:
            saved = dict(webapp._onboarding_run)
            webapp._onboarding_run.update(action="install_napcat", state="failed", message="下载失败")
        self.addCleanup(lambda: webapp._onboarding_run.update(saved))
        status, body = self._get("/api/onboarding/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "failed")
        self.assertEqual(body["action"], "install_napcat")
        self.assertEqual(body["error"], "下载失败", "失败态要把原因顶到 error，前端才有话可说")


class PairDenyTests(_ServerCase):
    def test_deny_is_post_only(self) -> None:
        """盘点说它是 GET；实际路线表里是 POST，GET 不该命中。"""
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._get("/api/pair/deny")
        self.assertNotEqual(caught.exception.code, 200)

    def test_non_six_digit_code_is_rejected(self) -> None:
        for code in ("12345", "1234567", "abcdef", ""):
            with self.subTest(code=code):
                status, body = self._post("/api/pair/deny", {"code": code})
                self.assertEqual(status, 400)
                self.assertFalse(body["ok"])
                self.assertEqual(body["error"], "配对码必须是 6 位数字")

    def test_unknown_code_does_not_change_pending_state(self) -> None:
        before = pairing.pending()
        status, body = self._post("/api/pair/deny", {"code": "000000"})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])
        self.assertEqual(pairing.pending(), before, "拒绝一个不存在的配对码不能动任何状态")


class InboxDeleteBoundaryTests(_ServerCase):
    """未鉴权必须 401/403 **且一条都不删**；非法入参 400 且不动库。"""

    def setUp(self) -> None:
        super().setUp()
        for msg_id in ("m1", "m2"):
            self.store.insert_message(msg_id=msg_id, group_id="g1", content="正文 " + msg_id, group_name="学院通知群")
            self.store.save_mark(msg_id, "notice", reason="含截止时间", method="llm")

    def _mark_count(self) -> int:
        counts = self.store.inbox_counts()
        return counts["notice"] + counts["suspect"] + counts["noise"]

    def test_delete_without_token_is_rejected_and_keeps_rows(self) -> None:
        before = self._mark_count()
        status, body = self._post("/api/inbox/delete", {"msg_ids": ["m1"]}, token="")
        self.assertIn(status, (401, 403))
        self.assertFalse(body["ok"])
        self.assertEqual(self._mark_count(), before, "未鉴权请求不能删掉任何一条")
        self.assertEqual({row["msg_id"] for row in self.store.inbox_items()}, {"m1", "m2"})

    def test_invalid_payloads_are_rejected_without_side_effects(self) -> None:
        before = self._mark_count()
        cases = {
            "msg_ids 不是数组": {"msg_ids": "m1"},
            "空数组": {"msg_ids": []},
            "超过 200 条": {"msg_ids": ["m%d" % index for index in range(201)]},
            "单个编号超长": {"msg_ids": ["x" * 201]},
        }
        for label, payload in cases.items():
            with self.subTest(label=label):
                status, body = self._post("/api/inbox/delete", payload)
                self.assertEqual(status, 400)
                self.assertFalse(body["ok"])
                self.assertEqual(self._mark_count(), before, "非法入参不能删任何东西")


class TasksClearFinishedBoundaryTests(_ServerCase):
    def setUp(self) -> None:
        super().setUp()
        self.store.upsert_task(task_key="done-1", summary="已完成的作业", status="done")
        self.store.upsert_task(task_key="open-1", summary="还没做的作业", status="open")

    def _task_count(self) -> int:
        return len(self.store.list_tasks(limit=None))

    def test_clear_finished_without_token_keeps_tasks(self) -> None:
        before = self._task_count()
        status, body = self._post("/api/tasks/clear-finished", {}, token="")
        self.assertIn(status, (401, 403))
        self.assertFalse(body["ok"])
        self.assertEqual(self._task_count(), before, "未鉴权请求不能清掉任何任务")
        self.assertEqual({task["status"] for task in self.store.list_tasks(limit=None)}, {"done", "open"})

    def test_clear_finished_removes_only_finished(self) -> None:
        status, body = self._post("/api/tasks/clear-finished", {})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "removed": 1})
        self.assertEqual([task["status"] for task in self.store.list_tasks(limit=None)], ["open"])


class ClassifyCancelBoundaryTests(_ServerCase):
    def setUp(self) -> None:
        super().setUp()
        with webapp._JOB_LOCK:
            self._saved_job = (
                webapp._JOB_STATE["active"],
                webapp._JOB_STATE["cancel"],
                dict(webapp._JOB_STATE["classify"]),
            )
            webapp._JOB_STATE["active"] = None
            webapp._JOB_STATE["cancel"] = None
            webapp._JOB_STATE["classify"].update(running=False, stage="idle")
        self.addCleanup(self._restore_job)

    def _restore_job(self) -> None:
        with webapp._JOB_LOCK:
            active, cancel, classify = self._saved_job
            webapp._JOB_STATE["active"] = active
            webapp._JOB_STATE["cancel"] = cancel
            webapp._JOB_STATE["classify"].clear()
            webapp._JOB_STATE["classify"].update(classify)

    def test_cancel_without_token_is_rejected(self) -> None:
        status, body = self._post("/api/inbox/classify/cancel", {}, token="")
        self.assertIn(status, (401, 403))
        self.assertFalse(body["ok"])

    def test_cancel_without_running_job_reports_false(self) -> None:
        status, body = self._post("/api/inbox/classify/cancel", {})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "cancelled": False})

    def test_cancel_twice_is_idempotent(self) -> None:
        for _ in range(2):
            status, body = self._post("/api/inbox/classify/cancel", {})
            self.assertEqual(status, 200)
            self.assertEqual(body, {"ok": True, "cancelled": False})


if __name__ == "__main__":
    unittest.main()
