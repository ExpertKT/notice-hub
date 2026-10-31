"""beta.11 页面行为的定点断言：QQ 未安装的自救入口、扫码等待阶段、宿主写操作的确认。

这些断言单独放一个文件，是因为它们描述的是**这一版新增的用户可见行为**，
和 `test_webapp.py` 里的大堆既有断言混在一起容易被误删。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from noticehub import webapp as w


PAGE = w.PAGE_HTML

# page.html + js/*.js。页面拆块后，涉及已搬走代码的断言必须读合并源，
# 否则「搬家」本身就会让断言红（test_webapp.py 里早就是这么做的）。
_STATIC_DIR = Path(w.__file__).parent / "web_static"
PAGE_SOURCE = PAGE + "".join(
    p.read_text(encoding="utf-8").replace("\r\n", "\n")
    for p in sorted((_STATIC_DIR / "js").glob("*.js"))
)


class Beta11PageBehaviourTests(unittest.TestCase):
    def test_qq_missing_guide_gives_a_real_way_out(self) -> None:
        """「未找到电脑版 QQ」不能只丢一段红字：要有官方下载入口 + 重新检测 + 高级用户的路径。"""
        self.assertIn("function showQqMissingGuide(", PAGE_SOURCE)
        self.assertIn("https://im.qq.com/pcqq/index.shtml", PAGE_SOURCE)
        self.assertIn("'qq-redetect'", PAGE_SOURCE)
        self.assertIn("'下载官方 QQ'", PAGE_SOURCE)
        self.assertIn("'重新检测'", PAGE_SOURCE)
        # 根因要说人话：NapCat 不含 QQ.exe，要靠官方 QQ 才能跑
        self.assertIn("NapCat 不含 QQ.exe，要靠官方 QQ 才能跑", PAGE_SOURCE)
        # 高级路径写的是真正的 QQ 路径变量，不是 NapCat 目录
        self.assertIn("QQ_DIGEST_QQ_EXE", PAGE_SOURCE)
        # 触发点跟着主按钮走：一键接入拿到 qq_missing 就画出这张卡（旧的「下载并安装」按钮已收编）。
        self.assertIn("eStage === 'qq_missing' ? (showQqMissingGuide()", PAGE_SOURCE)
        # 文案走 textContent，不拼 innerHTML（内容来自接口错误）
        self.assertIn("why.textContent=", PAGE_SOURCE)

    def test_qr_waiting_state_uses_stage_and_waited_seconds(self) -> None:
        """扫码等待要能从接口拿 stage 与已等秒数，别再是一句恒定文案。"""
        self.assertIn("notReady.stage=x.stage||''", PAGE_SOURCE)
        self.assertIn("notReady.waitingSeconds=Number(x.waiting_seconds||0)", PAGE_SOURCE)
        self.assertIn("eStage === 'waiting_qr'", PAGE_SOURCE)
        self.assertIn("eStage === 'stopped'", PAGE_SOURCE)
        self.assertIn("扫完在手机上点「确认登录」", PAGE_SOURCE)

    def test_cx_step_helper_is_defined_before_use(self) -> None:
        """学习通向导三步徽标：setCxStep 必须真有定义且在首次调用之前，否则点了只抛 ReferenceError。"""
        defined = re.search(r"function\s+setCxStep\s*\(", PAGE_SOURCE)
        self.assertIsNotNone(defined, "页面调用了 setCxStep 却没有 function 定义")
        calls = [m.start() for m in re.finditer(r"\bsetCxStep\s*\(", PAGE_SOURCE)]
        self.assertTrue(calls, "至少应有一处 setCxStep 调用")
        self.assertLess(defined.start(), calls[0], "setCxStep 的定义要排在第一次调用之前")

    def test_cx_auto_cookie_resets_button_when_it_fails(self) -> None:
        """点「重试自动获取」失败也要解灰：复位要同时挂成功与失败，不能只挂一个 .then。"""
        start = PAGE_SOURCE.find("'cx-auto-cred').onclick")
        self.assertGreater(start, 0, "找不到「重试自动获取」的点击处理")
        seg = PAGE_SOURCE[start : start + 2400]
        self.assertIn("button.disabled=false", seg)
        self.assertTrue(
            ".then(done,done)" in seg or ".finally(" in seg,
            "解灰必须覆盖失败路径（只挂 .then 会让按钮在请求出错后永久灰）",
        )
        self.assertIn("再试一次", seg, "失败要给用户可见的提示，别静默")

    def test_run_info_reads_overview_instead_of_two_status_calls(self) -> None:
        """运行信息里的微信 / 学习通统一走 /api/overview，不再自己拼两个接口。"""
        self.assertIn("api('/api/overview'", PAGE_SOURCE)
        self.assertNotIn("Promise.all([api('/api/wechat/status')", PAGE_SOURCE)
        start = PAGE_SOURCE.find("['微信', '读取中…']")
        self.assertGreater(start, 0, "找不到运行信息里的微信行")
        end = PAGE_SOURCE.find("var insights = data.insights", start)
        seg = PAGE_SOURCE[start:end]
        self.assertIn("api('/api/overview'", seg)
        self.assertNotIn("/api/wechat/status", seg)
        self.assertNotIn("/api/chaoxing/status", seg)

    def test_announce_block_moved_to_its_own_script(self) -> None:
        """公告块已拆进 js/announce.js：页面只留 <script src>，块里的符号必须真在新文件里，否则铃铛一加载就 ReferenceError。"""
        self.assertIn('<script src="js/announce.js"></script>', PAGE)
        self.assertNotIn("function refreshAnnouncements(", PAGE, "公告逻辑应已搬出 page.html")
        script = Path(w.__file__).parent / "web_static" / "js" / "announce.js"
        self.assertTrue(script.is_file(), "js/announce.js 必须存在，否则 <script src> 404、整段页面脚本停摆")
        text = script.read_text(encoding="utf-8")
        for symbol in (
            "ANNOUNCE_READ_KEY",
            "function refreshAnnouncements(",
            "function setupAnnounceBell(",
            "function setupAnnounceModal(",
            "function announcePanelKeydown(",
        ):
            self.assertIn(symbol, text)

    def test_package_spec_ships_the_split_scripts(self) -> None:
        """打包 spec 必须带上 web_static/js：只带 page.html 的话，装出来的 exe 里
        <script src="js/*.js"> 全是 404，公告和配对整块静默失效（页面不报错，只是不动）。"""
        spec = Path(__file__).resolve().parents[1] / "packaging" / "noticehub.spec"
        text = spec.read_text(encoding="utf-8")
        self.assertIn('"web_static", "js"', text)

    def test_host_write_calls_send_confirm(self) -> None:
        """页面点这两个按钮会真改本机：请求必须带 confirm:true，否则后端会 400。

        这两段已搬进 js/pair.js，所以读合并源。
        """
        self.assertIn("body: JSON.stringify({confirm: true})", PAGE_SOURCE)
        self.assertIn("api('/api/tunnel/start'", PAGE_SOURCE)
        self.assertIn("api('/api/firewall/allow'", PAGE_SOURCE)


class SetupGuidanceTests(unittest.TestCase):
    """四个用户卡点的引导：状态不能只报一个名词，得告诉人下一步点哪。"""

    def test_dingtalk_reconnect_button_shows_only_when_listener_is_down(self) -> None:
        self.assertIn('id="dt-reconnect"', PAGE_SOURCE)
        self.assertIn("重连监听", PAGE_SOURCE)
        # 只有「已启用、监听没在跑、已经勾了群」才给重连；没勾群时该做的是去勾群。
        self.assertIn("reconnect.hidden=!(v.enabled&&!v.running&&Number(v.groups||0)>0)", PAGE_SOURCE)
        # 重连复用「保存设置」那条链路，不新造接口。
        self.assertIn("document.getElementById('dt-reconnect').onclick=saveDingtalkSettings", PAGE_SOURCE)

    def test_wechat_failure_shows_human_hint_and_retry(self) -> None:
        self.assertIn("var hasError=!!(failed||v.last_error)", PAGE_SOURCE)  # 扫描失败或拉群失败都算错
        self.assertIn("if(v.error_hint&&hasError)parts.push(v.error_hint)", PAGE_SOURCE)  # 人话原因进状态行
        self.assertIn("node.title='原始报错：'+v.last_error", PAGE_SOURCE)  # 原始报错收进 title
        self.assertIn('id="wx-scan-retry"', PAGE_SOURCE)
        self.assertIn("retry.hidden=!(failed||(v.error_hint&&hasError))", PAGE_SOURCE)  # 只在有错时显示
        self.assertIn("document.getElementById('wx-scan-retry').onclick=scanWechat", PAGE_SOURCE)
        # 扫描错误行要人话优先（error_hint），人话缺了依次退 last_error/scan_stage，详情追加。
        self.assertIn(
            "else if(v.scan_state==='error'){var scanMsg=v.error_hint||v.last_error||v.scan_stage||'未知错误';if(v.detail)scanMsg+='（'+v.detail+'）';target.textContent='扫描失败：'+scanMsg;}",
            PAGE_SOURCE,
        )
        # issue #5：手填密钥的入口要写在页面上，别只活在文档里。
        self.assertIn("QQ_DIGEST_WECHAT_KEY", PAGE_SOURCE)
        self.assertIn("正在用你手填的密钥", PAGE_SOURCE)

    def test_wechat_storage_hint_and_dir_setting_are_reachable(self) -> None:
        """找不到数据目录的引导必须自己显示出来；页面里也要能直接填目录，不用改 .env 再重启。"""
        self.assertIn('id="wx-storage-hint"', PAGE_SOURCE)
        self.assertIn("var hint=String(v.hint||'').trim();hintEl.hidden=!hint;hintEl.textContent=hint;", PAGE_SOURCE)
        self.assertIn("node.dataset.state=hasError||v.available===false?'error'", PAGE_SOURCE)
        self.assertIn("if(v.storage_dir)parts.push('数据目录：'+v.storage_dir)", PAGE_SOURCE)
        self.assertIn('id="wx-dir-input"', PAGE_SOURCE)
        self.assertIn('id="wx-dir-save"', PAGE_SOURCE)
        self.assertIn("body:JSON.stringify({wechat_dir:value})", PAGE_SOURCE)
        # 异步装载只在用户没手改过目录输入时才回填（keep 标志由 harvestSettingsInputs 维护）。
        self.assertIn("if(dirInput&&!keep['wx-dir-input'])dirInput.value=typeof v.wechat_dir==='string'?v.wechat_dir:''", PAGE_SOURCE)
        self.assertIn("已保存，立即生效。", PAGE_SOURCE)
        self.assertIn("保存后立即生效，不用重启群务台", PAGE_SOURCE)
        # 密钥改成保存即生效（settings_api 会重建 reader），不能再说「重启后生效」。
        self.assertNotIn("重启群务台后生效", PAGE_SOURCE)
        self.assertIn("已保存并生效（不回显内容）。", PAGE_SOURCE)

    def test_wechat_scan_and_empty_groups_explain_why(self) -> None:
        """已在扫描/没启用/没勾群/扫描器没起来要分清楚；拉到 0 个群要把后端给的原因带出来。"""
        self.assertIn("if(v.started===false){if(v.already_running){box.textContent=v.message", PAGE_SOURCE)
        # ② 扫描反馈保护期：两条提示分支都先开 30s hold，避免轮询立刻盖掉提示；详情并入提示。
        self.assertIn(
            "else if(v.ok){wxScanHoldUntil=Date.now()+30000;box.textContent='未开始扫描：'+(v.error||'先勾选「我的群」并保存，或先启用微信')",
            PAGE_SOURCE,
        )
        self.assertIn(
            "else{wxScanHoldUntil=Date.now()+30000;var why=String(v.error||'未知原因');if(v.detail)why+='（'+v.detail+'）';box.textContent='扫描启动失败：'+why;",
            PAGE_SOURCE,
        )
        self.assertIn("box.textContent='正在扫描…';updateScanStatus('wechat')", PAGE_SOURCE)
        self.assertIn("扫描启动失败：", PAGE_SOURCE)
        self.assertIn("var reason=String(payload.error_hint||payload.error||'').trim();", PAGE_SOURCE)
        self.assertIn("'没有解析到群：'+reason", PAGE_SOURCE)
        self.assertIn("（确认微信已登录，再点「拉取我的群」重试）", PAGE_SOURCE)
        # 拉取失败也要刷状态行，否则「未找到数据目录」的引导不会出现。
        self.assertIn("if (config.after) config.after();", PAGE_SOURCE)

    def test_calendar_prefers_public_address_and_offers_tunnel(self) -> None:
        self.assertIn("function primaryAddress(calendars)", PAGE_SOURCE)
        self.assertIn("item.kind === 'public'", PAGE_SOURCE)  # 不靠后端返回顺序
        self.assertIn("公网地址 · 手机流量可用", PAGE_SOURCE)
        self.assertIn("手机切到流量就收不到更新", PAGE_SOURCE)  # 只有局域网时说清后果
        self.assertIn('id="sync-public-open"', PAGE_SOURCE)
        self.assertIn("api('/api/tunnel/start'", PAGE_SOURCE)  # 复用既有开通链路
        self.assertIn("body: JSON.stringify({confirm: true})", PAGE_SOURCE)

    def test_history_group_ticks_survive_rerender(self) -> None:
        """切回回溯标签会重拉群列表并重绘：群集合没变时要沿用用户勾选，不能冲回默认。"""
        self.assertIn("var kept = selectedHistoryGroups();", PAGE_SOURCE)
        self.assertIn(
            "var restore = prevIds === nextIds && root.querySelector('input[type=\"checkbox\"]') !== null;",
            PAGE_SOURCE,
        )
        self.assertIn(
            "input.checked = restore ? kept.indexOf(String(group.group_id)) !== -1 : group.selected === true;",
            PAGE_SOURCE,
        )

    def test_push_channels_use_service_names_not_internal_ids(self) -> None:
        for pair in (
            "wxpusher:'WxPusher'",
            "serverchan:'Server酱'",
            "pushplus:'PushPlus'",
            "webhook:'Webhook'",
            "'qq-bot':'QQ 机器人'",
        ):
            self.assertIn(pair, PAGE_SOURCE)
        self.assertIn("function pushChannelName(id)", PAGE_SOURCE)
        self.assertIn("还没设置 · 去设置里加一个推送通道", PAGE_SOURCE)


_RACE_HARNESS_JS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf-8');

function extract(name) {
  const marker = 'function ' + name + '(';
  const start = src.indexOf(marker);
  if (start < 0) throw new Error('找不到函数：' + name);
  const end = src.indexOf('\n}', start);
  if (end < 0) throw new Error('函数没有行首收尾：' + name);
  return src.slice(start, end + 2);
}

const factory = new Function(extract('loadRulesKeepingEdits') + '\n'
  + extract('harvestGroupEdits') + '\n'
  + 'return {keep: loadRulesKeepingEdits, harvest: harvestGroupEdits};');
const mod = factory();
const report = {};

function makeDeps(serverRules) {
  const list = {
    children: [],
    appendChild(node) { this.children.push(node); },
    insertBefore(node, ref) {
      const index = ref ? this.children.indexOf(ref) : -1;
      if (index < 0) this.children.push(node); else this.children.splice(index, 0, node);
    },
  };
  let resolveRequest = null;
  const deps = {
    list,
    request: () => new Promise((resolve) => { resolveRequest = resolve; }),
    toCards: (rules) => rules.map((rule) => ({ id: rule.id })),
    render: (cards) => { list.children = cards.map((card) => ({ dataset: {}, __id: card.id })); },
    refreshEmpty: () => {},
    readId: (node) => node.__id,
    resolveWith: (payload) => resolveRequest(payload || serverRules || { rules: [] }),
  };
  return deps;
}

function idsOf(list) {
  return list.children.map((node) => node.__id);
}

// ① 复现用户报的 bug：请求发出后用户点了「新增」，响应才回来 → 新增行必须还在。
const d1 = makeDeps();
const p1 = mod.keep(d1);
const added = { dataset: { dirty: '1' }, __id: '' };
d1.list.children.push(added);          // 响应到达前用户加的空卡
d1.resolveWith({ rules: [{ id: '规则A' }, { id: '规则B' }] });
report.addedRowSurvives = p1.then((count) => ({
  count,
  ids: idsOf(d1.list),
  keptIdentity: d1.list.children.indexOf(added) >= 0,
  stillDirty: added.dataset.dirty === '1',
}));

// ② 已存在且被改过的同 id 行：以用户那行为准，服务端同名行让位，不能出现两张。
const d2 = makeDeps();
const edited = { dataset: { dirty: '1' }, __id: '规则A' };
d2.list.children.push(edited);
const p2 = mod.keep(d2);                 // 先发请求（响应到达时才采集现场）
d2.resolveWith({ rules: [{ id: '规则A' }, { id: '规则B' }] });
report.editedRowWins = p2.then((count) => ({
  count,
  ids: idsOf(d2.list),
  keptIdentity: d2.list.children.indexOf(edited) >= 0,
}));

// ③ 用户没动过：正常整表换成服务端结果（老行为不变）。
const d3 = makeDeps();
const p3 = mod.keep(d3);
d3.resolveWith({ rules: [{ id: '规则C' }] });
report.untouchedReplaces = p3.then((count) => ({ count, ids: idsOf(d3.list) }));

// ④ 群白名单收割：未保存的勾选与别名要进本地集合；空列表不动本地状态。
const boxes = [{ checked: true, value: 'g1' }, { checked: false, value: 'g2' }];
const aliasInputs = [{ getAttribute: () => 'g1', value: '班委通知' }];
const fullBox = { queryAll: (sel) => (sel.indexOf('checkbox') >= 0 ? boxes : aliasInputs) };
report.harvest = mod.harvest('wechat', { querySelectorAll: fullBox.queryAll }, ['g3'], { g3: 'x' });
report.harvestEmptyGuard = mod.harvest('wechat', { querySelectorAll: () => [] }, ['g2'], { g2: 'y' });

// ⑤ 改过名字的卡：刷新时按「原来的 dataset.ruleId」认人，服务端旧名字那张不能变成幽灵卡。
const d5 = makeDeps();
const renamed = { dataset: { dirty: '1', ruleId: '规则A' }, __id: '规则B' };
d5.list.children.push(renamed);
const p5 = mod.keep(d5);
d5.resolveWith({ rules: [{ id: '规则A' }, { id: '规则C' }] });
report.renamedNoGhost = p5.then((count) => ({
  count,
  ids: idsOf(d5.list),
  keptIdentity: d5.list.children.indexOf(renamed) >= 0,
}));

Promise.all([
  report.addedRowSurvives, report.editedRowWins, report.untouchedReplaces, report.renamedNoGhost,
]).then((values) => {
  report.addedRowSurvives = values[0];
  report.editedRowWins = values[1];
  report.untouchedReplaces = values[2];
  report.renamedNoGhost = values[3];
  process.stdout.write(JSON.stringify(report));
}).catch((error) => {
  process.stdout.write(JSON.stringify({ fatal: String((error && error.stack) || error) }));
  process.exitCode = 2;
});
"""


