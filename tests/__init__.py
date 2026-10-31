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
    page.goto(base + "/?nowiz=1", wait_until="domcontentloaded", timeout=timeout_ms)


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
