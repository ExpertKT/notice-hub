"""noticehub 测试包。

Keep test discovery hermetic: config.py reads NOTICEHUB_ENV / QQ_DIGEST_ENV at import time,
so point it at an isolated file and disable provider settings before any test
module imports noticehub.config. This does not clear unrelated process
variables such as PATH, TEMP, or USERPROFILE.
"""

import atexit
import os
import shutil
import tempfile
import time
from pathlib import Path

_TEST_ENV_DIR = Path(tempfile.mkdtemp(prefix="noticehub-tests-"))
# 测试结束删掉隔离用的 .env 目录，别留在 TEMP（或仓库根）里。
atexit.register(shutil.rmtree, _TEST_ENV_DIR, ignore_errors=True)
# 系统 TEMP 不可写时 tempfile 会回退到当前目录（即仓库根），把全部测试临时目录
# 收进这一个父目录，退出时一并删除，新测试也不会再漏到仓库根。
_TEST_TMP_DIR = _TEST_ENV_DIR / "tmp"
_TEST_TMP_DIR.mkdir()
tempfile.tempdir = str(_TEST_TMP_DIR)
os.environ["QQ_DIGEST_ENV"] = str(_TEST_ENV_DIR / "test.env")
for _key in (
    "DASHSCOPE_API_KEY",
    "LLM",
    "LLM_BACKEND",
    "LLM_ENDPOINT",
    "LLM_MODEL",
    "LLM_TIMEOUT",
    "CODEBUDDY_CLI",
):
    # 新旧两套名字都要清：NOTICEHUB_* 优先，QQ_DIGEST_* 兜底。
    os.environ.pop("NOTICEHUB_" + _key, None)
    os.environ.pop("QQ_DIGEST_" + _key, None)

_ENV_PREFIXES = ("NOTICEHUB_", "QQ_DIGEST_")


def _restore_env(key: str, previous: "str | None") -> None:
    if previous is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = previous


def clear_env_prefixes(case, *suffixes: str) -> None:
    """把 NOTICEHUB_<s> 与 QQ_DIGEST_<s> 两套名字都摘干净，用例结束后还原。

    环境变量改名后新名优先：只清旧名的话，前面用例灌进进程的新名会把本用例的
    mock.patch.dict 压掉（表现为「读到了别人的 key」）。跨用例读环境变量的夹具都该调它。
    还原是**无条件**的：用例自己写进去的新值也要被清掉。
    """
    for suffix in suffixes:
        for prefix in _ENV_PREFIXES:
            key = prefix + suffix
            previous = os.environ.pop(key, None)
            case.addCleanup(_restore_env, key, previous)


def snapshot_env_prefixes(case) -> None:
    """快照进程里所有 NOTICEHUB_* / QQ_DIGEST_*，用例结束时精确还原。

    会热写设置的用例（POST /api/settings 之类）用它兜底：它们往里写的
    ``os.environ[...]`` 不会自己退，会串到后面读环境变量的用例里。
    """
    snapshot = {key: value for key, value in os.environ.items() if key.startswith(_ENV_PREFIXES)}

    def restore() -> None:
        for key in [key for key in os.environ if key.startswith(_ENV_PREFIXES)]:
            os.environ.pop(key, None)
        os.environ.update(snapshot)

    case.addCleanup(restore)


# Settings() is intentionally convenient for application code, but its
# dataclass defaults leave LLM enabled. Make implicit test fixtures offline;
# tests that exercise LLM behavior pass an explicit backend/key/flag.
from noticehub.config import Settings as _Settings  # noqa: E402
_original_settings_init = _Settings.__init__


def _offline_settings_init(self, *args, **kwargs):
    explicit_backend = "llm_backend" in kwargs
    explicit_key = "dashscope_api_key" in kwargs
    if not any(key in kwargs for key in (
        "llm_enabled", "dashscope_api_key", "llm_backend", "dashscope_endpoint", "codebuddy_cli"
    )):
        kwargs["llm_enabled"] = False
    elif explicit_key and not explicit_backend:
        # Legacy tests that provide a DashScope endpoint intend the HTTP path;
        # make that intent explicit instead of trying the CodeBuddy CLI first.
        kwargs["llm_backend"] = "ollama"
        kwargs.setdefault("llm_defer_max_attempts", 0)
    _original_settings_init(self, *args, **kwargs)


