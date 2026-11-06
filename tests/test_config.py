from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import noticehub.config as config  # noqa: E402
from noticehub.config import (  # noqa: E402
    Settings,
    parse_aliases,
    parse_bool,
    parse_quiet_hours,
    split_list,
)


def _env(**overrides) -> Settings:
    """按环境变量构造 Settings：键名照原样写，省掉每个用例重复的 env={...} 包装。"""
    return Settings.from_env(env=dict(overrides))


@contextmanager
def _env_file(content: str):
    """在一个临时目录里写一份 .env，用完自动删掉。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".env"
        path.write_text(content, encoding="utf-8")
        yield tmp, path


class _RecordingHost:
    """假 handler：只实现 `_json`，把状态码和响存下来给断言看。"""

    def __init__(self, settings: Settings) -> None:
        self.server = type("S", (), {})()
        self.server.settings = settings
        self.payload: dict = {}

    def _json(self, status, body):
        self.payload = {"status": status, **body}


class ConfigTest(unittest.TestCase):
    def test_parse_aliases(self) -> None:
        aliases = parse_aliases("g1=学院群;g2=班级群,broken")
        self.assertEqual(aliases, {"g1": "学院群", "g2": "班级群"})

    def test_parse_bool(self) -> None:
        for value in ("1", "true", "YES", "on", "是"):
            self.assertTrue(parse_bool(value))
        for value in ("0", "false", "", "no"):
            self.assertFalse(parse_bool(value))
        self.assertTrue(parse_bool(None, True))

    def test_split_list_accepts_chinese_separators(self) -> None:
        self.assertEqual(split_list("a,b；c\n d"), ("a", "b", "c", "d"))
        self.assertEqual(split_list(""), ())
        self.assertEqual(split_list(["x", " y "]), ("x", "y"))

    def test_web_host_defaults_to_loopback_off_windows_and_env_overrides(self) -> None:
        # 非 Windows 上不给局域网地址，默认只听回环。
        with mock.patch.object(config.os, "name", "posix"):
            self.assertEqual(config._default_web_host(), "127.0.0.1")
            self.assertEqual(Settings().web_host, "127.0.0.1")
        with mock.patch.object(config, "_default_web_host", return_value="127.0.0.1"):
            self.assertEqual(Settings.from_env(env={}).web_host, "127.0.0.1")
        self.assertEqual(
            Settings.from_env(env={"QQ_DIGEST_WEB_HOST": "0.0.0.0"}).web_host,
            "0.0.0.0",
        )

    def test_from_env(self) -> None:
        settings = _env(
            QQ_BOT_APPID="1234567",
            QQ_BOT_SECRET="secret",
            QQ_BOT_SANDBOX="1",
            QQ_DIGEST_GROUPS="g1,g2",
            QQ_DIGEST_GROUP_ALIASES="g1=学院群",
            QQ_DIGEST_QUIET_GROUPS="g2",
            QQ_DIGEST_IMMEDIATE_GROUPS="g1",
            QQ_DIGEST_PUSH_OPENIDS="u1",
            WXPUSHER_APP_TOKEN="AT_x",
            WXPUSHER_UIDS="UID_a",
            WXPUSHER_TOPIC_IDS="1,2",
            QQ_DIGEST_WINDOW_MINUTES="5",
            QQ_DIGEST_WEB_BASE_URL="https://qq-digest.tailnet.ts.net/",
            QQ_DIGEST_DATA_DIR="D:/CodexTemp/qq-digest-test-data",
            QQ_DIGEST_LOG_DIR="",
        )
        self.assertEqual(settings.appid, "1234567")
        self.assertTrue(settings.sandbox)
        self.assertEqual(settings.group_whitelist, ("g1", "g2"))
        self.assertEqual(settings.group_name("g1"), "学院群")
        self.assertEqual(settings.group_name("gz"), "gz")
        self.assertEqual(settings.quiet_groups, ("g2",))
        self.assertEqual(settings.immediate_groups, ("g1",))
        # 「某个群算不算安静 / 要不要立刻提醒」两边都验，别只测命中的那一边。
        for method, group, expected in (
            (settings.is_quiet_group, "g2", True),
            (settings.is_quiet_group, "g1", False),
            (settings.is_immediate_group, "g1", True),
            (settings.is_immediate_group, "g2", False),
        ):
            with self.subTest(group=group):
                self.assertEqual(method(group), expected)
        self.assertEqual(settings.wxpusher_topic_ids, (1, 2))
        self.assertEqual(settings.window_minutes, 5)
        self.assertEqual(settings.web_base_url, "https://qq-digest.tailnet.ts.net")
        self.assertEqual(settings.update_channel, "")
        self.assertTrue(settings.data_dir.as_posix().endswith("qq-digest-test-data"))
        self.assertTrue(str(settings.log_dir).endswith("logs"))
        self.assertEqual(settings.push_channels(), ["qq-bot", "wxpusher"])

    def test_update_channel_accepts_only_stable_or_beta(self) -> None:
        # 写错的通道名一律当没写，避免分流到不存在的通道。
        accepted = {"beta": "beta", "stable": "stable", "preview": "", "off": ""}
        for value, expected in accepted.items():
            with self.subTest(value=value):
                self.assertEqual(
                    Settings.from_env(env={"QQ_DIGEST_UPDATE_CHANNEL": value}).update_channel,
                    expected,
                )

    def test_llm_api_key_alias_precedence(self) -> None:
        self.assertEqual(
            Settings.from_env(env={"QQ_DIGEST_LLM_API_KEY": "new"}).dashscope_api_key,
            "new",
        )
        self.assertEqual(
            Settings.from_env(env={"DASHSCOPE_API_KEY": "old"}).dashscope_api_key,
            "old",
        )
        self.assertEqual(
            Settings.from_env(
                env={"QQ_DIGEST_LLM_API_KEY": "new", "DASHSCOPE_API_KEY": "old"}
            ).dashscope_api_key,
            "new",
        )

    def test_vision_active_requires_a_vision_capable_endpoint(self) -> None:
        # 只有端点自带视觉模型才算开了读图；默认模型挂在别的厂商上就是没开。
        self.assertTrue(Settings.from_env(env={"DASHSCOPE_API_KEY": "k"}).vision_active)
        deepseek = {
            "DASHSCOPE_API_KEY": "k",
            "QQ_DIGEST_LLM_ENDPOINT": "https://api.deepseek.com/v1/chat/completions",
        }
        self.assertFalse(Settings.from_env(env=deepseek).vision_active)
        # 用户自己指定了视觉模型就按他说的来。
        self.assertTrue(
            Settings.from_env(
                env={**deepseek, "QQ_DIGEST_VL_MODEL": "Qwen/Qwen2.5-VL-7B-Instruct"}
            ).vision_active
        )
        # 换成本机 ollama 同理：没有视觉模型就不读图。
        self.assertFalse(
            Settings.from_env(
                env={
                    "DASHSCOPE_API_KEY": "k",
                    "QQ_DIGEST_LLM_ENDPOINT": "http://127.0.0.1:11434/v1/chat/completions",
                }
            ).vision_active
        )
        # 开关关掉、或者压根没配 key，一样不可用。
        self.assertFalse(
            Settings.from_env(env={"DASHSCOPE_API_KEY": "k", "QQ_DIGEST_VISION": "0"}).vision_active
        )
        self.assertFalse(Settings.from_env(env={"QQ_DIGEST_VISION": "1"}).vision_active)
        # describe() 对外报的必须是同一口径。
        self.assertFalse(Settings.from_env(env=deepseek).describe()["attachments"]["vision"])
        self.assertEqual(Settings.from_env(env=deepseek).endpoint_host, "api.deepseek.com")

    def test_api_docs_and_env_example_describe_cloud_routes(self) -> None:
        env_example = (_ROOT / ".env.example").read_text(encoding="utf-8")
        api_doc = (_ROOT / "docs" / "接API.md").read_text(encoding="utf-8")
        self.assertIn("deepseek-flash", env_example)
        self.assertIn("https://api.deepseek.com", env_example)
        self.assertIn("DASHSCOPE_API_KEY", env_example)
        self.assertIn("任何", env_example)
        for title in ("## A. 免费层", "## B. 便宜官方 API（推荐）", "## C. 本地 Ollama"):
            self.assertIn(title, api_doc)

    def test_group_whitelist_parsing_boundaries(self) -> None:
        settings = Settings.from_env(env={"QQ_DIGEST_GROUPS": " 123, ,456 ,456, "})
        self.assertTrue(settings.accepts_group("123"))
        self.assertTrue(settings.accepts_group("456"))
        self.assertFalse(settings.accepts_group("789"))
        self.assertFalse(settings.accepts_group(""))

    def test_accepts_group(self) -> None:
        only = Settings(group_whitelist=("g1",))
        self.assertTrue(only.accepts_group("g1"))
        self.assertFalse(only.accepts_group("g2"))
        # fail-closed：没配白名单就一个群都不收，不会偷偷收全部。
        self.assertFalse(Settings().accepts_group("anything"))

    def test_group_whitelist_is_fail_closed(self) -> None:
        # 空值、纯分隔符、非数字，一律当「没配」，绝不退化成收全部。
        for raw in ("", "   ", ",,,", "；", "not-a-number"):
            settings = Settings.from_env(env={"QQ_DIGEST_GROUPS": raw})
            self.assertFalse(settings.accepts_group("123"), raw)
            self.assertFalse(settings.accepts_group("anything"), raw)

    def test_empty_group_whitelist_is_reported_and_described(self) -> None:
        problems = Settings().problems()
        self.assertTrue(
            any("NOTICEHUB_GROUPS" in item and "不会处理任何群" in item for item in problems)
        )
        self.assertEqual(Settings().describe()["groups"], ["<未配置：不处理任何群>"])

    def test_official_bot_and_catchup_flags(self) -> None:
        settings = _env(
            QQ_BOT_APPID="1",
            QQ_BOT_SECRET="x",
            QQ_DIGEST_PUSH_OPENIDS="u1",
            QQ_DIGEST_OFFICIAL_BOT_ENABLED="0",
            QQ_DIGEST_CATCHUP_ENABLED="1",
            QQ_DIGEST_CATCHUP_HOURS="12",
            QQ_DIGEST_CATCHUP_COUNT="25",
            QQ_DIGEST_NAPCAT_API_URL="http://127.0.0.1:3001",
            QQ_DIGEST_ONEBOT_TOKEN="tok",
            WXPUSHER_APP_TOKEN="AT",
            WXPUSHER_UIDS="UID",
        )
        self.assertEqual(settings.napcat_api_url, "http://127.0.0.1:3001")
        self.assertEqual(settings.napcat_api_token, "tok")
        self.assertFalse(settings.official_bot_enabled)
        self.assertEqual(settings.push_channels(), ["wxpusher"])
        self.assertTrue(settings.catchup_enabled)
        self.assertEqual(settings.catchup_hours, 12)
        self.assertEqual(settings.catchup_count, 25)

    def test_frozen_defaults_use_runtime_root(self) -> None:
        original = config.RUNTIME_ROOT
        try:
            config.RUNTIME_ROOT = Path("D:/frozen-app")
            settings = Settings.from_env(env={})
            self.assertEqual(settings.data_dir, Path("D:/frozen-app/data"))
            self.assertEqual(settings.log_dir, Path("D:/frozen-app/logs"))
            self.assertEqual(settings.napcat_qr_path, Path("D:/frozen-app/data/qrcode.png"))
        finally:
            config.RUNTIME_ROOT = original

    def test_default_env_file_follows_runtime_root_when_frozen(self) -> None:
        original = config.RUNTIME_ROOT
        try:
            config.RUNTIME_ROOT = Path("D:/frozen-app")
            with mock.patch.dict(os.environ, {}, clear=True):
                settings = Settings.from_env()
            self.assertEqual(settings.env_file, Path("D:/frozen-app/.env"))
        finally:
            config.RUNTIME_ROOT = original

    def test_env_file_override_wins_over_runtime_root(self) -> None:
        original = config.RUNTIME_ROOT
        try:
            config.RUNTIME_ROOT = Path("D:/frozen-app")
            with mock.patch.dict(os.environ, {"QQ_DIGEST_ENV": "E:/custom/env-file"}, clear=True):
                settings = Settings.from_env()
            self.assertEqual(settings.env_file, Path("E:/custom/env-file"))
        finally:
            config.RUNTIME_ROOT = original

    def test_napcat_qr_path_defaults_under_data_dir(self) -> None:
        settings = Settings.from_env(env={"QQ_DIGEST_DATA_DIR": "D:/qq-data"})
        self.assertEqual(settings.napcat_qr_path, Path("D:/qq-data/qrcode.png"))

    def test_napcat_qr_path_honors_environment_override(self) -> None:
        settings = Settings.from_env(
            env={"QQ_DIGEST_DATA_DIR": "D:/qq-data", "QQ_DIGEST_NAPCAT_QR_PATH": "E:/napcat/qr.png"}
        )
        self.assertEqual(settings.napcat_qr_path, Path("E:/napcat/qr.png"))

    def test_problems_report_missing_credentials(self) -> None:
        joined = "\n".join(Settings().problems())
        self.assertIn("QQ_BOT_APPID", joined)
        self.assertIn("推送通道", joined)

    def test_describe_masks_secrets(self) -> None:
        described = Settings(
            appid="1234567890", secret="topsecret", push_c2c_openids=("abcdefgh",)
        ).describe()
        self.assertNotIn("topsecret", str(described))
        self.assertTrue(described["appid"].startswith("1234"))

    def test_quiet_hours_parsing_and_membership(self) -> None:
        for raw, expected in (
            ("23:00-07:00", "23:00-07:00"),
            ("22:30~06:15", "22:30-06:15"),
            ("bad", ""),
            ("08:00-08:00", ""),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(parse_quiet_hours(raw), expected)

        night = Settings(quiet_hours="23:00-07:00")
        for moment, expected in (
            (dt.datetime(2026, 10, 6, 23, 30), True),
            (dt.datetime(2026, 10, 6, 6, 30), True),
            (dt.datetime(2026, 10, 6, 12, 0), False),
        ):
            with self.subTest(moment=moment):
                self.assertEqual(night.in_quiet_hours(moment), expected)
        self.assertFalse(Settings(quiet_hours="").in_quiet_hours(dt.datetime(2026, 10, 6, 3, 0)))
        daytime = Settings(quiet_hours="13:00-14:00")
        self.assertTrue(daytime.in_quiet_hours(dt.datetime(2026, 10, 6, 13, 30)))
        self.assertFalse(daytime.in_quiet_hours(dt.datetime(2026, 10, 6, 14, 0)))

    def test_lab_flags_default_off_and_env_parsing(self) -> None:
        settings = Settings.from_env(env={})
        self.assertFalse(settings.lab_search)
        self.assertFalse(settings.lab_replies)
        self.assertFalse(settings.lab_events)
        self.assertFalse(settings.lab_rules)

        truthy = ("1", "true", "YES", "On", "是")
        falsy = ("0", "false", "", "  ", "乱写")
        for name in ("search", "replies", "events", "rules"):
            key = f"QQ_DIGEST_LAB_{name.upper()}"
            for value in truthy:
                self.assertTrue(Settings.from_env(env={key: value}).lab_enabled(name), (key, value))
            for value in falsy:
                self.assertFalse(Settings.from_env(env={key: value}).lab_enabled(name), (key, value))

    def test_community_telemetry_defaults_off_and_parses_env(self) -> None:
        self.assertFalse(Settings().community_telemetry)
        self.assertFalse(Settings.from_env(env={}).community_telemetry)
        self.assertTrue(Settings.from_env(env={"QQ_DIGEST_COMMUNITY_TELEMETRY": "1"}).community_telemetry)
        self.assertFalse(Settings.from_env(env={"QQ_DIGEST_COMMUNITY_TELEMETRY": "0"}).community_telemetry)
        # describe() 只报布尔，绝不带出任何密钥。
        self.assertIs(Settings.from_env(env={}).describe()["community_telemetry"], False)

    def test_floatball_defaults_off_and_parses_env(self) -> None:
        """桌面浮球默认关闭，只有显式打开才出现；新前缀优先、旧前缀仍可读。"""
        self.assertFalse(Settings().floatball_enabled)
        self.assertFalse(Settings.from_env(env={}).floatball_enabled)
        self.assertFalse(Settings.from_env(env={"NOTICEHUB_FLOATBALL_ENABLED": "0"}).floatball_enabled)
        for value in ("1", "true", "YES", "On", "是"):
            self.assertTrue(Settings.from_env(env={"NOTICEHUB_FLOATBALL_ENABLED": value}).floatball_enabled, value)
        for value in ("0", "false", "", "  ", "乱写"):
            self.assertFalse(Settings.from_env(env={"NOTICEHUB_FLOATBALL_ENABLED": value}).floatball_enabled, value)
        self.assertTrue(Settings.from_env(env={"QQ_DIGEST_FLOATBALL_ENABLED": "1"}).floatball_enabled)
        # 新旧两个键都在时，新键说了算。
        self.assertFalse(Settings.from_env(env={"NOTICEHUB_FLOATBALL_ENABLED": "0", "QQ_DIGEST_FLOATBALL_ENABLED": "1"}).floatball_enabled)

    def test_community_telemetry_save_path_writes_env_and_hot_updates(self) -> None:
        from noticehub.settings_api import SettingsPostMixin

        with _env_file("KEEP=yes\n") as (tmp, env_path):
            settings = Settings(env_file=env_path, community_telemetry=False)
            host = _RecordingHost(settings)

            SettingsPostMixin._save_telemetry_settings(host, {"community_telemetry": True})
            self.assertEqual(host.payload["status"], 200)
            self.assertTrue(settings.community_telemetry)
            self.assertIn("NOTICEHUB_COMMUNITY_TELEMETRY=1", env_path.read_text(encoding="utf-8"))
            self.assertIn("KEEP=yes", env_path.read_text(encoding="utf-8"))

            # 传字符串 "1" 不算布尔，必须当场拒绝而不是悄悄当 true。
            with self.assertRaises(ValueError):
                SettingsPostMixin._save_telemetry_settings(host, {"community_telemetry": "1"})

    def test_new_env_prefix_wins_over_legacy(self) -> None:
        """新名 NOTICEHUB_* 优先：两个都写且值不同时，以新名为准。"""
        settings = _env(
            NOTICEHUB_WEB_PORT="58066",
            QQ_DIGEST_WEB_PORT="58067",
            NOTICEHUB_GROUPS="new-group",
            QQ_DIGEST_GROUPS="legacy-group",
        )
        self.assertEqual(settings.web_port, 58066)
        self.assertEqual(settings.group_whitelist, ("new-group",))

    def test_legacy_env_prefix_still_reads(self) -> None:
        """只写旧名也照样能读到（用户现有 .env 不用改）。"""
        from noticehub.bootstrap import settings as settings_module

        # 「只警告一次」是进程级状态：这条用例要自己复位，否则前面任何一次旧名读取都会吃掉 warning。
        settings_module._legacy_env_warned = False
        with self.assertLogs("noticehub.config", level="WARNING") as logs:
            settings = Settings.from_env(
                env={
                    "QQ_DIGEST_WEB_PORT": "58068",
                    "QQ_DIGEST_GROUPS": "legacy-group",
                }
            )
        self.assertEqual(settings.web_port, 58068)
        self.assertEqual(settings.group_whitelist, ("legacy-group",))
        self.assertTrue(
            any("旧环境变量名" in line for line in logs.output),
            "只设旧名时要提示一次改成 NOTICEHUB_*",
        )

    def test_env_port_overrides_dotenv_port_and_warns(self) -> None:
        """`.env` 写 58066、环境变量写 58067：生效的是环境变量，但必须在日志里说清楚。"""
        with _env_file("QQ_DIGEST_WEB_PORT=58066\n") as (tmp, env_path):
            with mock.patch.dict(os.environ, {"QQ_DIGEST_WEB_PORT": "58067"}, clear=True):
                with self.assertLogs("noticehub.config", level="WARNING") as logs:
                    settings = Settings.from_env(env_file=env_path)
        self.assertEqual(settings.web_port, 58067, "环境变量优先，行为不变")
        self.assertIn("端口 58067 来自环境变量，覆盖了 .env 里的 58066", "\n".join(logs.output))

    def test_same_port_in_dotenv_and_env_does_not_warn(self) -> None:
        """两边一致时一句 warning 都不许刷。"""
        with _env_file("QQ_DIGEST_WEB_PORT=58066\n") as (tmp, env_path):
            with mock.patch.dict(os.environ, {"QQ_DIGEST_WEB_PORT": "58066"}, clear=True):
                with self.assertNoLogs("noticehub.config", level="WARNING"):
                    settings = Settings.from_env(env_file=env_path)
        self.assertEqual(settings.web_port, 58066)

    def test_update_env_file_writes_atomically(self) -> None:
        """写入走临时文件 + os.replace：内容正确，且不留下 .tmp 残渣。"""
        with _env_file("KEEP=yes\nQQ_DIGEST_GROUPS=a\n") as (tmp, env_path):
            result = config.update_env_file(env_path, {"QQ_DIGEST_GROUPS": "a,b"})
            self.assertTrue(result["ok"], result)
            self.assertEqual(env_path.read_text(encoding="utf-8"), "KEEP=yes\nQQ_DIGEST_GROUPS=a,b\n")
            self.assertEqual(list(Path(tmp).glob(".env.tmp-*")), [])

    def test_update_env_file_keeps_original_when_replace_fails(self) -> None:
        """os.replace 失败（断电/被占用）时，原 .env 必须逐字节保持原样。"""
        original = "KEEP=yes\nQQ_DIGEST_GROUPS=a\n"
        with _env_file(original) as (tmp, env_path):
            with mock.patch.object(config.os, "replace", side_effect=OSError("磁盘满了")):
                result = config.update_env_file(env_path, {"QQ_DIGEST_GROUPS": "a,b"})
            self.assertFalse(result["ok"])
            self.assertIn("写入 .env 失败", result["error"])
            self.assertEqual(env_path.read_text(encoding="utf-8"), original)
            self.assertEqual(list(Path(tmp).glob(".env.tmp-*")), [], "失败后必须清掉临时文件")

    def test_remove_env_keys_is_atomic_and_survives_failure(self) -> None:
        original = "KEEP=yes\nQQ_DIGEST_NAPCAT_DIR=D:\\NapCat\nOTHER=1\n"
        with _env_file(original) as (tmp, env_path):
            result = config.remove_env_keys(env_path, ["QQ_DIGEST_NAPCAT_DIR"])
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["removed"], ["QQ_DIGEST_NAPCAT_DIR"])
            self.assertEqual(env_path.read_text(encoding="utf-8"), "KEEP=yes\nOTHER=1\n")

            env_path.write_text(original, encoding="utf-8")
            with mock.patch.object(config.os, "replace", side_effect=OSError("被占用")):
                failed = config.remove_env_keys(env_path, ["QQ_DIGEST_NAPCAT_DIR"])
            self.assertFalse(failed["ok"])
            self.assertEqual(env_path.read_text(encoding="utf-8"), original)

    def test_napcat_dir_clear_goes_through_atomic_remove(self) -> None:
        """`_save_napcat_settings` 清空目录走 remove_env_keys，不再手写不原子覆盖。"""
        from noticehub.settings_api import SettingsPostMixin

        original = "KEEP=yes\nQQ_DIGEST_NAPCAT_DIR=D:\\NapCat\n"
        with _env_file(original) as (tmp, env_path):
            host = _RecordingHost(Settings(env_file=env_path))

            SettingsPostMixin._save_napcat_settings(host, {"napcat_dir": ""})
            self.assertEqual(host.payload["status"], 200)
            self.assertEqual(env_path.read_text(encoding="utf-8"), "KEEP=yes\n")
            self.assertNotIn("NOTICEHUB_NAPCAT_DIR", os.environ)

            env_path.write_text(original, encoding="utf-8")
            with mock.patch.object(config.os, "replace", side_effect=OSError("被占用")):
                with self.assertRaises(ValueError):
                    SettingsPostMixin._save_napcat_settings(host, {"napcat_dir": ""})
            self.assertEqual(env_path.read_text(encoding="utf-8"), original)

    def test_is_loopback_host(self) -> None:
        for host in ("127.0.0.1", "127.1.2.3", "::1", "[::1]", "localhost", "", None):
            self.assertTrue(config.is_loopback_host(host), host)
        for host in ("0.0.0.0", "192.168.1.5", "example.com"):
            self.assertFalse(config.is_loopback_host(host), host)

    def test_lab_enabled_only_named_flags(self) -> None:
        settings = Settings(lab_search=True)
        self.assertTrue(settings.lab_enabled("search"))
        self.assertTrue(settings.lab_enabled(" SEARCH "))
        for name in ("replies", "events", "rules", "", "unknown", "searchx"):
            self.assertFalse(settings.lab_enabled(name))
