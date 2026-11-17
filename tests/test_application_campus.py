"""西太湖校区地点目录的数量、坐标方向与未知地点边界。"""

import unittest

from noticehub.application.campus import load_pois, match_place


class CampusCatalogTest(unittest.TestCase):
    def test_catalog_is_plan_buildings_with_south_positive_y(self):
        pois = load_pois(None)
        names = {'1号楼', '2号楼', '3号楼', '4号楼', '5号楼', '6号楼',
                 '7号楼', '8号楼', '9号楼', '10号楼', '11号楼',
                 '第一餐厅', '第二餐厅', '图书馆', '图文信息中心楼',
                 '马士楼', '白云楼', '武进楼', '汤湖楼',
                 '立德楼', '立功楼', '立言楼', '立行楼', '立艺楼',
                 '汤湖体育馆', '运动场', '博雅居'}
        self.assertEqual(len(pois), 27)
        self.assertEqual({p['name'] for p in pois}, names)
        self.assertEqual(len({p['id'] for p in pois}), 27)
        library = match_place('图书馆还书', pois)
        # 高德图书馆点按平面图线性拟合落到图上：x 向东、y 向南（原点西北角）。
        self.assertEqual((library['x'], library['y']), (563.3, 622.1))
        self.assertEqual(library['floors'], 5)
        self.assertTrue(all(p['open'] == '' for p in pois))  # 数据源不含营业时间，不编造。

    def test_y_points_south_so_lide_is_below_dormitory_one(self):
        # OSM 立德楼 y_north=154.9 比学生公寓 613.4 靠南；平面图上立德楼同样在 1 号楼下面一格。
        pois = load_pois(None)
        lide = match_place('立德楼', pois)
        dorm = match_place('1号楼', pois)
        self.assertGreater(lide['y'], dorm['y'])
        self.assertGreater(lide['y'], 0)  # y 正南、原点在图的西北角，不会有负数。
        self.assertGreater(match_place('立言楼', pois)['y'], match_place('图书馆', pois)['y'])

    def test_library_and_tuwen_are_two_places(self):
        # 用户明确：图书馆与图文信息中心楼不是同一栋，不合并成同一个服务点。
        # 两个名字各自的落点要分得开：说「图书馆」不能落到图文信息中心楼，反之也一样。
        pois = load_pois(None)
        library = match_place('图书馆', pois)
        tuwen = match_place('图文信息楼', pois)
        self.assertEqual(library['id'], 'library')
        self.assertEqual(tuwen['id'], 'tuwen')
        self.assertEqual(match_place('图文信息中心楼', pois)['id'], 'tuwen')
        self.assertNotEqual(library['id'], tuwen['id'])
        self.assertEqual(tuwen['name'], '图文信息中心楼')
        for poi in (library, tuwen):
            self.assertIsInstance(poi['x'], float)
            self.assertIsInstance(poi['y'], float)
            self.assertNotEqual((poi['x'], poi['y']), (0.0, 0.0))
        self.assertNotEqual((library['x'], library['y']), (tuwen['x'], tuwen['y']))
        self.assertLess(abs(library['x'] - tuwen['x']) + abs(library['y'] - tuwen['y']), 200)
        # 演示里那句原话，走的是图文信息中心楼那条记录。
        self.assertEqual(match_place('到图文信息楼交材料', pois)['id'], 'tuwen')
        self.assertEqual(match_place('去图书馆还书', pois)['id'], 'library')

    def test_plan_codes_resolve_to_the_same_buildings(self):
        pois = load_pois(None)
        for code, name in (('J1', '立德楼'), ('J2', '立功楼'), ('J3', '立言楼'),
                           ('J4', '立行楼'), ('J5', '立艺楼'),
                           ('X1', '马士楼'), ('X2', '白云楼'), ('X3', '武进楼'), ('X4', '汤湖楼')):
            with self.subTest(code=code):
                self.assertEqual(match_place(code, pois)['id'], match_place(name, pois)['id'])

    def test_numbered_dormitories_do_not_collide(self):
        pois = load_pois(None)
        ids = {str(n): match_place(f'{n}号楼', pois)['id'] for n in range(1, 12)}
        self.assertEqual(ids['1'], 'dorm_1')
        self.assertEqual(ids['11'], 'dorm_11')
        self.assertEqual(len(set(ids.values())), 11)

    def test_partial_calibration_is_applied_once_and_bad_values_are_ignored(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'campus_poi.json'
            path.write_text(json.dumps([{'id': 'library', 'floors': 7, 'dx': 10, 'dy': 20}]), encoding='utf-8')
            first = match_place('图书馆', load_pois(tmp))
            self.assertEqual((first['x'], first['y'], first['floors']), (573.3, 602.1, 7))
            self.assertEqual(match_place('图书馆', load_pois(tmp)), first)
            for bad in ('bad', None, True, [], {}, float('nan'), float('inf')):
                path.write_text(json.dumps([{'id': 'library', 'floors': bad, 'dx': bad, 'dy': bad}]), encoding='utf-8')
                with self.subTest(bad=bad):
                    poi = match_place('图书馆', load_pois(tmp))
                    self.assertEqual((poi['x'], poi['y'], poi['floors']), (563.3, 622.1, 5))
            path.write_text(json.dumps([{'id': 'new', 'name': '自定义楼', 'x': 10, 'y': 20, 'dx': 2, 'dy': 3, 'floors': 2}]), encoding='utf-8')
            poi = match_place('自定义楼', load_pois(tmp))
            self.assertEqual((poi['x'], poi['y'], poi['floors']), (12, 17, 2))

    def test_unknown_or_removed_place_is_not_guessed(self):
        for text in ('不存在的楼', '菜鸟驿站', '学生公寓', '基础实验楼', '立信楼'):
            with self.subTest(text=text):
                self.assertIsNone(match_place(text, load_pois(None)))
