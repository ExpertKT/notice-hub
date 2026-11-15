"""演示版的启动脚本别在搬家或改端口时悄悄坏掉。

这几条都是「打开演示版就该成立」的事：独立数据目录、行程接口开着、首启弹窗不挡路。
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class DemoEditionTest(unittest.TestCase):
    def test_launcher_uses_its_own_data_dir_and_port(self) -> None:
        text = (ROOT / "demo" / "启动演示.cmd").read_text(encoding="utf-8")
        self.assertIn("NOTICEHUB_DATA_DIR=%~dp0data", text)
        self.assertIn("NOTICEHUB_AGENT_SHELL=1", text)
        self.assertIn("NOTICEHUB_WEB_PORT=8899", text)
        self.assertIn("demo=1", text)

    def test_page_turns_off_first_run_doors_in_demo_mode(self) -> None:
        page = (ROOT / "noticehub" / "web_static" / "page.html").read_text(encoding="utf-8")
        self.assertIn("/[?&]demo=1(&|$)/.test(location.search)", page)
        self.assertIn("localStorage.setItem('qq_digest_journey_hidden','1')", page)
        self.assertIn("localStorage.setItem('nh_setup_wizard_seen','1')", page)

    def test_seed_writes_a_lesson_for_today(self) -> None:
        seed = (ROOT / "demo" / "seed.py").read_text(encoding="utf-8")
        self.assertIn("date.today().isoweekday()", seed)
        self.assertIn("create_task(store", seed)


if __name__ == "__main__":
    unittest.main()
