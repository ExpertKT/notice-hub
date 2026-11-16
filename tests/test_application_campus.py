"""西太湖校区地点目录的数量、坐标方向与未知地点边界。"""

import unittest

from noticehub.application.campus import load_pois, match_place


class CampusCatalogTest(unittest.TestCase):
    def test_catalog_has_seventeen_real_buildings_and_south_positive_y(self):
        pois = load_pois(None)
        names = {'图书馆', '立言楼', '润湖体育馆', '立德楼', '基础实验楼',
                 '一食堂', '立行楼', '立信楼', *('学生公寓' + letter for letter in 'ABCDEFGHI')}
        self.assertEqual(len(pois), 17)
        self.assertEqual({p['name'] for p in pois}, names)
        self.assertEqual(len({p['id'] for p in pois}), 17)
        library = match_place('图书馆还书', pois)
        # 来源 c=[391.1,368.6]，原点仍为西南角；北向正数转成南向正数。
        self.assertEqual((library['x'], library['y']), (391.1, -368.6))
        self.assertEqual(library['floors'], 5)
        self.assertLess(match_place('立言楼', pois)['y'], library['y'])
        self.assertTrue(all(p['open'] == '' for p in pois))  # 数据源不含营业时间，不编造。

    def test_unknown_or_removed_place_is_not_guessed(self):
        for text in ('不存在的楼', '博雅居', '立功楼', '图文信息楼', '菜鸟驿站', '学生公寓'):
            with self.subTest(text=text):
                self.assertIsNone(match_place(text, load_pois(None)))

    def test_apartment_letters_resolve_without_colliding(self):
        pois = load_pois(None)
        for letter in 'ABCDEFGHI':
            with self.subTest(letter=letter):
                self.assertEqual(match_place('回学生公寓' + letter, pois)['id'], 'dorm_' + letter.lower())
