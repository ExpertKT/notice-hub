"""路网寻路的规矩：坐标系能往返、折线不穿楼、绕行不离谱。"""

from __future__ import annotations

import math
import unittest

from noticehub.application import campus, campus_roads


def _pois() -> dict[str, dict]:
    return {poi["id"]: poi for poi in campus.load_pois(None)}


class CoordinateTest(unittest.TestCase):
    def test_plan_and_snapshot_round_trip(self) -> None:
        # 楼位和路网分属两套像素平面，换算错一处整条线就整片平移（曾经北移约 290 米）。
        for point in ((0.0, 0.0), (312.5, 844.0), (-120.0, 1500.0)):
            here = campus_roads.to_snapshot(point)
            back = campus_roads.to_plan(here)
            self.assertAlmostEqual(back[0], point[0], places=6)
            self.assertAlmostEqual(back[1], point[1], places=6)


class RoadLineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pois = _pois()

    def _line(self, left: str, right: str) -> list[list[float]]:
        a, b = self.pois[left], self.pois[right]
        return campus_roads.road_line([(a["x"], a["y"]), (b["x"], b["y"])])

    def test_line_starts_and_ends_where_asked(self) -> None:
        # 立德楼→立行楼本来就近乎直着走，折线只有两个点是对的；
        # 这栋楼到那栋楼的门必须还是用户点的那两栋，长度不许比直线短（那说明点被算丢了）。
        a, b = self.pois["lide"], self.pois["lixing"]
        line = self._line("lide", "lixing")
        self.assertEqual(tuple(round(v) for v in line[0]), (round(a["x"]), round(a["y"])))
        self.assertEqual(tuple(round(v) for v in line[-1]), (round(b["x"]), round(b["y"])))
        walked = sum(
            math.hypot(line[i + 1][0] - line[i][0], line[i + 1][1] - line[i][1])
            for i in range(len(line) - 1)
        )
        crow = math.hypot(b["x"] - a["x"], b["y"] - a["y"])
        self.assertGreaterEqual(walked, crow * 0.95)
        self.assertLess(walked, crow * 1.1)

    def test_line_bends_when_a_building_is_in_the_way(self) -> None:
        # 图书馆→第一餐厅直线要穿过楼，图上线必须拐出去；只给两个点说明格子图根本没生效。
        a, b = self.pois["library"], self.pois["canteen1"]
        line = self._line("library", "canteen1")
        self.assertGreaterEqual(len(line), 3, "一个拐点都没有，说明格子图没起作用")
        self.assertEqual(tuple(round(v) for v in line[0]), (round(a["x"]), round(a["y"])))
        self.assertEqual(tuple(round(v) for v in line[-1]), (round(b["x"]), round(b["y"])))
        walked = sum(
            math.hypot(line[i + 1][0] - line[i][0], line[i + 1][1] - line[i][1])
            for i in range(len(line) - 1)
        )
        crow = math.hypot(b["x"] - a["x"], b["y"] - a["y"])
        self.assertGreater(walked, crow * 1.05, "和直线一样长，说明没绕楼")

    def test_detour_stays_sane(self) -> None:
        # 沿 OSM 路网硬走时这一段是 1106 米（直线 140 米）。绕楼不许绕成这样。
        a, b = self.pois["lide"], self.pois["lixing"]
        line = self._line("lide", "lixing")
        walked = sum(
            math.hypot(line[i + 1][0] - line[i][0], line[i + 1][1] - line[i][1])
            for i in range(len(line) - 1)
        ) * 0.9
        crow = math.hypot(b["x"] - a["x"], b["y"] - a["y"]) * 0.9
        self.assertLess(walked, crow * 2.0, "绕成了 %.0f 米，直线才 %.0f 米" % (walked, crow))
        self.assertGreaterEqual(walked, crow * 0.95, "比直线还短，说明点被算丢了")

    def test_no_segment_cuts_through_a_building(self) -> None:
        data = campus_roads._snapshot()
        blocks = [part["pts"] for part in data.get("buildings") or [] if len(part.get("pts") or []) >= 3]
        self.assertTrue(blocks, "快照里没有楼块，这条用例就白测了")
        for left, right in (("lide", "lixing"), ("library", "canteen1"), ("canteen1", "dorm_1")):
            line = campus_roads.road_line(
                [(self.pois[left]["x"], self.pois[left]["y"]), (self.pois[right]["x"], self.pois[right]["y"])]
            )
            for index in range(len(line) - 1):
                ax, ay = line[index]
                bx, by = line[index + 1]
                for step in range(1, 10):
                    t = step / 10
                    x, y = ax + (bx - ax) * t, ay + (by - ay) * t
                    if math.hypot(x - ax, y - ay) < 8 or math.hypot(x - bx, y - by) < 8:
                        continue   # 楼门口那几米本来就压在楼边上
                    spot = campus_roads.to_snapshot((x, y))
                    for poly in blocks:
                        self.assertFalse(
                            campus_roads._inside(poly, spot[0], spot[1]),
                            "%s→%s 的线穿过了楼（%.0f, %.0f）" % (left, right, x, y),
                        )

    def test_single_point_and_empty_are_handed_back_as_is(self) -> None:
        self.assertEqual(campus_roads.road_line([]), [])
        self.assertEqual(campus_roads.road_line([(1.0, 2.0)]), [[1.0, 2.0]])


if __name__ == "__main__":
    unittest.main()
