"""行程排程。纯函数，不起服务、不碰网络。"""

from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from noticehub.application.campus import (  # noqa: E402
    DEFAULT_POIS,
    load_pois,
    load_schedule,
    match_place,
    route_room,
)
from noticehub.application.trips import _order_stops, free_windows, plan_day  # noqa: E402

DAY = dt.date(2026, 11, 16)
WEEKDAY = DAY.isoweekday()
POIS = load_pois(None)


def _task(task_id: int, summary: str, deadline: str = "", status: str = "open",
          created_at: str = "", place_hint: str = "") -> dict:
    return {
        "id": task_id,
        "summary": summary,
        "deadline": deadline,
        "status": status,
        "done": status == "done",
        "importance": 3,
        "source": "manual",
        "created_at": created_at,
        "place_hint": place_hint,
    }


def _lesson(start: str, end: str, place: str = "立行楼 105", weekday: int | None = None) -> dict:
    return {
        "weekday": WEEKDAY if weekday is None else weekday,
        "start": start,
        "end": end,
        "name": "课",
        "place": place,
    }


class MatchPlaceTest(unittest.TestCase):
    def test_longest_alias_wins(self) -> None:
        poi = match_place("把报告交到图文信息中心楼 B203", POIS)
        self.assertIsNotNone(poi)
        self.assertEqual(poi["id"], "tuwen")
        # 别名和楼名指向同一处：换个叫法不能解析成另一个地点。
        self.assertEqual(match_place("去图文信息楼还书", POIS)["id"], "tuwen")

    def test_numbered_buildings_and_canteen_are_distinct(self) -> None:
        self.assertEqual(match_place("去2号楼取物品", POIS)["id"], "dorm_2")
        self.assertEqual(match_place("去11号楼取物品", POIS)["id"], "dorm_11")
        self.assertEqual(match_place("去一食堂吃饭", POIS)["id"], "canteen1")

    def test_no_place_returns_none_instead_of_guessing(self) -> None:
        self.assertIsNone(match_place("写一份读书笔记", POIS))

    def test_room_extraction(self) -> None:
        self.assertEqual(route_room("把实验报告交到武进楼 B203"), "B203")
        self.assertEqual(route_room("找学长拿材料"), "")


