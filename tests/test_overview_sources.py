"""overview 每个来源的判定细节：微信四分类文案、推送异常兜底、学习通过期算失败。

直接调 `_Handler._overview_payload`，不启服务：只验证「单源出错不拖垮整块」和
「文案/严重度来自统一出处」这两件事，HTTP 层的总览结构在 test_webapp 里另有覆盖。
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from qq_live_digest import webapp


def _handler(settings=None, groups=(), **payloads):
    server = SimpleNamespace(
        settings=settings or SimpleNamespace(web_port=8790, push_channels=lambda: []),
        meta_provider=lambda: {"groups": list(groups)},
    )
    handler = object.__new__(webapp._Handler)
    handler.server = server
    handler._napcat_status_payload = lambda: {"ok": True, "online": True, "stage": "online"}
    handler._wechat_status_payload = lambda: {"ok": True, "enabled": False}
    handler._dingtalk_status_payload = lambda: {"ok": True, "enabled": False}
    handler._chaoxing_status_payload = lambda: {"ok": False, "has_cookie": False}
    for name, value in payloads.items():
        setattr(handler, name, value)
    return handler


def _sources(handler):
    with mock.patch("qq_live_digest.webapp.tunnel.tailscale_executable", return_value=""), \
            mock.patch("qq_live_digest.webapp._tailscale_public_app_url", return_value=""):
        return webapp._Handler._overview_payload(handler)["sources"]


class OverviewWechatTests(unittest.TestCase):
    KEY_HINT = "别关微信，等 3–6 分钟让它在后台写一次数据，再点「立即扫描一次」。"

    def _wechat(self, **extra):
        payload = {
            "ok": True,
            "enabled": True,
            "available": True,
            "last_error": "未能从微信本地进程取得数据库密钥",
        }
        payload.update(extra)
        return _sources(_handler(_wechat_status_payload=lambda: payload))["wechat"]

    def test_error_detail_uses_category_hint_not_raw_error(self):
        source = self._wechat(error_category="key_timeout", error_hint=self.KEY_HINT)
        self.assertEqual(source["state"], "error")
        self.assertEqual(source["detail"], self.KEY_HINT)
        self.assertEqual(source["action"], "等几分钟再扫描")

    def test_each_category_offers_its_own_next_step(self):
        self.assertEqual(
            self._wechat(error_category="not_logged_in", error_hint="去登录")["action"],
            "去电脑上登录微信",
        )
        self.assertEqual(
            self._wechat(error_category="version_unsupported", error_hint="升级")["action"],
            "升级微信到 4.0",
        )

    def test_unknown_category_falls_back_to_rescan_and_keeps_last_error(self):
        source = self._wechat(error_category="")
        self.assertEqual(source["detail"], "未能从微信本地进程取得数据库密钥")
        self.assertEqual(source["action"], "去设置页点「立即扫描一次」")


class OverviewDingtalkTests(unittest.TestCase):
    def test_listener_down_points_at_the_real_button(self):
        # 已启用、有群、但监听没起来：next step 必须指设置页里真实存在的「立即扫描一次」。
        source = _sources(_handler(_dingtalk_status_payload=lambda: {
            "ok": True,
            "enabled": True,
            "running": False,
            "groups": 3,
        }))["dingtalk"]
        self.assertEqual(source["state"], "needs_setup")
        self.assertEqual(source["action"], "去设置页点「立即扫描一次」")


class OverviewPushTests(unittest.TestCase):
    def test_push_read_failure_does_not_break_the_whole_overview(self):
        settings = SimpleNamespace(
            web_port=8790,
            push_channels=mock.Mock(side_effect=RuntimeError("推送凭据文件坏了")),
        )
        sources = _sources(_handler(settings))
        self.assertEqual(set(sources), {"qq", "napcat", "wechat", "dingtalk", "chaoxing", "funnel", "push"})
        self.assertEqual(sources["push"]["state"], "error")
        self.assertIn("推送凭据文件坏了", sources["push"]["detail"])
        self.assertEqual(sources["push"]["action"], "重启程序后重试")
        self.assertEqual(sources["wechat"]["state"], "off")

    def test_push_configured_channels_stay_ready(self):
        settings = SimpleNamespace(web_port=8790, push_channels=lambda: ["飞书"])
        sources = _sources(_handler(settings))
        self.assertEqual(sources["push"]["state"], "ready")
        self.assertIn("飞书", sources["push"]["detail"])


class OverviewChaoxingTests(unittest.TestCase):
    def test_expired_cookie_is_error_not_needs_setup(self):
        sources = _sources(
            _handler(_chaoxing_status_payload=lambda: {"ok": False, "has_cookie": True, "expired": True})
        )
        self.assertEqual(sources["chaoxing"]["state"], "error")
        self.assertEqual(sources["chaoxing"]["detail"], "登录已过期，需重新读取")
        self.assertEqual(sources["chaoxing"]["action"], "重新读取学习通 Cookie")


if __name__ == "__main__":
    unittest.main()
