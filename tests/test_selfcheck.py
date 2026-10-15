import unittest
from types import SimpleNamespace

from qq_live_digest.selfcheck import check, feature_items


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
             ("wechat", "微信"), ("chaoxing", "学习通 Cookie"), ("attachments", "附件解析"),
             ("calendar", "公网日历"), ("push", "消息推送")],
        )
        self.assertTrue(all(set(item) == {"key", "label", "state", "value", "hint"} for item in items))

    def test_qq_diagnosis_first_break_is_passed_through(self):
        item = feature_items(self.settings(), doctor={"ok": False, "broken": "NapCat", "hint": "启动服务"})[0]
        self.assertEqual((item["state"], item["value"], item["hint"]), ("bad", "NapCat", "启动服务"))

    def test_enabled_dingtalk_without_selected_groups_is_not_misreported_as_unstarted(self):
        item = feature_items(self.settings(dingtalk_enabled=True), dingtalk={"enabled": True, "running": False})[2]
        self.assertEqual((item["state"], item["value"]), ("warn", "已启用，但没勾群"))
        self.assertIn("点「立即扫描」也不会动", item["hint"])

    def test_enabled_wechat_without_selected_groups_is_not_misreported_as_unstarted(self):
        item = feature_items(self.settings(wechat_enabled=True), wechat={"enabled": True, "available": True, "running": False})[3]
        self.assertEqual((item["state"], item["value"]), ("warn", "已启用，但没勾群"))
        self.assertIn("点「立即扫描」也不会动", item["hint"])

    def test_check_returns_fixed_contract(self):
        result = check(self.settings())
        self.assertEqual(set(result), {"ok", "items"})
        self.assertIs(result["ok"], True)
        self.assertEqual(len(result["items"]), 8)


if __name__ == "__main__":
    unittest.main()
