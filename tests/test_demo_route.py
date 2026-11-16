"""演示数据下的「三件事顺路」：三个地点都要解析得出来，路线要真算得出来。

锁的是演示地点表：seed 不写 campus_poi.json 时，博雅居/立功楼/图文信息楼都查不到，
plan_route 会抛「找不到地点」，界面上就是那句空回话。
"""
import tempfile
import unittest

from demo import seed
from noticehub.application.campus import load_pois
from noticehub.application.route_tools import plan_route


class DemoRouteTest(unittest.TestCase):
    def test_three_places_plan_a_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed.write_pois(tmp)
            pois = load_pois(tmp)
        result = plan_route(["博雅居", "立功楼", "图文信息楼"], pois=pois)
        self.assertEqual(result["algorithm"], "exact")
        self.assertEqual(result["order"][0], "学生公寓A", "起点该是文本里第一个地点（博雅居挂它在身上）")
        self.assertEqual(set(result["order"]), {"学生公寓A", "立功楼", "图文信息楼"})
        self.assertEqual(len(result["legs"]), 2)
        self.assertGreater(result["distance_m"], 0)
        self.assertGreater(result["walk_minutes"], 0)

    def test_unknown_place_still_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed.write_pois(tmp)
            pois = load_pois(tmp)
        with self.assertRaises(ValueError) as caught:
            plan_route(["博雅居", "不存在楼"], pois=pois)
        self.assertIn("不存在楼", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