_Settings.__init__ = _offline_settings_init


# —— 浏览器测试共用：Playwright 的 Node 驱动不认 Python 的 tempfile ——
def isolate_browser_temp_env() -> dict:
    """把 Playwright 驱动看得到的 TEMP/TMP/TMPDIR 指到隔离临时目录，返回原值。

    Playwright 起 Node 驱动时用 `os.tmpdir()` 建 `playwright-artifacts-*`，读的是
    **环境变量**，不看 `tempfile.tempdir`。%TEMP% 对进程不可写的机器上会直接
    `EPERM: mkdtemp playwright-artifacts-XXXXXX`。所以每个浏览器测试类在
    `setUpClass()` 里先调它，`tearDownClass()` 里把返回值交给
    `restore_browser_temp_env()`。新增浏览器测试请调用这两个函数，别自己再写一遍。
    """
    original = {key: os.environ.get(key) for key in ("TEMP", "TMP", "TMPDIR")}
    isolated = tempfile.gettempdir()  # 本模块开头已指向可写的隔离目录
    for key in original:
        os.environ[key] = isolated
    return original


def restore_browser_temp_env(original: dict) -> None:
    """还原 `isolate_browser_temp_env()` 改掉的三个环境变量。"""
    for key, value in original.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _ensure_app_shell(page, base: str, timeout_ms: int = 8000) -> None:
    """确认加载到的是应用页面，不是半截/空白文档。

    CI 实测过一次：两个 workflow 同时触发，runner 被抢资源，页面请求被拖垮，最终文档里
    连应用外壳都没有，之后任何「等元素可见」都只会超时（`#tab-tasks`、`#tab-settings`
    都因此红过）。这种一次性环境故障重载一次就好——与 `open_settings` 同一副药。
    """
    js = "() => !!document.getElementById('tab-tasks')"
    try:
        page.wait_for_function(js, timeout=timeout_ms)
        return
    except Exception:
        pass
    page.reload(wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_function(js, timeout=timeout_ms)
        return
    except Exception as exc:
        evidence = {"href": "", "title": "", "ready_state": ""}
        try:
            evidence = page.evaluate(
                """() => ({href: location.href, title: document.title,
                           ready_state: document.readyState})""")
        except Exception as probe_exc:
            evidence = {"probe_error": repr(probe_exc)}
        raise AssertionError(
            "重载一次后页面里仍没有应用外壳（#tab-tasks），加载到的不是应用页面："
            "base=%r 现场=%r 原始异常=%r" % (base, evidence, exc)) from exc


def open_authed_page(context, page, base: str, token: str, timeout_ms: int = 30000) -> None:
    """预置会话 Cookie 后打开页面，**不带 `?token=`**，因此不会触发那次自动 reload。

    为什么这样做：页面拿到 `?token=` 时会先 `history.replaceState` 抹掉地址栏里的 token、
    再 `location.reload()` 改用 Cookie 继续。那次换页会让测试读到 reload 前的初始态
    （引擎行还写着「正在检查…」、按钮全 hidden），CI 的慢 runner 上就是这么红的。
    服务端鉴权顺序是「会话 Cookie → X-Token → ?token=」（`webapp._authorized`），
    所以直接给上下文种上会话 Cookie、不带 token 打开，行为等价而不换页。

    「带 token 换 Cookie 再重载」那条路单独由 `test_url_token_exchanges_for_cookie_and_reloads`
    覆盖——只有它需要等换页。
    """
    from noticehub.webapp import SESSION_COOKIE_NAME, _session_cookie_value

    context.add_cookies([{
        "name": SESSION_COOKIE_NAME,
        "value": _session_cookie_value(token),
        "url": base,
    }])
    harden(page)          # 防传输层抖动（见 harden 的注释）
    harden(context.request)
    watch_page_js_errors(page)   # 首次加载就抛的脚本异常只有从这里才抓得到
    page.goto(base + "/?nowiz=1", wait_until="domcontentloaded", timeout=timeout_ms)
    _ensure_app_shell(page, base, timeout_ms=min(8000, timeout_ms))
    # 新 UI 的「开始使用」是首启遮罩弹窗（#journey.journey-overlay，全屏 fixed）：
    # 它会盖住整页拦截所有点击（CI 实测 #push-save 等 30 秒点不下去，全是它）。
    # 这些用例都不在测引导，统一按「已看过引导」处理：预置 dismiss 标记 + 当场藏掉。
    # 标记进 localStorage：renderJourney 每次托管状态轮询都会重跑，只改 hidden 会被它翻回来。
    page.evaluate(
        """() => {
             try { localStorage.setItem('qq_digest_journey_hidden', '1'); } catch (e) {}
             const j = document.getElementById('journey');
             if (j) j.hidden = true;
           }"""
    )


def watch_page_js_errors(page) -> list:
    """收集页面里的 JS 报错（最多 5 条，各截 200 字），供「点了没反应」时定位。

    幂等：同一个 page 重复调用返回同一个列表。必须在 goto 之前注册——内联主脚本是解析时
    一次性执行的，它抛的异常只在那时出现，事后再挂监听就抓不到了。
    """
    existing = getattr(page, "_nh_js_errors", None)
    if existing is not None:
        return existing
    errors: list = []
    setattr(page, "_nh_js_errors", errors)

    def note(text) -> None:
        if len(errors) < 5:
            errors.append(str(text)[:200])

    def on_console(message) -> None:
        try:
            if getattr(message, "type", "") == "error":
                note(getattr(message, "text", ""))
        except Exception:                          # noqa: BLE001 - 诊断用，不许影响主流程
            pass

    try:
        page.on("pageerror", note)
        page.on("console", on_console)
    except Exception:                              # noqa: BLE001 - 旧版本 playwright 没有这个事件
        pass
    return errors


def open_settings(page, timeout_ms: int = 45000) -> None:
    """切到「设置」页签并等它真的显示。

    新 UI 把 QQ 接入、推送通道这些面板都搬进了设置页签（#tab-settings 默认 hidden）。
    找页头横幅时代元素的 browser 用例（#qrbox / #go / #status / #push-save…）必须先切过去，
    否则元素全在 hidden 子树里，「等可见」一律超时。

    为什么要按「轮询 + 整页重载」重试，而不是点一次等 15 秒：
    CI 上实测过一次 `test_every_scenario_renders_a_managed_state(engine_state='engine_missing')`
    的两次等待（15+15 秒）全部超时——那 30 秒里页面上**连 `#tab-settings-button` 都没有**，
    说明加载到的不是应用页面（runner 被同时触发的两个 workflow 抢资源，browser job 从
    88 秒拖到 124 秒，页面请求被拖垮）。这种一次性环境故障重载一次就好，不该让用例红。

    预算 45 秒：本地整类用例才 7–17 秒，CI 慢 4 倍也有富余；失败时把现场（地址、标题、
    readyState、按钮/页签是否存在）一起报出来，下次再红能直接看出是页面没加载还是点击没生效。
    """
    # 直接用 DOM click：页签按钮在浮岛里，首启引导/通知等固定浮层可能在点击瞬间盖住它，
    # 走 Playwright 的可操作性检查会一直等到超时。
    js_click = "() => { const b = document.getElementById('tab-settings-button'); if (b) b.click(); }"
    js_shown = ("() => { const el = document.getElementById('tab-settings');"
                " return !!el && !el.hidden; }")
    js_probe = ("() => ({href: location.href, title: document.title, ready: document.readyState,"
                " button: !!document.getElementById('tab-settings-button'),"
                " tab: !!document.getElementById('tab-settings'),"
                " hidden: (document.getElementById('tab-settings') || {}).hidden,"
                " handler: typeof (document.getElementById('tab-settings-button') || {}).onclick,"
                " tasks: !!document.getElementById('tab-tasks')})")
    # 设置页按用途收成四组（接入与通道 / 提醒与订阅 / 系统与维护 / 关于），卡片默认折在
    # <details class="settings-group"> 里。浏览器用例断言的多是卡片本身可见，所以切过去之后
    # 把组摊开——等价于用户逐组点开后的状态；没有分组的页面这里什么都不做。
    #
    # 只摊一次不够：settings.js 后面还会重挂面板，applyGroups() 见到新的 data-render 就把组
    # 整批重建，新组的 open 取值来自 groupOpen（点开时才由 toggle 事件写进去）。CI 慢机上重建
    # 常发生在这次摊开之后，组又收回默认的收起态，于是「#update-card 可见」15 秒超时。
    # 所以挂一个观察者：只要组被重建就再摊开，等价于用户一直把它们点开着。
    js_expand_groups = ("() => { const open = () => document.querySelectorAll"
                        "('#tab-settings details.settings-group')"
                        ".forEach(d => { if (!d.open) d.open = true; });"
                        " open();"
                        " const root = document.getElementById('tab-settings');"
                        " if (root && window.MutationObserver && !window.__nhGroupsOpen) {"
                        "   window.__nhGroupsOpen = new MutationObserver(open);"
                        "   window.__nhGroupsOpen.observe(root, {childList: true, subtree: true});"
                        " } }")
    deadline = time.monotonic() + timeout_ms / 1000.0
    # 点不动时最需要的是页面里的 JS 报错：主脚本是一大段内联 <script>，中间任何一句抛异常，
    # 后面的页签绑定（`button.onclick = …`）都不会执行，表现就是「按钮在、点了没反应」。
    js_errors = watch_page_js_errors(page)
    evidence = None
    last = ""
    while time.monotonic() < deadline:
        try:
            # 页面还没解析完时点了没用（按钮存在但切页脚本还没跑）：先等按钮出现。
            page.wait_for_function(
                "() => !!document.getElementById('tab-settings-button')", timeout=5000)
            page.evaluate(js_click)
            page.wait_for_function(js_shown, timeout=5000)
            try:
                page.wait_for_selector(
                    "#tab-settings details.settings-group", state="attached", timeout=3000)
                page.evaluate(js_expand_groups)
            except Exception:                     # noqa: BLE001 - 还没分组的面板不影响本函数
                pass
            return
        except Exception as exc:                      # noqa: BLE001 - 现场留证后再重试
            last = "%s: %s" % (type(exc).__name__, str(exc).split("\n")[0][:100])
            try:
                evidence = page.evaluate(js_probe)
            except Exception as probe_exc:            # 页面正在换页时探针也会失败
                evidence = {"probe_error": repr(probe_exc)[:100]}
            if isinstance(evidence, dict) and evidence.get("button") is False:
                # 页面上没有应用界面（加载到别的文档 / 请求被拖垮）：整页重载再来一次。
                try:
                    page.reload(wait_until="domcontentloaded", timeout=30000)
                except Exception:                     # noqa: BLE001 - 重载失败就继续下一轮
                    pass
            time.sleep(0.3)
    raise AssertionError(
        "%.0f 秒内没能切到设置页签（最后一次：%s）；现场=%s；页面报错=%s"
        % (timeout_ms / 1000.0, last, evidence, js_errors))


def wait_for_page_settled(page, timeout_s: float = 15.0, quiet_s: float = 0.4) -> None:
    """等页面自己那次自动 reload 落定（带 token 首次访问必发生一次）。

    页面拿到 `?token=…` 后会把 token 从地址栏抹掉（`history.replaceState`）再
    `location.reload()`，改用 Cookie 继续。也就是说**每次带 token 打开的页面都会换一次页**。
    测试里如果 `goto` 完就立刻断言或点击，快机器上恰好错过这次换页、慢一点的 runner 上
    就会读到 reload 前的初始态（引擎行还写着「正在检查…」），或者在 evaluate 时撞上
    `Execution context was destroyed`。所以先等主框架安静一段时间再动页面。
    """
    deadline = time.monotonic() + timeout_s
    navigations = {"count": 0}

    def on_navigate(frame) -> None:
        if frame == page.main_frame:
            navigations["count"] += 1

    page.on("framenavigated", on_navigate)
    try:
        last = navigations["count"]
        quiet_since = time.monotonic()
        while time.monotonic() < deadline:
            page.wait_for_timeout(50)
            if navigations["count"] != last:
                last = navigations["count"]
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since >= quiet_s:
                break
    finally:
        page.remove_listener("framenavigated", on_navigate)


def scroll_into_view(page, selector: str, timeout_ms: int = 5000) -> None:
    """把元素滚进视口并**等它真的到位**再返回。

    页面全局有 `html{scroll-behavior:smooth}`（新 UI 的滚动动效），
    `scrollIntoView({block:'center'})` 因此是异步的：紧接着量 getBoundingClientRect /
    elementFromPoint 会量到「还没滚过去」的位置——实测 #go 的 top 仍是 1035（视口 900），
    中心点命中 null，被误判成「按钮被别的层盖住」。这里用 behavior:'instant' 立即滚，
    再轮询到「上下边都在视口内」才返回（元素比视口还高时只要求上边可见）。
    """
    page.eval_on_selector(selector, "el => el.scrollIntoView({block: 'center', behavior: 'instant'})")
    page.wait_for_function(
        """(sel) => {
             const el = document.querySelector(sel);
             if (!el) return false;
             const r = el.getBoundingClientRect();
             return r.top >= 0 && (r.bottom <= innerHeight || r.height > innerHeight);
           }""",
        arg=selector, timeout=timeout_ms)


# ── playwright 同步 API 的环境抖动兜底 ─────────────────────────────────
# Windows runner 的 playwright 1.64 传输层会**间歇性**抛
# TypeError: <api>: 'str' object is not callable（同一 commit 的两次运行可能一次过、一次挂，
# 挂的 API 每次还不一样：evaluate / wait_for_function / context.request.get 都中过）。
# 这是环境问题，不是用例问题。错误发生在动作真正执行之前，所以对幂等的页面操作
# 重试是安全的；只认这一种错误文本，其它 TypeError 照常抛出。
_PW_GLITCH = "'str' object is not callable"


def harden(obj):
    """给 playwright 同步对象的常用方法加「环境抖动重试」（最多 3 次）。"""
    methods = ("evaluate", "wait_for_function", "wait_for_selector", "click", "fill",
               "eval_on_selector", "eval_on_selector_all", "inner_text", "is_hidden",
               "is_visible", "get_attribute", "goto", "reload", "dispatch_event",
               "focus", "select_option", "request")

    def wrap(name, orig):
        def runner(*args, **kwargs):
            attempts = 0
            while True:
                try:
                    return orig(*args, **kwargs)
                except TypeError as exc:
                    if _PW_GLITCH not in str(exc) or attempts >= 3:
                        raise
                    attempts += 1
                    time.sleep(0.2)   # 瞬态故障实测下一次调用就恢复，稍等再试
        runner.__name__ = name
        return runner

    for name in methods:
        orig = getattr(obj, name, None)
        if callable(orig):
            setattr(obj, name, wrap(name, orig))
    return obj


def safe_close(context, timeout_ms: int = 15000) -> None:
    """关掉 browser context，**清理失败不判红**。

    playwright 1.64.1 在 Windows runner 上的清理路径
    （BrowserContext.close → self.request.dispose → channel.send("dispose")）
    会间歇抛同一类瞬态 TypeError: 'str' object is not callable。
    清理不承载任何产品契约，把它变成用例失败只会制造假红；
    先重试一次，仍失败就放弃（进程退出时浏览器也会被回收）。
    """
    for attempt in range(4):
        try:
            context.close()
            return
        except TypeError as exc:
            if _PW_GLITCH not in str(exc):
                raise
            if attempt == 3:
                return
            time.sleep(0.3)
        except Exception as exc:                       # 已关/目标已消失：无需处理
            if "closed" in str(exc).lower():
                return
            raise
