"""今日行程的 HTTP 面：课表导入、常用行程、行程接口。不起服务。"""

from __future__ import annotations

import base64
import datetime as dt
import json
import math
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
    CAMPUS_BOX,
    HERE_FILE,
    OVERRIDE_FILE,
    PLAN_PATH,
    ROUTINES_PATH,
    SCHEDULE_PATH,
    load_plan_groups,
    load_plan_order,
    load_here,
    load_routines,
    optional_get,
    optional_post,
    parse_schedule_text,
    save_here,
    save_plan_order,
    save_poi,
    save_routines,
    save_schedule,
    today_plan,
)
from noticehub.application.campus import (  # noqa: E402
    DEFAULT_POIS,
    POI_FILE,
    load_pois,
    load_schedule,
    match_place,
)
from noticehub.timeutil import now_local  # noqa: E402

POIS = load_pois(None)


class ParseScheduleTest(unittest.TestCase):
    def test_parses_lines_and_keeps_bad_ones(self) -> None:
        text = "周一 08:00-09:40 高等数学 立德楼302\n随便一行\n星期三 14:00~15:40 大物 武进楼B203"
        lessons, rejected = parse_schedule_text(text, POIS)
        self.assertEqual(len(lessons), 2)
        self.assertEqual(rejected, ["随便一行"])
        first = lessons[0]
        self.assertEqual((first["weekday"], first["start"], first["end"]), (1, "08:00", "09:40"))
        self.assertEqual(first["place"], "立德楼302")
        self.assertEqual(first["name"], "高等数学")
        self.assertEqual(lessons[1]["weekday"], 3)
        self.assertEqual(lessons[1]["place"], "武进楼B203")
        self.assertEqual(lessons[1]["name"], "大物")

    def test_rejects_reversed_time(self) -> None:
        lessons, rejected = parse_schedule_text("周一 15:40-08:00 反了", POIS)
        self.assertEqual(lessons, [])
        self.assertEqual(len(rejected), 1)

    def test_bare_building_keeps_no_room(self) -> None:
        lessons, _ = parse_schedule_text("周一 08:00-09:40 高等数学 立德楼\n周二 08:00-09:40 大物 武进楼 B203", POIS)
        self.assertEqual((lessons[0]["name"], lessons[0]["place"]), ("高等数学", "立德楼"))
        self.assertEqual((lessons[1]["name"], lessons[1]["place"]), ("大物", "武进楼B203"))


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

    def _settings(self, tmp: str) -> SimpleNamespace:
        return SimpleNamespace(
            data_dir=Path(tmp),
            dashscope_endpoint="https://example.invalid/v1/chat/completions",
            dashscope_model="qwen-vl-max",
            dashscope_api_key="",
            llm_backend="auto",
            llm_timeout=60,
        )

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

    def test_image_failures_say_what_to_do_without_vendor_text(self) -> None:
        """没钥匙／钥匙错／超时：页面只看到一句人话，厂商原文一个字都不出现。"""
        import io
        import urllib.error

        bodies = {
            "no key": b'{"error":{"message":"You didn\'t provide an API key. Need Bearer auth."}}',
            "wrong key": b'{"error":{"message":"Incorrect API key provided: sk-bad-key"}}',
        }
        forbidden = ("HTTP", "401", "Incorrect", "Authorization", "Bearer", "http://", "https://")
        with tempfile.TemporaryDirectory() as tmp:
            for name, body in bodies.items():
                with self.subTest(name):
                    error = urllib.error.HTTPError(
                        "https://example.invalid/v1/chat/completions", 401, "Unauthorized", {}, io.BytesIO(body)
                    )
                    with patch("urllib.request.urlopen", side_effect=error), self.assertLogs(
                        "noticehub.application.agent_chat", level="WARNING"
                    ):
                        with self.assertRaises(ValueError) as caught:
                            save_schedule(self._settings(tmp), {"image": self._data_url()})
                    text = str(caught.exception)
                    self.assertIn("这张图没读成", text)
                    self.assertIn("llm.key", text)
                    for word in forbidden:
                        self.assertNotIn(word, text)
            with self.subTest("timeout"):
                with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")), self.assertLogs(
                    "noticehub.application.agent_chat", level="WARNING"
                ):
                    with self.assertRaises(ValueError) as caught:
                        save_schedule(self._settings(tmp), {"image": self._data_url()})
                text = str(caught.exception)
                self.assertIn("这张图没读成", text)
                self.assertNotIn("timed out", text)
                for word in forbidden:
                    self.assertNotIn(word, text)

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
            # 写成别名也行：存下来的是这张表里的正式楼名。
            self.assertEqual(routine["place"], "第一餐厅")
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
                {"id": 1, "summary": "取生活用品 1号楼", "status": "open", "deadline": ""},
                {"id": 2, "summary": "写读书笔记", "status": "open", "deadline": ""},
            ])
            plan = today_plan(store, settings)
        self.assertEqual(plan["lesson_count"], 1)
        self.assertEqual(store.calls[0]["statuses"], ("open",))
        titles = [stop["title"] for trip in plan["trips"] for stop in trip["stops"]]
        self.assertIn("取生活用品 1号楼", titles)
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


