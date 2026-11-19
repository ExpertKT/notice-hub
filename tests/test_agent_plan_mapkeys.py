"""map_keys.json 存取 + /api/agent/map/keys 路由的验收。

锁住的行为：
- env 优先于文件：两边都有时以环境变量为准，source 标 "env"。
- 文件能写入再读回（round-trip），source 标 "file"。
- 非法 provider、含空白 key、不含 {x} 的 osm tile 一律拒绝，且不写文件。
- map_config() 读到损坏 JSON 不抛异常，退回默认（osm enabled + 默认瓦片）。
"""
import json
import os
import unittest

from noticehub.application import agent_plan
from noticehub.config import Settings


class MapKeysStoreTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix='mapkeys-')
        self.settings = Settings(data_dir=self.tmp)
        self.keys_path = os.path.join(self.tmp, 'map_keys.json')
        self._saved_env = {}
        for name in (agent_plan.AMAP_KEY_ENV, agent_plan.AMAP_SECRET_ENV,
                     agent_plan.BAIDU_KEY_ENV, agent_plan.OSM_TILE_ENV):
            self._saved_env[name] = os.environ.pop(name, None)

    def tearDown(self):
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_env_wins_over_file(self):
        with open(self.keys_path, 'w', encoding='utf-8') as fh:
            json.dump({'amap': {'key': 'filekey123', 'secret': 'filesecret'}}, fh)
        os.environ[agent_plan.AMAP_KEY_ENV] = 'envkey456'
        conf = agent_plan.map_config(self.settings)
        self.assertTrue(conf['amap']['enabled'])
        self.assertEqual(conf['amap']['key'], 'envkey456')
        self.assertEqual(conf['amap']['secret'], 'filesecret')  # 文件里的 secret 仍在
        self.assertEqual(conf['amap']['source'], 'env')

    def test_file_round_trip(self):
        payload = {'action': 'keys', 'provider': 'amap', 'key': 'abcdef123456', 'secret': 'sec'}
        body = agent_plan.save_map_keys(self.settings, payload)
        self.assertTrue(body['ok'])
        conf = agent_plan.map_config(self.settings)
        self.assertEqual(conf['amap']['key'], 'abcdef123456')
        self.assertEqual(conf['amap']['secret'], 'sec')
        self.assertEqual(conf['amap']['source'], 'file')

    def test_bad_provider_rejected(self):
        with self.assertRaises(ValueError):
            agent_plan.save_map_keys(self.settings,
                                     {'action': 'keys', 'provider': 'tianditu', 'key': 'abcdef123456'})
        self.assertFalse(os.path.exists(self.keys_path))

    def test_key_with_whitespace_rejected(self):
        with self.assertRaises(ValueError):
            agent_plan.save_map_keys(self.settings,
                                     {'action': 'keys', 'provider': 'baidu', 'key': 'abc 12345'})

    def test_osm_tile_without_xyz_rejected(self):
        with self.assertRaises(ValueError):
            agent_plan.save_map_keys(self.settings,
                                     {'action': 'keys', 'provider': 'osm', 'tile': 'https://a.com/t.png'})

    def test_osm_tile_good_saved(self):
        agent_plan.save_map_keys(self.settings,
                                 {'action': 'keys', 'provider': 'osm',
                                  'tile': 'https://a.b/{z}/{x}/{y}.png'})
        conf = agent_plan.map_config(self.settings)
        self.assertEqual(conf['osm']['tile'], 'https://a.b/{z}/{x}/{y}.png')
        self.assertTrue(conf['osm']['enabled'])

    def test_corrupt_file_never_raises(self):
        with open(self.keys_path, 'w', encoding='utf-8') as fh:
            fh.write('{not json')
        conf = agent_plan.map_config(self.settings)  # 不抛
        self.assertTrue(conf['osm']['enabled'])
        self.assertFalse(conf['amap']['enabled'])


    def test_secret_without_key_is_not_enabled(self):
        # 只有 security code 没有 key：这一档算没配。回 enabled:true 加空 key，前端会去拉一次注定失败的脚本。
        os.environ[agent_plan.AMAP_SECRET_ENV] = 'onlysecret'
        conf = agent_plan.map_config(self.settings)
        self.assertFalse(conf['amap']['enabled'])
        self.assertEqual(conf['amap']['key'], '')
        self.assertEqual(conf['amap']['secret'], 'onlysecret')
        # 补上 key（写在文件里）就启用，secret 仍取 env 那份
        with open(self.keys_path, 'w', encoding='utf-8') as fh:
            json.dump({'amap': {'key': 'filekey123'}}, fh)
        conf = agent_plan.map_config(self.settings)
        self.assertTrue(conf['amap']['enabled'])
        self.assertEqual(conf['amap']['key'], 'filekey123')
        self.assertEqual(conf['amap']['secret'], 'onlysecret')
        self.assertEqual(conf['amap']['source'], 'file')


if __name__ == '__main__':
    unittest.main()
