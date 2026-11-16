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

    def test_launcher_points_at_a_full_chat_endpoint(self) -> None:
        # 演示版两条路：demo\llm.key 有钥匙就走云端，没有就退回本机 Ollama。
        # 两个端点都要写全 /chat/completions：只写到 /v1 时请求打到 /v1 本身，
        # 回 404，对话就悄悄退成固定说法——演示时看不出来，评委一问「它怎么想的」就答不上。
        text = (ROOT / "demo" / "启动演示.cmd").read_text(encoding="utf-8")
        self.assertIn("NOTICEHUB_LLM_ENDPOINT=http://127.0.0.1:11434/v1/chat/completions", text)
        self.assertIn("NOTICEHUB_LLM_BACKEND=openai", text)
        self.assertIn("NOTICEHUB_LLM_BACKEND=ollama", text)
        self.assertIn('llm.key', text)
        cloud = text.split("NOTICEHUB_LLM_ENDPOINT=", 2)[1]
        self.assertIn("/chat/completions", cloud.splitlines()[0])

    def test_page_turns_off_first_run_doors_in_demo_mode(self) -> None:
        page = (ROOT / "noticehub" / "web_static" / "page.html").read_text(encoding="utf-8")
        self.assertIn("/[?&]demo=1(&|$)/.test(location.search)", page)
        self.assertIn("localStorage.setItem('qq_digest_journey_hidden','1')", page)
        self.assertIn("localStorage.setItem('nh_setup_wizard_seen','1')", page)

    def test_seed_produces_two_trips_with_every_stop_resolved(self) -> None:
        import datetime as dt
        import json
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import patch
        from demo import seed
        from noticehub.application.agent_plan import load_routines
        from noticehub.application.campus import load_pois, load_schedule, match_place
        from noticehub.application.trips import plan_day
        from noticehub.infrastructure.sqlite.store import Store

        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            store = Store(Path(tmp) / 'demo.sqlite3')
            with patch.object(seed.Settings, 'from_env', return_value=settings), patch.object(
                seed, 'build_service', return_value=SimpleNamespace(store=store),
            ):
                self.assertEqual(seed.main(), 0)
                before = store.list_tasks()
                self.assertEqual(seed.main(), 0)
                self.assertEqual(store.list_tasks(), before)
            pois = load_pois(tmp)
            lessons = load_schedule(tmp)
            routines = load_routines(tmp)
            self.assertEqual(len(before), 5)
            self.assertEqual(len(lessons), 3)
            self.assertEqual(len(routines), 1)
            for text in [t['summary'] for t in before] + [l['place'] for l in lessons] + [r['place'] for r in routines]:
                self.assertIsNotNone(match_place(text, pois), text)
            plan = plan_day(before, pois=pois, lessons=lessons, routines=routines,
                            today=dt.date.today(), now=dt.datetime.combine(dt.date.today(), dt.time(12)))
        self.assertEqual(plan['counts']['trips'], 2)
        self.assertEqual(plan['counts']['stops'], 6)
        self.assertEqual(plan['unscheduled'], [])
        self.assertEqual(plan['overflow'], [])
        stops = [stop for trip in plan['trips'] for stop in trip['stops']]
        self.assertGreaterEqual(stops[0]['arrive'], '15:40')
        for left, right in zip(stops, stops[1:]):
            self.assertGreaterEqual(right['arrive'], left['leave'])
        print('DEMO_PLAN=' + json.dumps({'counts': plan['counts'], 'trips': [
            {'distance_m': trip['distance_m'], 'stops': [
                {'place': s['place'], 'arrive': s['arrive'], 'leave': s['leave']} for s in trip['stops']
            ]} for trip in plan['trips']]}, ensure_ascii=False))

    def test_seed_writes_a_lesson_for_today(self) -> None:
        seed = (ROOT / "demo" / "seed.py").read_text(encoding="utf-8")
        self.assertIn("date.today().isoweekday()", seed)
        self.assertIn("create_task(store", seed)


if __name__ == "__main__":
    unittest.main()
