"""tools/campus_demo.py 得能跑出那条演示剧情。

手上已经有两件事（博雅居、立功楼），再说第三件在图文信息楼：智能体要把它排进
下课那趟、跟立功楼顺路，而不是另开一趟。脚本自己从 campus-geo.json 取坐标，
拿 DEFAULT_POIS（没有 x/y）去算距离会当场 KeyError。
"""

import unittest

from tools import campus_demo


class CampusDemoScriptTest(unittest.TestCase):
    def test_demo_plan_matches_the_contest_story(self):
        data = campus_demo.build()
        self.assertEqual(set(data), {"pois", "lessons", "routines", "before", "after"})
        self.assertEqual(len(data["pois"]), 27)
        self.assertEqual([p["id"] for p in data["pois"] if "x" not in p or "y" not in p], [])

        before = data["before"]
        # counts 从 30 天窗口那批改动起多一栏 unscheduled：按 reason_code 数没排进去的件数。
        self.assertEqual(before["counts"],
                         {"trips": 2, "stops": 2, "distance_m": 688,
                          "unscheduled": {"no_place": 1}})
        self.assertEqual([s["place"] for t in before["trips"] for s in t["stops"]], ["立功楼", "博雅居"])

        after = data["after"]
        self.assertEqual(after["origin"]["name"], "立功楼")
        self.assertEqual(after["counts"],
                         {"trips": 2, "stops": 3, "distance_m": 711,
                          "unscheduled": {"no_place": 2}})
        # 第三件（图文信息楼）跟着下课那趟走，没另开一趟。
        self.assertEqual([s["place"] for s in after["trips"][0]["stops"]], ["立功楼", "图文信息中心楼"])
        self.assertEqual([s["place"] for s in after["trips"][1]["stops"]], ["博雅居"])
        # 认不出地点的两件事照样列出来，不硬塞进趟里。
        self.assertEqual([t["task_id"] for t in after["unscheduled"]], ["t3", "t5"])
        self.assertEqual({t["reason"] for t in after["unscheduled"]}, {"没认出地点"})


if __name__ == "__main__":
    unittest.main()
