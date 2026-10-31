"""红队 S1/S6 的前端行为用例：URL 令牌换 HttpOnly Cookie、令牌不落 localStorage、清空待配对。

用本机 node 直接跑 page.html / js 里的**真实** JS 函数（不锁源码字符串）：
fetch / history / location 都是桩，断言用户实际会看到的结果（地址栏、提示文案、状态行）。
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
PAGE_HTML_PATH = PAGE_DIR / "page.html"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")


HARNESS_JS = r"""
const fs = require('fs');
const path = require('path');
const pagePath = process.argv[2];
const jsDir = path.join(path.dirname(pagePath), 'js');
// page.html 在前、各 js 块在后；函数按名字取第一处定义，和浏览器里先加载 page.html 的脚本一致。
const page = fs.readFileSync(pagePath, 'utf-8')
  + (fs.existsSync(jsDir)
    ? fs.readdirSync(jsDir).filter((n) => n.endsWith('.js')).sort()
      .map((n) => '\n' + fs.readFileSync(path.join(jsDir, n), 'utf-8')).join('')
    : '');

function extractFunction(src, name) {
  const marker = 'function ' + name + '(';
  const start = src.indexOf(marker);
  if (start < 0) throw new Error('找不到函数：' + name);
  const end = src.indexOf('\n}', start);
  if (end < 0) throw new Error('函数未以行首 } 结束：' + name);
  return src.slice(start, end + 2);
}

function makeEnv(options) {
  options = options || {};
  const hint = { textContent: '', hidden: true };
  const document = {
    getElementById(id) { return id === 'token-hint' ? hint : null; },
  };
  const calls = { replaced: [], reloads: 0, fetches: [] };
  const location = {
    search: options.search || '',
    href: options.href || '',
    reload() { calls.reloads += 1; },
  };
  const history = { replaceState(state, title, url) { calls.replaced.push(url); } };
  const responses = (options.responses || []).slice();
  function fetchStub(url, init) {
    calls.fetches.push({ url: url, init: init || {} });
    const next = responses.shift();
    if (!next) return Promise.reject(new Error('没有更多桩响应'));
    if (next.throw) return Promise.reject(next.throw);
    return Promise.resolve({ ok: next.ok !== false, status: next.status || 200, json: () => Promise.resolve(next.body || {}) });
  }
  return { document, location, history, fetch: fetchStub, calls, hint };
}

function makeExchange(env, token) {
  const factory = new Function('document', 'location', 'history', 'fetch', 'nhUrlToken',
    extractFunction(page, 'nhUrlWithoutToken') + '\n'
    + extractFunction(page, 'showTokenHint') + '\n'
    + extractFunction(page, 'exchangeUrlToken')
    + '\nreturn exchangeUrlToken;');
  return factory(env.document, env.location, env.history, env.fetch, token);
}

function makeClearPending(results) {
  const calls = { api: [], statuses: [], refreshes: 0 };
  const queue = results.slice();
  function api(url, init) {
    calls.api.push({ url: url, init: init || {} });
    const next = queue.shift();
    if (!next) return Promise.reject(new Error('没有更多桩响应'));
    if (next.throw) return Promise.reject(next.throw);
    return Promise.resolve(next.body);
  }
  function setPairStatus(text, state) { calls.statuses.push({ text: text, state: state }); }
  function refreshPair() { calls.refreshes += 1; }
  const factory = new Function('api', 'setPairStatus', 'refreshPair',
    extractFunction(page, 'clearPendingPairs') + '\nreturn clearPendingPairs;');
  return { clear: factory(api, setPairStatus, refreshPair), calls: calls };
}