class SettingsLoadRaceTests(unittest.TestCase):
    """装载响应晚于用户编辑到达时，未保存的行/勾选不能被冲掉（node 跑真实函数，断言结果不看实现）。"""

    @classmethod
    def setUpClass(cls) -> None:
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("本机没有 node，跳过装载竞态测试")
        cls.settings = (_STATIC_DIR / "js" / "settings.js").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "race_harness.js"
            script.write_text(_RACE_HARNESS_JS, encoding="utf-8")
            result = subprocess.run(
                [node, str(script), str(_STATIC_DIR / "js" / "settings.js")],
                capture_output=True, timeout=60,
            )
        if result.returncode != 0:
            raise AssertionError(
                "harness 崩溃，退出码 %d\nstderr:\n%s"
                % (result.returncode, result.stderr.decode("utf-8", "replace"))
            )
        cls.report = json.loads(result.stdout.decode("utf-8"))

    def test_new_row_added_before_response_is_kept(self) -> None:
        row = self.report.get("addedRowSurvives") or {}
        self.assertEqual(row.get("ids"), ["规则A", "规则B", ""], row)
        self.assertTrue(row.get("keptIdentity"), "新增行对象必须是同一个（原样接回）")
        self.assertTrue(row.get("stillDirty"))
        self.assertEqual(row.get("count"), 2, "只清点服务端那两条")

    def test_edited_row_of_same_id_wins_without_duplicate(self) -> None:
        row = self.report.get("editedRowWins") or {}
        self.assertEqual(row.get("ids"), ["规则A", "规则B"], row)
        self.assertTrue(row.get("keptIdentity"), "被改过的行应保留在原位")
        self.assertEqual(row.get("count"), 1)

    def test_untouched_list_still_replaced_by_server_rows(self) -> None:
        row = self.report.get("untouchedReplaces") or {}
        self.assertEqual(row.get("ids"), ["规则C"], row)

    def test_group_edits_are_harvested_and_empty_box_is_untouched(self) -> None:
        harvested = self.report.get("harvest") or {}
        self.assertEqual(harvested.get("selected"), ["g1"], harvested)
        self.assertEqual(harvested.get("aliases"), {"g3": "x", "g1": "班委通知"}, harvested)
        guard = self.report.get("harvestEmptyGuard") or {}
        self.assertEqual(guard.get("selected"), ["g2"], "列表被报错文案占住时不动本地状态")
        self.assertEqual(guard.get("aliases"), {"g2": "y"})

    def test_renamed_card_does_not_leave_ghost_row(self) -> None:
        row = self.report.get("renamedNoGhost") or {}
        self.assertEqual(row.get("ids"), ["规则B", "规则C"], row)
        self.assertTrue(row.get("keptIdentity"))
        self.assertEqual(row.get("count"), 1, "旧名字那张服务端卡要被挡住")

    def test_wiring_in_settings_source(self) -> None:
        self.assertIn("loadRulesKeepingEdits({", self.settings)
        self.assertIn("harvestGroupEdits('wechat',box,wxSelected,wxAliases)", self.settings)
        self.assertIn("harvestGroupEdits('dingtalk',box,dtSelected,null)", self.settings)
        # 前缀填充不能反过来清掉用户正往里粘的内容
        self.assertIn("if(document.activeElement!==keyInput)keyInput.value='';", self.settings)
        # 日历订阅地址：用户手改过的地址不能被静默刷新覆盖（要换回自动地址得点「换一个地址」）
        self.assertIn("input.dataset.edited = '1';", self.settings)
        self.assertIn("if (input.dataset.edited !== '1') {", self.settings)


if __name__ == "__main__":
    unittest.main()
