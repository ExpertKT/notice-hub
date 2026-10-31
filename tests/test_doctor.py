"""诊断包（``doctor --bundle``）的脱敏与打包测试。"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from noticehub.config import Settings
from noticehub.doctor import DoctorError, build_bundle, sanitize
from noticehub.version import APP_VERSION

SECRETS = (
    "supersecretvalue",
    "SESSDATA-deadbeef",
    "sk-live-1234567890",
    "hunter2",
    "bearer-token-xyz",
)


class SanitizeTest(unittest.TestCase):
    def test_masks_common_credential_shapes(self) -> None:
        cases = [
            "token=supersecretvalue",
            "QQ_DIGEST_WEB_TOKEN=sk-live-1234567890",
            '"api_key": "sk-live-1234567890"',
            "password: hunter2",
            "Cookie: SESSDATA=SESSDATA-deadbeef",
            "Authorization: Bearer bearer-token-xyz",
        ]
        for text in cases:
            cleaned = sanitize(text)
            for secret in SECRETS:
                self.assertNotIn(secret, cleaned, f"{text!r} 泄漏了 {secret}")
            self.assertIn("***", cleaned)

    def test_keeps_ordinary_text(self) -> None:
        line = "2026-10-23 23:00:00 INFO 已发送 3 条摘要 group_id=42"
        self.assertEqual(sanitize(line), line)

    def test_keeps_key_names(self) -> None:
        cleaned = sanitize("QQ_DIGEST_WEB_TOKEN=sk-live-1234567890")
        self.assertIn("QQ_DIGEST_WEB_TOKEN", cleaned)


class BundleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="nh-doctor-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.data_dir = self.root / "data"
        self.log_dir = self.root / "logs"
        self.data_dir.mkdir()
        self.log_dir.mkdir()
        self.env_file = self.root / ".env"
        self.env_file.write_text(
            "QQ_BOT_APPID=123456\n"
            "QQ_BOT_SECRET=supersecretvalue\n"
            "QQ_DIGEST_CHAOXING_COOKIE=SESSDATA-deadbeef\n"
            "QQ_DIGEST_WEB_TOKEN=sk-live-1234567890\n"
            "QQ_DIGEST_LLM_API_KEY=\n",
            encoding="utf-8",
        )
        (self.log_dir / "noticehub.log").write_text(
            "boot ok\n" + "\n".join(f"line {index} token=supersecretvalue" for index in range(260)),
            encoding="utf-8",
        )
        (self.log_dir / "noticehub.log.1").write_text(
            "old Cookie: SESSDATA=SESSDATA-deadbeef\npassword: hunter2\n", encoding="utf-8"
        )
        (self.data_dir / "updates").mkdir()
        (self.data_dir / "updates" / "state.json").write_text(
            json.dumps(
                {
                    "checked_at": "2026-10-23T23:00:00",
                    "latest": {"tag": "v1.1.0-beta.9"},
                    "access_token": "supersecretvalue",
                }
            ),
            encoding="utf-8",
        )
        (self.data_dir / "updates" / "pending.json").write_text(
            json.dumps({"version": "1.1.0-beta.9", "secret": "hunter2"}), encoding="utf-8"
        )
        (self.data_dir / "hosting.json").write_text(json.dumps({"active": False}), encoding="utf-8")
        connection = sqlite3.connect(self.data_dir / "digest.sqlite3")
        try:
            connection.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT)")
            connection.execute("INSERT INTO messages (content) VALUES ('token=supersecretvalue')")
            connection.commit()
        finally:
            connection.close()
        self.settings = Settings.from_env(
            env={
                "QQ_DIGEST_DATA_DIR": str(self.data_dir),
                "QQ_DIGEST_LOG_DIR": str(self.log_dir),
            },
            env_file=self.env_file,
        )

    def _bundle(self, out: Path | None = None) -> Path:
        return build_bundle(
            self.settings,
            out or (self.root / "out"),
            napcat={"ok": True, "napcat_running": False, "hosting_active": False},
        )

    def test_bundle_has_sections_and_no_secrets(self) -> None:
        bundle = self._bundle()
        self.assertTrue(bundle.is_file(), bundle)
        with zipfile.ZipFile(bundle) as archive:
            names = archive.namelist()
            texts = {name: archive.read(name).decode("utf-8", "replace") for name in names}

        for required in (
            "version.txt",
            "env.sanitized.txt",
            "paths.txt",
            "db.txt",
            "state.txt",
            "napcat.txt",
            "README.txt",
        ):
            self.assertIn(required, names)
        self.assertEqual(len([name for name in names if name.startswith("logs/")]), 2)

        for name, text in texts.items():
            for secret in SECRETS:
                self.assertNotIn(secret, text, f"{name} 泄漏了 {secret}")

        self.assertIn(APP_VERSION, texts["version.txt"])
        for key in ("QQ_BOT_SECRET", "QQ_DIGEST_CHAOXING_COOKIE", "QQ_DIGEST_LLM_API_KEY"):
            self.assertIn(key, texts["env.sanitized.txt"])
        self.assertIn("未设置", texts["env.sanitized.txt"])

        # 日志只保留末尾 200 行
        latest = texts["logs/noticehub.log"]
        self.assertNotIn("line 0 token", latest)
        self.assertIn("line 259", latest)

        # 状态：非敏感字段保留，敏感键仍可见但值打码
        self.assertIn("v1.1.0-beta.9", texts["state.txt"])
        self.assertIn("access_token", texts["state.txt"])
        # 数据库只导出结构与行数，不含消息内容
        self.assertIn("integrity_check", texts["db.txt"])
        self.assertIn("messages", texts["db.txt"])

    def test_explicit_zip_path_is_used(self) -> None:
        target = self.root / "自定义包.zip"
        self.assertEqual(self._bundle(target), target.resolve())
        self.assertTrue(target.is_file())

    def test_output_path_that_is_a_file_raises(self) -> None:
        blocker = self.root / "blocked"
        blocker.write_text("x", encoding="utf-8")
        with self.assertRaises(DoctorError):
            self._bundle(blocker)

    def test_missing_data_dir_does_not_crash(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        settings = Settings.from_env(
            env={"QQ_DIGEST_DATA_DIR": str(empty / "nope"), "QQ_DIGEST_LOG_DIR": str(empty / "nolog")},
            env_file=self.env_file,
        )
        bundle = build_bundle(settings, self.root / "out2", napcat={"ok": False})
        with zipfile.ZipFile(bundle) as archive:
            self.assertIn("db.txt", archive.namelist())
            self.assertIn("日志目录", archive.read("logs/_no-logs.txt").decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
