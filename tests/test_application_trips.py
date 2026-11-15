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
from noticehub.application.trips import free_windows, plan_day  # noqa: E402

DAY = dt.date(2026, 11, 16)
WEEKDAY = DAY.isoweekday()
POIS = load_pois(None)


def _task(task_id: int, summary: str, deadline: str = "", status: str = "open") -> dict:
    return {
        "id": task_id,
        "summary": summary,
        "deadline": deadline,
        "status": status,
        "done": status == "done",
        "importance": 3,
        "source": "manual",
    }


def _lesson(start: str, end: str, place: str = "博雅居 105", weekday: int | None = None) -> dict:
    return {
        "weekday": WEEKDAY if weekday is None else weekday,
        "start": start,
        "end": end,
        "name": "课",
        "place": place,
    }


class MatchPlaceTest(unittest.TestCase):
    def test_longest_alias_wins(self) -> None:
        poi = match_place("把报告交到图文信息楼 B203", POIS)
        self.assertIsNotNone(poi)
        self.assertEqual(poi["id"], "tuwen")

    def test_canteen_number_beats_generic(self) -> None:
        self.assertEqual(match_place("去二食堂买饭", POIS)["id"], "canteen2")
        self.assertEqual(match_place("去一食堂吃饭", POIS)["id"], "canteen1")

    def test_no_place_returns_none_instead_of_guessing(self) -> None:
        self.assertIsNone(match_place("写一份读书笔记", POIS))

    def test_room_extraction(self) -> None:
        self.assertEqual(route_room("把实验报告交到图文信息楼 B203"), "B203")
        self.assertEqual(route_room("找学长拿材料"), "")


