"""演示所说的「三件事顺路」：三个地点都要解析得出来，路线要真算得出来。

锁的是内置地点表本身：博雅居、立功楼、图文信息中心楼都在表里，
不靠演示种子另写一份 campus_poi.json，plan_route 才不抛「找不到地点」。
"""
import unittest

from noticehub.application.campus import load_pois
from noticehub.application.route_tools import plan_route


class DemoRouteTest(unittest.TestCase):
    def test_three_places_plan_a_route(self):
        result = plan_route(["博雅居", "立功楼", "图文信息楼"], pois=load_pois(None))
        self.assertEqual(result["algorithm"], "exact")
        self.assertEqual(result["order"][0], "博雅居", "起点该是文本里第一个地点")
        self.assertEqual(set(result["order"]), {"博雅居", "立功楼", "图文信息中心楼"})
        self.assertEqual(len(result["legs"]), 2)
        self.assertGreater(result["distance_m"], 0)
        self.assertGreater(result["walk_minutes"], 0)

    def test_unknown_place_still_fails_loudly(self):
        with self.assertRaises(ValueError) as caught:
            plan_route(["博雅居", "不存在楼"], pois=load_pois(None))
        self.assertIn("不存在楼", str(caught.exception))


if __name__ == "__main__":
    unittest.main()