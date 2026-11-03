"""计划任务脚本的静态契约（不真注册任务）。

看门狗的关键不是「注册了」，而是「按周期跑」——只有 AtStartup 的话，服务中途退出
之后到下一次开机之前都不会被拉起。所以这里锁的是触发器的形状，以及「注册完要回头
验证」这件事本身，而不是脚本能不能跑。
"""

import unittest
from pathlib import Path

from noticehub import version

ROOT = Path(__file__).parents[1]


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def read_bytes(name: str) -> bytes:
    return (ROOT / name).read_bytes()


class WatchdogTriggerTests(unittest.TestCase):
    def setUp(self):
        self.source = read("install-task.ps1")

    def test_watchdog_has_periodic_trigger_not_only_at_startup(self):
        """只有 AtStartup = 开机跑一次就没了，服务退出后没人管。"""
        self.assertIn(
            "$watchdogRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date) "
            "-RepetitionInterval (New-TimeSpan -Minutes 5) "
            "-RepetitionDuration ([TimeSpan]::MaxValue)",
            self.source,
        )
        self.assertIn("Trigger = @($startup, $watchdogRepeat)", self.source)
        # 不能退化成单触发器写法。
        self.assertNotIn("Trigger = $startup", self.source)

    def test_watchdog_action_is_unchanged(self):
        """动作保持原样：不带额外参数，指向仓库里的 watchdog-task.ps1。"""
        self.assertIn(
            "$watchdogArgs = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"{0}\"' -f $watchdogScript",
            self.source,
        )
        self.assertNotIn("-ServiceTaskName", self.source)

    def test_whatif_prints_trigger_repetition(self):
        """WhatIf 只看 action 看不出「多久跑一次」，必须把触发器也打出来。"""
        self.assertIn("foreach ($trigger in @($task.Trigger))", self.source)
        self.assertIn("repeat_interval=", self.source)
        self.assertIn("repeat_duration=", self.source)

    def test_registration_is_idempotent(self):
        self.assertIn("-Force", self.source)


class WatchdogVerificationTests(unittest.TestCase):
    def setUp(self):
        self.source = read("install-task.ps1")

    def test_rechecks_task_and_repetition_after_register(self):
        """Register 返回成功不算装好：回头查任务在不在、重复触发挂没挂上。"""
        self.assertIn("$watchdogTask = Get-ScheduledTask -TaskName 'NapCat-QQ-Watchdog'", self.source)
        self.assertIn(
            "$repeatTriggers = @($watchdogTask.Triggers | Where-Object "
            "{ $_.Repetition -and $_.Repetition.Interval })",
            self.source,
        )
        self.assertIn("Watchdog verified: NapCat-QQ-Watchdog repeats every", self.source)

    def test_verification_failures_are_reported_not_silent(self):
        """两种没装好的情况都要说出「下一步怎么办」，不许当成功。"""
        self.assertIn("看门狗没装上：查不到 NapCat-QQ-Watchdog 任务。", self.source)
        self.assertIn("看门狗装上了但没有重复触发", self.source)
        self.assertIn("以管理员身份重跑 install-task.ps1", self.source)
        self.assertIn("以管理员身份重跑本脚本", self.source)

    def test_access_denied_is_a_hint_not_a_stack_trace(self):
        self.assertIn("看门狗需要以管理员身份运行 install-task.ps1。", self.source)


class UninstallAndStatusTests(unittest.TestCase):
    def test_uninstall_removes_watchdog(self):
        self.assertIn("'NapCat-QQ-Watchdog'", read("uninstall-task.ps1"))

    def test_status_shows_watchdog_period(self):
        source = read("status.ps1")
        self.assertIn("Get-ScheduledTask -TaskName 'NapCat-QQ-Watchdog'", source)
        self.assertIn("$repeat[0].Repetition.Interval", source)
        # 没有重复触发时要说清后果，不能只显示「已注册」。
        self.assertIn("没有重复触发：服务退出后不会自动拉起", source)


class WikiTests(unittest.TestCase):
    def test_install_page_documents_periodic_watchdog(self):
        source = read("docs/wiki/install.md")
        self.assertIn("NoticeHub", source)
        self.assertIn("每 5 分钟", source)
        self.assertIn("不会把你拉起来", source)

    def test_use_page_documents_tray_exit_exemption(self):
        source = read("docs/wiki/use.md")
        self.assertIn("看门狗会在 5 分钟内把它拉起来", source)
        self.assertIn("从托盘「退出」主动关掉的不会被拉起", source)

    def test_changelog_documents_current_batch(self):
        """本批改动必须出现在用户能翻到的地方。

        同一版本重出包时，改动写进当前版本那一节；没重出包就写「未发布」。
        所以只认这两节，不接受全文搜索——写在别的版本节里等于放错了位置。
        """
        source = read("docs/wiki/changelog.md")
        sections = {}
        for chunk in source.split("\n## ")[1:]:
            heading = chunk.split("\n", 1)[0].strip()
            sections[heading] = chunk
        candidates = [
            name for name in sections
            if name == "未发布" or name.startswith(version.APP_VERSION)
        ]
        self.assertTrue(candidates, "changelog 里既没有「未发布」也没有当前版本小节")
        for needle in ("NapCat-QQ-Watchdog", "manual-exit.flag"):
            if not any(needle in sections[name] for name in candidates):
                self.fail(
                    f"本批改动 {needle} 既没进未发布、也没进当前版本小节"
                    f"（{', '.join(candidates)}）"
                )


class ScriptEncodingTests(unittest.TestCase):
    def test_scripts_keep_utf8_bom(self):
        """PS 5.1 读无 BOM 的脚本会按 ANSI 解码，中文一多就语法崩。"""
        for name in ("install-task.ps1", "status.ps1"):
            with self.subTest(script=name):
                data = read_bytes(name)
                self.assertEqual(data[:3], b"\xef\xbb\xbf", f"{name} 丢了 UTF-8 BOM")


if __name__ == "__main__":
    unittest.main()
