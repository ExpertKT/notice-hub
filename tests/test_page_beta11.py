"""beta.11 页面行为的定点断言：QQ 未安装的自救入口、扫码等待阶段、宿主写操作的确认。

这些断言单独放一个文件，是因为它们描述的是**这一版新增的用户可见行为**，
和 `test_webapp.py` 里的大堆既有断言混在一起容易被误删。
"""

from __future__ import annotations

import unittest

from qq_live_digest import webapp as w


PAGE = w.PAGE_HTML


class Beta11PageBehaviourTests(unittest.TestCase):
    def test_qq_missing_guide_gives_a_real_way_out(self) -> None:
        """「未找到电脑版 QQ」不能只丢一段红字：要有官方下载入口 + 重新检测 + 高级用户的路径。"""
        self.assertIn("function showQqMissingGuide(", PAGE)
        self.assertIn("https://im.qq.com/pcqq/index.shtml", PAGE)
        self.assertIn("'qq-redetect'", PAGE)
        self.assertIn("'下载官方 QQ'", PAGE)
        self.assertIn("'重新检测'", PAGE)
        # 根因要说人话：NapCat 只是壳、不含 QQ.exe
        self.assertIn("NapCat 只是一层壳", PAGE)
        # 高级路径写的是真正的 QQ 路径变量，不是 NapCat 目录
        self.assertIn("QQ_DIGEST_QQ_EXE", PAGE)
        self.assertIn("/未找到电脑版 ?QQ|NapCat Shell/", PAGE)
        # 文案走 textContent，不拼 innerHTML（内容来自接口错误）
        self.assertIn("why.textContent=", PAGE)

    def test_qr_waiting_state_uses_stage_and_waited_seconds(self) -> None:
        """扫码等待要能从接口拿 stage 与已等秒数，别再是一句恒定文案。"""
        self.assertIn("notReady.stage=x.stage||''", PAGE)
        self.assertIn("notReady.waitingSeconds=Number(x.waiting_seconds||0)", PAGE)
        self.assertIn("eStage === 'waiting_qr'", PAGE)
        self.assertIn("eStage === 'stopped'", PAGE)
        self.assertIn("扫完在手机上点「确认登录」", PAGE)

    def test_host_write_calls_send_confirm(self) -> None:
        """页面点这两个按钮会真改本机：请求必须带 confirm:true，否则后端会 400。"""
        self.assertIn("body: JSON.stringify({confirm: true})", PAGE)
        self.assertIn("api('/api/tunnel/start'", PAGE)
        self.assertIn("api('/api/firewall/allow'", PAGE)


if __name__ == "__main__":
    unittest.main()