class TripOriginLineTest(unittest.TestCase):
    """一趟行程记住「从哪儿出发」，并带上绕楼的折线：画图那边直接拿它描路线。"""

    NOW = dt.datetime(2026, 11, 18, 16, 20)  # 周三，课刚下
    TASK = [{"id": 1, "summary": "还书 图书馆", "status": "open", "deadline": ""}]

    def _plan(self, text: str, tasks: list[dict]) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            if text:
                save_schedule(settings, {"text": text})
            with patch(
                "noticehub.application.agent_plan.now_local", return_value=self.NOW
            ):
                plan = today_plan(_Store(tasks), settings)
        self.by_id = {item["id"]: item for item in plan["pois"]}
        return plan

    def test_line_starts_at_the_last_lesson_and_ends_at_the_stop(self) -> None:
        plan = self._plan("周三 14:00-15:40 计算机导论 立行楼201", self.TASK)
        trip = plan["trips"][0]
        self.assertEqual(trip["from_place"], "lixing")
        self.assertEqual([stop["place_id"] for stop in trip["stops"]], ["library"])
        start, end = trip["line"][0], trip["line"][-1]
        self.assertAlmostEqual(start[0], self.by_id["lixing"]["x"], places=3)
        self.assertAlmostEqual(start[1], self.by_id["lixing"]["y"], places=3)
        self.assertAlmostEqual(end[0], self.by_id["library"]["x"], places=3)
        self.assertAlmostEqual(end[1], self.by_id["library"]["y"], places=3)

    def test_one_stop_without_origin_has_no_line_to_draw(self) -> None:
        """没有起点、只有一站，就没有「线」可言——给空列表，别让画图那边凭空补一段。"""
        plan = self._plan("", self.TASK)
        trip = plan["trips"][0]
        self.assertIsNone(plan["origin"])
        self.assertIsNone(trip["from_place"])
        self.assertEqual(trip["line"], [])