async function scenarioExchangeSuccess() {
  const env = makeEnv({
    search: '?token=abc&x=1',
    href: 'http://127.0.0.1:8766/?token=abc&x=1#tab-2',
    responses: [{ ok: true, status: 200 }],
  });
  const result = await makeExchange(env, 'abc')();
  const failed = [];
  if (result !== true) failed.push('换 Cookie 成功却没返回 true');
  if (env.calls.replaced.length !== 1) failed.push('replaceState 调用次数 = ' + env.calls.replaced.length);
  if (env.calls.replaced[0] !== '/?x=1#tab-2') failed.push('抹掉 token 后的地址 = ' + env.calls.replaced[0]);
  if (env.calls.reloads !== 1) failed.push('成功后应重载一次，实际 ' + env.calls.reloads);
  const first = env.calls.fetches[0] || { init: {} };
  if (first.url !== '/api/session/exchange') failed.push('请求地址 = ' + first.url);
  if (first.init.method !== 'POST') failed.push('换取 Cookie 不是 POST');
  if (first.init.credentials !== 'same-origin') failed.push('换取 Cookie 没带 same-origin 凭据');
  let body = null;
  try { body = JSON.parse(first.init.body); } catch (error) { body = null; }
  if (!body || body.token !== 'abc') failed.push('请求体没带 URL 里的令牌');
  if (!env.hint.hidden) failed.push('成功时不该出现错误提示');
  return { pass: failed.length === 0, detail: failed.join('；') || '地址栏只去掉 token、其它参数与 hash 原样保留' };
}

async function scenarioExchange401() {
  const env = makeEnv({ search: '?token=bad', href: 'http://127.0.0.1:8766/?token=bad', responses: [{ ok: false, status: 401 }] });
  const result = await makeExchange(env, 'bad')();
  const failed = [];
  if (result !== false) failed.push('401 时不该报成功');
  if (env.hint.hidden) failed.push('令牌错误时没给人话提示');
  if (!/令牌不正确或已失效/.test(env.hint.textContent)) failed.push('提示文案 = ' + env.hint.textContent);
  if (env.calls.replaced.length) failed.push('失败时不该改写地址栏');
  if (env.calls.reloads) failed.push('失败时不该重载（会死循环）');
  return { pass: failed.length === 0, detail: failed.join('；') || '401 → 明确提示「令牌不正确或已失效」，不重载' };
}

async function scenarioExchangeNetworkFallback() {
  const env = makeEnv({
    search: '?token=abc',
    href: 'http://127.0.0.1:8766/?token=abc',
    responses: [{ throw: new Error('offline') }],
  });
  const result = await makeExchange(env, 'abc')();
  const failed = [];
  if (result !== false) failed.push('网络失败时不该报成功');
  if (env.hint.hidden === false) failed.push('网络失败不该吓唬用户说令牌错了');
  if (env.calls.replaced.length) failed.push('退回兼容路径时不该抹掉地址栏令牌');
  if (env.calls.reloads) failed.push('退回兼容路径时不该重载');
  return { pass: failed.length === 0, detail: failed.join('；') || '旧后端/断网时静默退回 X-Token 兼容路径' };
}

async function scenarioPairClearSuccess() {
  const env = makeClearPending([{ body: { ok: true, cleared: 3 } }]);
  const button = { disabled: false };
  await env.clear(button);
  const failed = [];
  const call = env.calls.api[0] || { init: {} };
  if (call.url !== '/api/pair/pending/clear') failed.push('请求地址 = ' + call.url);
  if (call.init.method !== 'POST') failed.push('清空待配对不是 POST');
  const status = env.calls.statuses[env.calls.statuses.length - 1] || {};
  if (!/已清空 3 条/.test(String(status.text))) failed.push('成功提示 = ' + status.text);
  if (env.calls.refreshes !== 1) failed.push('清空后没刷新列表');
  if (button.disabled !== false) failed.push('按钮没恢复可点');
  return { pass: failed.length === 0, detail: failed.join('；') || '清空 3 条并刷新列表' };
}

async function scenarioPairClearUnsupported() {
  const env = makeClearPending([{ throw: new Error('请求失败（404），请重试。') }]);
  const button = { disabled: false };
  await env.clear(button);
  const failed = [];
  const status = env.calls.statuses[env.calls.statuses.length - 1] || {};
  if (!/当前版本不支持，请升级/.test(String(status.text))) failed.push('失败提示 = ' + status.text);
  if (button.disabled !== false) failed.push('失败后按钮没恢复可点');
  return { pass: failed.length === 0, detail: failed.join('；') || '旧后端没有该路由时说清是版本问题' };
}

