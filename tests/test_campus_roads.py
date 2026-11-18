"""campus_roads.road_line 的路线真得跟着路网走，还不能为了贴路绕出荒唐的远路。

坐标一律是快照像素：和 `campus-geo.json` 的 `px`、`_blocks()` 的楼轮廓、前端
`toScreen` 同一个空间。早先这个文件把立德楼/立功楼写成平面图坐标、立行楼/立言楼
写成快照像素，混着喂给 road_line，于是「立德楼→立行楼 5.5 倍」其实是换算错位
算出来的；换算删掉以后同一对楼是 2.05 倍、且不穿楼。

两头的底线：一是没有遮挡的近程不能绕出荒唐的远路（立德楼→立功楼，绕行比封在 2.2 倍），
二是折线不许从楼里或水面上穿过去——这条优先于远近。
"""

import unittest
from unittest import mock

from noticehub.application import campus_roads as roads

# 快照像素，取自 campus-geo.json 的 pois[*].px。
LI_DE = (357.7, 1101.0)  # 立德楼
LI_XING = (345.4, 1255.6)  # 立行楼
LI_YAN = (630.1, 1289.6)  # 立言楼
LI_GONG = (660.7, 1145.5)  # 立功楼
BOYAJU = (280.7, 572.1)  # 博雅居


def _blocks():
    return roads._blocks()


