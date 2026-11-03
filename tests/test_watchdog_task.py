"""看门狗脚本（tools/watchdog-task.ps1）的契约。

用户 2026-11-02 21:42 遇到服务整个消失没人拉起，第二天才发现。脚本原来只查计划任务
状态、默认任务名还是废弃的 QQ-Live-Digest，且只随开机触发一次。这里锁住新契约：
健康就退出、用户主动退出就跳过、拉起后必须等到健康才算成功。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

SOURCE = Path(__file__).parents[1] / "tools" / "watchdog-task.ps1"


def source() -> str:
    return SOURCE.read_text(encoding="utf-8-sig")


class WatchdogScriptContractTests(unittest.TestCase):
    def test_script_is_utf8_with_bom(self):
        """PS 5.1 按 ANSI 读无 BOM 的 UTF-8 文件，中文注释会把它拆成语法错。"""
        raw = SOURCE.read_bytes()
        self.assertEqual(raw[:3], b"\xef\xbb\xbf", "脚本必须带 UTF-8 BOM")

    def test_root_is_parent_of_script_directory(self):
        text = source()
        self.assertIn("Split-Path $PSScriptRoot -Parent", text)

    def test_health_check_is_a_real_http_get(self):
        text = source()
        self.assertIn("/api/health", text)
        self.assertIn("Invoke-WebRequest", text)
        self.assertIn("-UseBasicParsing", text)
        self.assertIn("-TimeoutSec", text)

    def test_healthy_service_exits_zero_without_starting_anything(self):
        text = source()
        # 健康检查成功分支必须在「读标记」和「拉起」之前 return/exit 0
        health_idx = text.index("if (Test-ServiceAlive -Port $port -TimeoutSeconds $HealthTimeoutSeconds)")
        manual_idx = text.index("if (Test-Path -LiteralPath $manualFlag)")
        start_idx = text.index("Start-Process -FilePath $exe")
        self.assertLess(health_idx, manual_idx)
        self.assertLess(manual_idx, start_idx)
        self.assertIn("exit 0", text[health_idx:manual_idx])

    def test_manual_exit_flag_skips_relaunch(self):
        text = source()
        manual_idx = text.index("if (Test-Path -LiteralPath $manualFlag)")
        start_idx = text.index("Start-Process -FilePath $exe")
        self.assertIn("exit 0", text[manual_idx:start_idx], "有标记时必须退出 0，不许拉起")

    def test_flag_path_is_localappdata_not_install_dir(self):
        text = source()
        self.assertIn("'NoticeHub'", text)
        self.assertIn("manual-exit.flag", text)
        self.assertIn("LOCALAPPDATA", text)

    def test_relaunch_passes_no_browser_and_from_watchdog(self):
        text = source()
        self.assertIn("'--no-browser'", text)
        self.assertIn("'--from-watchdog'", text)
        self.assertIn("-WindowStyle Hidden", text)

    def test_success_only_after_health_comes_back(self):
        """拉起后必须轮询到健康才退出 0；20 秒内没应答就退 2。"""
        text = source()
        start_idx = text.index("Start-Process -FilePath $exe")
        tail = text[start_idx:]
        self.assertIn("$deadline = (Get-Date).AddSeconds($WaitSeconds)", tail)
        self.assertIn("if (Test-ServiceAlive", tail)
        self.assertIn("exit 2", tail)
        self.assertIn("仍无应答", tail)

    def test_never_kills_processes_by_name(self):
        text = source()
        for forbidden in ("Stop-Process", "taskkill", "Get-Process", "qq.exe"):
            self.assertNotIn(forbidden, text, "看门狗不许按进程名杀任何东西")

    def test_logs_to_localappdata_watchdog_log(self):
        text = source()
        self.assertIn("watchdog.log", text)
        self.assertIn("Add-Content", text)

    def test_port_read_from_env_with_8766_fallback(self):
        text = source()
        self.assertIn("$port = 8766", text)
        self.assertIn("QQ_DIGEST_WEB_PORT", text)
        self.assertIn("NOTICEHUB_WEB_PORT", text)
        self.assertTrue(re.search(r"\$candidate -ge 1 -and \$candidate -le 65535", text))

    def test_no_scheduled_task_state_as_proxy_for_service_health(self):
        """旧写法用 Get-ScheduledTask 的 State 当「服务活着」，新脚本不许再这么判。"""
        text = source()
        self.assertNotIn("Get-ScheduledTask", text)
        self.assertNotIn("Start-ScheduledTask", text)
        self.assertNotIn("QQ-Live-Digest", text)


if __name__ == "__main__":
    unittest.main()
