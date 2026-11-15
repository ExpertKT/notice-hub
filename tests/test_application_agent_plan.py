"""今日行程的 HTTP 面：课表导入、常用行程、行程接口。不起服务。"""

from __future__ import annotations

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
        text = "周一 08:00-09:40 高等数学 立功楼302\n随便一行\n星期三 14:00~15:40 大物 图文信息楼B203"
        lessons, rejected = parse_schedule_text(text, POIS)
        self.assertEqual(len(lessons), 2)
        self.assertEqual(rejected, ["随便一行"])
        first = lessons[0]
        self.assertEqual((first["weekday"], first["start"], first["end"]), (1, "08:00", "09:40"))
        self.assertEqual(first["place"], "立功楼")
        self.assertEqual(lessons[1]["weekday"], 3)
        self.assertEqual(lessons[1]["place"], "图文信息楼")

    def test_rejects_reversed_time(self) -> None:
        lessons, rejected = parse_schedule_text("周一 15:40-08:00 反了", POIS)
        self.assertEqual(lessons, [])
        self.assertEqual(len(rejected), 1)


class ScheduleSaveTest(unittest.TestCase):
    def test_save_text_then_read_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            result = save_schedule(settings, {"text": "周二 10:00-11:40 大学英语 博雅居201"})
            self.assertTrue(result["ok"])
            self.assertEqual(result["lessons"], 1)
            lessons = load_schedule(tmp)
        self.assertEqual(lessons[0]["place"], "博雅居")

    def test_save_nothing_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            with self.assertRaises(ValueError):
                save_schedule(settings, {"text": "今天天气不错"})


class RoutinesTest(unittest.TestCase):
    def test_add_then_remove(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            added = save_routines(settings, {"action": "add", "title": "去超市买东西", "minutes": 20})
            self.assertEqual(len(added["routines"]), 1)
            routine = added["routines"][0]
            self.assertEqual(routine["place"], "超市")
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
            save_schedule(settings, {"text": "周一 14:00-15:40 计算机导论 博雅居201"})
            save_routines(settings, {"action": "add", "title": "去超市买东西", "minutes": 20})
            store = _Store([
                {"id": 1, "summary": "取快递 菜鸟驿站", "status": "open", "deadline": ""},
                {"id": 2, "summary": "写读书笔记", "status": "open", "deadline": ""},
            ])
            plan = today_plan(store, settings)
        self.assertEqual(plan["lesson_count"], 1)
        self.assertEqual(store.calls[0]["statuses"], ("open",))
        titles = [stop["title"] for trip in plan["trips"] for stop in trip["stops"]]
        self.assertIn("取快递 菜鸟驿站", titles)
        self.assertIn("去超市买东西", titles)
        self.assertEqual([item["task_id"] for item in plan["unscheduled"]], [2])

    def test_plan_survives_without_any_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            plan = today_plan(_Store([]), settings)
        self.assertEqual(plan["trips"], [])
        self.assertEqual(plan["lesson_count"], 0)
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
