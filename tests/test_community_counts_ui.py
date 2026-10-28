"""社区在线数三态 + 设置页开关的前端用例。

用本机 node 跑 guide.js 里真实的 initCommunityCounts（document/api/window 都是桩），
断言用户实际看到的那一行：关闭=未开启、打开有数、打开无数、缺项只拼一项、失败重试。
设置页开关本身在 loadSettings 里（几千字符的 DOM 构建），这里断言关键接线与文案。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GUIDE_JS = PROJECT_ROOT / "qq_live_digest" / "web_static" / "js" / "guide.js"
SETTINGS_JS = PROJECT_ROOT / "qq_live_digest" / "web_static" / "js" / "settings.js"
PAGE_HTML = PROJECT_ROOT / "qq_live_digest" / "web_static" / "page.html"


HARNESS_JS = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[2], 'utf-8');

function extractFunction(src, name) {
  const marker = 'function ' + name + '(';
  const start = src.indexOf(marker);
  if (start < 0) throw new Error('找不到函数：' + name);
  const end = src.indexOf('\n}', start);
  if (end < 0) throw new Error('函数未以行首 } 结束：' + name);
  return src.slice(start, end + 2);
}

const fnSource = extractFunction(source, 'initCommunityCounts');
const report = {};

function makeEnv(options) {
  options = options || {};
  const line = { hidden: true, textContent: '' };
  const document = {
    getElementById(id) { return id === 'rail-community' ? line : null; },
    readyState: 'complete',
    addEventListener() {},
  };
  const queue = {
    settings: (options.settings || []).slice(),
    counts: (options.counts || []).slice(),
  };
  const calls = { settings: 0, counts: 0 };
  function api(path) {
    if (path === '/api/settings') {
      calls.settings += 1;
      const next = queue.settings.shift();
      if (!next) return Promise.resolve({ community_telemetry: true });
      if (next.throw) return Promise.reject(next.throw);
      return Promise.resolve(next);
    }
    if (path === '/api/community/counts') {
      calls.counts += 1;
      const next = queue.counts.shift();
      if (!next) return Promise.resolve({ counts: { online: null, today: null } });
      if (next.throw) return Promise.reject(next.throw);
      return Promise.resolve(next);
    }
    return Promise.reject(new Error('没桩这个接口：' + path));
  }
  const timers = [];
  const window = {
    setTimeout(fn) {
      // 重试立即执行：测试里不真的等 1.5 秒，但请求次数照实算。
      timers.push(fn);
      return timers.length;
    },
  };
  const factory = new Function('document', 'api', 'window', fnSource + '\nreturn initCommunityCounts;');
  return { line, calls, window, run: factory(document, api, window), timers };
}

async function settle(env) {
  for (let i = 0; i < 60; i += 1) await Promise.resolve();
  // setTimeout 桩只排队不执行：把排队的重试跑完（最多两轮，够覆盖一次重试）。
  for (let round = 0; round < 3; round += 1) {
    const pending = env.timers.splice(0, env.timers.length);
    if (!pending.length) break;
    pending.forEach((fn) => fn());
    for (let i = 0; i < 60; i += 1) await Promise.resolve();
  }
}

async function scenario(name, options) {
  const env = makeEnv(options);
  try {
    env.run();
    await settle(env);
    report[name] = {
      pass: true,
      hidden: env.line.hidden,
      text: env.line.textContent,
      settingsCalls: env.calls.settings,
      countsCalls: env.calls.counts,
    };
  } catch (error) {
    report[name] = { pass: false, text: '场景崩溃：' + String((error && error.stack) || error) };
  }
}

(async () => {
  await scenario('closed_switch_off', { settings: [{ community_telemetry: false }], counts: [] });
  await scenario('open_with_counts', { settings: [{ community_telemetry: true }], counts: [{ counts: { online: 12, today: 34 } }] });
  await scenario('open_without_counts', { settings: [{ community_telemetry: true }], counts: [{ counts: { online: null, today: null } }] });
  await scenario('open_partial_counts', { settings: [{ community_telemetry: true }], counts: [{ counts: { online: 7, today: null } }] });
  await scenario('open_request_fails', { settings: [{ community_telemetry: true }], counts: [{ throw: new Error('offline') }, { throw: new Error('offline') }] });
  await scenario('field_missing_falls_back', { settings: [{}], counts: [{ counts: { online: 3, today: 4 } }] });
  await scenario('settings_request_fails', { settings: [{ throw: new Error('no settings') }], counts: [{ counts: { online: null, today: null } }] });
  process.stdout.write(JSON.stringify(report));
})();
"""


