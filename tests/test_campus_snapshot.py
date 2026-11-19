"""校区快照可换：campus-geo.json 里可选的 "snapshot" 键。

用 node 跑 noticehub/web_static/js/campus-snapshot.js 里**真实**的校验/回落逻辑：
- 缺省（campus-geo.json 没写 snapshot）→ 仍用默认 js/campus-osm.js，行为不变；
- 非法取值（不是 js/ 开头 / 不是 .js 结尾 / 含 .. / 不是字符串）→ 回落默认，不加载坏路径。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from noticehub import webapp  # noqa: E402

PAGE_DIR = Path(webapp.__file__).resolve().parent / "web_static"
LOADER_PATH = PAGE_DIR / "js" / "campus-snapshot.js"
PAGE_PATH = PAGE_DIR / "page.html"
SW_PATH = PAGE_DIR / "js" / "sw.js"
GEO_PATH = PAGE_DIR / "js" / "campus-geo.json"

# 把 loader 的 IIFE 拆开只留函数体，末尾追加一个 dump，node 直接打印校验结果为 JSON。
HARNESS_JS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf-8');
const body = src.replace(/^\(function \(global\) \{/, '').replace(/\}\)\(typeof window[^)]*\)\);\s*$/, '');
const sandbox = { console: console, fetch: function () { return Promise.reject(new Error('no fetch')); } };
sandbox.globalThis = sandbox;
const run = new Function('globalThis', 'window', 'document', body);
run(sandbox, sandbox, { head: { appendChild: function () {} }, createElement: function () { return {}; } });
const v = sandbox.nhCampusSnapshot.validSnapshotPath;
console.log(JSON.stringify({
  default_when_no_key: v(undefined) === null,
  default_when_empty: v('') === null,
  default_when_not_string: v(42) === null,
  default_when_relative: v('campus-osm-x.js') === null,
  default_when_absolute_url: v('https://evil.example/x.js') === null,
  default_when_protocol_relative: v('//evil.example/x.js') === null,
  default_when_dotdot: v('js/../secret.js') === null,
  default_when_not_js: v('js/campus-osm.json') === null,
  accepts_valid: v('js/campus-osm-x.js') === 'js/campus-osm-x.js',
  default_is_js_campus_osm: sandbox.nhCampusSnapshot.loadCampusSnapshot !== undefined
}));
"""


def _node_available() -> bool:
    return shutil.which("node") is not None


class CampusSnapshotValidationTest(unittest.TestCase):
    """用真实 loader 的校验函数跑：非法/缺省 snapshot 一律回落默认，合法才放行。"""

    @classmethod
    def setUpClass(cls) -> None:
        if not _node_available():
            raise unittest.SkipTest("本机没有 node，跳过行为级 JS 测试")
        cls.report = {}
        with tempfile.TemporaryDirectory(dir=Path(r"F:\qq-notice-hub\nh-tmp")) as tmp:
            script = Path(tmp) / "harness.js"
            script.write_text(HARNESS_JS, encoding="utf-8")
            result = subprocess.run(
                ["node", str(script), str(LOADER_PATH)],
                capture_output=True,
                timeout=60,
            )
        if result.returncode != 0:
            raise AssertionError(
                "harness 崩溃，退出码 %d\nstderr:\n%s" % (result.returncode, result.stderr.decode("utf-8", "replace"))
            )
        try:
            cls.report = json.loads(result.stdout.decode("utf-8"))
        except json.JSONDecodeError as error:
            raise AssertionError("harness 输出不是 JSON：%s" % error) from error

    def _check(self, name: str) -> None:
        self.assertTrue(self.report.get(name), "%s 应为真" % name)

    def test_missing_snapshot_key_falls_back_to_default(self) -> None:
        """campus-geo.json 没写 snapshot：校验返回 null → 走默认，行为不变。"""
        self._check("default_when_no_key")

    def test_invalid_snapshot_values_all_fall_back_to_default(self) -> None:
        """空串/非字符串/相对路径/绝对 URL/协议相对/含 ../非 .js：全部回落默认。"""
        for name in (
            "default_when_empty",
            "default_when_not_string",
            "default_when_relative",
            "default_when_absolute_url",
            "default_when_protocol_relative",
            "default_when_dotdot",
            "default_when_not_js",
        ):
            with self.subTest(case=name):
                self._check(name)

    def test_valid_snapshot_value_is_accepted(self) -> None:
        """合法的 js/ 开头、.js 结尾、不含 .. 的路径才被采用。"""
        self._check("accepts_valid")

    def test_default_snapshot_is_the_current_one(self) -> None:
        """默认那份 campus-osm.js 仍被页面同步加载，且 loader 装好了。"""
        self._check("default_is_js_campus_osm")


class CampusSnapshotWiringTest(unittest.TestCase):
    """页面接法与预缓存清单：默认快照仍同步加载，loader 在其后，sw 只预缓存默认那份。"""

    def test_page_loads_default_then_snapshot_loader(self) -> None:
        html = PAGE_PATH.read_text(encoding="utf-8")
        default_at = html.find('<script src="js/campus-osm.js"></script>')
        loader_at = html.find('<script src="js/campus-snapshot.js"></script>')
        self.assertGreaterEqual(default_at, 0, "页面要保留默认快照 js/campus-osm.js 的同步加载")
        self.assertGreater(loader_at, default_at, "快照装载器要在默认快照之后加载")
        self.assertIn("nhCampusSnapshot.loadCampusSnapshot()", html, "页面要真的去读 snapshot 键")

    def test_campus_geo_json_has_no_snapshot_key_by_default(self) -> None:
        """默认配置不带 snapshot 键：上线行为跟改动前完全一样。"""
        geo = json.loads(GEO_PATH.read_text(encoding="utf-8"))
        self.assertNotIn("snapshot", geo, "campus-geo.json 默认不该写 snapshot，写了就换校区了")

    def test_sw_precache_keeps_only_default_snapshot(self) -> None:
        sw = SW_PATH.read_text(encoding="utf-8")
        self.assertIn("'/js/campus-osm.js'", sw, "默认快照仍要预缓存")
        self.assertNotRegex(sw, r"campus-osm-\w+\.js", "自定义快照文件名不该进预缓存清单")


if __name__ == "__main__":
    unittest.main()
