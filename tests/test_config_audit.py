"""`config_audit.audit_config`：来源判定、防泄密、无子进程副作用。

只测事实：值是多少、从哪来、被谁盖住，以及**不许**把密钥写进返回体。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from noticehub.config import Settings  # noqa: E402
from noticehub.config_audit import SET, UNSET, audit_config  # noqa: E402
from noticehub.store import Store  # noqa: E402
from noticehub.webapp import TaskWebServer  # noqa: E402


def _by_key(items: list[dict]) -> dict[str, dict]:
    return {item["key"]: item for item in items}


class AuditSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env_file = Path(self.tmp.name) / ".env"

    def _write_env(self, content: str) -> None:
        self.env_file.write_text(content, encoding="utf-8")

    def _audit(self, content: str, environ: dict[str, str] | None = None) -> dict[str, dict]:
        self._write_env(content)
        with mock.patch.dict(os.environ, environ or {}, clear=False):
            for key in ("QQ_DIGEST_WEB_PORT", "QQ_DIGEST_NAPCAT_API_URL", "QQ_DIGEST_WECHAT_DIR"):
                if environ is None or key not in environ:
                    os.environ.pop(key, None)
            settings = Settings.from_env(env_file=self.env_file)
            return _by_key(audit_config(settings))

    def test_env_port_overrides_dotenv_and_reports_shadowed(self) -> None:
        items = self._audit("QQ_DIGEST_WEB_PORT=58066\n", {"QQ_DIGEST_WEB_PORT": "58067"})
        port = items["web_port"]
        self.assertEqual(port["value"], "58067", "生效值取环境变量")
        self.assertEqual(port["source"], "env")
        self.assertEqual(port["shadowed"], {"value": "58066", "source": "dotenv"})

    def test_dotenv_only_has_no_shadowed(self) -> None:
        port = self._audit("QQ_DIGEST_WEB_PORT=58066\n")["web_port"]
        self.assertEqual(port["value"], "58066")
        self.assertEqual(port["source"], "dotenv")
        self.assertIsNone(port["shadowed"])

    def test_missing_everywhere_is_default(self) -> None:
        items = self._audit("")
        port = items["web_port"]
        self.assertEqual(port["value"], "8766")
        self.assertEqual(port["source"], "default")
        self.assertIsNone(port["shadowed"])

    def test_unset_push_channel(self) -> None:
        push = self._audit("")["push_channel"]
        self.assertEqual(push["source"], "unset")
        self.assertEqual(push["value"], UNSET)

    def test_probed_napcat_url_is_auto(self) -> None:
        self._write_env("")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QQ_DIGEST_NAPCAT_API_URL", None)
            settings = Settings.from_env(env_file=self.env_file)
            settings.napcat_api_url = "http://127.0.0.1:3002"  # 模拟探测后写回
            item = _by_key(audit_config(settings))["napcat_api_url"]
        self.assertEqual(item["source"], "auto")
        self.assertEqual(item["value"], "http://127.0.0.1:3002")

    def test_all_ten_keys_present_in_order(self) -> None:
        keys = [item["key"] for item in audit_config(Settings())]
        self.assertEqual(
            keys,
            [
                "web_port",
                "napcat_api_url",
                "wechat_storage",
                "chaoxing_cookie",
                "push_channel",
                "data_dir",
                "log_dir",
                "update_channel",
                "llm_model",
                "attachments_enabled",
            ],
        )


class AuditSecretTests(unittest.TestCase):
    def test_secrets_never_appear_in_values_or_shadowed(self) -> None:
        cookie = "sessionid=SUPERSECRETCOOKIE123"
        api_key = "sk-SUPERSECRETKEY456"
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "QQ_DIGEST_CHAOXING_COOKIE=%s\nQQ_DIGEST_LLM_API_KEY=%s\nQQ_DIGEST_ATTACHMENTS=0\n" % (cookie, api_key),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=False):
                settings = Settings.from_env(env_file=env_file)
                items = audit_config(settings)

        dumped = repr(items)
        self.assertNotIn(cookie, dumped)
        self.assertNotIn(api_key, dumped)
        self.assertNotIn("SUPERSECRET", dumped)
        cookie_item = _by_key(items)["chaoxing_cookie"]
        self.assertEqual(cookie_item["value"], SET, "凭据只报已设置")
        self.assertEqual(_by_key(items)["attachments_enabled"]["value"], "关闭")


class AuditSideEffectTests(unittest.TestCase):
    def test_never_spawns_subprocess(self) -> None:
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("不许起子进程")) as popen, \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("不许起子进程")) as run, \
                mock.patch.object(subprocess, "check_output", side_effect=AssertionError("不许起子进程")) as check:
            items = audit_config(Settings())
        self.assertEqual(len(items), 10)
        self.assertEqual(popen.call_count + run.call_count + check.call_count, 0)


class AuditSnapshotTests(unittest.TestCase):
    """`snapshot` 是调用方算好的探测结果：传了就报实际值，不传行为不许变。"""

    def test_snapshot_supplies_located_values_as_auto(self) -> None:
        snapshot = {
            "wechat_storage": "D:/wechat/xwechat_files",
            "napcat_api_url": "http://127.0.0.1:3002",
        }
        items = _by_key(audit_config(Settings(), snapshot))
        for key, value in snapshot.items():
            with self.subTest(key=key):
                self.assertEqual(items[key]["source"], "auto")
                self.assertEqual(items[key]["value"], value)
                self.assertTrue(items[key]["note"], "auto 也要有一句可操作的话")

    def test_snapshot_is_optional(self) -> None:
        plain = _by_key(audit_config(Settings()))
        explicit_none = _by_key(audit_config(Settings(), None))
        self.assertEqual(plain, explicit_none, "不传与传 None 必须完全一致")
        self.assertEqual(plain["wechat_storage"]["source"], "unset")
        self.assertEqual(plain["napcat_api_url"]["source"], "default")
        self.assertEqual(plain["wechat_storage"]["value"], UNSET)

    def test_partial_snapshot_only_affects_given_keys(self) -> None:
        items = _by_key(audit_config(Settings(), {"wechat_storage": "D:/wechat"}))
        self.assertEqual(items["wechat_storage"]["source"], "auto")
        self.assertEqual(items["napcat_api_url"]["source"], "default", "没给的那项不许跟着变")


class AuditRouteTests(unittest.TestCase):
    """`GET /api/config/audit`：真起 TaskWebServer 走 HTTP，鉴权与防泄密都在这一层验。"""

    TOKEN = "SECRET-TOKEN-9f3a2b"
    API_KEY = "sk-SECRET-KEY-1234abcd"
    COOKIE = "sessionid=SECRETCOOKIE5678"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.env_file = root / ".env"
        self.env_file.write_text(
            "QQ_DIGEST_WEB_TOKEN=%s\n"
            "QQ_DIGEST_LLM_API_KEY=%s\n"
            "QQ_DIGEST_CHAOXING_COOKIE=%s\n"
            "QQ_DIGEST_WEB_PORT=0\n"
            "QQ_DIGEST_DATA_DIR=%s\n"
            "QQ_DIGEST_LOG_DIR=%s\n" % (self.TOKEN, self.API_KEY, self.COOKIE, root / "data", root / "logs"),
            encoding="utf-8",
        )
        # 只摘掉配置类变量，别清空整份环境：SSL_CERT_* / 代理这类系统变量一没，
        # 连本机 HTTP 请求都会在 urllib 建 SSL 上下文时炸掉。
        base_env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("QQ_DIGEST_", "WXPUSHER_", "SERVERCHAN_", "PUSHPLUS_"))
        }
        patcher = mock.patch.dict(os.environ, base_env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings = Settings.from_env(env_file=self.env_file)
        self.server = TaskWebServer(self.settings, Store(root / "audit.sqlite3"))
        self.assertTrue(self.server.start())
        self.addCleanup(self.server.stop)
        assert self.server.server is not None
        self.base = "http://127.0.0.1:%d" % self.server.server.server_address[1]

    def _get(self, token: str) -> dict:
        headers = {"X-Token": token} if token else {}
        request = urllib.request.Request(self.base + "/api/config/audit", headers=headers)
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_returns_ten_items_in_fixed_order_with_token(self) -> None:
        body = self._get(self.TOKEN)
        self.assertIs(body["ok"], True)
        self.assertEqual(
            [item["key"] for item in body["items"]],
            [
                "web_port",
                "napcat_api_url",
                "wechat_storage",
                "chaoxing_cookie",
                "push_channel",
                "data_dir",
                "log_dir",
                "update_channel",
                "llm_model",
                "attachments_enabled",
            ],
        )

    def test_requires_token(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._get("")
        self.assertEqual(caught.exception.code, 401)
        payload = json.loads(caught.exception.read().decode("utf-8"))
        self.assertIs(payload["ok"], False)

    def test_response_never_leaks_secrets(self) -> None:
        raw = json.dumps(self._get(self.TOKEN), ensure_ascii=False)
        for secret in (self.TOKEN, self.API_KEY, self.COOKIE, "SECRET"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, raw)

    def test_request_path_spawns_no_subprocess(self) -> None:
        boom = AssertionError("审计请求不许起子进程")
        with mock.patch.object(subprocess, "Popen", side_effect=boom) as popen, \
                mock.patch.object(subprocess, "run", side_effect=boom) as run, \
                mock.patch.object(subprocess, "check_output", side_effect=boom) as check:
            body = self._get(self.TOKEN)
        self.assertEqual(len(body["items"]), 10)
        self.assertEqual(popen.call_count + run.call_count + check.call_count, 0)


if __name__ == "__main__":
    unittest.main()
