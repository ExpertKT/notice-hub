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
    load_plan_order,
    load_routines,
    optional_get,
    optional_post,
    parse_schedule_text,
    save_plan_order,
    save_poi,
    save_routines,
    save_schedule,
    today_plan,
)
from noticehub.application.campus import load_pois, load_schedule, match_place  # noqa: E402
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
