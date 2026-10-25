"""四道门向导接口的契约：前端不看后端代码也要能画出五步。

直接调 handler 方法，不起 HTTP 服务；HTTP 层（confirm/测试模式 403）由 test_webapp 回归覆盖。
"""

from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest import mock

from qq_live_digest import webapp


def _source(state: str = "ready", detail: str = "") -> dict[str, str]:
    return {"state": state, "label": "", "detail": detail, "action": ""}


class OnboardingTestBase(unittest.TestCase):
    def setUp(self) -> None:
        with webapp._onboarding_lock:
            webapp._onboarding_run.clear()
            webapp._onboarding_run.update({"state": "idle", "action": "", "message": "", "at": ""})
        patcher = mock.patch.object(webapp, "_firewall_allows", return_value={"allowed": True, "checked": True})
        self._fw = patcher.start()
        self.addCleanup(patcher.stop)

    def handler(self, sources: dict | None = None, allowed: bool = True, body: dict | None = None):
        sources = sources or {
            "qq": _source(),
            "napcat": _source(),
            "funnel": _source(),
            "push": _source(),
        }
        self._fw.return_value = {"allowed": bool(allowed), "checked": True}
        settings = SimpleNamespace(web_port=8790, push_channels=lambda: [])
        handler = object.__new__(webapp._Handler)
        handler.server = SimpleNamespace(settings=settings)
        handler._overview_payload = lambda: {"ok": True, "sources": dict(sources)}
        handler._json_body = lambda: dict(body or {})
        return handler


class NextStepTests(OnboardingTestBase):
    def test_fresh_install_points_at_qq_with_a_human_reason(self) -> None:
        sources = {
            "qq": _source("off"),
            "napcat": _source("off"),
            "funnel": _source("off"),
            "push": _source("off"),
        }
        payload = self.handler(sources)._onboarding_next_payload()
        self.assertEqual(payload["step"], "qq")
        self.assertTrue(payload["title"])
        self.assertTrue(payload["reason"])
        self.assertNotIn("state", payload["reason"])
        self.assertEqual(payload["action"], "connect_qq")
        self.assertTrue(payload["blocked"])
        self.assertFalse(payload["skippable"])
        self.assertEqual(payload["state"], "off")
        self.assertEqual(payload["done"], [])

    def test_qq_ready_moves_to_napcat(self) -> None:
        sources = {
            "qq": _source(),
            "napcat": _source("off"),
            "funnel": _source("off"),
            "push": _source("off"),
        }
        payload = self.handler(sources)._onboarding_next_payload()
        self.assertEqual(payload["step"], "napcat")
        self.assertEqual(payload["done"], ["qq"])
        self.assertEqual(payload["action"], "install_napcat")
        self.assertFalse(payload["blocked"])
        self.assertIn("引擎", payload["reason"])

    def test_push_step_is_skippable(self) -> None:
        sources = {
            "qq": _source(),
            "napcat": _source(),
            "funnel": _source(),
            "push": _source("off"),
        }
        payload = self.handler(sources)._onboarding_next_payload()
        self.assertEqual(payload["step"], "push")
        self.assertTrue(payload["skippable"])
        self.assertTrue(payload["blocked"])

    def test_everything_ready_reports_done(self) -> None:
        payload = self.handler()._onboarding_next_payload()
        self.assertEqual(payload["step"], "done")
        self.assertEqual(payload["action"], "")
        self.assertFalse(payload["skippable"])
        self.assertEqual(payload["done"], ["qq", "napcat", "tailscale", "firewall", "push"])

    def test_error_gate_puts_the_real_error_in_reason(self) -> None:
        sources = {
            "qq": _source(),
            "napcat": _source("error", "token 不对"),
            "funnel": _source("off"),
            "push": _source("off"),
        }
        payload = self.handler(sources)._onboarding_next_payload()

        self.assertEqual(payload["step"], "napcat")
        self.assertEqual(payload["state"], "error")
        self.assertEqual(payload["reason"], "token 不对")


class RunActionTests(OnboardingTestBase):
    def test_missing_confirm_is_rejected(self) -> None:
        code, payload = self.handler(body={"action": "install_napcat"})._onboarding_run_payload()
        self.assertEqual(code, 400)
        self.assertTrue(payload["needs_confirm"])

    def test_unknown_action_is_rejected(self) -> None:
        code, payload = self.handler(body={"action": "nuke", "confirm": True})._onboarding_run_payload()
        self.assertEqual(code, 400)
        self.assertIn("未知", payload["error"])

    def test_second_click_does_not_start_a_second_thread(self) -> None:
        handler = self.handler(body={"action": "install_napcat", "confirm": True})
        with mock.patch.object(webapp.threading, "Thread") as thread:
            first = handler._onboarding_run_payload()
            second = handler._onboarding_run_payload()
        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 200)
        self.assertNotIn("deduped", first[1])
        self.assertTrue(second[1]["deduped"])
        self.assertEqual(second[1]["state"], "running")
        thread.assert_called_once()

    def test_stale_running_does_not_block_forever(self) -> None:
        with webapp._onboarding_lock:
            webapp._onboarding_run.update(
                {
                    "state": "running",
                    "action": "install_napcat",
                    "message": "老的运行",
                    "started": time.monotonic() - webapp.ONBOARDING_RUNNING_TIMEOUT_SECONDS - 5,
                }
            )
        with mock.patch.object(webapp.threading, "Thread"):
            code, payload = self.handler(body={"action": "install_napcat", "confirm": True})._onboarding_run_payload()
        self.assertEqual(code, 200)
        self.assertNotIn("deduped", payload)


class StatusErrorTests(OnboardingTestBase):
    def _set_run(self, action: str, message: str = "", state: str = "running") -> None:
        with webapp._onboarding_lock:
            webapp._onboarding_run.update(
                {"state": state, "action": action, "message": message, "at": "", "started": time.monotonic()}
            )

    def test_firewall_failure_carries_an_error(self) -> None:
        self._set_run("allow_firewall")
        with mock.patch.object(
            webapp, "_firewall_status_payload", return_value={"state": "failed", "message": "托盘没响应"}
        ):
            payload = self.handler()._onboarding_status_payload()
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(payload["error"], "托盘没响应")

    def test_tailscale_failure_carries_an_error(self) -> None:
        self._set_run("open_funnel")
        setup = mock.Mock()
        setup.snapshot.return_value = {"state": "error", "error": "tailscale 起不来", "message": ""}
        with mock.patch.object(webapp, "_funnel_setup", return_value=setup):
            payload = self.handler()._onboarding_status_payload()
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(payload["error"], "tailscale 起不来")

    def test_napcat_failure_carries_an_error(self) -> None:
        self._set_run("install_napcat", "下载失败：连接超时", state="failed")
        payload = self.handler()._onboarding_status_payload()
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(payload["error"], "下载失败：连接超时")

    def test_success_has_empty_error(self) -> None:
        self._set_run("install_napcat", "1.2.3", state="done")
        payload = self.handler()._onboarding_status_payload()
        self.assertEqual(payload["state"], "done")
        self.assertEqual(payload["error"], "")


if __name__ == "__main__":
    unittest.main()