const report = {};
const scenarios = [
  ['exchange_success_strips_token_keeps_hash', scenarioExchangeSuccess],
  ['exchange_401_hint_and_no_reload_loop', scenarioExchange401],
  ['exchange_network_error_keeps_legacy_token', scenarioExchangeNetworkFallback],
  ['pair_clear_success_reports_count', scenarioPairClearSuccess],
  ['pair_clear_unsupported_says_upgrade', scenarioPairClearUnsupported],
];
let chain = Promise.resolve();
scenarios.forEach(function (entry) {
  chain = chain.then(function () {
    return Promise.resolve().then(entry[1]).then(function (result) {
      report[entry[0]] = result;
    }, function (error) {
      report[entry[0]] = { pass: false, detail: '场景崩溃：' + (error && (error.stack || error.message) || error) };
    });
  });
});
chain.then(function () {
  process.stdout.write(JSON.stringify(report));
}, function (error) {
  process.stdout.write(JSON.stringify({ fatal: String(error && (error.stack || error.message) || error) }));
  process.exitCode = 2;
});
"""


class TokenSessionBehaviorTest(unittest.TestCase):
    """Token exchange: URL token -> HttpOnly cookie; localStorage 里不再留令牌。"""

    report: dict = {}

    @classmethod
    def setUpClass(cls) -> None:
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("本机没有 node，跳过行为级 JS 测试")
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "harness.js"
            script.write_text(HARNESS_JS, encoding="utf-8")
            result = subprocess.run(
                [node, str(script), str(PAGE_HTML_PATH)],
                capture_output=True,
                timeout=60,
            )
        if result.returncode != 0:
            raise AssertionError(
                f"harness 崩溃，退出码 {result.returncode}\nstderr:\n{result.stderr.decode('utf-8', 'replace')}"
            )
        try:
            cls.report = json.loads(result.stdout.decode("utf-8"))
        except json.JSONDecodeError as error:
            raise AssertionError(
                "harness 输出不是 JSON：%s\nstdout:\n%s" % (error, result.stdout.decode("utf-8", "replace"))
            ) from error
        if "fatal" in cls.report:
            raise AssertionError("harness 致命错误：" + str(cls.report["fatal"]))

    def _check(self, name: str) -> None:
        row = self.report.get(name)
        self.assertIsNotNone(row, f"没有拿到场景 {name} 的结果")
        self.assertTrue(row.get("pass"), f"{name}: {row.get('detail')}")

    def test_exchange_success_strips_token_and_keeps_rest_of_url(self) -> None:
        self._check("exchange_success_strips_token_keeps_hash")

    def test_exchange_401_shows_message_without_reload_loop(self) -> None:
        self._check("exchange_401_hint_and_no_reload_loop")

    def test_exchange_network_error_falls_back_to_url_token(self) -> None:
        self._check("exchange_network_error_keeps_legacy_token")

    def test_pair_clear_success_reports_cleared_count(self) -> None:
        self._check("pair_clear_success_reports_count")

    def test_pair_clear_unsupported_tells_user_to_upgrade(self) -> None:
        self._check("pair_clear_unsupported_says_upgrade")


class TokenNotPersistedTest(unittest.TestCase):
    """令牌不再写进 localStorage：页面与脚本里都不许再出现旧的存储键。"""

    def test_token_is_not_kept_in_local_storage(self) -> None:
        sources = [_read(PAGE_HTML_PATH)] + [_read(p) for p in sorted((PAGE_DIR / "js").glob("*.js"))]
        for text in sources:
            self.assertNotIn("qq_digest_token", text, "还有代码把令牌存 localStorage")
            self.assertNotIn("localStorage.setItem(KEY, params.get('token'))", text, "还有代码把 URL 令牌写进 localStorage")

    def test_token_hint_node_exists_and_is_hidden_by_default(self) -> None:
        html = _read(PAGE_HTML_PATH)
        self.assertIn('id="token-hint"', html)
        tag = html.split('id="token-hint"', 1)[1].split(">", 1)[0]
        self.assertIn("hidden", tag)

    def test_pair_card_has_clear_pending_button(self) -> None:
        pair = _read(PAGE_DIR / "js" / "pair.js")
        self.assertIn('id="pair-pending-clear"', pair)
        self.assertIn("/api/pair/pending/clear", pair)
        self.assertIn("clearPendingPairs", pair)


if __name__ == "__main__":
    unittest.main()
