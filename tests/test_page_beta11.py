"""beta.11 页面行为的定点断言：QQ 未安装的自救入口、扫码等待阶段、宿主写操作的确认。

这些断言单独放一个文件，是因为它们描述的是**这一版新增的用户可见行为**，
和 `test_webapp.py` 里的大堆既有断言混在一起容易被误删。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from qq_live_digest import webapp as w


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
        # 根因要说人话：NapCat 只是壳、不含 QQ.exe
        self.assertIn("NapCat 只是一层壳", PAGE_SOURCE)
        # 高级路径写的是真正的 QQ 路径变量，不是 NapCat 目录
        self.assertIn("QQ_DIGEST_QQ_EXE", PAGE_SOURCE)
        self.assertIn("/未找到电脑版 ?QQ|NapCat Shell/", PAGE_SOURCE)
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
        start = PAGE_SOURCE.find("'cx-auto-cookie').onclick")
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
        spec = Path(__file__).resolve().parents[1] / "packaging" / "qq_live_digest.spec"
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
        self.assertIn("else if(v.scan_state==='error')target.textContent='扫描失败：'+(v.error_hint", PAGE_SOURCE)
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
        self.assertIn("if(dirInput)dirInput.value=typeof v.wechat_dir==='string'?v.wechat_dir:''", PAGE_SOURCE)
        self.assertIn("已保存，立即生效。", PAGE_SOURCE)
        self.assertIn("保存后立即生效，不用重启群务台", PAGE_SOURCE)
        # 密钥改成保存即生效（settings_api 会重建 reader），不能再说「重启后生效」。
        self.assertNotIn("重启群务台后生效", PAGE_SOURCE)
        self.assertIn("已保存并生效（不回显内容）。", PAGE_SOURCE)

    def test_wechat_scan_and_empty_groups_explain_why(self) -> None:
        """已在扫描/没启用/没勾群/扫描器没起来要分清楚；拉到 0 个群要把后端给的原因带出来。"""
        self.assertIn("if(v.started===false){if(v.already_running){box.textContent=v.message", PAGE_SOURCE)
        self.assertIn("else if(v.ok){box.textContent='未开始扫描：'+(v.error||'先勾选「我的群」并保存，或先启用微信')", PAGE_SOURCE)
        self.assertIn("else{box.textContent='扫描启动失败：'+(v.error||'未知原因')", PAGE_SOURCE)
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
        self.assertIn("未配置 · 去设置里添加推送通道", PAGE_SOURCE)


if __name__ == "__main__":
    unittest.main()