class CommunityCountsLineTest(unittest.TestCase):
    """右栏那一行的三态（node 跑真实 initCommunityCounts）。"""

    report: dict = {}

    @classmethod
    def setUpClass(cls) -> None:
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("本机没有 node，跳过社区在线前端测试")
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "harness.js"
            script.write_text(HARNESS_JS, encoding="utf-8")
            result = subprocess.run([node, str(script), str(GUIDE_JS)], capture_output=True, timeout=60)
        if result.returncode != 0:
            raise AssertionError(
                "harness 崩溃，退出码 %d\nstderr:\n%s"
                % (result.returncode, result.stderr.decode("utf-8", "replace"))
            )
        cls.report = json.loads(result.stdout.decode("utf-8"))

    def _row(self, name: str) -> dict:
        row = self.report.get(name)
        self.assertIsNotNone(row, "没有拿到场景 %s" % name)
        self.assertTrue(row.get("pass"), row.get("text"))
        return row

    def test_closed_shows_hint_instead_of_hiding(self) -> None:
        row = self._row("closed_switch_off")
        self.assertFalse(row["hidden"], "关闭态不该把整行藏掉")
        self.assertIn("社区在线：未开启", row["text"])
        self.assertIn("设置", row["text"])
        self.assertIn("支持开发者", row["text"])

    def test_open_with_both_numbers(self) -> None:
        row = self._row("open_with_counts")
        self.assertFalse(row["hidden"])
        self.assertEqual(row["text"], "社区在线：12 人在用 · 今日 34 次打开")

    def test_open_without_numbers_says_unavailable(self) -> None:
        row = self._row("open_without_counts")
        self.assertFalse(row["hidden"])
        self.assertIn("暂不可用", row["text"])
        self.assertNotIn("未开启", row["text"])

    def test_open_with_only_online_builds_one_part(self) -> None:
        row = self._row("open_partial_counts")
        self.assertEqual(row["text"], "社区在线：7 人在用")

    def test_open_request_failure_retries_once_then_says_unavailable(self) -> None:
        row = self._row("open_request_fails")
        self.assertEqual(row["countsCalls"], 2, "失败应重试一次")
        self.assertIn("暂不可用", row["text"])

    def test_missing_settings_field_falls_back_to_counts(self) -> None:
        row = self._row("field_missing_falls_back")
        self.assertEqual(row["text"], "社区在线：3 人在用 · 今日 4 次打开")

    def test_settings_failure_does_not_claim_switch_is_off(self) -> None:
        row = self._row("settings_request_fails")
        self.assertNotIn("未开启", row["text"])
        self.assertIn("暂不可用", row["text"])


class CommunityTelemetrySwitchTest(unittest.TestCase):
    """设置页开关：id、代价说明、POST 字段、保存后立刻重跑那一行。"""

    def setUp(self) -> None:
        self.settings = SETTINGS_JS.read_text(encoding="utf-8")
        self.page = PAGE_HTML.read_text(encoding="utf-8")

    def test_switch_markup_and_warning(self) -> None:
        self.assertIn('id="pref-community-telemetry"', self.settings)
        self.assertIn('id="pref-community-telemetry-result"', self.settings)
        self.assertIn("role=\"status\" aria-live=\"polite\"", self.settings)
        self.assertIn("第三方计数服务", self.settings)
        self.assertIn("不包含任何消息、群号或账号信息", self.settings)

    def test_switch_posts_boolean_and_refreshes_rail(self) -> None:
        self.assertIn('body:JSON.stringify({community_telemetry:enabled})', self.settings)
        self.assertIn("window.initCommunityCounts==='function'", self.settings)
        self.assertIn("已打开，正在读取在线数…", self.settings)
        self.assertIn("已关闭，不再上报", self.settings)

    def test_rail_line_exists_in_page(self) -> None:
        self.assertIn('id="rail-community"', self.page)


if __name__ == "__main__":
    unittest.main()
