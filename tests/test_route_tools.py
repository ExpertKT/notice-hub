"""规划工具的米制几何契约；固定坐标是测试样本，不是校园测绘。"""

import math
import unittest
from itertools import permutations

from noticehub.application import route_tools


POIS = [
    {"id": "a", "name": "起点楼", "x": 0, "y": 0},
    {"id": "b", "name": "远处楼", "x": 600, "y": 0},
    {"id": "c", "name": "中间楼", "x": 300, "y": 0},
]


class RouteToolsTest(unittest.TestCase):
    def test_route_shortens_walk(self):
        result = route_tools.plan_route(
            ["起点楼", "远处楼", "中间楼"], pois=POIS,
        )
        self.assertEqual(result["order"], ["起点楼", "中间楼", "远处楼"])
        self.assertEqual(result["distance_m"], 600)
        self.assertEqual(result["input_distance_m"], 900)
        self.assertEqual(result["saved_m"], 300)
        self.assertEqual(result["walk_minutes"], 8)
        self.assertEqual(
            [(leg["distance_m"], leg["walk_minutes"]) for leg in result["legs"]],
            [(300, 4), (300, 4)],
        )

    def test_explicit_start_and_exact_solution(self):
        pois = [dict(id=str(i), name=str(i), x=x, y=y) for i, (x, y) in enumerate(
            [(0, 0), (3, 4), (7, 1), (1, 9), (8, 8), (-3, 2), (5, -5), (-2, 7), (10, 0)]
        )]
        def length(order):
            return sum(math.hypot(pois[a]['x'] - pois[b]['x'], pois[a]['y'] - pois[b]['y'])
                       for a, b in zip((0,) + order, order))
        result = route_tools.plan_route([str(i) for i in range(1, 9)], start='0', pois=pois)
        self.assertEqual(result['distance_m'], round(min(map(length, permutations(range(1, 9)))), 1))
        self.assertEqual(result['order'][0], '0')
        self.assertEqual(result['algorithm'], 'exact')

    def test_large_route_uses_two_opt_and_never_worsens_input(self):
        pois = [dict(id=str(i), name=str(i), x=x, y=y) for i, (x, y) in enumerate(
            [(73, 88), (67, 94), (61, 32), (95, 42), (89, 84), (95, 6), (69, 52), (83, 54), (97, 76), (3, 78)]
        )]
        result = route_tools.plan_route([str(i) for i in range(1, 10)], start='0', pois=pois)
        self.assertEqual(result['algorithm'], 'nearest_neighbor_2opt')
        self.assertEqual(set(result['order']), {str(i) for i in range(10)})
        self.assertLessEqual(result['distance_m'], result['input_distance_m'])
        self.assertLess(result['distance_m'], 293.9)  # 此样本的最近邻原路线 293.989 米，必须真的改进。
        order = [int(i) for i in result['order']]
        def length(indices):
            return sum(math.hypot(pois[a]['x'] - pois[b]['x'], pois[a]['y'] - pois[b]['y'])
                       for a, b in zip(indices, indices[1:]))
        for left in range(1, len(order) - 1):
            for right in range(left + 1, len(order)):
                candidate = order[:left] + order[left:right + 1][::-1] + order[right + 1:]
                self.assertGreaterEqual(length(candidate) + 1e-9, length(order))

    def test_radius_boundary_and_only_unfinished_tasks(self):
        pois = POIS + [dict(id='d', name='界外楼', x=300.01, y=0)]
        tasks = [dict(id=1, summary='中间楼交表', status='open'),
                 dict(id=2, summary='中间楼取书', status='done'),
                 dict(id=3, summary='界外楼领卡', status='open')]
        result = route_tools.poi_near('起点楼', 300, pois=pois, tasks=tasks)
        self.assertEqual([p['name'] for p in result['places']], ['起点楼', '中间楼'])
        self.assertEqual(result['tasks'], [dict(task_id=1, summary='中间楼交表', place='中间楼', distance_m=300)])
        self.assertEqual(route_tools.poi_near('起点楼', 0, pois=pois)['places'][0]['distance_m'], 0)

    def test_empty_unknown_and_invalid_arguments_fail(self):
        for points in ([], None, '起点楼', [None], ['不存在'], ['起点楼'] * 65):
            with self.subTest(points=points), self.assertRaises(ValueError):
                route_tools.plan_route(points, pois=POIS)
        with self.assertRaisesRegex(ValueError, '找不到'):
            route_tools.plan_route(['起点楼'], start='不存在', pois=POIS)
        for radius in (-1, float('nan'), float('inf'), None, True, 'bad'):
            with self.subTest(radius=radius), self.assertRaises(ValueError):
                route_tools.poi_near('起点楼', radius, pois=POIS)
        with self.assertRaisesRegex(ValueError, '找不到'):
            route_tools.poi_near('不存在', 300, pois=POIS)
        with self.assertRaisesRegex(ValueError, '坐标'):
            route_tools.plan_route(['坏楼'], pois=[dict(id='bad', name='坏楼', x=float('nan'), y=0)])

    def test_id_alias_duplicate_and_default_catalog(self):
        pois = [dict(POIS[0], aliases=['起点']), *POIS[1:]]
        result = route_tools.plan_route(['a', '起点', '远处楼'], pois=pois)
        self.assertEqual(result['order'], ['起点楼', '远处楼'])
        self.assertEqual(route_tools.plan_route(['图书馆'])['distance_m'], 0)
        self.assertEqual(route_tools.poi_near('图书馆', 0)['places'][0]['name'], '图书馆')
        with self.assertRaisesRegex(ValueError, '不唯一'):
            route_tools.plan_route(['起点楼'], pois=[*pois, dict(id='other', name='起点楼', x=2, y=2)])

    def test_contest_coordinates_are_computed_not_scripted(self):
        # 形态稿上那两个数：用内置 17 栋的坐标现算，不是抄进代码里的。只复算直线距离，
        # 不声称已测量校园步行路网。
        result = route_tools.plan_route(['立德楼', '图书馆'])
        self.assertEqual(result['order'], ['立德楼', '图书馆'])
        self.assertEqual(result['distance_m'], 272.8)
        self.assertEqual(result['walk_minutes'], 3.64)
        self.assertEqual(result['legs'][0]['distance_m'], 272.8)
        self.assertEqual(result['legs'][0]['walk_minutes'], 3.64)
