"""今日行程的 HTTP 面：课表导入、常用行程、行程接口。不起服务。"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from noticehub.application import agent_plan  # noqa: E402
from noticehub.application.agent_plan import (  # noqa: E402
    PLAN_PATH,
    ROUTINES_PATH,
    SCHEDULE_PATH,
    load_routines,
    optional_get,
    optional_post,
    parse_schedule_text,
    save_routines,
    save_schedule,
    today_plan,
)
from noticehub.application.campus import load_pois, load_schedule  # noqa: E402

POIS = load_pois(None)


class ParseScheduleTest(unittest.TestCase):
    def test_parses_lines_and_keeps_bad_ones(self) -> None:
        text = "周一 08:00-09:40 高等数学 立德楼302\n随便一行\n星期三 14:00~15:40 大物 基础实验楼B203"
        lessons, rejected = parse_schedule_text(text, POIS)
        self.assertEqual(len(lessons), 2)
        self.assertEqual(rejected, ["随便一行"])
        first = lessons[0]
        self.assertEqual((first["weekday"], first["start"], first["end"]), (1, "08:00", "09:40"))
        self.assertEqual(first["place"], "立德楼302")
        self.assertEqual(first["name"], "高等数学")
        self.assertEqual(lessons[1]["weekday"], 3)
        self.assertEqual(lessons[1]["place"], "基础实验楼B203")
        self.assertEqual(lessons[1]["name"], "大物")

    def test_rejects_reversed_time(self) -> None:
        lessons, rejected = parse_schedule_text("周一 15:40-08:00 反了", POIS)
        self.assertEqual(lessons, [])
        self.assertEqual(len(rejected), 1)

    def test_bare_building_keeps_no_room(self) -> None:
        lessons, _ = parse_schedule_text("周一 08:00-09:40 高等数学 立德楼\n周二 08:00-09:40 大物 基础实验楼 B203", POIS)
        self.assertEqual((lessons[0]["name"], lessons[0]["place"]), ("高等数学", "立德楼"))
        self.assertEqual((lessons[1]["name"], lessons[1]["place"]), ("大物", "基础实验楼B203"))


class ScheduleSaveTest(unittest.TestCase):
    def test_save_text_then_read_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            result = save_schedule(settings, {"text": "周二 10:00-11:40 大学英语 立行楼201"})
            self.assertTrue(result["ok"])
            self.assertEqual(result["lessons"], 1)
            lessons = load_schedule(tmp)
        self.assertEqual(lessons[0]["place"], "立行楼201")
        self.assertEqual(lessons[0]["name"], "大学英语")

    def test_save_nothing_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            with self.assertRaises(ValueError):
                save_schedule(settings, {"text": "今天天气不错"})


class ScheduleImageTest(unittest.TestCase):
    """课表截图只出草稿：认出来的样子要能直接进那个框，存不存由人点保存。

    课表是排程的输入，认错一节课比认不出更麻烦，所以这条路径一律不落盘。
    """

    def _data_url(self, size: int = 40) -> str:
        raw = b"\x89PNG\r\n\x1a\n" + b"0" * size
        return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")

    def test_image_comes_back_as_draft_and_is_not_saved(self) -> None:
        reply = "周一 08:00-09:40 高等数学 立德楼302\n周二 10:00-11:40 体育 体育馆\n看不懂的这一行"
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            with patch.object(agent_plan, "read_schedule_image", return_value=reply):
                result = save_schedule(settings, {"image": self._data_url()})
            self.assertEqual(load_schedule(tmp), [])
            self.assertEqual((tmp and Path(tmp) / "campus_schedule.json").exists(), False)
        self.assertTrue(result["ok"])
        self.assertFalse(result["saved"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["lessons"][0]["place"], "立德楼302")
        self.assertEqual(result["lessons"][0]["name"], "高等数学")
        self.assertEqual(result["rejected"], ["看不懂的这一行"])
        self.assertTrue(result["text"].startswith("周一 08:00-09:40 高等数学 立德楼302"))

    def test_image_reading_nothing_says_what_the_model_said(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            with patch.object(agent_plan, "read_schedule_image", return_value="这张图里是一张风景照"):
                with self.assertRaises(ValueError) as caught:
                    save_schedule(settings, {"image": self._data_url()})
            self.assertEqual(load_schedule(tmp), [])
        self.assertIn("没认出一节课", str(caught.exception))
        self.assertIn("风景照", str(caught.exception))

    def test_bad_payload_never_reaches_the_model(self) -> None:
        cases = {
            "not a data url": "https://example.com/课表.png",
            "no base64 flag": "data:image/png,abc",
            "wrong type": "data:application/pdf;base64,AAAA",
            "broken base64": "data:image/png;base64,%%%%",
            "empty": "data:image/png;base64,",
        }
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            calls = []
            with patch("noticehub.application.agent_chat.ask_vision", side_effect=lambda *a: calls.append(a)):
                for name, payload in cases.items():
                    with self.subTest(name):
                        with self.assertRaises(ValueError):
                            save_schedule(settings, {"image": payload})
        self.assertEqual(calls, [])

    def test_oversized_image_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            with self.assertRaises(ValueError) as caught:
                save_schedule(settings, {"image": self._data_url(agent_plan.SCHEDULE_IMAGE_MAX_BYTES)})
        self.assertIn("8 MB", str(caught.exception))


class RoutinesTest(unittest.TestCase):
    def test_add_then_remove(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            added = save_routines(settings, {"action": "add", "title": "去一食堂吃饭", "minutes": 20})
            self.assertEqual(len(added["routines"]), 1)
            routine = added["routines"][0]
            self.assertEqual(routine["place"], "一食堂")
            self.assertEqual(routine["minutes"], 20)
            removed = save_routines(settings, {"action": "remove", "id": routine["id"]})
            self.assertEqual(removed["routines"], [])

    def test_unknown_place_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            with self.assertRaises(ValueError) as caught:
                save_routines(settings, {"action": "add", "title": "去月球"})
            self.assertIn("地点", str(caught.exception))

    def test_remove_missing_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            with self.assertRaises(ValueError):
                save_routines(settings, {"action": "remove", "id": "r99"})

    def test_broken_file_reads_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "campus_routines.json").write_text("{坏的", encoding="utf-8")
            self.assertEqual(load_routines(tmp), [])


class _Store:
    def __init__(self, tasks: list[dict]) -> None:
        self._tasks = tasks
        self.calls: list[dict] = []

    def list_tasks(self, **kwargs: object) -> list[dict]:
        self.calls.append(kwargs)
        return list(self._tasks)


class TodayPlanTest(unittest.TestCase):
    def test_reads_tasks_and_lessons_from_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            save_schedule(settings, {"text": "周一 14:00-15:40 计算机导论 立行楼201"})
            save_routines(settings, {"action": "add", "title": "去一食堂吃饭", "minutes": 20})
            store = _Store([
                {"id": 1, "summary": "取生活用品 学生公寓A", "status": "open", "deadline": ""},
                {"id": 2, "summary": "写读书笔记", "status": "open", "deadline": ""},
            ])
            plan = today_plan(store, settings)
        self.assertEqual(plan["lesson_count"], 1)
        self.assertEqual(store.calls[0]["statuses"], ("open",))
        titles = [stop["title"] for trip in plan["trips"] for stop in trip["stops"]]
        self.assertIn("取生活用品 学生公寓A", titles)
        self.assertIn("去一食堂吃饭", titles)
        self.assertEqual([item["task_id"] for item in plan["unscheduled"]], [2])

    def test_plan_survives_without_any_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            plan = today_plan(_Store([]), settings)
        self.assertEqual(plan["trips"], [])
        self.assertEqual(plan["lesson_count"], 0)
        self.assertEqual(next(p for p in plan['pois'] if p['id'] == 'library')['floors'], 5)
        self.assertIn("没课", " ".join(plan["checks"]))


class RouteTest(unittest.TestCase):
    def test_routes_only_exist_when_shell_enabled(self) -> None:
        with patch.dict(os.environ, {"NOTICEHUB_AGENT_SHELL": ""}):
            self.assertIsNone(optional_get(PLAN_PATH))
            self.assertIsNone(optional_post(SCHEDULE_PATH))
        with patch.dict(os.environ, {"NOTICEHUB_AGENT_SHELL": "1"}):
            self.assertEqual(optional_get(PLAN_PATH), ("_route_get_api_agent_plan", False))
            self.assertEqual(optional_get(ROUTINES_PATH), ("_route_get_api_agent_plan", False))
            self.assertEqual(optional_post(ROUTINES_PATH), ("_route_post_api_agent_plan", False))
            self.assertIsNone(optional_get("/api/tasks"))


if __name__ == "__main__":
    unittest.main()
