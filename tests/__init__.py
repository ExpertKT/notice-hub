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
    page.goto(base + "/?nowiz=1", wait_until="domcontentloaded", timeout=timeout_ms)
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


def open_settings(page, timeout_ms: int = 15000) -> None:
    """切到「设置」页签并等它真的显示。

    新 UI 把 QQ 接入、推送通道这些面板都搬进了设置页签（#tab-settings 默认 hidden）。
    找页头横幅时代元素的 browser 用例（#qrbox / #go / #status / #push-save…）必须先切过去，
    否则元素全在 hidden 子树里，「等可见」一律 15 秒超时。
    """
    # 直接用 DOM click：页签按钮在浮岛里，首启引导/通知等固定浮层可能在点击瞬间盖住它，
    # 走 Playwright 的可操作性检查会一直等到超时（CI 上偶发过一次 15s 超时）。
    js_click = "() => { const b = document.getElementById('tab-settings-button'); if (b) b.click(); }"
    js_shown = ("() => { const el = document.getElementById('tab-settings');"
                " return !!el && !el.hidden; }")
    page.evaluate(js_click)
    try:
        page.wait_for_function(js_shown, timeout=timeout_ms)
    except Exception:
        # 等待失败就再点一次再等：首启引导/轮询可能在第一击瞬间恰好盖住按钮，
        # 或撞上 playwright 传输层的一次性抖动（见 tests.harden 注释）。
        page.evaluate(js_click)
        page.wait_for_function(js_shown, timeout=timeout_ms)


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