class LoadConfigTest(unittest.TestCase):
    def test_custom_poi_overrides_by_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "campus_poi.json"
            path.write_text(
                json.dumps([{"id": "ligonglou", "name": "立功楼", "x": 10, "y": 20}]),
                encoding="utf-8",
            )
            pois = load_pois(tmp)
        ligong = [p for p in pois if p["id"] == "ligonglou"]
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
                            {"weekday": 1, "start": "08:00", "end": "09:40", "place": "博雅居"},
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
    def test_origin_is_last_lesson_building(self) -> None:
        plan = plan_day(
            [],
            pois=POIS,
            lessons=[_lesson("08:00", "09:40", "立功楼 302"), _lesson("14:00", "15:40", "博雅居 105")],
            today=DAY,
            now=dt.datetime(2026, 11, 16, 12, 0),
        )
        self.assertIsNotNone(plan["origin"])
        self.assertEqual(plan["origin"]["id"], "boyaju")

    def test_stop_never_starts_before_class_ends(self) -> None:
        plan = plan_day(
            [_task(1, "取快递 菜鸟驿站")],
            pois=POIS,
            lessons=[_lesson("14:00", "15:40")],
            today=DAY,
            now=dt.datetime(2026, 11, 16, 12, 0),
        )
        self.assertEqual(len(plan["trips"]), 1)
        self.assertGreaterEqual(plan["trips"][0]["start_at"], "15:40")

    def test_same_zone_merges_into_one_trip(self) -> None:
        plan = plan_day(
            [_task(1, "取快递 菜鸟驿站"), _task(2, "去一食堂买饭")],
            pois=POIS,
            lessons=[],
            today=DAY,
            now=dt.datetime(2026, 11, 16, 8, 0),
        )
        self.assertEqual(len(plan["trips"]), 1)
        self.assertEqual(len(plan["trips"][0]["stops"]), 2)

    def test_task_without_place_stays_out_of_trips(self) -> None:
        plan = plan_day(
            [_task(1, "写一份读书笔记"), _task(2, "取快递 菜鸟驿站")],
            pois=POIS,
            lessons=[],
            today=DAY,
            now=dt.datetime(2026, 11, 16, 8, 0),
        )
        titles = [s["title"] for trip in plan["trips"] for s in trip["stops"]]
        self.assertEqual(titles, ["取快递 菜鸟驿站"])
        self.assertEqual(plan["unscheduled"][0]["task_id"], 1)
        self.assertEqual(plan["unscheduled"][0]["reason"], "没认出地点")

    def test_finished_tasks_are_ignored(self) -> None:
        plan = plan_day(
            [_task(1, "取快递 菜鸟驿站", status="done"), _task(2, "还书 图书馆", status="dismissed")],
            pois=POIS,
            lessons=[],
            today=DAY,
            now=dt.datetime(2026, 11, 16, 8, 0),
        )
        self.assertEqual(plan["trips"], [])
        self.assertEqual(plan["unscheduled"], [])

    def test_overflow_is_reported_with_alternative(self) -> None:
        plan = plan_day(
            [_task(1, "取材料 立功楼 407"), _task(2, "交报告 图文信息楼 B203")],
            pois=POIS,
            lessons=[_lesson("14:00", "15:40")],
            today=DAY,
            now=dt.datetime(2026, 11, 16, 12, 0),
            day_end="16:10",
        )
        self.assertEqual(len(plan["trips"]), 1)
        self.assertEqual(len(plan["trips"][0]["stops"]), 1)
        self.assertEqual(len(plan["overflow"]), 1)
        self.assertIn("明天上午", plan["overflow"][0]["reason"])

    def test_closing_window_warning(self) -> None:
        plan = plan_day(
            [_task(1, "取快递 菜鸟驿站")],
            pois=POIS,
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
             _task(2, "交报告 图文信息楼", deadline="2026-11-16 18:00:00")],
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
            routines=[{"title": "去超市买东西", "place": "超市", "minutes": 20}],
            today=DAY,
            now=dt.datetime(2026, 11, 16, 8, 0),
        )
        stop = plan["trips"][0]["stops"][0]
        self.assertEqual(stop["kind"], "routine")
        self.assertEqual(stop["minutes"], 20)

    def test_plan_is_deterministic(self) -> None:
        tasks = [_task(1, "取快递 菜鸟驿站"), _task(2, "交报告 图文信息楼 B203")]
        kwargs = dict(pois=POIS, lessons=[_lesson("14:00", "15:40")], today=DAY,
                      now=dt.datetime(2026, 11, 16, 12, 0))
        first = json.dumps(plan_day(tasks, **kwargs), sort_keys=True, ensure_ascii=False)
        second = json.dumps(plan_day(tasks, **kwargs), sort_keys=True, ensure_ascii=False)
        self.assertEqual(first, second)

    def test_checks_mention_lessons_and_origin(self) -> None:
        plan = plan_day(
            [_task(1, "取快递 菜鸟驿站")],
            pois=POIS,
            lessons=[_lesson("14:00", "15:40")],
            today=DAY,
            now=dt.datetime(2026, 11, 16, 12, 0),
        )
        text = " ".join(plan["checks"])
        self.assertIn("1 节", text)
        self.assertIn("博雅居", text)


    def test_first_stop_at_origin_does_not_claim_another_task(self) -> None:
        # 起点就是下课的楼：第一站的说明不能写「同一栋楼里还有一件」——那是给后一站用的
        plan = plan_day(
            [_task(1, "找辅导员签字 立功楼512")],
            pois=POIS,
            lessons=[_lesson("14:00", "15:40", "立功楼401")],
            today=DAY,
            now=dt.datetime(2026, 11, 16, 12, 0),
        )
        stop = plan["trips"][0]["stops"][0]
        self.assertEqual(stop["place_id"], "ligonglou")
        self.assertIn("顺路先办", stop["why"])
        self.assertNotIn("还有一件", stop["why"])

    def test_second_stop_in_same_building_says_one_trip(self) -> None:
        plan = plan_day(
            [_task(1, "找辅导员签字 立功楼512"), _task(2, "交班会材料 立功楼302")],
            pois=POIS,
            lessons=[_lesson("14:00", "15:40", "立功楼401")],
            today=DAY,
            now=dt.datetime(2026, 11, 16, 12, 0),
        )
        stops = plan["trips"][0]["stops"]
        self.assertEqual([s["place_id"] for s in stops], ["ligonglou", "ligonglou"])
        self.assertIn("还有一件", stops[1]["why"])


if __name__ == "__main__":
    unittest.main()
