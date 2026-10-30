"""B6/B5/B4/B1/B3/B8/B11/B10 + NapCat 安装进度的前端回归用例。

能用 node 跑真实函数的都用 node 跑（window、document、fetch 响应都是桩）；
挂载点和文案这类只能从源码确认的，用源码断言补，不拿字符串冒充行为覆盖。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB = PROJECT_ROOT / "qq_live_digest" / "web_static"
PAGE_HTML = WEB / "page.html"
SETTINGS_JS = WEB / "js" / "settings.js"
GUIDE_JS = WEB / "js" / "guide.js"
ANNOUNCE_JS = WEB / "js" / "announce.js"


HARNESS_JS = r"""
const fs = require('fs');
const read = (p) => fs.readFileSync(p, 'utf-8');
const page = read(process.argv[2]);
const settings = read(process.argv[3]);
const guide = read(process.argv[4]);
const announce = read(process.argv[5]);

function extractFunction(src, name) {
  const marker = 'function ' + name + '(';
  const start = src.indexOf(marker);
  if (start < 0) throw new Error('找不到函数：' + name);
  const open = src.indexOf('{', start);
  let depth = 0;
  for (let i = open; i < src.length; i += 1) {
    const ch = src[i];
    if (ch === '{') depth += 1;
    else if (ch === '}') {
      depth -= 1;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error('括号不配对：' + name);
}

// 把若干顶层函数拼进同一作用域，函数之间可以互相调用。
function bind(src, names, args, values) {
  const body = names.map((name) => extractFunction(src, name)).join('\n')
    + '\nreturn {' + names.join(',') + '};';
  return new Function(...(args || []), body)(...(values || []));
}

const report = {};
function check(name, fn) {
  const failed = [];
  try { fn(failed); } catch (error) { failed.push('场景崩溃：' + String((error && error.stack) || error)); }
  report[name] = { pass: failed.length === 0, detail: failed.join('；') || 'ok' };
}
async function settle() { for (let i = 0; i < 80; i += 1) await Promise.resolve(); }

// ---- NapCat 安装进度文案 ----
const nap = bind(guide, ['formatInstallMegabytes', 'describeNapcatInstall']);
check('napcat_download_with_total', (failed) => {
  const text = nap.describeNapcatInstall({stage: 'download', downloaded_bytes: 3 * 1048576, total_bytes: 12 * 1048576});
  if (text !== '正在下载 NapCat：3.0 MB / 12.0 MB（25%）') failed.push('实际：' + text);
});
check('napcat_download_without_total', (failed) => {
  const text = nap.describeNapcatInstall({stage: 'download', downloaded_bytes: 3145728, total_bytes: null});
  if (text !== '正在下载 NapCat：已下载 3.0 MB') failed.push('实际：' + text);
  if (text.indexOf('%') >= 0) failed.push('没有总量却编了百分比：' + text);
});
check('napcat_extract_counts', (failed) => {
  const byDone = nap.describeNapcatInstall({stage: 'extract', done: 3, total: 7});
  if (byDone !== '正在解压 NapCat：3/7 个文件') failed.push('done/total 实际：' + byDone);
  const byEntries = nap.describeNapcatInstall({stage: 'extract', extracted: 2, entries: 9});
  if (byEntries !== '正在解压 NapCat：2/9 个文件') failed.push('extracted/entries 实际：' + byEntries);
  const empty = nap.describeNapcatInstall({stage: 'extract'});
  if (empty !== '正在解压 NapCat…') failed.push('缺数据实际：' + empty);
});
check('napcat_verify_and_install', (failed) => {
  if (nap.describeNapcatInstall({stage: 'verify'}) !== '正在校验安装包') failed.push('verify 文案不对');
  if (nap.describeNapcatInstall({stage: 'install'}) !== '正在安装') failed.push('install 文案不对');
});
check('napcat_complete_and_error', (failed) => {
  if (nap.describeNapcatInstall({stage: 'complete'}) !== '已安装完成') failed.push('complete 文案不对');
  const err = nap.describeNapcatInstall({stage: 'error', note: '磁盘空间不足'});
  if (err !== '安装失败：磁盘空间不足') failed.push('error 文案不对：' + err);
  const noNote = nap.describeNapcatInstall({stage: 'error'});
  if (noNote.indexOf('安装失败') !== 0) failed.push('错误没有说明：' + noNote);
});
check('napcat_megabytes_have_one_decimal', (failed) => {
  const text = nap.describeNapcatInstall({stage: 'download', downloaded_bytes: 3145729.9, total_bytes: null});
  if (!/已下载 \d+\.\d MB/.test(text)) failed.push('MB 格式不是一位小数：' + text);
});

// ---- B11 服务时间 ----
const time = bind(settings, ['formatServiceStartedAt']);
check('service_time_formatted', (failed) => {
  const cases = [
    ['2026-10-28T20:23:18', '2026-10-28 20:23', '2026-10-28T20:23:18'],
    ['2026-10-28 20:23:18', '2026-10-28 20:23', '2026-10-28 20:23:18'],
    ['', '未知', ''],
    [null, '未知', ''],
    ['nonsense', 'nonsense', 'nonsense'],
  ];
  cases.forEach(([raw, text, title]) => {
    const got = time.formatServiceStartedAt(raw);
    if (got.text !== text) failed.push(JSON.stringify(raw) + ' → ' + got.text + '（应为 ' + text + '）');
    if (got.title !== title) failed.push(JSON.stringify(raw) + ' title 应为 ' + JSON.stringify(title));
  });
});

// ---- B3 主地址选择 ----
const addr = bind(settings, ['primaryAddress']);
check('primary_address_prefers_backend_primary', (failed) => {
  const picked = addr.primaryAddress([
    {url: 'http://192.168.1.2:8766/calendar.ics', kind: 'lan'},
    {url: 'http://100.78.12.25:8766/calendar.ics', kind: 'lan', primary: true},
  ]);
  if (!picked || picked.url.indexOf('100.78.12.25') < 0) failed.push('没用 primary：' + JSON.stringify(picked));
});
check('primary_address_falls_back_to_public_then_first', (failed) => {
  const publicPick = addr.primaryAddress([
    {url: 'http://a/calendar.ics', kind: 'lan'},
    {url: 'https://pub.example/calendar.ics', kind: 'public'},
  ]);
  if (!publicPick || publicPick.kind !== 'public') failed.push('没退回公网：' + JSON.stringify(publicPick));
  const first = addr.primaryAddress([{url: 'http://only/calendar.ics', kind: 'lan'}]);
  if (!first || first.url.indexOf('only') < 0) failed.push('没退回第一条');
  if (addr.primaryAddress([]) !== null) failed.push('空列表应为 null');
  if (addr.primaryAddress(null) !== null) failed.push('null 应为 null');
});

// ---- B6 更新通道单选 ----
check('update_channel_selection', (failed) => {
  const radios = [{value: 'stable', checked: false}, {value: 'beta', checked: false}];
  const factory = new Function('document', extractFunction(settings, 'selectUpdateChannel') + '\nreturn selectUpdateChannel;');
  const select = factory({querySelectorAll() { return radios; }});
  select('beta');
  if (!radios[1].checked || radios[0].checked) failed.push('beta 没选中');
  select('stable');
  if (!radios[0].checked || radios[1].checked) failed.push('stable 没选中');
  radios[0].checked = true; radios[1].checked = true;
  select('');
  if (radios[0].checked || radios[1].checked) failed.push('未知通道应两个都不选');
  if (select('nightly') !== '') failed.push('未知通道应归一成空串');
});

// ---- B4 平台磁贴点击 ----
const tile = bind(page, ['togglePlatformTile']);
check('platform_tile_toggles_and_dispatches_change', (failed) => {
  const events = [];
  const box = {checked: false, dispatchEvent(event) { events.push(event.type); return true; }};
  const fakeTile = {querySelector() { return box; }};
  const first = tile.togglePlatformTile(fakeTile);
  if (first !== true || box.checked !== true) failed.push('第一次没选中');
  const second = tile.togglePlatformTile(fakeTile);
  if (second !== false || box.checked !== false) failed.push('第二次没取消');
  if (events.length !== 2 || events[0] !== 'change') failed.push('没有每次都补发 change：' + JSON.stringify(events));
  if (tile.togglePlatformTile(null) !== null) failed.push('没有磁贴时应返回 null');
});

// ---- B1 URL 令牌与会话 Cookie ----
const withoutToken = bind(page, ['nhUrlWithoutToken']).nhUrlWithoutToken;
const exchangeSource = extractFunction(page, 'exchangeUrlToken');

async function runExchange(responses) {
  const state = {hint: '', replaced: null, reloads: 0, fetched: []};
  const hintEl = {hidden: true, textContent: ''};
  const document = {getElementById() { return hintEl; }};
  const location = {
    href: 'http://127.0.0.1:9/?token=stale&keep=1#frag',
    reload() { state.reloads += 1; },
  };
  const history = {replaceState(_s, _t, url) { state.replaced = url; }};
  function fetchStub(path) {
    state.fetched.push(path);
    const spec = responses[path];
    if (!spec) return Promise.reject(new Error('没有桩：' + path));
    if (spec === 'throw') return Promise.reject(new Error('offline'));
    return Promise.resolve({ok: !!spec.ok, status: spec.status});
  }
  const showTokenHint = new Function('document', extractFunction(page, 'showTokenHint') + '\nreturn showTokenHint;')(document);
  const factory = new Function(
    'fetch', 'history', 'location', 'nhUrlToken', 'nhUrlWithoutToken', 'showTokenHint',
    exchangeSource + '\nreturn exchangeUrlToken;'
  );
  const fn = factory(fetchStub, history, location, 'stale', withoutToken, showTokenHint);
  await fn();
  await settle();
  state.hint = hintEl.textContent;
  return state;
}

// ---- B5 公告 Esc ----
check('announce_keydown_binds_document_capture', (failed) => {
  const calls = [];
  const fakeDoc = {
    addEventListener(type, fn, capture) { calls.push(['add', type, capture]); },
    removeEventListener(type, fn, capture) { calls.push(['remove', type, capture]); },
  };
  const factory = new Function('document', 'announcePanelKeydown',
    extractFunction(announce, 'setAnnounceKeydown') + '\nreturn setAnnounceKeydown;');
  const bindFn = factory(fakeDoc, function () {});
  bindFn(true);
  bindFn(false);
  if (calls.length !== 2) failed.push('应一挂一解：' + JSON.stringify(calls));
  if (calls[0][0] !== 'add' || calls[0][1] !== 'keydown' || calls[0][2] !== true) failed.push('打开时应挂 document+capture：' + JSON.stringify(calls[0]));
  if (calls[1][0] !== 'remove' || calls[1][1] !== 'keydown' || calls[1][2] !== true) failed.push('关闭时应解绑同一个监听：' + JSON.stringify(calls[1]));
});

(async () => {
  const okExchange = await runExchange({'/api/session/exchange': {ok: true, status: 200}});
  check('token_exchange_success_strips_and_reloads', (failed) => {
    if (okExchange.hint) failed.push('成功时不该提示：' + okExchange.hint);
    if (okExchange.replaced !== '/?keep=1#frag') failed.push('替换后的地址不对：' + okExchange.replaced);
    if (okExchange.reloads !== 1) failed.push('应 reload 一次：' + okExchange.reloads);
  });
  const cookieFallback = await runExchange({
    '/api/session/exchange': {ok: false, status: 401},
    '/api/overview': {ok: true, status: 200},
  });
  check('bad_token_but_valid_cookie_is_silent', (failed) => {
    if (cookieFallback.hint) failed.push('Cookie 可用时不该报令牌错：' + cookieFallback.hint);
    if (cookieFallback.replaced !== '/?keep=1#frag') failed.push('应抹掉地址栏里的过期令牌：' + cookieFallback.replaced);
    if (cookieFallback.reloads !== 0) failed.push('不该 reload');
  });
  const noCookie = await runExchange({
    '/api/session/exchange': {ok: false, status: 401},
    '/api/overview': {ok: false, status: 401},
  });
  check('bad_token_without_cookie_shows_hint', (failed) => {
    if (!/令牌不正确或已失效/.test(noCookie.hint)) failed.push('提示文案不对：' + noCookie.hint);
    if (noCookie.replaced) failed.push('没权限时不该改地址栏：' + noCookie.replaced);
  });
  const oldBackend = await runExchange({'/api/session/exchange': {ok: false, status: 500}});
  check('exchange_500_falls_back_silently', (failed) => {
    if (oldBackend.hint) failed.push('接口不可用不该冒充令牌错：' + oldBackend.hint);
    if (oldBackend.reloads !== 0) failed.push('不该 reload');
  });
  process.stdout.write(JSON.stringify(report));
})();
"""


class UxFixBehaviourTest(unittest.TestCase):
    """node 跑真实函数：安装进度文案、服务时间、主地址、更新通道、磁贴、令牌交换、公告按键。"""

    report: dict = {}

    @classmethod
    def setUpClass(cls) -> None:
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("本机没有 node，跳过前端行为测试")
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "harness.js"
            script.write_text(HARNESS_JS, encoding="utf-8")
            result = subprocess.run(
                [node, str(script), str(PAGE_HTML), str(SETTINGS_JS), str(GUIDE_JS), str(ANNOUNCE_JS)],
                capture_output=True,
                timeout=60,
            )
        if result.returncode != 0:
            raise AssertionError(
                "harness 崩溃，退出码 %d\nstderr:\n%s"
                % (result.returncode, result.stderr.decode("utf-8", "replace"))
            )
        cls.report = json.loads(result.stdout.decode("utf-8"))
        fatal = cls.report.get("fatal")
        if fatal:
            raise AssertionError(fatal)

    def _check(self, name: str) -> None:
        row = self.report.get(name)
        self.assertIsNotNone(row, "没有拿到场景 %s 的结果" % name)
        self.assertTrue(row.get("pass"), "%s: %s" % (name, row.get("detail")))

    def test_napcat_download_with_total(self) -> None:
        self._check("napcat_download_with_total")

    def test_napcat_download_without_total_does_not_invent_percent(self) -> None:
        self._check("napcat_download_without_total")

    def test_napcat_extract_counts(self) -> None:
        self._check("napcat_extract_counts")

    def test_napcat_verify_and_install(self) -> None:
        self._check("napcat_verify_and_install")

    def test_napcat_complete_and_error(self) -> None:
        self._check("napcat_complete_and_error")

    def test_napcat_megabytes_have_one_decimal(self) -> None:
        self._check("napcat_megabytes_have_one_decimal")

    def test_service_time_formatted(self) -> None:
        self._check("service_time_formatted")

    def test_primary_address_prefers_primary(self) -> None:
        self._check("primary_address_prefers_backend_primary")

    def test_primary_address_fallbacks(self) -> None:
        self._check("primary_address_falls_back_to_public_then_first")

    def test_update_channel_selection(self) -> None:
        self._check("update_channel_selection")

    def test_platform_tile_toggles(self) -> None:
        self._check("platform_tile_toggles_and_dispatches_change")

    def test_token_exchange_success(self) -> None:
        self._check("token_exchange_success_strips_and_reloads")

    def test_bad_token_but_valid_cookie_is_silent(self) -> None:
        self._check("bad_token_but_valid_cookie_is_silent")

    def test_bad_token_without_cookie_shows_hint(self) -> None:
        self._check("bad_token_without_cookie_shows_hint")

    def test_exchange_500_falls_back_silently(self) -> None:
        self._check("exchange_500_falls_back_silently")

    def test_announce_keydown_binds_document_capture(self) -> None:
        self._check("announce_keydown_binds_document_capture")


class UxFixWiringTest(unittest.TestCase):
    """挂载点 / 文案这类只能从源码确认的接线。"""

    def setUp(self) -> None:
        self.page = PAGE_HTML.read_text(encoding="utf-8")
        self.settings = SETTINGS_JS.read_text(encoding="utf-8")
        self.guide = GUIDE_JS.read_text(encoding="utf-8")
        self.announce = ANNOUNCE_JS.read_text(encoding="utf-8")

    def test_update_card_no_placeholder_channel(self) -> None:
        self.assertNotIn("<!--UPDATE_CHANNEL-->", self.settings)
        self.assertIn("selectUpdateChannel(data.channel)", self.settings)
        self.assertIn("selectUpdateChannel(payload.update_channel)", self.settings)

    def test_announce_panel_unbinds_on_close(self) -> None:
        self.assertIn("setAnnounceKeydown(true)", self.announce)
        self.assertIn("setAnnounceKeydown(false)", self.announce)
        self.assertNotIn("panel.addEventListener('keydown'", self.announce)
        self.assertIn("event.key === 'Escape'", self.announce)

    def test_sync_choices_are_collapsed_details(self) -> None:
        self.assertIn("details.className = 'sync-alt-details'", self.settings)
        self.assertIn("'换一个地址（' + others.length + ' 个）'", self.settings)
        self.assertIn(".sync-alt-details{", self.page)
        self.assertIn("renderPublicNote(item)", self.settings)

    def test_theme_defaults_to_light_until_user_picks(self) -> None:
        head = self.page.split("<head>", 1)[1].split("</head>", 1)[0]
        first_script = head.split("<script>", 1)[1].split("</script>", 1)[0]
        # 没选过 → 浅色；不看系统、不跟系统；首屏同时把 theme-color 设对。
        self.assertIn("var d=t==='dark'", first_script)
        self.assertIn("d?'dark':'light'", first_script)
        self.assertIn("m.setAttribute('content',d?'#0b0d12':'#e8ebee')", first_script)
        self.assertNotIn("prefers-color-scheme", first_script)
        self.assertNotIn("跟随系统", self.page)
        self.assertIn("还没选过：默认浅色（只保存在此浏览器）", self.page)
        # 选过之后地址栏底色跟着页面走。
        self.assertIn("themeMeta.setAttribute('content', next === 'dark' ? '#0b0d12' : '#e8ebee')", self.page)
        # 刷新也要一起带上：首屏脚本同步 meta，已选过时提示与点击后一致。
        self.assertIn("m.setAttribute('content',d?'#0b0d12':'#e8ebee')", first_script)
        self.assertIn("'已固定为' + (saved === 'dark' ? '深色' : '浅色') + '（仅保存在此浏览器）'", self.page)

    def test_install_progress_wiring(self) -> None:
        self.assertIn("/api/napcat/install/status", self.guide)
        self.assertIn("installPollTimer = window.setInterval(pollInstallStatus, 1000)", self.guide)
        self.assertIn("if (document.hidden) return;", self.guide)
        self.assertIn("window.nhInstallInProgress = true", self.guide)
        self.assertIn("window.nhInstallInProgress = false", self.guide)
        self.assertIn("if (window.nhInstallInProgress) return;", self.page)
        self.assertIn("if (window.nhInstallInProgress) return;", self.guide)

    def test_copy_fixes(self) -> None:
        self.assertIn('>跳到二维码</button>', self.page)
        self.assertIn("暂无推送记录。等第一次推送成功后", self.page)
        self.assertIn("暂无判定结果。先去「设置」里看看消息来源和推送通道接上没有。", self.page)


if __name__ == "__main__":
    unittest.main()
