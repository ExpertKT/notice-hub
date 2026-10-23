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
