"""演示数据的地点表：用户场景点名的三个楼要能查到，而且不能叠在同一处。"""
import math
import tempfile
import unittest
from pathlib import Path

from demo import seed
from noticehub.application.campus import load_pois


class DemoPoisTest(unittest.TestCase):
    def test_scenario_places_resolve_apart(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed.write_pois(tmp)
            pois = load_pois(tmp)
        by_name = {item["name"]: item for item in pois}
        self.assertIn("博雅居", by_name["学生公寓A"]["aliases"], "博雅居该挂成学生公寓A的别名")
        spots = {"博雅居": (by_name["学生公寓A"]["x"], by_name["学生公寓A"]["y"])}
        for name in ("立功楼", "图文信息楼"):
            self.assertIn(name, by_name)
            spots[name] = (by_name[name]["x"], by_name[name]["y"])
        ordered = sorted(spots.items())
        for index, (left_name, left) in enumerate(ordered):
            for right_name, right in ordered[index + 1:]:
                self.assertGreater(math.dist(left, right), 100,
                                   f"{left_name} 和 {right_name} 挤在一起，地图上会叠成一栋")

    def test_write_pois_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed.write_pois(tmp)
            first = (Path(tmp) / seed.POI_FILE).read_text(encoding="utf-8")
            seed.write_pois(tmp)
            second = (Path(tmp) / seed.POI_FILE).read_text(encoding="utf-8")
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
