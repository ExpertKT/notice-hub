"""planToSnapshot 仿射变换的 round-trip 验收（赛事 MAP TASK 1 addendum）。

campus-geo.json 里 planToSnapshot = {a,b,tx,ty} 是「平面坐标 → 快照像素」的旋转+缩放，
trip.js 的 toMode/fromMode 全靠它。这里用 Python 复刻同一套矩阵，锁两件事：
- 每个 POI 的 from_plan 过去再回来，误差 < 1 px（可逆，不该有漂移）。
- 立德楼走 plan→snapshot→WGS 后，跟文件里记的 lng/lat 残差 < 5 m（换算链没接错空间）。
JS 不复制这些系数，全从 campus-geo.json 读（trip.js 里也是 fetch 这份），所以这里直接断言
文件字段存在即可。
"""
import json
import math
import os
import unittest

M_PER_DEG = 111320.0
GEO_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "noticehub", "web_static", "js", "campus-geo.json",
)


def _load():
    with open(GEO_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def plan_to_snapshot(tf, x, y):
    return (tf["a"] * x - tf["b"] * y + tf["tx"], tf["b"] * x + tf["a"] * y + tf["ty"])


def snapshot_to_plan(tf, sx, sy):
    det = tf["a"] * tf["a"] + tf["b"] * tf["b"]
    dx, dy = sx - tf["tx"], sy - tf["ty"]
    return ((tf["a"] * dx + tf["b"] * dy) / det, (tf["a"] * dy - tf["b"] * dx) / det)


class PlanTransformTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.geo = _load()
        cls.tf = cls.geo["planToSnapshot"]
        cls.origin = cls.geo["projection"]["origin"]
        cls.mpp = cls.geo["projection"]["metersPerPixel"]

    def test_transform_fields_present(self):
        for key in ("a", "b", "tx", "ty"):
            self.assertIn(key, self.tf, "planToSnapshot 缺 %s，JS 会退化成恒等" % key)

    def test_every_poi_round_trips_within_1px(self):
        worst = 0.0
        checked = 0
        for poi in self.geo["pois"].values():
            plan = poi.get("from_plan")
            if not plan:
                continue
            checked += 1
            sx, sy = plan_to_snapshot(self.tf, plan[0], plan[1])
            bx, by = snapshot_to_plan(self.tf, sx, sy)
            residual = math.hypot(bx - plan[0], by - plan[1])
            worst = max(worst, residual)
            self.assertLess(residual, 1.0, "%s round-trip 漂了 %.3f px" % (poi["name"], residual))
        self.assertGreater(checked, 0, "campus-geo.json 里没有带 from_plan 的 POI")

    def test_lide_residual_under_5m(self):
        """立德楼：平面坐标经换算得到的经纬度，跟文件里实测的 lng/lat 比，残差多少米。"""
        lide = self.geo["pois"]["lide"]
        sx, sy = plan_to_snapshot(self.tf, lide["from_plan"][0], lide["from_plan"][1])
        lat = self.origin["lat"] - (sy * self.mpp) / M_PER_DEG
        lon = self.origin["lon"] + (sx * self.mpp) / (M_PER_DEG * math.cos(math.radians(self.origin["lat"])))
        # 把经纬度差折回米（纬度方向用 M_PER_DEG，经度方向乘 cos(lat)）
        dy = (lat - lide["lat"]) * M_PER_DEG
        dx = (lon - lide["lng"]) * M_PER_DEG * math.cos(math.radians(lide["lat"]))
        residual = math.hypot(dx, dy)
        self.assertLess(residual, 5.0, "立德楼残差 %.1f m，换算链接错了空间" % residual)


if __name__ == "__main__":
    unittest.main()