def _segment_distance(p, a, b):
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    if length < 1e-12:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length))
    return ((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2) ** 0.5


def _crow(a, b):
    return ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5


class CampusRoadsTest(unittest.TestCase):
    def test_route_between_li_de_and_li_gong_stays_within_2_2_times_straight(self):
        crow = _crow(LI_DE, LI_GONG)
        line = roads.road_line([LI_DE, LI_GONG])
        walked = roads._polyline_length(line)
        self.assertLessEqual(walked, 2.2 * crow)

    def test_a_building_in_the_way_costs_distance_not_legality(self):
        # 立德楼→立行楼：直线压着一栋楼，宁可绕远也不许画过去，但也不能绕到天边。
        crow = _crow(LI_DE, LI_XING)
        line = roads.road_line([LI_DE, LI_XING])
        walked = roads._polyline_length(line)
        self.assertGreater(walked, 1.0 * crow)
        self.assertLessEqual(walked, roads.MAX_DETOUR * crow)
        blocks = _blocks()
        for i in range(len(line) - 1):
            start, end = line[i], line[i + 1]
            span = _crow(start, end)
            steps = max(1, int(span / 4.0))
            for s in range(steps + 1):
                t = s / steps
                point = (start[0] + (end[0] - start[0]) * t, start[1] + (end[1] - start[1]) * t)
                for poly in blocks:
                    self.assertFalse(roads._inside(poly, point[0], point[1]))

    def test_line_never_crosses_a_building_or_water(self):
        blocks = _blocks()
        self.assertTrue(blocks)
        line = roads.road_line([LI_DE, LI_XING, LI_YAN, LI_GONG])
        for i in range(len(line) - 1):
            start, end = line[i], line[i + 1]
            span = _crow(start, end)
            steps = max(1, int(span / 4.0))
            for s in range(steps + 1):
                t = s / steps
                point = (start[0] + (end[0] - start[0]) * t, start[1] + (end[1] - start[1]) * t)
                for poly in blocks:
                    self.assertFalse(roads._inside(poly, point[0], point[1]))

    def test_boyaju_to_ligong_follows_streets_instead_of_cutting_through(self):
        # 演示里最常走的一段：博雅居→立功楼。博雅居的重心压在自家楼轮廓里，
        # 起点到楼下那 40 像素必然穿自家楼（没有门的位置数据），其余部分不许穿楼。
        crow = _crow(BOYAJU, LI_GONG)
        line = roads.road_line([BOYAJU, LI_GONG])
        walked = roads._polyline_length(line)
        self.assertGreater(len(line), 3)
        self.assertLessEqual(walked, 1.6 * crow)
        blocks = _blocks()
        # 两头那栋楼自己的轮廓不算：快照里没有门，从楼心走出来必然穿自家楼。
        own = {i for i, poly in enumerate(blocks) for pt in (BOYAJU, LI_GONG) if roads._inside(poly, pt[0], pt[1])}
        for i in range(len(line) - 1):
            start, end = line[i], line[i + 1]
            span = _crow(start, end)
            steps = max(1, int(span / 4.0))
            for s in range(steps + 1):
                t = s / steps
                point = (start[0] + (end[0] - start[0]) * t, start[1] + (end[1] - start[1]) * t)
                for index, poly in enumerate(blocks):
                    if index in own:
                        continue
                    self.assertFalse(roads._inside(poly, point[0], point[1]))

    def test_endpoints_survive_rounding(self):
        line = roads.road_line([LI_DE, LI_XING])
        # 站数不是契约（可能为了绕楼加站），两头的端点才是。
        self.assertEqual(line[0], [LI_DE[0], LI_DE[1]])
        self.assertEqual(line[-1], [LI_XING[0], LI_XING[1]])

    def test_route_between_buildings_near_a_named_road_rides_that_road(self):
        # 立言楼↔立行楼：楼群中心离命名路最近的这一对，路线要贴着某条路中心线走。
        crow = _crow(LI_XING, LI_YAN)
        line = roads.road_line([LI_XING, LI_YAN])
        walked = roads._polyline_length(line)
        self.assertGreater(walked, crow)
        # 上限用模块自己的 MAX_DETOUR：绕得比这更离谱就不该收下这条线。
        self.assertLessEqual(walked, roads.MAX_DETOUR * crow)
        named = [r for r in roads._snapshot().get("roads") or [] if r.get("name")]
        self.assertTrue(named)
        centre_lines = [r.get("pts") or [] for r in named]
        best = min(
            _segment_distance(v, centre_lines[i][k], centre_lines[i][k + 1])
            for i in range(len(centre_lines))
            for k in range(len(centre_lines[i]) - 1)
            for v in line[1:-1]
        )
        self.assertLessEqual(best, 9.0)

    def test_identical_points_collapse_to_one_point(self):
        self.assertEqual(roads.road_line([LI_DE, LI_DE]), [[LI_DE[0], LI_DE[1]]])

    def test_empty_snapshot_falls_back_to_a_straight_line(self):
        with mock.patch.object(roads, "_snapshot", return_value={}):
            roads._road_graph.cache_clear()
            roads._grid.cache_clear()
            roads._block_boxes.cache_clear()
            try:
                line = roads.road_line([LI_DE, LI_GONG])
            finally:
                roads._road_graph.cache_clear()
                roads._grid.cache_clear()
                roads._block_boxes.cache_clear()
        self.assertEqual(line[0], [LI_DE[0], LI_DE[1]])
        self.assertEqual(line[-1], [LI_GONG[0], LI_GONG[1]])

    def test_same_input_gives_the_same_line(self):
        first = roads.road_line([LI_DE, LI_XING, LI_GONG])
        second = roads.road_line([LI_DE, LI_XING, LI_GONG])
        self.assertEqual(first, second)

    def test_a_five_stop_trip_is_drawn_under_the_performance_budget(self):
        # 一组行程最多五站（四腿），这是界面真实的一次调用；重复量同一条腿十倍量出来的是
        # 最坏绕行乘十，跟用户等多久没关系。
        pts = [LI_DE, LI_XING, LI_GONG, LI_YAN, LI_DE]
        roads.road_line(pts)  # 预热快照与网格缓存，量的是稳态
        import time

        start = time.perf_counter()
        for _ in range(3):
            roads.road_line(pts)
        elapsed_ms = (time.perf_counter() - start) * 1000 / 3
        self.assertLess(elapsed_ms, 200.0)


class RoadLegTest(unittest.TestCase):
    """一句腿的老实话：接上路网就画折线，接不上就说只能按直线示意。"""

    def test_a_routable_leg_comes_back_as_a_polyline(self):
        leg = roads.road_leg(LI_DE, LI_GONG)
        self.assertFalse(leg["degraded"])
        self.assertEqual(leg["reason"], "routed")
        self.assertGreater(len(leg["path"]), 2)

    def test_a_straight_line_through_a_building_is_marked_as_a_straight_line(self):
        # 拿一栋楼的两侧当两头：直线必然穿楼，画出来只有两个点，界面必须能说这是按直线示意的。
        # 用楼块自己的 bbox 造这一对，不依赖 data/ 里那份可能被挪过的地点表。
        boxes = [box for box in roads._block_boxes() if box[2] - box[0] > 60 and box[3] - box[1] > 60]
        self.assertTrue(boxes)
        x0, y0, x1, y1 = max(boxes, key=lambda box: (box[2] - box[0]) * (box[3] - box[1]))
        middle = (y0 + y1) / 2.0
        here = (x0 - 40.0, middle)
        there = (x1 + 40.0, middle)
        leg = roads.road_leg(here, there)
        self.assertTrue(leg["degraded"])
        self.assertEqual(leg["reason"], "straight")
        self.assertLessEqual(len(leg["path"]), 2)

    def test_no_origin_leg_is_not_degraded(self):
        leg = roads.road_leg(None, LI_DE)
        self.assertIsNone(leg["path"])
        self.assertFalse(leg["degraded"])
        self.assertEqual(leg["reason"], "no_leg")

    def test_missing_coordinates_degrade_instead_of_raising(self):
        for here, there in (((), ()), ((1.0,), (2.0, 3.0)), (("x", "y"), (2.0, 3.0))):
            leg = roads.road_leg(here, there)
            self.assertTrue(leg["degraded"], (here, there))
            self.assertEqual(leg["reason"], "missing_data")


if __name__ == "__main__":
    unittest.main()