class HereTest(unittest.TestCase):
    """「我现在在哪」：标了就按它算起点；隔天作废，坏文件要说清楚。"""

    NOW = dt.datetime(2026, 11, 18, 16, 20)
    TASK = [{"id": 1, "summary": "还书 图书馆", "status": "open", "deadline": ""}]

    def test_here_replaces_the_last_lesson_as_the_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            save_schedule(settings, {"text": "周三 14:00-15:40 计算机导论 立行楼201"})
            with patch("noticehub.application.agent_plan.now_local", return_value=self.NOW):
                saved = save_here(settings, {"place": "第一餐厅"})
                self.assertTrue(saved["ok"])
                self.assertEqual(saved["here"]["name"], "第一餐厅")
                plan = today_plan(_Store(self.TASK), settings)
        self.assertEqual(plan["here"]["name"], "第一餐厅")
        self.assertEqual(plan["origin"]["id"], "canteen1")
        self.assertEqual(plan["trips"][0]["from_place"], "canteen1")
        self.assertIn("起点是你标的「第一餐厅」", " ".join(plan["checks"]))

    def test_unknown_place_is_refused_and_nothing_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            with self.assertRaises(ValueError) as ctx:
                save_here(settings, {"place": "火星基地"})
            self.assertIn("没认出", str(ctx.exception))
            self.assertFalse((Path(tmp) / agent_plan.HERE_FILE).exists())

    def test_poi_id_is_accepted_like_the_route_tools(self) -> None:
        """智能体可能给编号（plan_route 就认编号），这里也得认，否则同一句话两处两种结果。"""
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            with patch("noticehub.application.agent_plan.now_local", return_value=self.NOW):
                saved = save_here(settings, {"place": "canteen1"})
        self.assertEqual((saved["here"]["place_id"], saved["here"]["name"]), ("canteen1", "第一餐厅"))

    def test_empty_place_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            with self.assertRaises(ValueError) as ctx:
                save_here(settings, {"place": "  "})
            self.assertIn("先说你在哪", str(ctx.exception))

    def test_yesterday_here_does_not_steer_today(self) -> None:
        """昨天的位置留着会把今天的路线指到错的方向，必须当没标。"""
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            (Path(tmp) / agent_plan.HERE_FILE).write_text(
                json.dumps({"place_id": "canteen1", "name": "第一餐厅", "at": "2026-11-17T09:00"}),
                encoding="utf-8",
            )
            with patch("noticehub.application.agent_plan.now_local", return_value=self.NOW):
                self.assertIsNone(load_here(tmp, self.NOW.date()))
                plan = today_plan(_Store(self.TASK), settings)
        self.assertIsNone(plan["here"])
        self.assertIsNone(plan["origin"])
        self.assertIn("还没说你人在哪", " ".join(plan["checks"]))

    def test_broken_here_file_is_reported_not_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            (Path(tmp) / agent_plan.HERE_FILE).write_text("{坏了", encoding="utf-8")
            with patch("noticehub.application.agent_plan.now_local", return_value=self.NOW):
                plan = today_plan(_Store(self.TASK), settings)
        self.assertIn("你的位置文件坏了", " ".join(plan["warnings"]))


