"""09 微信目录有效：拉群能出群行（群名与替身 fixture 一致）、状态不是错误态、扫描能真跑。

为什么：这条覆盖「填对了目录，一切正常」的基线。它红了，后面 10/11 的判定都不可信。

替身：tags=("fakes:wechat",)，run.py 把 `QQ_DIGEST_WECHAT_DIR` / `_WECHAT_KEY` 写进冷装 .env。
替身默认 fixture：群 id `1234@chatroom`、群名「学院通知群」（见 fakes/wechat_store.py）。

复现：
    python tools/e2e/run.py --only 09_settings_wechat_dir_valid
"""

import time

from harness import scenario

GROUP_ID = "1234@chatroom"
GROUP_NAME = "学院通知群"


def _dismiss_wizard(app) -> None:
    if app.count("#platform-setup-wizard") and app.page.evaluate(
        "(function(){var w=document.getElementById('platform-setup-wizard');return !!(w&&!w.hidden);})()"
    ):
        app.click("#platform-setup-wizard .platform-setup-later")
        app.wait_for("#platform-setup-wizard", state="hidden", timeout=10)


def _wait_js(app, expression: str, desc: str, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if app.page.evaluate(expression):
            return
        time.sleep(0.1)
    raise AssertionError(f"等待{desc}超时（{timeout}s）")


def _open_wechat(app) -> None:
    app.goto("/")
    _dismiss_wizard(app)
    app.click("#tab-settings-button")
    app.wait_for("#tab-settings", state="visible", timeout=10)
    app.wait_for("#wechat-settings-card", state="visible", timeout=15)
    app.wait_for("#wx-load-groups", state="visible", timeout=10)


_ENGLISH_ERRORS_JS = (
    "(function(){var t=document.body.innerText||'';"
    "return ['Failed to fetch','Traceback','NetworkError','undefined']"
    ".filter(function(word){return t.indexOf(word)>=0;}).join(',');})()"
)


def _assert_no_english_error(app, timeout: float = 8.0) -> None:
    """最终态**用户看得见的文字**里不许出现原始英文/内部报错。

    注意：必须读 `body.innerText`，不能读 textContent——页面内联 <script> 的源码里
    本来就有 'undefined'/'NetworkError' 这些词，用 textContent 会把源码当界面文字，
    这条断言就变成「永远红」或「与用户看到的内容无关」。
    启用微信会重启读取线程，期间状态轮询可能瞬时断连并在界面上露出 fetch 原文，
    所以给这段时间收尾的机会；一直挂着才判失败。
    """
    seen: list[str] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hit = str(app.page.evaluate(_ENGLISH_ERRORS_JS) or "")
        if not hit:
            return
        for needle in hit.split(","):
            if needle and needle not in seen:
                seen.append(needle)
        app.settle(0.5)
    where = app.page.evaluate(
        "(function(){var words=['Failed to fetch','NetworkError','Traceback','undefined'];var out=[];"
        "var all=document.querySelectorAll('body *');"
        "for(var i=0;i<all.length;i++){var el=all[i];var tag=String(el.tagName||'').toLowerCase();"
        "if(tag==='script'||tag==='style')continue;"
        "var t=(el.innerText||'');"
        "for(var w=0;w<words.length;w++){if(t.indexOf(words[w])>=0){out.push((el.id?'#'+el.id:tag)"
        "+'.'+String(el.className||'').split(' ')[0]+' :: '+t.trim().slice(0,80));break;}}"
        "if(out.length>4)break;}return out;})()"
    )
    raise AssertionError(f"页面上一直挂着英文/内部报错：{seen}；位置：{where}")


@scenario("09_settings_wechat_dir_valid", viewports=("desktop",), tags=("fakes:wechat",))
def run(app):
    _open_wechat(app)

    app.click("#wx-load-groups")

    # 先等接口真的给出群，再等 DOM 渲染。顺序反过来的话，替身拉群慢时就会在「等 DOM」
    # 里空转 90 秒，报出来的错也分不清是「接口没回来」还是「页面没画」。
    api_deadline = time.monotonic() + 120
    listed: dict = {}
    while time.monotonic() < api_deadline:
        listed = app.api_get("/api/wechat/groups") or {}
        if listed.get("ok") is True and (listed.get("groups") or []):
            break
        time.sleep(1.0)
    else:
        raise AssertionError(f"/api/wechat/groups 120s 内没给出群：{str(listed)[:300]}")
    api_groups = [item for item in (listed.get("groups") or []) if isinstance(item, dict)]

    _wait_js(app, "!!document.querySelector('#wx-groups .wx-group-row')",
             "接口已给出群但页面没画出群行", timeout=30)
    rows = app.text("#wx-groups")
    assert GROUP_NAME in rows, f"群行里没有 fixture 的群名「{GROUP_NAME}」；实际：{rows!r}"
    assert GROUP_ID in rows, f"群行里没有 fixture 的群 id「{GROUP_ID}」；实际：{rows!r}"
    app.shot("groups")

    # 与接口回读对账：页面画出来的群必须就是刚拿到的那一批。
    # 只断言「文案里出现了某个群名」会被残留 DOM／硬编码的固定文案假绿。
    row_count = app.count("#wx-groups .wx-group-row")
    assert row_count == len(api_groups), (
        f"页面群行 {row_count} 条，接口给了 {len(api_groups)} 个群：{[g.get('name') for g in api_groups]}"
    )
    for group in api_groups:
        name = str(group.get("name") or "").strip()
        gid = str(group.get("id") or "").strip()
        if name:
            assert name in rows, f"接口里的群「{name}」没画到页面上：{rows[:300]!r}"
        assert gid in rows, f"接口里的群号「{gid}」没画到页面上：{rows[:300]!r}"

    status = app.page.evaluate(
        "(function(){var n=document.getElementById('wx-status');return n?{state:n.dataset.state||'',text:n.textContent||''}:null;})()"
    )
    assert status is not None, "设置页没有 #wx-status"
    assert status["state"] != "error", f"有效目录却显示错误态：{status}"
    # 正向对账：有效目录时接口必须报 available=true（只断言「不是 error」在冷装默认态下也会绿）。
    snapshot = app.api_get("/api/wechat/status") or {}
    assert snapshot.get("available") is True, f"有效目录但接口 available 不是 true：{snapshot}"

    # 扫描：按钮要有反馈，接口里 scan_state 必须走到 running/done，不能是 error。
    # 「立即扫描一次」要同时满足两个条件：微信已启用（wechat_enabled）+ 至少选了一个群
    # （wechat_group_whitelist 非空）；缺少任一条后端只会回 scan_state=disabled，
    # 按钮点了也没反应。所以先按真实用户的做法勾启用、勾建议群、点保存，再回读接口确认。
    if not app.page.evaluate("!!(document.getElementById('wx-enabled')||{}).checked"):
        app.click("#wx-enabled")
    app.click("#wx-select-suggested")
    app.click("#wx-save")

    ready_deadline = time.monotonic() + 20
    settings_now: dict = {}
    while time.monotonic() < ready_deadline:
        settings_now = app.api_get("/api/settings") or {}
        whitelist = settings_now.get("wechat_group_whitelist") or []
        if settings_now.get("wechat_enabled") is True and whitelist:
            break
        time.sleep(0.2)
    else:
        raise AssertionError(
            "勾选「启用微信」+ 选群 + 保存后接口没就绪：wechat_enabled=%r whitelist=%r"
            % (settings_now.get("wechat_enabled"), settings_now.get("wechat_group_whitelist"))
        )

    app.click("#wx-scan-now")
    _wait_js(app, "String((document.getElementById('wx-scan')||{}).textContent||'').trim().length>0",
             "扫描按钮给了反馈", timeout=20)

    deadline = time.monotonic() + 90
    seen: list[str] = []
    while time.monotonic() < deadline:
        snapshot = app.api_get("/api/wechat/status") or {}
        state = str(snapshot.get("scan_state") or "")
        if state and (not seen or seen[-1] != state):
            seen.append(state)
        if state in ("running", "done"):
            break
        if state == "error":
            raise AssertionError(f"有效目录扫描却是 error：{snapshot}")
        time.sleep(0.5)
    assert "running" in seen or "done" in seen, f"扫描没进 running/done，只见：{seen}"
    app.shot("scan")

    _assert_no_english_error(app)
    app.assert_no_console_errors()
