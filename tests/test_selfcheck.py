import unittest
from types import SimpleNamespace

from noticehub.selfcheck import check, feature_items


class SelfcheckTests(unittest.TestCase):
    def settings(self, **overrides):
        values = {
            "dingtalk_enabled": False,
            "dingtalk_group_whitelist": (),
            "wechat_enabled": False,
            "wechat_group_whitelist": (),
            "chaoxing_enabled": False,
            "chaoxing_cookie": "",
            "attachments_enabled": True,
            "attachment_max_mb": 10,
            "attachment_max_per_hour": 30,
            "vision_active": False,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_fixed_eight_items_and_labels(self):
        items = feature_items(self.settings())
        self.assertEqual(
            [(item["key"], item["label"]) for item in items],
            [("qq", "QQ 登录"), ("hosting", "引擎托管"), ("dingtalk", "钉钉"),
             ("wechat", "微信"), ("chaoxing", "学习通登录信息"), ("attachments", "附件解析"),
             ("calendar", "公网日历"), ("push", "消息推送")],
        )
        self.assertTrue(all(set(item) == {"key", "label", "state", "value", "hint"} for item in items))

    def test_qq_diagnosis_first_break_is_passed_through(self):
        item = feature_items(self.settings(), doctor={"ok": False, "broken": "NapCat", "hint": "启动服务"})[0]
        self.assertEqual((item["state"], item["value"], item["hint"]), ("bad", "NapCat", "启动服务"))

    def test_wechat_scan_failure_hint_uses_category_action_not_fallback(self):
        fallback = "到「设置 → 微信」点「立即扫描一次」看具体报错。"
        item = feature_items(
            self.settings(wechat_enabled=True, wechat_group_whitelist=("g1",)),
            wechat={
                "available": True,
                "running": False,
                "groups": 1,
                "scan_state": "error",
                "last_error": "未能从微信本地进程取得数据库密钥",
                "error_category": "key_timeout",
                "error_hint": "别关微信，等 3–6 分钟让它在后台写一次数据，再点「重新扫描微信群」。",
            },
        )[3]
        self.assertEqual((item["state"], item["value"]), ("bad", "上一次扫描失败"))
        self.assertNotEqual(item["hint"], fallback)
        self.assertIn("3–6 分钟", item["hint"])

    def test_enabled_dingtalk_without_selected_groups_is_not_misreported_as_unstarted(self):
        item = feature_items(self.settings(dingtalk_enabled=True), dingtalk={"enabled": True, "running": False})[2]
        self.assertEqual((item["state"], item["value"]), ("warn", "已启用，但没勾群"))
        self.assertIn("点「立即扫描」也不会动", item["hint"])

    def test_enabled_wechat_without_selected_groups_is_not_misreported_as_unstarted(self):
        item = feature_items(self.settings(wechat_enabled=True), wechat={"enabled": True, "available": True, "running": False})[3]
        self.assertEqual((item["state"], item["value"]), ("warn", "已启用，但没勾群"))
        self.assertIn("点「立即扫描」也不会动", item["hint"])

    def test_push_row_without_channels_says_what_to_configure_and_where(self):
        item = feature_items(self.settings(), push_channels=[])[7]
        self.assertEqual((item["key"], item["state"]), ("push", "warn"))
        # 不再只说「未配置」：说清缺什么（没有通道、登录 QQ 不算）与去哪配（设置 → 提醒怎么送到手机）。
        self.assertIn("通道", item["value"])
        self.assertEqual(item["value"], "没有推送通道")
        self.assertIn("只登录 QQ 不算", item["hint"])
        self.assertIn("提醒怎么送到手机", item["hint"])
        for name in ("Server酱", "WxPusher", "PushPlus", "回调地址"):
            self.assertIn(name, item["hint"])

    def test_push_row_with_channels_names_them(self):
        item = feature_items(self.settings(), push_channels=["wxpusher", "serverchan"])[7]
        self.assertEqual((item["state"], item["value"], item["hint"]), ("ok", "wxpusher、serverchan", ""))

    def test_check_returns_fixed_contract(self):
        result = check(self.settings())
        self.assertEqual(set(result), {"ok", "items"})
        self.assertIs(result["ok"], True)
        self.assertEqual(len(result["items"]), 8)

    def test_overview_error_makes_selfcheck_fail_with_overview_action(self):
        overview = {"wechat": {"state": "error", "detail": "扫描时解密失败", "action": "点「重新扫描微信群」"}}
        item = feature_items(self.settings(wechat_enabled=True), overview=overview)[3]
        self.assertEqual((item["state"], item["value"], item["hint"]), ("bad", "扫描时解密失败", "点「重新扫描微信群」"))

    def test_overview_ready_makes_selfcheck_pass(self):
        overview = {
            "dingtalk": {"state": "ready", "detail": "已连接实时监听 3 个群", "action": ""},
            "chaoxing": {"state": "ready", "detail": "上次同步 2026-10-25 01:00", "action": ""},
        }
        items = feature_items(self.settings(dingtalk_enabled=True, chaoxing_enabled=True, chaoxing_cookie="sid=1"), overview=overview)
        self.assertEqual((items[2]["state"], items[2]["value"]), ("ok", "已连接实时监听 3 个群"))
        self.assertEqual((items[4]["state"], items[4]["value"]), ("ok", "上次同步 2026-10-25 01:00"))

    def test_overview_does_not_cover_hosting_attachments_or_calendar(self):
        """overview 没覆盖的环境/本机项仍由 selfcheck 自己判，不能因为传了 overview 就变空。"""
        overview = {"wechat": {"state": "off", "detail": "", "action": "去设置里扫描微信"}}
        items = feature_items(
            self.settings(vision_active=True),
            overview=overview,
            hosting={"hosting_active": False},
            sync={"calendars": [{"kind": "public", "events": 7}], "events": 7},
        )
        self.assertEqual((items[1]["state"], items[1]["value"]), ("warn", "未开启"))
        self.assertEqual(items[5]["state"], "ok")
        self.assertEqual((items[6]["state"], items[6]["value"]), ("ok", "可访问 · 7 条事项"))


if __name__ == "__main__":
    unittest.main()
