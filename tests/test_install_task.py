"""计划任务脚本的静态契约（不真注册任务）。

看门狗的关键不是「注册了」，而是「按周期跑」——只有 AtStartup 的话，服务中途退出
之后到下一次开机之前都不会被拉起。所以这里锁的是触发器的形状，以及「注册完要回头
验证」这件事本身，而不是脚本能不能跑。
"""

import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from noticehub import version

ROOT = Path(__file__).parents[1]


def _powershell_runner():
    """解析能跑 install-task.ps1 的解释器：Windows PowerShell 优先，其次跨平台的 pwsh。"""
    return shutil.which("powershell") or shutil.which("pwsh")


def _can_run_powershell() -> bool:
    """install-task.ps1 依赖 New-ScheduledTask / schtasks，只有 Windows 能真跑。

    Linux 上没有这些 cmdlet，缺解释器时跳过是对的（不是掩盖失败）——纯静态断言的
    用例照跑，不许跟着跳过。
    """
    return os.name == "nt" and _powershell_runner() is not None


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
            "-RepetitionInterval (New-TimeSpan -Minutes 5)",
            self.source,
        )
        # 看门狗必须挂两个触发器：开机一次 + 每 5 分钟重复一次。
        self.assertIn(
            "Triggers = @(@{ Kind = 'Boot' }, @{ Kind = 'Repeat'; Interval = 'PT5M' })",
            self.source,
        )
        # 两个种类都要真的映射到 ScheduledTask 触发器对象，不然数据里写了也不生效。
        self.assertIn(
            "$triggerObjects = @{ Logon = $logon; Boot = $startup; Repeat = $watchdogRepeat }",
            self.source,
        )
        # 不能退化成单触发器写法。
        self.assertNotIn("Trigger = $startup", self.source)

    def test_watchdog_repetition_has_no_duration(self):
        """不写 Duration 才是无限期重复。

        实机实测：写 -RepetitionDuration ([TimeSpan]::MaxValue) 会被任务计划拒绝——
        任务 XML 不接受 P99999999DT23H59M59S，Register-ScheduledTask 抛 CimException，
        任务根本建不出来（异常还在提权窗口里被吞掉，日志一行都没有）。
        所以源码里不许出现那个 Duration 的最大值。
        """
        self.assertNotIn(
            "[TimeSpan]::MaxValue",
            self.source,
            "任务计划 XML 不接受这个 Duration，会抛 CimException，任务建不出来",
        )
        # 只查真调用：注释里解释这个坑时会出现参数名，那是应该留的说明。
        for line in self.source.splitlines():
            if line.lstrip().startswith("#"):
                continue
            self.assertNotIn("-RepetitionDuration", line, line)

    @unittest.skipUnless(_can_run_powershell(), "install-task.ps1 只在 Windows 上可跑")
    def test_whatif_shows_pt5m_interval_and_no_duration(self):
        """-WhatIf 里真的能看到每 5 分钟一次，且没有被拒绝的 Duration。"""
        result = subprocess.run(
            [
                _powershell_runner(), "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(ROOT / "install-task.ps1"), "-WhatIf",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        output = result.stdout + result.stderr
        self.assertIn("repeat_interval=PT5M", output)
        self.assertNotIn("P99999999", output)
        self.assertNotIn("CimException", output)

    def test_watchdog_action_is_unchanged(self):
        """动作指向仓库里的 watchdog-task.ps1，不带额外参数，且经 wscript 无窗口启动。

        powershell.exe -WindowStyle Hidden 仍会先建控制台再隐藏，每 5 分钟闪一下黑框；
        所以动作换成 wscript.exe 调 tools\\run-hidden.vbs。
        """
        self.assertIn(
            "$hiddenRunner = Join-Path $root 'tools\\run-hidden.vbs'",
            self.source,
        )
        self.assertIn(
            "Execute = 'wscript.exe'; Arguments = '//B //Nologo \"{0}\" \"{1}\"' -f $hiddenRunner, $watchdogScript",
            self.source,
        )
        self.assertNotIn("-ServiceTaskName", self.source)

    def test_watchdog_is_skipped_without_the_hidden_runner(self):
        """缺无窗口启动器就不登记看门狗，免得每 5 分钟弹黑窗。"""
        self.assertIn(
            "if ((Test-Path -LiteralPath $watchdogScript) -and (Test-Path -LiteralPath $hiddenRunner)) {",
            self.source,
        )
        self.assertIn("没有无窗口启动器就跳过看门狗任务，否则每 5 分钟会弹黑窗", self.source)

    @unittest.skipUnless(_can_run_powershell(), "install-task.ps1 只在 Windows 上可跑")
    def test_whatif_shows_watchdog_started_through_wscript(self):
        """-WhatIf 里看门狗的动作必须是无窗口启动器，不是 powershell.exe。"""
        result = subprocess.run(
            [
                _powershell_runner(), "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(ROOT / "install-task.ps1"), "-WhatIf",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        output = result.stdout + result.stderr
        watchdog_lines = [line for line in output.splitlines() if "task=NapCat-QQ-Watchdog" in line]
        self.assertTrue(watchdog_lines, f"-WhatIf 没打印看门狗任务：{output}")
        action_line = next(line for line in watchdog_lines if "execute=" in line)
        self.assertIn("execute=wscript.exe", action_line)
        self.assertIn("run-hidden.vbs", action_line)
        self.assertIn("watchdog-task.ps1", action_line)
        # 黑窗就是这么来的：整个 -WhatIf 输出里不能再有「powershell.exe … watchdog-task.ps1」。
        for line in output.splitlines():
            if "execute=powershell.exe" in line and "watchdog-task.ps1" in line:
                self.fail(f"-WhatIf 里看门狗还走 powershell.exe，会每 5 分钟弹黑窗：{line}")

    def test_whatif_prints_trigger_repetition(self):
        """WhatIf 只看 action 看不出「多久跑一次」，必须把触发器也打出来。"""
        # 预览读的是纯数据里的 Triggers（真对象要到注册前才构造，见脚本注释）。
        self.assertIn("foreach ($trigger in @($task.Triggers))", self.source)
        self.assertIn("repeat_interval=", self.source)
        self.assertIn("repeat_duration=", self.source)
        # 打印的是数据里的 Interval，不是写死的常量。
        self.assertIn(
            "$interval = if ($trigger.Interval) { $trigger.Interval } else { '' }",
            self.source,
        )

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
        self.assertIn("看门狗会在 5 分钟内将其重新拉起", source)
        self.assertIn("从托盘「退出」主动关闭的不会被拉起", source)

    def test_changelog_documents_current_batch(self):
        """本批改动必须出现在用户能翻到的地方。

        同一版本重出包时，改动写进当前版本那一节；没重出包就写「未发布」。
        所以只认这两节，不接受全文搜索——写在别的版本节里等于放错了位置。
        针脚跟着批次走：换批就换成这一批改动里的关键词，别删掉这条用例。
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
        for needle in ("字号三档", "四组"):
            if not any(needle in sections[name] for name in candidates):
                self.fail(
                    f"本批改动 {needle} 既没进未发布、也没进当前版本小节"
                    f"（{', '.join(candidates)}）"
                )


class SkipOnNonWindowsTests(unittest.TestCase):
    """锁住「CI 的 Linux job 上这些用例会跳过」：跳过是有意的，不是掩盖。"""

    def test_cannot_run_powershell_on_posix(self):
        with mock.patch.object(os, "name", "posix"):
            self.assertFalse(_can_run_powershell())
            # 就算机器上碰巧装了 pwsh，Linux 也跑不了 install-task.ps1 的 cmdlet。
            with mock.patch.object(shutil, "which", return_value="/usr/bin/pwsh"):
                self.assertFalse(_can_run_powershell())

    def test_can_run_powershell_only_when_interpreter_exists(self):
        with mock.patch.object(os, "name", "nt"):
            with mock.patch.object(shutil, "which", return_value=None):
                self.assertFalse(_can_run_powershell())
            with mock.patch.object(shutil, "which", return_value="powershell.exe"):
                self.assertTrue(_can_run_powershell())


class ScriptEncodingTests(unittest.TestCase):
    def test_scripts_keep_utf8_bom(self):
        """PS 5.1 读无 BOM 的脚本会按 ANSI 解码，中文一多就语法崩。"""
        for name in ("install-task.ps1", "status.ps1"):
            with self.subTest(script=name):
                data = read_bytes(name)
                self.assertEqual(data[:3], b"\xef\xbb\xbf", f"{name} 丢了 UTF-8 BOM")


if __name__ == "__main__":
    unittest.main()