class LoadConfigTest(unittest.TestCase):
    def test_custom_poi_overrides_by_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "campus_poi.json"
            path.write_text(
                json.dumps([{"id": "lide", "name": "立德楼", "x": 10, "y": 20}]),
                encoding="utf-8",
            )
            pois = load_pois(tmp)
        ligong = [p for p in pois if p["id"] == "lide"]
        self.assertEqual(len(ligong), 1)
        self.assertEqual((ligong[0]["x"], ligong[0]["y"]), (10.0, 20.0))
        self.assertEqual(len(pois), len(DEFAULT_POIS))

    def test_broken_json_falls_back_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "campus_poi.json").write_text("{不是 JSON", encoding="utf-8")
            self.assertEqual(len(load_pois(tmp)), len(DEFAULT_POIS))

    def test_bad_lessons_are_dropped_not_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "campus_schedule.json").write_text(
                json.dumps(
                    {
                        "lessons": [
                            {"weekday": 1, "start": "08:00", "end": "09:40", "place": "立行楼"},
                            {"weekday": 9, "start": "08:00", "end": "09:40"},
                            {"weekday": 1, "start": "10:00", "end": "09:00"},
                            {"weekday": "x"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            lessons = load_schedule(tmp)
        self.assertEqual(len(lessons), 1)
        self.assertEqual(lessons[0]["start"], "08:00")


class FreeWindowsTest(unittest.TestCase):
    def test_windows_skip_lessons(self) -> None:
        windows = free_windows([_lesson("14:00", "15:40")])
        self.assertEqual([w["start"] for w in windows], ["08:00", "15:40"])
        self.assertEqual(windows[-1]["start"], "15:40")

    def test_no_lesson_means_whole_day(self) -> None:
        windows = free_windows([])
        self.assertEqual(len(windows), 1)
        self.assertEqual((windows[0]["start"], windows[0]["end"]), ("08:00", "19:00"))

    def test_short_gap_is_not_a_window(self) -> None:
        windows = free_windows([_lesson("09:00", "10:00"), _lesson("10:10", "11:00")])
        self.assertNotIn("10:00", [w["start"] for w in windows])


class PlanDayTest(unittest.TestCase):
    def test_future_deadline_is_unscheduled_with_reason(self) -> None:
        tasks = [
            _task(1, '还书 图书馆'),
            _task(2, '交报告 武进楼', '2026-11-16T18:00'),
            _task(3, '取生活用品 1号楼', '2026-11-17T15:00'),
        ]
        # 指定规划日期决定候选，不能误用机器日期或 now 的日期。
        plan = plan_day(tasks, pois=POIS, today=DAY, now=dt.datetime(2026, 11, 17, 8))
        self.assertEqual({s['task_id'] for trip in plan['trips'] for s in trip['stops']}, {1, 2})
        self.assertEqual(len(plan['unscheduled']), 1)
        self.assertEqual(plan['unscheduled'][0]['task_id'], 3)
        self.assertEqual(plan['unscheduled'][0]['deadline'], '2026-11-17T15:00')
        self.assertIn('2026-11-17', plan['unscheduled'][0]['reason'])
        self.assertIn('不排入今天', plan['unscheduled'][0]['reason'])

    def test_demo_seed_keeps_two_trips_six_stops_1070_metres(self) -> None:
        from demo.seed import TASKS, LESSONS
        from noticehub.application.agent_plan import parse_schedule_text

        lessons, rejected = parse_schedule_text('\n'.join(f'周一 {lesson}' for lesson in LESSONS), POIS)
        self.assertEqual(rejected, [])
        plan = plan_day([_task(i, title) for i, title in enumerate(TASKS, 1)], pois=POIS,
                        lessons=lessons, routines=[{'title': '一食堂 吃饭', 'place': '一食堂', 'minutes': 30}],
                        today=DAY, now=dt.datetime(2026, 11, 16, 12))
        # counts 现在还带 unscheduled 明细（按 reason_code 分组的真实总数），本用例全排进去了故为空。
        self.assertEqual(
            plan['counts'],
            {'trips': 2, 'stops': 6, 'distance_m': 1070, 'unscheduled': {}},
        )
        self.assertEqual(plan['unscheduled'], [])
        self.assertEqual(plan['overflow'], [])

    def test_origin_is_last_lesson_building(self) -> None:
        plan = plan_day(
            [],
            pois=POIS,
            lessons=[_lesson("08:00", "09:40", "立德楼 302"), _lesson("14:00", "15:40", "立行楼 105")],
            today=DAY,
            now=dt.datetime(2026, 11, 16, 12, 0),
        )
        self.assertIsNotNone(plan["origin"])
        self.assertEqual(plan["origin"]["id"], "lixing")

    def test_stop_never_starts_before_class_ends(self) -> None:
        plan = plan_day(
            [_task(1, "取生活用品 1号楼")],
            pois=POIS,
            lessons=[_lesson("14:00", "15:40")],
            today=DAY,
            now=dt.datetime(2026, 11, 16, 12, 0),
        )
        self.assertEqual(len(plan["trips"]), 1)
        self.assertGreaterEqual(plan["trips"][0]["start_at"], "15:40")

    def test_same_zone_merges_into_one_trip(self) -> None:
        plan = plan_day(
            [_task(1, "取生活用品 1号楼"), _task(2, "去一食堂买饭")],
            pois=POIS,
            lessons=[],
            today=DAY,
            now=dt.datetime(2026, 11, 16, 8, 0),
        )
        self.assertEqual(len(plan["trips"]), 1)
        self.assertEqual(len(plan["trips"][0]["stops"]), 2)

    def test_task_without_place_stays_out_of_trips(self) -> None:
        plan = plan_day(
            [_task(1, "写一份读书笔记"), _task(2, "取生活用品 1号楼")],
            pois=POIS,
            lessons=[],
            today=DAY,
            now=dt.datetime(2026, 11, 16, 8, 0),
        )
        titles = [s["title"] for trip in plan["trips"] for s in trip["stops"]]
        self.assertEqual(titles, ["取生活用品 1号楼"])
        self.assertEqual(plan["unscheduled"][0]["task_id"], 1)
        self.assertEqual(plan["unscheduled"][0]["reason"], "没认出地点")

    def test_finished_tasks_are_ignored(self) -> None:
        plan = plan_day(
            [_task(1, "取生活用品 1号楼", status="done"), _task(2, "还书 图书馆", status="dismissed")],
            pois=POIS,
            lessons=[],
            today=DAY,
            now=dt.datetime(2026, 11, 16, 8, 0),
        )
        self.assertEqual(plan["trips"], [])
        self.assertEqual(plan["unscheduled"], [])

    def test_overflow_is_reported_with_alternative(self) -> None:
        # 固定第二站在远处，锁住时间窗溢出，不随校园采样坐标调整改变题意。
        pois = [dict(p, x=2000, y=0) if p['id'] == 'wujin' else dict(p) for p in POIS]
        plan = plan_day(
            [_task(1, "取材料 立德楼 407"), _task(2, "交报告 武进楼 B203")],
            pois=pois,
            lessons=[_lesson("14:00", "15:40")],
            today=DAY,
            now=dt.datetime(2026, 11, 16, 12, 0),
            day_end="16:10",  # 保留可用的课后窗口，远处第二站必须明确溢出。
        )
        self.assertEqual(len(plan["trips"]), 1)
        self.assertEqual(len(plan["trips"][0]["stops"]), 1)
        self.assertEqual(len(plan["overflow"]), 1)
        self.assertIn("明天上午", plan["overflow"][0]["reason"])

    def test_closing_window_warning(self) -> None:
        # 营业时间是测试输入；真实地点采样不含窗口时间，不能替它编造。
        pois = [dict(p, open='08:00-20:00') if p['id'] == 'dorm_1' else dict(p) for p in POIS]
        plan = plan_day(
            [_task(1, "取生活用品 1号楼")],
            pois=pois,
            lessons=[_lesson("18:00", "19:50")],
            today=DAY,
            now=dt.datetime(2026, 11, 16, 12, 0),
            day_end="20:30",
        )
        self.assertTrue(any("窗口" in line for line in plan["warnings"]))
        self.assertEqual(len(plan["trips"]), 1)

    def test_earlier_deadline_goes_first(self) -> None:
        plan = plan_day(
            [_task(1, "还书 图书馆", deadline="2026-11-20 23:59:00"),
             _task(2, "交报告 武进楼", deadline="2026-11-16 18:00:00")],
            pois=POIS,
            lessons=[],
            today=DAY,
            now=dt.datetime(2026, 11, 16, 8, 0),
        )
        stops = plan["trips"][0]["stops"]
        self.assertEqual(stops[0]["task_id"], 2)

    def test_routine_can_be_inserted(self) -> None:
        plan = plan_day(
            [],
            pois=POIS,
            lessons=[],
            routines=[{"title": "去一食堂吃饭", "place": "一食堂", "minutes": 20}],
            today=DAY,
            now=dt.datetime(2026, 11, 16, 8, 0),
        )
        stop = plan["trips"][0]["stops"][0]
        self.assertEqual(stop["kind"], "routine")
        self.assertEqual(stop["minutes"], 20)

    def test_plan_is_deterministic(self) -> None:
        tasks = [_task(1, "取生活用品 1号楼"), _task(2, "交报告 武进楼 B203")]
        kwargs = dict(pois=POIS, lessons=[_lesson("14:00", "15:40")], today=DAY,
                      now=dt.datetime(2026, 11, 16, 12, 0))
        first = json.dumps(plan_day(tasks, **kwargs), sort_keys=True, ensure_ascii=False)
        second = json.dumps(plan_day(tasks, **kwargs), sort_keys=True, ensure_ascii=False)
        self.assertEqual(first, second)

    def test_checks_mention_lessons_and_origin(self) -> None:
        plan = plan_day(
            [_task(1, "取生活用品 1号楼")],
            pois=POIS,
            lessons=[_lesson("14:00", "15:40")],
            today=DAY,
            now=dt.datetime(2026, 11, 16, 12, 0),
        )
        text = " ".join(plan["checks"])
        self.assertIn("1 节", text)
        self.assertIn("立行楼", text)


    def test_first_stop_at_origin_does_not_claim_another_task(self) -> None:
        # 起点就是下课的楼：第一站的说明不能写「同一栋楼里还有一件」——那是给后一站用的
        plan = plan_day(
            [_task(1, "找辅导员签字 立德楼512")],
            pois=POIS,
            lessons=[_lesson("14:00", "15:40", "立德楼401")],
            today=DAY,
            now=dt.datetime(2026, 11, 16, 12, 0),
        )
        stop = plan["trips"][0]["stops"][0]
        self.assertEqual(stop["place_id"], "lide")
        self.assertIn("顺路先办", stop["why"])
        self.assertNotIn("还有一件", stop["why"])

    def test_second_stop_in_same_building_says_one_trip(self) -> None:
        plan = plan_day(
            [_task(1, "找辅导员签字 立德楼512"), _task(2, "交班会材料 立德楼302")],
            pois=POIS,
            lessons=[_lesson("14:00", "15:40", "立德楼401")],
            today=DAY,
            now=dt.datetime(2026, 11, 16, 12, 0),
        )
        stops = plan["trips"][0]["stops"]
        self.assertEqual([s["place_id"] for s in stops], ["lide", "lide"])
        self.assertIn("还有一件", stops[1]["why"])


class LegRoadTest(unittest.TestCase):
    """每段腿都要带贴路网信息：走得通给折线，只能画直线时标 degraded+straight，
    好让界面明着画「按直线示意」而不是一截假路网。"""

    def test_every_stop_carries_road_fields(self) -> None:
        plan = plan_day(
            [_task(1, "取生活用品 1号楼"), _task(2, "去一食堂买饭")],
            pois=POIS,
            lessons=[],
            today=DAY,
            now=dt.datetime(2026, 11, 16, 8, 0),
        )
        stops = plan["trips"][0]["stops"]
        self.assertGreaterEqual(len(stops), 1)
        for stop in stops:
            road = stop["road"]
            self.assertIn("path", road)
            self.assertIn("degraded", road)
            self.assertIn("reason", road)
            self.assertIsInstance(road["degraded"], bool)

    def test_routed_leg_has_polyline_not_degraded(self) -> None:
        plan = plan_day(
            [_task(1, "取生活用品 1号楼"), _task(2, "去一食堂买饭")],
            pois=POIS,
            lessons=[],
            today=DAY,
            now=dt.datetime(2026, 11, 16, 8, 0),
        )
        stops = plan["trips"][0]["stops"]
        routed = [s for s in stops if not s["road"]["degraded"] and s["road"]["reason"] == "routed"]
        self.assertGreaterEqual(len(routed), 1)
        for stop in routed:
            self.assertGreaterEqual(len(stop["road"]["path"]), 2)

    def test_cross_zone_leg_crossing_obstacle_is_straight(self) -> None:
        # 跨楼穿障碍、路网又确实走不通的腿：road_line 只出两点且直线穿楼 → degraded=True，
        # reason="straight"，不能假装沿路网走。第二餐厅到运动场就是这么一对（快照里没有门，
        # 两头都在楼轮廓里，出不来）。立德楼↔立言楼修好双坐标变换以后已经能沿路网走了，
        # 不再属于这一类。
        plan = plan_day(
            [_task(1, "去第二餐厅买饭"), _task(2, "去运动场跑步")],
            pois=POIS,
            lessons=[],
            today=DAY,
            now=dt.datetime(2026, 11, 16, 8, 0),
        )
        stops = [stop for trip in plan["trips"] for stop in trip["stops"]]
        degraded = [s for s in stops if s["road"]["degraded"]]
        self.assertGreaterEqual(len(degraded), 1)
        for stop in degraded:
            self.assertEqual(stop["road"]["reason"], "straight")


class UnscheduledReasonTest(unittest.TestCase):
    """「没说地点」那一栏要说真话：每件都给机器可读的理由，旧的不堆着，超量如实报。"""

    NOW = dt.datetime(2026, 11, 16, 8, 0)

    def _plan(self, tasks: list[dict]) -> dict:
        return plan_day(tasks, pois=POIS, today=DAY, now=self.NOW)

    def test_future_deadline_gets_reason_code(self) -> None:
        plan = self._plan([_task(1, "取生活用品 1号楼", "2026-11-17T15:00")])
        entry = plan["unscheduled"][0]
        self.assertEqual(entry["reason_code"], "future_deadline")
        self.assertNotIn("no_place", [e["reason_code"] for e in plan["unscheduled"]])
        # counts 按真实总数算，不受截断影响。
        self.assertEqual(plan["counts"]["unscheduled"], {"future_deadline": 1})

    def test_placeless_text_gets_no_place(self) -> None:
        plan = self._plan([_task(1, "写一份读书笔记")])
        entry = plan["unscheduled"][0]
        self.assertEqual(entry["reason_code"], "no_place")
        self.assertEqual(plan["counts"]["unscheduled"], {"no_place": 1})

    def test_task_older_than_window_is_excluded(self) -> None:
        old = "2026-09-01T08:00"  # 距 NOW 76 天，远超 30 天窗口
        plan = self._plan([_task(1, "写一份读书笔记", created_at=old)])
        self.assertEqual(plan["unscheduled"], [])
        self.assertEqual(plan["counts"]["unscheduled"], {})
        self.assertFalse(plan["truncated"])

    def test_task_without_created_at_is_kept(self) -> None:
        # 库里查不到 created_at 的旧数据不能整栏消失：没时间就当作还在窗口内。
        plan = self._plan([_task(1, "写一份读书笔记")])
        self.assertEqual(len(plan["unscheduled"]), 1)
        self.assertEqual(plan["unscheduled"][0]["reason_code"], "no_place")

    def test_window_keeps_placed_task_due_today(self) -> None:
        # 窗口只筛「没地点、得猜是哪天」的事：有地点且今天截止的老事不许被吞。
        old = "2026-10-16T08:00"  # 距 NOW 31 天，超 30 天窗口
        plan = self._plan([_task(1, "取生活用品 1号楼", "2026-11-16T15:00", created_at=old)])
        stops = [s for trip in plan["trips"] for s in trip["stops"]]
        self.assertEqual([s["task_id"] for s in stops], [1])
        self.assertEqual(stops[0]["place_id"], "dorm_1")
        self.assertEqual(plan["unscheduled"], [])

    def test_cap_trips_truncated_and_counts_stay_true(self) -> None:
        tasks = [_task(i, "写一份读书笔记") for i in range(1, 202)]  # 201 件
        plan = self._plan(tasks)
        self.assertEqual(len(plan["unscheduled"]), 200)
        self.assertTrue(plan["truncated"])
        self.assertEqual(plan["unscheduled_total"], 201)
        self.assertEqual(plan["counts"]["unscheduled"], {"no_place": 201})

    def test_place_hint_wins_over_summary_text(self) -> None:
        # 标题故意指向一食堂，标过的地点是武进楼：标过的优先，不再从标题猜。
        tasks = [_task(1, "去一食堂拿东西", place_hint="wujin")]
        plan = self._plan(tasks)
        stops = [s for trip in plan["trips"] for s in trip["stops"]]
        self.assertEqual([s["task_id"] for s in stops], [1])
        self.assertEqual(stops[0]["place_id"], "wujin")
        self.assertEqual(plan["unscheduled"], [])

    def test_unknown_place_hint_falls_back_to_text(self) -> None:
        tasks = [_task(1, "取生活用品 1号楼", place_hint="不存在的地方")]
        plan = self._plan(tasks)
        stops = [s for trip in plan["trips"] for s in trip["stops"]]
        self.assertEqual([s["task_id"] for s in stops], [1])
        self.assertEqual(stops[0]["place_id"], "dorm_1")
        self.assertEqual(stops[0]["place_source"], "text")
        self.assertEqual(plan["unscheduled"], [])

    def test_hint_placed_task_joins_trip_and_leaves_bucket(self) -> None:
        tasks = [_task(1, "写一份读书笔记", place_hint="图书馆")]
        plan = self._plan(tasks)
        stops = [s for trip in plan["trips"] for s in trip["stops"]]
        self.assertEqual([s["task_id"] for s in stops], [1])
        self.assertEqual(stops[0]["place_source"], "hint")
        self.assertEqual(plan["unscheduled"], [])
        self.assertEqual(plan["counts"]["unscheduled"], {})


class SetTaskPlaceHintTest(unittest.TestCase):
    """place_hint 的写入口：只认校园里真实存在的地点，认不出就报错。"""

    def setUp(self) -> None:
        import tempfile
        from noticehub.infrastructure.sqlite.store import Store
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "nh.db")
        self.store.init()
        self.task_id = self.store.upsert_task(
            task_key="manual:test", summary="写一份读书笔记", source="manual",
            confidence=1.0, classification_reason="页面手动新建",
        )
        from noticehub.application.campus import load_pois
        self.pois = load_pois(None)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _set(self, hint: str) -> dict:
        from noticehub.application.tasks import set_task_place_hint
        return set_task_place_hint(self.store, str(self.task_id), self.pois, hint)

    def test_valid_name_is_stored_as_id(self) -> None:
        result = self._set("图书馆")
        self.assertTrue(result["ok"])
        self.assertEqual(result["place_hint"], "library")

    def test_valid_id_is_accepted(self) -> None:
        result = self._set("wujin")
        self.assertTrue(result["ok"])
        self.assertEqual(result["place_hint"], "wujin")

    def test_invalid_hint_is_rejected_with_chinese_message(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self._set("火星基地")
        self.assertEqual(str(caught.exception), "「火星基地」不是校园里的地点")
        # 拒绝之后任务原样保留，没存进去半个字。
        task = self.store.get_task(self.task_id)
        self.assertEqual(str(task.get("place_hint") or ""), "")

    def test_empty_hint_clears(self) -> None:
        self._set("图书馆")
        result = self._set("")
        self.assertTrue(result["ok"])
        self.assertEqual(result["place_hint"], "")


class OrderStopsTest(unittest.TestCase):
    """顺路顺序：从这一趟的起点出发每次挑最近的一站，不按输入顺序硬排。

    顺序本身在 noticehub/application/trips.py:103 的 _order_stops（最近邻），
    前端只按这个顺序画，所以锁在这里，不是浏览器用例。
    """

    def test_three_stops_are_reordered_by_distance(self) -> None:
        given = ["博雅居", "立功楼", "图文信息楼"]
        # 名字先对着真表核过（走 match_place，和事项文本认地点是同一条路），
        # 认不出来就不该静默排进去。
        places = [match_place(name, POIS) for name in given]
        self.assertEqual([place and place["id"] for place in places], ["boyaju", "ligong", "tuwen"])
        ordered = _order_stops(
            [{"place": place, "title": name} for name, place in zip(given, places)],
            match_place("立德楼", POIS),
        )
        self.assertEqual([stop["place"]["id"] for stop in ordered], ["ligong", "tuwen", "boyaju"])
        self.assertNotEqual([stop["place"]["name"] for stop in ordered], given)


class ManualOrderTest(unittest.TestCase):
    """用户拖出来的站序：手排优先；名单里已经不存在的标题跳过；没排到的按最近邻补在后面。"""

    ROUTINES = [
        {"title": "还书", "place": "图书馆", "minutes": 10},
        {"title": "交报告", "place": "武进楼", "minutes": 10},
        {"title": "找老师", "place": "立德楼", "minutes": 10},
    ]

    def _titles(self, order=()) -> list[str]:
        plan = plan_day(
            [],
            pois=POIS,
            routines=self.ROUTINES,
            order=list(order),
            today=DAY,
            now=dt.datetime(2026, 11, 16, 8, 0),
        )
        return [stop["title"] for trip in plan["trips"] for stop in trip["stops"]]

    def test_manual_order_beats_nearest_neighbour(self) -> None:
        plain = self._titles()
        self.assertEqual(plain, ["还书", "交报告", "找老师"])
        self.assertEqual(self._titles(["找老师", "还书", "交报告"]), ["找老师", "还书", "交报告"])

    def test_unknown_titles_are_skipped_without_raising(self) -> None:
        titles = self._titles(["已经不存在的站", "交报告"])
        self.assertEqual(titles[0], "交报告")
        self.assertEqual(sorted(titles), ["交报告", "找老师", "还书"])

    def test_task_titles_match_and_the_rest_follow(self) -> None:
        tasks = [_task(1, "还书 图书馆"), _task(2, "交报告 武进楼"), _task(3, "找老师 立德楼")]
        plan = plan_day(
            tasks,
            pois=POIS,
            order=["找老师 立德楼", "还书 图书馆"],
            today=DAY,
            now=dt.datetime(2026, 11, 16, 8, 0),
        )
        titles = [stop["title"] for trip in plan["trips"] for stop in trip["stops"]]
        self.assertEqual(titles, ["找老师 立德楼", "还书 图书馆", "交报告 武进楼"])

    def test_without_order_nothing_changes(self) -> None:
        self.assertEqual(self._titles(), self._titles([]))

    def test_dragged_into_another_trip_it_stays_there(self) -> None:
        """拖到别的区的趟里就留在那趟：常做的事不属于哪个区，出门一趟顺手办了才合理。"""
        routines = [
            {"title": "还书", "place": "图书馆", "minutes": 10},
            {"title": "吃饭", "place": "第一餐厅", "minutes": 30},
        ]
        kwargs = {
            "pois": POIS,
            "routines": routines,
            "today": DAY,
            "now": dt.datetime(2026, 11, 16, 8, 0),
        }
        plain = plan_day([], **kwargs)
        self.assertEqual(len(plain["trips"]), 2)
        merged = plan_day([], order=["还书", "吃饭"], groups=[["还书", "吃饭"]], **kwargs)
        self.assertEqual(len(merged["trips"]), 1)
        self.assertEqual([stop["title"] for stop in merged["trips"][0]["stops"]], ["还书", "吃饭"])

    def test_a_dragged_stop_does_not_rename_the_trip(self) -> None:
        """把生活区的站拖到最前面，整趟仍按多数站叫教学区，不跟着第一站改名。"""
        routines = [
            {"title": "还书", "place": "图书馆", "minutes": 10},
            {"title": "找老师", "place": "立德楼", "minutes": 10},
            {"title": "吃饭", "place": "第一餐厅", "minutes": 30},
        ]
        plan = plan_day(
            [],
            pois=POIS,
            routines=routines,
            order=["吃饭", "还书", "找老师"],
            groups=[["吃饭", "还书", "找老师"]],
            today=DAY,
            now=dt.datetime(2026, 11, 16, 8, 0),
        )
        self.assertEqual(len(plan["trips"]), 1)
        self.assertEqual([stop["title"] for stop in plan["trips"][0]["stops"]],
                         ["吃饭", "还书", "找老师"])
        self.assertEqual(plan["trips"][0]["zone"], "教学区")

    def test_groups_say_nothing_about_the_rest(self) -> None:
        """只提到一站的趟，别的站照旧按区域分：手排不该把关掉的站也并进来。"""
        routines = [
            {"title": "还书", "place": "图书馆", "minutes": 10},
            {"title": "吃饭", "place": "第一餐厅", "minutes": 30},
        ]
        plan = plan_day(
            [],
            pois=POIS,
            routines=routines,
            order=["还书", "吃饭"],
            groups=[["还书"]],
            today=DAY,
            now=dt.datetime(2026, 11, 16, 8, 0),
        )
        self.assertEqual(len(plan["trips"]), 2)
        self.assertEqual([stop["title"] for stop in plan["trips"][0]["stops"]], ["还书"])
        self.assertEqual([stop["title"] for stop in plan["trips"][1]["stops"]], ["吃饭"])


if __name__ == "__main__":
    unittest.main()
