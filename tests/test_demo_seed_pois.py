"""演示数据的地点表：用户场景点名的三个地方，内置表就得认得，而且不能叠在一处。"""
import math
import unittest

from noticehub.application.campus import load_pois


class DemoPoisTest(unittest.TestCase):
    def test_scenario_places_resolve_apart(self):
        pois = load_pois(None)
        by_name = {item["name"]: item for item in pois}
        spots = {}
        for name in ("博雅居", "立功楼", "图文信息中心楼"):
            self.assertIn(name, by_name, f"内置表里没有 {name}")
            spots[name] = (by_name[name]["x"], by_name[name]["y"])
        ordered = sorted(spots.items())
        for index, (left_name, left) in enumerate(ordered):
            for right_name, right in ordered[index + 1:]:
                self.assertGreater(math.dist(left, right), 100,
                                   f"{left_name} 和 {right_name} 挤在一起，地图上会叠成一栋")

    def test_boyaju_is_its_own_dorm_area_not_a_building_alias(self):
        # 博雅居是宿舍区名，不是某一栋楼：挂给某栋楼就是编精确度。
        pois = load_pois(None)
        boyaju = next(item for item in pois if item["name"] == "博雅居")
        self.assertEqual(boyaju["id"], "boyaju")
        for poi in pois:
            if poi["id"] != "boyaju":
                self.assertNotIn("博雅居", poi.get("aliases") or [], poi["id"])

    def test_boyaju_sits_in_the_dorm_area_on_the_plan(self):
        # 平面图把「博雅居 Boys Dormitory」标在 3/4 号楼那排宿舍的北侧。
        # 锁住「在图纸范围内、且在北半区」：早先按高德换算的 x=914.6 已经越出图宽 864。
        pois = {item["name"]: item for item in load_pois(None)}
        boyaju = pois["博雅居"]
        self.assertGreaterEqual(boyaju["x"], 0)
        self.assertLessEqual(boyaju["x"], 864, "博雅居落到平面图东边界之外了")
        self.assertGreaterEqual(boyaju["y"], 0)
        self.assertLessEqual(boyaju["y"], 576, "博雅居该在图纸北半区（宿舍片）")
        self.assertLess(boyaju["y"], pois["立德楼"]["y"], "博雅居该在立德楼北侧")


if __name__ == "__main__":
    unittest.main()