class PlanOrderTest(unittest.TestCase):
    """拖出来的站序：按天存、按天读，不串日；空 order 等于把这一天的手排清掉。"""

    FILE = "campus_plan_order.json"

    def setUp(self) -> None:
        # 保留窗口锚在「今天」，写死日期过一阵就过期，所以按当天算。
        self.today = now_local().date()
        self.DAY = self.today.isoformat()

    def test_order_round_trips_by_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            save_plan_order(
                settings,
                {"action": "order", "date": self.DAY, "order": ["去一食堂吃饭", "还书 图书馆"]},
            )
            raw = json.loads((Path(tmp) / self.FILE).read_text(encoding="utf-8"))
            self.assertEqual(raw, {self.DAY: ["去一食堂吃饭", "还书 图书馆"]})
            self.assertEqual(
                load_plan_order(tmp, self.today), ["去一食堂吃饭", "还书 图书馆"]
            )
            # 跨日不串：明天没有手排，不受今天这次影响。
            self.assertEqual(load_plan_order(tmp, self.today + dt.timedelta(days=1)), [])

    def test_empty_order_clears_the_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            save_plan_order(settings, {"action": "order", "date": self.DAY, "order": ["A"]})
            save_plan_order(settings, {"action": "order", "date": self.DAY, "order": []})
            self.assertEqual(load_plan_order(tmp, self.today), [])
            raw = json.loads((Path(tmp) / self.FILE).read_text(encoding="utf-8"))
            self.assertNotIn(self.DAY, raw)

    def test_bad_input_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            for payload in (
                {"action": "order", "date": "17/11/2026", "order": ["A"]},
                {"action": "order", "order": ["A"]},
                {"action": "order", "date": self.DAY, "order": "A"},
                {"action": "order", "date": self.DAY, "order": ["A", 3]},
            ):
                with self.assertRaises(ValueError):
                    save_plan_order(settings, payload)
            # 一条都没写进去：坏输入不该留下半份顺序。
            self.assertFalse((Path(tmp) / self.FILE).exists())

    def test_broken_file_is_reported_not_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / self.FILE).write_text("{坏的", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_plan_order(tmp, dt.date(2026, 11, 17))
            with self.assertRaises(ValueError):
                save_plan_order(
                    SimpleNamespace(data_dir=Path(tmp)),
                    {"action": "order", "date": self.DAY, "order": ["A"]},
                )

    def test_groups_round_trip_by_date(self) -> None:
        """趟边界另存一份：站序文件格式不变，老客户端不带 groups 照样能用。"""
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            save_plan_order(
                settings,
                {
                    "action": "order",
                    "date": self.DAY,
                    "order": ["吃饭", "还书"],
                    "groups": [["吃饭", "还书"]],
                },
            )
            trips = json.loads((Path(tmp) / "campus_plan_trips.json").read_text(encoding="utf-8"))
            self.assertEqual(trips, {self.DAY: [["吃饭", "还书"]]})
            self.assertEqual(load_plan_groups(tmp, self.today), [["吃饭", "还书"]])
            self.assertEqual(load_plan_groups(tmp, self.today + dt.timedelta(days=1)), [])
            self.assertEqual(
                json.loads((Path(tmp) / self.FILE).read_text(encoding="utf-8")),
                {self.DAY: ["吃饭", "还书"]},
            )

    def test_empty_order_clears_the_groups_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            save_plan_order(
                settings,
                {"action": "order", "date": self.DAY, "order": ["A"], "groups": [["A"]]},
            )
            save_plan_order(settings, {"action": "order", "date": self.DAY, "order": []})
            self.assertEqual(load_plan_groups(tmp, self.today), [])
            trips = json.loads((Path(tmp) / "campus_plan_trips.json").read_text(encoding="utf-8"))
            self.assertNotIn(self.DAY, trips)

    def test_bad_groups_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            for groups in ("A", ["A"], [["A", 3]], [None]):
                with self.assertRaises(ValueError):
                    save_plan_order(
                        settings,
                        {"action": "order", "date": self.DAY, "order": ["A"], "groups": groups},
                    )
            # 坏 groups 不许留下半份：站序与趟边界一个文件都不该建。
            self.assertFalse((Path(tmp) / self.FILE).exists())
            self.assertFalse((Path(tmp) / "campus_plan_trips.json").exists())

    def test_broken_trips_file_is_reported_not_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "campus_plan_trips.json").write_text("{坏的", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_plan_groups(tmp, dt.date(2026, 11, 17))
            with self.assertRaises(ValueError):
                save_plan_order(
                    SimpleNamespace(data_dir=Path(tmp)),
                    {"action": "order", "date": self.DAY, "order": ["A"]},
                )

    def test_only_recent_days_are_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            for offset in range(14):
                day = self.today - dt.timedelta(days=offset)
                save_plan_order(
                    settings, {"action": "order", "date": day.isoformat(), "order": ["A"]}
                )
            too_old = self.today - dt.timedelta(days=20)
            with self.assertRaises(ValueError):
                save_plan_order(
                    settings, {"action": "order", "date": too_old.isoformat(), "order": ["A"]}
                )
            raw = json.loads((Path(tmp) / self.FILE).read_text(encoding="utf-8"))
            self.assertEqual(len(raw), 14)
            self.assertNotIn(too_old.isoformat(), raw)


class TodayPlanOrderTest(unittest.TestCase):
    """手排顺序要真的进今天的行程；没排到的按最近邻补在后面，删掉的标题跳过不报错。"""

    ROUTINES = (("还书", "图书馆"), ("交报告", "武进楼"), ("找老师", "立德楼"))

    def _plan_titles(self, settings):
        plan = today_plan(_Store([]), settings)
        return [stop["title"] for trip in plan["trips"] for stop in trip["stops"]]

    def test_plan_follows_the_saved_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            for title, place in self.ROUTINES:
                save_routines(settings, {"action": "add", "title": title, "place": place})
            today = now_local().date().isoformat()
            self.assertEqual(sorted(self._plan_titles(settings)), sorted(t for t, _ in self.ROUTINES))
            save_plan_order(
                settings,
                {"action": "order", "date": today, "order": ["找老师", "还书", "交报告"]},
            )
            self.assertEqual(self._plan_titles(settings), ["找老师", "还书", "交报告"])
            # 只排了一站：那一站排在最前，没排到的按最近邻补在后面，站数不变。
            save_plan_order(settings, {"action": "order", "date": today, "order": ["找老师"]})
            titles = self._plan_titles(settings)
            self.assertEqual(titles[0], "找老师")
            self.assertEqual(sorted(titles), ["交报告", "找老师", "还书"])
            # 名单里残留一个已经不存在的标题：跳过，不报错、不多出一站。
            save_plan_order(
                settings,
                {"action": "order", "date": today, "order": ["已经被删掉的事", "交报告"]},
            )
            titles = self._plan_titles(settings)
            self.assertEqual(titles[0], "交报告")
            self.assertEqual(sorted(titles), ["交报告", "找老师", "还书"])
            # 空 order = 回到最近邻：与从没排过时一致。
            save_plan_order(settings, {"action": "order", "date": today, "order": []})
            self.assertEqual(sorted(self._plan_titles(settings)), ["交报告", "找老师", "还书"])

    def test_dragged_into_another_trip_it_stays_there(self) -> None:
        """把生活区的常做的事拖进教学区那趟，重排后它还在那趟里，不会被区拆回去。"""
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            for title, place in (("还书", "图书馆"), ("吃饭", "第一餐厅")):
                save_routines(settings, {"action": "add", "title": title, "place": place})
            today = now_local().date().isoformat()
            self.assertEqual(len(today_plan(_Store([]), settings)["trips"]), 2)
            save_plan_order(
                settings,
                {
                    "action": "order",
                    "date": today,
                    "order": ["吃饭", "还书"],
                    "groups": [["吃饭", "还书"]],
                },
            )
            plan = today_plan(_Store([]), settings)
            self.assertEqual(len(plan["trips"]), 1)
            self.assertEqual([stop["title"] for stop in plan["trips"][0]["stops"]], ["吃饭", "还书"])

    def test_order_belongs_to_its_own_day(self) -> None:
        """手排是按天的：给明天排的顺序不许改今天的走法。"""
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            for title, place in self.ROUTINES:
                save_routines(settings, {"action": "add", "title": title, "place": place})
            default = self._plan_titles(settings)
            save_plan_order(
                settings,
                {"action": "order",
                 "date": (now_local().date() + dt.timedelta(days=1)).isoformat(),
                 "order": ["找老师", "还书", "交报告"]},
            )
            self.assertEqual(self._plan_titles(settings), default)


class RouteTest(unittest.TestCase):
    def test_routes_are_served_without_any_setting(self) -> None:
        env = {key: item for key, item in os.environ.items() if key != "NOTICEHUB_AGENT_SHELL"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(optional_get(PLAN_PATH), ("_route_get_api_agent_plan", False))
            self.assertEqual(optional_post(SCHEDULE_PATH), ("_route_post_api_agent_plan", False))

    def test_routes_are_hidden_when_the_switch_is_zero(self) -> None:
        with patch.dict(os.environ, {"NOTICEHUB_AGENT_SHELL": "0"}):
            self.assertIsNone(optional_get(PLAN_PATH))
            self.assertIsNone(optional_post(SCHEDULE_PATH))
        with patch.dict(os.environ, {"NOTICEHUB_AGENT_SHELL": "1"}):
            self.assertEqual(optional_get(PLAN_PATH), ("_route_get_api_agent_plan", False))
            self.assertEqual(optional_get(ROUTINES_PATH), ("_route_get_api_agent_plan", False))
            self.assertEqual(optional_post(ROUTINES_PATH), ("_route_post_api_agent_plan", False))
            self.assertIsNone(optional_get("/api/tasks"))

    def test_plan_accepts_post_for_manual_order(self) -> None:
        """拖拽排出来的顺序走 POST /api/agent/plan（action=order），这条路得真注册上。"""
        env = {key: item for key, item in os.environ.items() if key != "NOTICEHUB_AGENT_SHELL"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(optional_post(PLAN_PATH), ("_route_post_api_agent_plan", False))


class PoiOverrideTest(unittest.TestCase):
    """内置点的改名/挪位置/隐藏写进 campus_poi_override.json，只存改过的字段。"""

    def _override(self, tmp: str) -> Path:
        return Path(tmp) / OVERRIDE_FILE

    def test_override_applies_name_and_position_and_keeps_untouched_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (self._override(tmp)).write_text(
                json.dumps({"library": {"name": "图书馆（新）", "x": 600.0}}, ensure_ascii=False),
                encoding="utf-8")
            pois = {item["id"]: item for item in load_pois(tmp)}
        self.assertEqual(pois["library"]["name"], "图书馆（新）")
        self.assertEqual(pois["library"]["x"], 600.0)
        # y 没在 override 里：内置行没写坐标，y 来自 geo 表，没被 override 碰就保持原值。
        geo = json.loads(
            (Path(__file__).resolve().parent.parent / "noticehub" / "web_static" / "js" / "campus-geo.json")
            .read_text(encoding="utf-8"))
        self.assertEqual(pois["library"]["y"], geo["pois"]["library"]["px"][1])

    def test_hidden_place_absent_from_table_and_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            save_poi(SimpleNamespace(data_dir=Path(tmp)), {"action": "hide", "id": "library"})
            names = [item["name"] for item in load_pois(tmp)]
            plan = today_plan(_Store([]), SimpleNamespace(data_dir=Path(tmp)))
        self.assertNotIn("图书馆", names)
        self.assertNotIn("图书馆", [p["name"] for p in plan["pois"]])

    def test_unhide_restores_with_override_still_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            save_poi(settings, {"action": "edit", "id": "library", "name": "图书馆（新）"})
            save_poi(settings, {"action": "hide", "id": "library"})
            save_poi(settings, {"action": "unhide", "id": "library"})
            names = [item["name"] for item in load_pois(tmp)]
        self.assertIn("图书馆（新）", names)

    def test_corrupt_override_file_never_breaks_the_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (self._override(tmp)).write_text("{这不是 JSON", encoding="utf-8")
            names = [item["name"] for item in load_pois(tmp)]
            plan = today_plan(_Store([]), SimpleNamespace(data_dir=Path(tmp)))
        self.assertIn("图书馆", names)
        self.assertIn("pois", plan)  # 坏 override 没拖垮排程，plan 照常算出来。

    def test_rename_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            save_poi(settings, {"action": "add", "name": "实验楼东门", "x": 420.0, "y": 700.0})
            # 撞上看得见的内置名：拒。
            with self.assertRaises(ValueError) as caught:
                save_poi(settings, {"action": "edit", "id": "custom_1", "name": "图书馆"})
            self.assertIn("已经有叫", str(caught.exception))
            # 改成一处藏着的地方的名字：允许（重名只对看得见的地点算）。
            save_poi(settings, {"action": "hide", "id": "tuwen"})
            save_poi(settings, {"action": "edit", "id": "custom_1", "name": "图文信息中心楼"})
            self.assertEqual(
                next(p["name"] for p in load_pois(tmp) if p["id"] == "custom_1"),
                "图文信息中心楼")
            # 超长 / 空名：拒，并保留原来的中文提示。
            with self.assertRaises(ValueError) as caught:
                save_poi(settings, {"action": "edit", "id": "custom_1", "name": "甲" * 21})
            self.assertIn("字以内", str(caught.exception))
            with self.assertRaises(ValueError) as caught:
                save_poi(settings, {"action": "edit", "id": "custom_1", "name": "   "})
            self.assertIn("起个名字", str(caught.exception))

    def test_move_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            # 挪到校区外：拒。
            with self.assertRaises(ValueError) as caught:
                save_poi(settings, {"action": "move", "id": "library", "x": -5.0, "y": 100.0})
            self.assertIn("校区外面", str(caught.exception))
            # 挪内置点：只进 override，出厂表和用户文件都不动。
            before_default = json.dumps(DEFAULT_POIS, ensure_ascii=False, sort_keys=True)
            save_poi(settings, {"action": "move", "id": "library", "x": 560.0, "y": 880.0})
            override = json.loads((self._override(tmp)).read_text(encoding="utf-8"))
            self.assertEqual(override["library"]["x"], 560.0)
            self.assertEqual(override["library"]["y"], 880.0)
            self.assertEqual(json.dumps(DEFAULT_POIS, ensure_ascii=False, sort_keys=True), before_default)
            self.assertFalse((Path(tmp) / POI_FILE).exists())
            moved = next(p for p in load_pois(tmp) if p["id"] == "library")
            self.assertEqual((moved["x"], moved["y"]), (560.0, 880.0))

    def test_campus_box_falls_back_to_constant_when_ring_is_missing(self) -> None:
        """快照里没有 campus 环时 _campus_box() 回落 CAMPUS_BOX 常量，越界点照样被拒。"""
        import tempfile
        from pathlib import Path

        from noticehub.application import campus_roads

        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            campus_roads._road_graph.cache_clear()
            campus_roads._grid.cache_clear()
            campus_roads._block_boxes.cache_clear()
            with patch.object(campus_roads, "_snapshot", return_value={}):
                self.assertEqual(agent_plan._campus_box(), CAMPUS_BOX)
                with self.assertRaises(ValueError) as caught:
                    agent_plan._valid_xy({"x": -5.0, "y": 100.0})
                self.assertIn("校区外面", str(caught.exception))
                # 落在常量盒内：通过。
                x, y = agent_plan._valid_xy({"x": 100.0, "y": 200.0})
                self.assertEqual((x, y), (100.0, 200.0))
            campus_roads._road_graph.cache_clear()
            campus_roads._grid.cache_clear()
            campus_roads._block_boxes.cache_clear()

    def test_remove_builtin_refused_with_hide_advice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as caught:
                save_poi(SimpleNamespace(data_dir=Path(tmp)), {"action": "remove", "id": "library"})
        message = str(caught.exception)
        self.assertIn("内置", message)
        self.assertIn("隐藏", message)

    def test_remove_custom_keeps_other_ids_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            save_poi(settings, {"action": "add", "name": "实验楼东门", "x": 420.0, "y": 700.0})
            save_poi(settings, {"action": "add", "name": "快递自提点", "x": 120.0, "y": 300.0})
            save_poi(settings, {"action": "remove", "id": "custom_1"})
            ids = [p["id"] for p in load_pois(tmp) if p["id"].startswith("custom_")]
            self.assertEqual(ids, ["custom_2"])
            # 再加一处：编号接着最大的用，不回填被删的 custom_1。
            third = save_poi(settings, {"action": "add", "name": "打印店", "x": 200.0, "y": 400.0})
            self.assertEqual(third["poi"]["id"], "custom_3")


class HereByCoordinatesTest(unittest.TestCase):
    """拖地图标的「我在哪」：给 x/y，40 米内有楼就挂到那栋楼，否则算自由位置。"""

    LIBRARY_PX = None  # 从 geo 表读，别写死——geo 表变了测试自己跟上。
    MPP = None

    @classmethod
    def setUpClass(cls) -> None:
        geo = json.loads(
            (Path(__file__).resolve().parent.parent / "noticehub" / "web_static" / "js" / "campus-geo.json")
            .read_text(encoding="utf-8"))
        cls.LIBRARY_PX = geo["pois"]["library"]["px"]
        cls.MPP = geo["projection"]["metersPerPixel"]

    def test_dropped_near_library_snaps_to_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            offset = 15.0 / self.MPP  # 15 米，在 40 米吸附半径里
            saved = save_here(settings, {"x": self.LIBRARY_PX[0] + offset, "y": self.LIBRARY_PX[1]})
            self.assertEqual(saved["here"]["place_id"], "library")
            self.assertEqual(saved["here"]["name"], "图书馆")
            # 落点坐标原样存下来，地图好把标记画在真正拖到的地方。
            self.assertAlmostEqual(saved["here"]["x"], self.LIBRARY_PX[0] + offset, places=1)
            # 排程能拿它当起点（library 在地点表里，取回完整那条）。
            plan = today_plan(_Store([]), settings)
            self.assertEqual(plan["origin"]["id"], "library")

    def test_dropped_far_from_everything_is_a_free_point(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            offset = 300.0 / self.MPP  # 300 米，超出吸附半径
            saved = save_here(settings, {"x": self.LIBRARY_PX[0] + offset, "y": self.LIBRARY_PX[1]})
            self.assertTrue(saved["here"]["place_id"].startswith("here_"))
            self.assertEqual(saved["here"]["name"], "自由位置")
            # 自由位置不在地点表里，但排程仍按它算起点（拿 here 自带的 x/y）。
            plan = today_plan(_Store([]), settings)
            self.assertIsNotNone(plan["origin"])
            self.assertEqual(plan["origin"]["id"], saved["here"]["place_id"])

    def test_place_path_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            saved = save_here(SimpleNamespace(data_dir=Path(tmp)), {"place": "第一餐厅"})
        self.assertEqual((saved["here"]["place_id"], saved["here"]["name"]), ("canteen1", "第一餐厅"))

    def test_string_xy_is_rejected(self) -> None:
        """x/y 传字符串（不是地图上取到的数字）要被拒，报错点明要重新取位置。"""
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            with self.assertRaises(ValueError) as caught:
                save_here(settings, {"x": "abc", "y": "def"})
            self.assertIn("取位置", str(caught.exception))

    def test_non_finite_xy_is_rejected(self) -> None:
        """x/y 是 NaN/inf（序列化漏进来的脏数）要被拒，报错点明不是有效数字。"""
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            for bad in (float("nan"), float("inf")):
                with self.subTest(bad=bad):
                    with self.assertRaises(ValueError) as caught:
                        save_here(settings, {"x": self.LIBRARY_PX[0], "y": bad})
                    self.assertIn("有效数字", str(caught.exception))


class CampusPoiTest(unittest.TestCase):
    """地图上自己加的地点：校验在信任边界上，存下来之后能当真楼用。"""

    def test_saved_place_becomes_a_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            saved = save_poi(settings, {"action": "add", "name": "实验楼东门",
                                        "x": 420.0, "y": 700.0, "kind": "teach"})
            self.assertEqual(saved["poi"]["id"], "custom_1")
            # 「加完能当行程的目的地」：排线读的是 load_pois，不是这份原始文件，
            # 所以这里必须从盘上读回来，还得能被姓名匹配成地点。
            pois = load_pois(tmp)
            self.assertIn("实验楼东门", [p["name"] for p in pois])
            self.assertIsNotNone(match_place("去实验楼东门", pois))
            second = save_poi(settings, {"action": "add", "name": "快递自提点",
                                         "x": 120.0, "y": 300.0})
            self.assertEqual(second["poi"]["id"], "custom_2")
            removed = save_poi(settings, {"action": "remove", "id": "custom_1"})
            self.assertEqual(removed["removed"], "custom_1")
            self.assertNotIn("实验楼东门", [p["name"] for p in load_pois(tmp)])

    def test_bad_point_and_duplicate_name_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            for payload, hint in (
                ({"name": "", "x": 100, "y": 100}, "名字"),
                ({"name": "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥", "x": 100, "y": 100}, "字以内"),
                ({"name": "点在湖里", "x": -40, "y": 100}, "校区"),
                ({"name": "没取点"}, "点一下"),
                # 内置表里已经有这个名字：再加一个只会让「去图书馆」分不清是哪个。
                ({"name": "图书馆", "x": 563.0, "y": 622.0}, "已经有"),
            ):
                with self.assertRaises(ValueError) as caught:
                    save_poi(settings, payload)
                self.assertIn(hint, str(caught.exception))

    def test_added_point_count_is_what_you_can_actually_see(self) -> None:
        """卡片上写「共 N 处」，这个 N 得是看得见的地点：换过校区（seed）时不能再加 27。"""
        from noticehub.application import campus

        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            seed = [{"id": "a", "name": "甲楼", "x": 100.0, "y": 100.0},
                    {"id": "b", "name": "乙楼", "x": 200.0, "y": 200.0}]
            (Path(tmp) / campus.SEED_FILE).write_text(json.dumps(seed), encoding="utf-8")
            first = save_poi(settings, {"action": "add", "name": "快递自提点", "x": 120.0, "y": 300.0})
            self.assertEqual(first["count"], 3)
            self.assertEqual(first["count"], len(load_pois(tmp)))
            save_poi(settings, {"action": "hide", "id": "a"})
            later = save_poi(settings, {"action": "add", "name": "打印店", "x": 200.0, "y": 400.0})
            # 甲楼被藏着，地图和行程都看不见它，就不该算进「共 N 处」。
            self.assertEqual(later["count"], 3)

    def test_map_config_never_fakes_a_key(self) -> None:
        """没 key 只能回 enabled:false 和空串，不能拿个占位当有效值。"""
        empty = {key: value for key, value in os.environ.items()
                 if key not in (agent_plan.AMAP_KEY_ENV, agent_plan.AMAP_SECRET_ENV,
                                agent_plan.BAIDU_KEY_ENV)}
        with patch.dict(os.environ, empty, clear=True):
            conf = agent_plan.map_config()
            self.assertFalse(conf["amap"]["enabled"])
            self.assertEqual(conf["amap"]["key"], "")
            self.assertFalse(conf["baidu"]["enabled"])
            self.assertTrue(conf["osm"]["enabled"])
        with patch.dict(os.environ, {agent_plan.BAIDU_KEY_ENV: " abc ", agent_plan.AMAP_KEY_ENV: ""}):
            conf = agent_plan.map_config()
            self.assertTrue(conf["baidu"]["enabled"])
            self.assertEqual(conf["baidu"]["key"], "abc")
            self.assertFalse(conf["amap"]["enabled"])


if __name__ == "__main__":
    unittest.main()
