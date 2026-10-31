"""qq-live-digest 测试包。

Keep test discovery hermetic: config.py reads QQ_DIGEST_ENV at import time,
so point it at an isolated file and disable provider settings before any test
module imports qq_live_digest.config. This does not clear unrelated process
variables such as PATH, TEMP, or USERPROFILE.
"""

import atexit
import os
import shutil
import tempfile
import time
from pathlib import Path

_TEST_ENV_DIR = Path(tempfile.mkdtemp(prefix="qq-live-digest-tests-"))
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
    "QQ_DIGEST_LLM",
    "QQ_DIGEST_LLM_BACKEND",
    "QQ_DIGEST_LLM_ENDPOINT",
    "QQ_DIGEST_LLM_MODEL",
    "QQ_DIGEST_LLM_TIMEOUT",
    "QQ_DIGEST_CODEBUDDY_CLI",
):
    os.environ.pop(_key, None)

# Settings() is intentionally convenient for application code, but its
# dataclass defaults leave LLM enabled. Make implicit test fixtures offline;
# tests that exercise LLM behavior pass an explicit backend/key/flag.
from qq_live_digest.config import Settings as _Settings  # noqa: E402
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
