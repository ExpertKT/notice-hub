"""更新器测试（task-150）。

红线：**绝不真的连 GitHub**（urlopen 全部 mock），**绝不在测试里启动更新脚本**
（脚本会 kill 掉本安装目录的进程），只在文本层面断言脚本正文。
"""

import importlib.util
import json
import os
import tempfile
from datetime import datetime, timedelta
import unittest
import urllib.error
from urllib.parse import parse_qs, urlsplit
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from qq_live_digest import updater, update_sources
from qq_live_digest.version import APP_CHANNEL, APP_VERSION

_SPEC = importlib.util.spec_from_file_location(
    "packaging_launcher_updater", Path(__file__).parents[1] / "packaging" / "launcher.py"
)
launcher = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(launcher)


class FakeResponse:
    """够用的假 urlopen 响应：支持分块 read、context manager、Content-Length。"""

    def __init__(self, body, *, status=200, headers=None):
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self._pos = 0
        self.status = status
        self.headers = headers if headers is not None else {}

    def read(self, size=-1):
        if size is None or size < 0:
            chunk = self._body[self._pos:]
        else:
            chunk = self._body[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def release_payload(tag="v1.1.0", *, name="群务台 1.1.0", digest="sha256:" + "a" * 64, size=1234):
    asset = {
        "name": updater.ASSET_NAME,
        "size": size,
        "browser_download_url": "https://example.invalid/QQ-Notice-Hub.zip",
    }
    if digest is not None:
        asset["digest"] = digest
    return {
        "tag_name": tag,
        "name": name,
        "body": "修复了一堆问题\n\n- 新增：A 功能\n- 修复：B 问题",
        "html_url": "https://github.com/ExpertKT/notice-hub/releases/tag/" + tag,
        "published_at": "2026-10-11T01:00:00Z",
        "draft": False,
        "assets": [asset],
    }


class CheckUrlTests(unittest.TestCase):
    @staticmethod
    def _github_only():
        return [{
            "id": "github",
            "repo": updater.DEFAULT_REPO,
            "api_root": update_sources.GITHUB_API,
            "raw_base": update_sources.GITHUB_RAW % updater.DEFAULT_REPO,
        }]

    def test_stable_channel_uses_release_list(self):
        seen = {}

        def fake_urlopen(request, timeout=None):
            seen["url"] = request.full_url
            seen["timeout"] = timeout
            seen["headers"] = dict(request.headers)
            return FakeResponse([release_payload()])

        with patch.object(updater.update_sources, "sources", return_value=self._github_only()):
            with patch.object(updater.urllib.request, "urlopen", fake_urlopen):
                result = updater.check(channel="stable")

        self.assertTrue(result["ok"])
        self.assertIn("/releases?per_page=100", seen["url"])
        self.assertEqual(result["channel"], "stable")
        self.assertEqual(seen["timeout"], 8.0)
        self.assertEqual(seen["headers"].get("User-agent"), updater.USER_AGENT)
        self.assertEqual(result["latest"]["tag"], "v1.1.0")
        self.assertEqual(result["latest"]["version"], "1.1.0")
        self.assertEqual(result["latest"]["asset_name"], updater.ASSET_NAME)
        self.assertEqual(result["latest"]["sha256"], "a" * 64)
        self.assertEqual(result["latest"]["size"], 1234)
        self.assertEqual(result["current"]["version"], APP_VERSION)
        self.assertEqual(result["current"]["tag"], "v" + APP_VERSION)

    def test_stable_skips_newer_test_release_while_beta_keeps_it(self):
        beta = release_payload(tag="v1.1.0-beta.1", name="beta")
        beta["published_at"] = "2026-10-12T00:00:00Z"
        stable = release_payload(tag="v1.0.0", name="stable")
        stable["published_at"] = "2026-10-11T00:00:00Z"

        with patch.object(updater, "_fetch_json", return_value=[beta, stable]):
            stable_result = updater.check(channel="stable")
            beta_result = updater.check(channel="beta")

        self.assertTrue(stable_result["ok"])
        self.assertEqual(stable_result["latest"]["tag"], "v1.0.0")
        self.assertEqual(beta_result["latest"]["tag"], "v1.1.0-beta.1")

    def test_stable_selects_newest_formal_release(self):
        older = release_payload(tag="v1.0.0")
        older["published_at"] = "2026-10-11T00:00:00Z"
        newer = release_payload(tag="v2026.10.27")
        newer["published_at"] = "2026-10-12T00:00:00Z"

        with patch.object(updater, "_fetch_json", return_value=[older, newer]):
            result = updater.check(channel="stable")

        self.assertTrue(result["ok"])
        self.assertEqual(result["latest"]["tag"], "v2026.10.27")

    def test_latest_carries_name_notes_and_html_url_verbatim(self):
        payload = release_payload(tag="v1.1.0", name="群务台 1.1.0 · 更新说明")

        with patch.object(updater.urllib.request, "urlopen", lambda request, timeout=None: FakeResponse([payload])):
            result = updater.check(channel="stable")

        latest = result["latest"]
        self.assertEqual(latest["name"], "群务台 1.1.0 · 更新说明")
        # notes 必须是 body 原文，不许格式化/截断
        self.assertEqual(latest["notes"], payload["body"])
        self.assertEqual(
            latest["html_url"],
            "https://github.com/ExpertKT/notice-hub/releases/tag/v1.1.0",
        )

    def test_missing_html_url_degrades_to_empty_string(self):
        payload = release_payload()
        payload.pop("html_url")

        with patch.object(updater.urllib.request, "urlopen", lambda request, timeout=None: FakeResponse([payload])):
            result = updater.check(channel="stable")

        self.assertTrue(result["ok"])
        self.assertEqual(result["latest"]["html_url"], "")

    def test_beta_channel_scans_list_and_skips_drafts(self):
        draft = release_payload(tag="draftnot", name="draft")
        draft["draft"] = True
        seen = {}

        def fake_urlopen(request, timeout=None):
            seen["url"] = request.full_url
            return FakeResponse([draft, release_payload(tag="v9.9.9", name="beta 版")])

        with patch.object(updater.update_sources, "sources", return_value=self._github_only()):
            with patch.object(updater.urllib.request, "urlopen", fake_urlopen):
                result = updater.check(channel="beta")

        self.assertIn("/releases?per_page=100", seen["url"])
        self.assertEqual(result["latest"]["tag"], "v9.9.9")
        self.assertEqual(result["latest"]["name"], "beta 版")
        self.assertTrue(result["has_update"])

    def test_beta_channel_picks_the_newest_published_release(self):
        """GitHub 的 /releases 按 created_at（草稿建立时间）排，不是发布顺序：
        先建草稿、后来才发布的版本会排在后面，不能让它把真正最新的压住
        （v1.0.0 就被更早发布的 v2026.10.27 压住过）。"""
        listed_first = release_payload(tag="v2026.10.27", name="老日历版")
        listed_first["published_at"] = "2026-10-09T13:37:34Z"
        published_later = release_payload(tag="v1.0.0", name="新品牌版")
        published_later["published_at"] = "2026-10-10T15:00:00Z"

        def fake_urlopen(request, timeout=None):
            return FakeResponse([listed_first, published_later])

        with patch.object(updater.urllib.request, "urlopen", fake_urlopen):
            result = updater.check(channel="beta")

        self.assertEqual(result["latest"]["tag"], "v1.0.0")
        self.assertEqual(result["latest"]["name"], "新品牌版")

    @staticmethod
    def _beta_catalog():
        releases = []
        for index in range(21):
            release = release_payload(tag="v2026.10.%d" % (index + 1))
            release["published_at"] = "2026-10-%02dT00:00:00Z" % (index + 1)
            if index == 12:
                release["tag_name"] = "v1.0.0"
                release["published_at"] = "2026-10-22T00:00:00Z"
            releases.append(release)
        return releases

    def test_beta_scans_beyond_first_ten_for_latest_published_release(self):
        releases = self._beta_catalog()
        with patch.object(updater, "_fetch_json", return_value=releases):
            result = updater.check(channel="beta")

        self.assertEqual(result["latest"]["tag"], "v1.0.0")

    def test_beta_requests_at_least_100_and_receives_beyond_first_ten(self):
        releases = self._beta_catalog()
        seen = {}

        def fake_fetch_json(url, timeout):
            seen["url"] = url
            page_size = int(parse_qs(urlsplit(url).query)["per_page"][0])
            return releases if page_size >= 100 else releases[:10]

        with patch.object(updater.update_sources, "sources", return_value=self._github_only()):
            with patch.object(updater, "_fetch_json", fake_fetch_json):
                result = updater.check(channel="beta")

        page_size = int(parse_qs(urlsplit(seen["url"]).query)["per_page"][0])
        self.assertGreaterEqual(page_size, 100)
        self.assertEqual(result["latest"]["tag"], "v1.0.0")

    def test_beta_uses_last_listed_release_when_its_publish_time_is_newest(self):
        listed_first = release_payload(tag="v2026.10.27")
        listed_first["published_at"] = "2026-10-09T13:37:34Z"
        listed_last = release_payload(tag="v1.0.0")
        listed_last["published_at"] = "2026-10-09T15:19:49Z"

        with patch.object(updater, "_fetch_json", return_value=[listed_first, listed_last]):
            result = updater.check(channel="beta")

        self.assertEqual(result["latest"]["tag"], "v1.0.0")

    def test_beta_treats_missing_or_malformed_publish_times_as_oldest(self):
        published = release_payload(tag="v1.0.0")
        published["published_at"] = "2026-10-09T15:19:49Z"
        for missing_time in (None, "", "not-a-timestamp"):
            with self.subTest(published_at=missing_time):
                unparseable = release_payload(tag="v9.9.9")
                unparseable["published_at"] = missing_time
                with patch.object(
                    updater, "_fetch_json", return_value=[published, unparseable]
                ):
                    result = updater.check(channel="beta")

                self.assertTrue(result["ok"])
                self.assertEqual(result["latest"]["tag"], "v1.0.0")

    def test_default_channel_comes_from_version_module(self):
        seen = {}

        def fake_urlopen(request, timeout=None):
            seen["url"] = request.full_url
            return FakeResponse([release_payload()])

        clean_env = {k: v for k, v in updater.os.environ.items() if k != "QQ_DIGEST_UPDATE_CHANNEL"}
        with patch.dict("os.environ", clean_env, clear=True):
            with patch.object(updater.update_sources, "sources", return_value=self._github_only()):
                with patch.object(updater.urllib.request, "urlopen", fake_urlopen):
                    result = updater.check()

        self.assertEqual(result["channel"], APP_CHANNEL)
        self.assertIn("/releases?per_page=100", seen["url"])


class ChannelResolutionTests(unittest.TestCase):
    """契约：显式参数 > 环境变量 > version.APP_CHANNEL；大小写不敏感；非法值当 beta。"""

    def test_explicit_argument_wins_over_environment(self):
        with patch.dict("os.environ", {"QQ_DIGEST_UPDATE_CHANNEL": "stable"}):
            self.assertEqual(updater.resolve_channel("beta"), "beta")

    def test_environment_wins_over_app_channel(self):
        with patch.dict("os.environ", {"QQ_DIGEST_UPDATE_CHANNEL": "stable"}):
            self.assertEqual(updater.resolve_channel(), "stable")
            self.assertEqual(updater.resolve_channel(None), "stable")

    def test_environment_is_case_insensitive(self):
        with patch.dict("os.environ", {"QQ_DIGEST_UPDATE_CHANNEL": "BeTa"}):
            self.assertEqual(updater.resolve_channel(), "beta")
        with patch.dict("os.environ", {"QQ_DIGEST_UPDATE_CHANNEL": "OFF"}):
            self.assertEqual(updater.resolve_channel(), "off")

    def test_illegal_values_fall_back_to_beta(self):
        with patch.dict("os.environ", {"QQ_DIGEST_UPDATE_CHANNEL": "nightly"}):
            self.assertEqual(updater.resolve_channel(), "beta")
        self.assertEqual(updater.resolve_channel("nightly"), "beta")

    def test_empty_environment_value_uses_app_channel(self):
        with patch.dict("os.environ", {"QQ_DIGEST_UPDATE_CHANNEL": "   "}):
            self.assertEqual(updater.resolve_channel(), updater._normalize_channel(APP_CHANNEL))

    def test_off_check_returns_empty_result_without_network(self):
        def boom(*args, **kwargs):
            raise AssertionError("off 通道不许联网")

        with patch.object(updater.urllib.request, "urlopen", boom):
            result = updater.check(channel="off")
        self.assertTrue(result["ok"])
        self.assertFalse(result["has_update"])
        self.assertEqual(result["error"], "")
        self.assertEqual(result["channel"], "off")
        self.assertEqual(result["latest"]["tag"], "")

    def test_environment_off_is_honoured_by_check(self):
        def boom(*args, **kwargs):
            raise AssertionError("off 通道不许联网")

        with patch.dict("os.environ", {"QQ_DIGEST_UPDATE_CHANNEL": "off"}):
            with patch.object(updater.urllib.request, "urlopen", boom):
                result = updater.check()
        self.assertEqual(result["channel"], "off")
        self.assertTrue(result["ok"])
        self.assertFalse(result["has_update"])


class CheckDegradationTests(unittest.TestCase):
    def test_returns_dict_on_http_403_without_raising(self):
        error = urllib.error.HTTPError("https://api.github.com/x", 403, "Forbidden", {}, None)
        with patch.object(updater.urllib.request, "urlopen", side_effect=error):
            result = updater.check(channel="stable")
        self.assertFalse(result["ok"])
        self.assertFalse(result["has_update"])
        self.assertIn("403", result["error"])
        self.assertEqual(result["latest"]["tag"], "")
        self.assertTrue(result["checked_at"])

    def test_returns_dict_on_http_404(self):
        error = urllib.error.HTTPError("https://api.github.com/x", 404, "Not Found", {}, None)
        with patch.object(updater.urllib.request, "urlopen", side_effect=error):
            result = updater.check(channel="stable")
        self.assertFalse(result["ok"])
        self.assertIn("404", result["error"])

    def test_returns_dict_on_timeout(self):
        with patch.object(updater.urllib.request, "urlopen", side_effect=TimeoutError("timed out")):
            result = updater.check(channel="stable")
        self.assertFalse(result["ok"])
        self.assertIn("超时", result["error"])

    def test_returns_dict_on_connection_error(self):
        error = urllib.error.URLError(OSError("目标计算机积极拒绝"))
        with patch.object(updater.urllib.request, "urlopen", side_effect=error):
            result = updater.check(channel="stable")
        self.assertFalse(result["ok"])
        self.assertIn("网络", result["error"])

    def test_returns_dict_on_broken_json(self):
        with patch.object(updater.urllib.request, "urlopen", lambda request, timeout=None: FakeResponse(b"<html>nope")):
            result = updater.check(channel="stable")
        self.assertFalse(result["ok"])
        self.assertIn("JSON", result["error"])

    def test_returns_dict_on_unexpected_exception(self):
        with patch.object(updater.urllib.request, "urlopen", side_effect=ValueError("boom")):
            result = updater.check(channel="stable")
        self.assertFalse(result["ok"])
        self.assertIn("boom", result["error"])

    def test_ok_true_but_error_explains_missing_asset(self):
        payload = release_payload()
        payload["assets"] = []
        with patch.object(updater.urllib.request, "urlopen", lambda request, timeout=None: FakeResponse([payload])):
            result = updater.check(channel="stable")
        self.assertTrue(result["ok"])
        self.assertEqual(result["latest"]["asset_name"], "")
        self.assertEqual(result["latest"]["sha256"], "")
        self.assertIn("附件", result["error"])

    def test_ok_true_but_error_explains_missing_digest(self):
        payload = release_payload(digest=None)
        with patch.object(updater.urllib.request, "urlopen", lambda request, timeout=None: FakeResponse([payload])):
            result = updater.check(channel="stable")
        self.assertTrue(result["ok"])
        self.assertEqual(result["latest"]["sha256"], "")
        self.assertIn("sha256", result["error"])


class MirrorCheckTests(unittest.TestCase):
    @staticmethod
    def _manifest(tag="v1.2.0", version="1.2.0", sha="b" * 64):
        return {
            "version": 1,
            "items": [{
                "tag": tag,
                "version": version,
                "name": "notice-hub " + version,
                "notes": "mirror release",
                "published_at": "2026-10-22T00:00:00Z",
                "asset_name": updater.ASSET_NAME,
                "size": 12,
                "sha256": sha,
                "download_url": "https://gitee.com/ExpertKT/notice-hub/releases/download/" + tag + "/" + updater.ASSET_NAME,
            }],
        }

    def test_github_failure_falls_back_to_manifest(self):
        seen = []
        github_error = urllib.error.URLError("offline")

        def fake_urlopen(request, timeout=None):
            seen.append(request.full_url)
            if "api.github.com" in request.full_url:
                raise github_error
            return FakeResponse(self._manifest())

        with patch.dict("os.environ", {"QQ_DIGEST_UPDATE_MIRROR": "ExpertKT/notice-hub"}, clear=True):
            with patch.object(updater.urllib.request, "urlopen", fake_urlopen):
                result = updater.check(channel="beta")

        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "gitee")
        self.assertTrue(result["has_update"])
        self.assertEqual(result["latest"]["tag"], "v1.2.0")
        self.assertEqual([item["id"] for item in result["sources_tried"]], ["github", "gitee"])
        self.assertFalse(result["sources_tried"][0]["ok"])
        self.assertTrue(result["sources_tried"][1]["ok"])
        self.assertIn("update-manifest.json", seen[-1])

    def test_both_sources_fail_and_error_names_both(self):
        error = urllib.error.URLError("offline")
        with patch.dict("os.environ", {"QQ_DIGEST_UPDATE_MIRROR": "ExpertKT/notice-hub"}, clear=True):
            with patch.object(updater.urllib.request, "urlopen", side_effect=error):
                result = updater.check(channel="beta")
        self.assertFalse(result["ok"])
        self.assertIn("github", result["error"])
        self.assertIn("gitee", result["error"])
        self.assertEqual([item["id"] for item in result["sources_tried"]], ["github", "gitee"])

    def test_invalid_manifest_falls_back_without_raising(self):
        def fake_urlopen(request, timeout=None):
            if "api.github.com" in request.full_url:
                raise urllib.error.URLError("offline")
            return FakeResponse({"version": 1, "items": [{"tag": "v1.2.0"}]})

        with patch.dict("os.environ", {"QQ_DIGEST_UPDATE_MIRROR": "ExpertKT/notice-hub"}, clear=True):
            with patch.object(updater.urllib.request, "urlopen", fake_urlopen):
                result = updater.check(channel="beta")
        self.assertFalse(result["ok"])
        self.assertIn("gitee", result["error"])
        self.assertFalse(result["sources_tried"][1]["ok"])

    def test_manifest_json_and_types_are_rejected_without_raising(self):
        for bad_body in (b"not json", {"version": 1, "items": "wrong type"}):
            with self.subTest(body=bad_body):
                def fake_urlopen(request, timeout=None):
                    if "api.github.com" in request.full_url:
                        raise urllib.error.URLError("offline")
                    return FakeResponse(bad_body) if isinstance(bad_body, bytes) else FakeResponse(bad_body)

                with patch.dict("os.environ", {"QQ_DIGEST_UPDATE_MIRROR": "ExpertKT/notice-hub"}, clear=True):
                    with patch.object(updater.urllib.request, "urlopen", fake_urlopen):
                        result = updater.check(channel="beta")
                self.assertFalse(result["ok"])
                self.assertIn("gitee", result["error"])

    def test_stable_manifest_excludes_prerelease_tags(self):
        manifest = self._manifest(tag="v1.3.0-beta.1", version="1.3.0-beta.1")
        manifest["items"].append(self._manifest(tag="v1.2.0", version="1.2.0")["items"][0])

        def fake_urlopen(request, timeout=None):
            if "api.github.com" in request.full_url:
                raise urllib.error.URLError("offline")
            return FakeResponse(manifest)

        with patch.dict("os.environ", {"QQ_DIGEST_UPDATE_MIRROR": "ExpertKT/notice-hub"}, clear=True):
            with patch.object(updater.urllib.request, "urlopen", fake_urlopen):
                result = updater.check(channel="stable")
        self.assertTrue(result["ok"])
        self.assertEqual(result["latest"]["tag"], "v1.2.0")

    def test_same_tag_gitee_asset_is_added_as_download_fallback(self):
        github = release_payload(tag="v1.2.0", digest="sha256:" + "a" * 64)
        github["assets"][0]["browser_download_url"] = "https://github.com/asset.zip"
        mirror = self._manifest(tag="v1.2.0", version="1.2.0", sha="a" * 64)

        def fake_urlopen(request, timeout=None):
            if "api.github.com" in request.full_url:
                return FakeResponse([github])
            return FakeResponse(mirror)

        with patch.dict("os.environ", {"QQ_DIGEST_UPDATE_MIRROR": "ExpertKT/notice-hub"}, clear=True):
            with patch.object(updater.urllib.request, "urlopen", fake_urlopen):
                result = updater.check(channel="beta")
        self.assertEqual(result["source"], "github")
        self.assertEqual(result["latest"]["sha256"], "a" * 64)
        self.assertEqual(result["latest"]["fallbacks"], [mirror["items"][0]["download_url"]])


class HasUpdateTests(unittest.TestCase):
    def _check(self, tag):
        payload = release_payload(tag=tag)
        with patch.object(updater.urllib.request, "urlopen", lambda request, timeout=None: FakeResponse([payload])):
            return updater.check(channel="beta")

    def test_different_formal_tag_means_update(self):
        self.assertTrue(self._check("v" + APP_VERSION + ".1")["has_update"])

    def test_remote_older_beta_does_not_update(self):
        with patch.object(updater, "current_version", return_value="1.1.0-beta.8"):
            self.assertFalse(self._check("v1.1.0-beta.6")["has_update"])

    def test_remote_newer_beta_updates(self):
        with patch.object(updater, "current_version", return_value="1.1.0-beta.6"):
            self.assertTrue(self._check("v1.1.0-beta.8")["has_update"])

    def test_unparseable_tag_does_not_update(self):
        result = self._check("release-latest")
        self.assertFalse(result["has_update"])

    def test_same_tag_means_no_update(self):
        self.assertFalse(self._check("v" + APP_VERSION)["has_update"])

    def test_tag_without_leading_v_is_normalised(self):
        self.assertEqual(self._check(APP_VERSION)["latest"]["version"], APP_VERSION)

    def test_missing_current_version_does_not_update(self):
        payload = release_payload(tag="v1.1.0")
        with patch.object(updater, "current_version", return_value=""):
            with patch.object(
                updater.urllib.request,
                "urlopen",
                lambda request, timeout=None: FakeResponse([payload]),
            ):
                result = updater.check(channel="stable")
        self.assertFalse(result["has_update"])

    def test_current_version_helper(self):
        self.assertEqual(updater.current_version(), APP_VERSION)


class DownloadTests(unittest.TestCase):
    def _info(self, digest, tag="v1.1.0"):
        return {
            "tag": tag,
            "sha256": digest,
            "download_url": "https://example.invalid/QQ-Notice-Hub.zip",
        }

    def test_download_writes_named_file_and_verifies_sha256(self):
        import hashlib

        body = b"hello notice hub"
        digest = hashlib.sha256(body).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            response = FakeResponse(body, headers={"Content-Length": str(len(body))})
            with patch.object(updater.urllib.request, "urlopen", lambda request, timeout=None: response):
                result = updater.download(self._info(digest), Path(tmp))
            self.assertTrue(result["ok"], result["error"])
            self.assertEqual(result["bytes"], len(body))
            self.assertEqual(result["sha256"], digest)
            path = Path(result["path"])
            self.assertEqual(path.name, "QQ-Notice-Hub-v1.1.0.zip")
            self.assertEqual(path.read_bytes(), body)

    def test_download_deletes_file_when_sha256_mismatches(self):
        with tempfile.TemporaryDirectory() as tmp:
            response = FakeResponse(b"tampered", headers={"Content-Length": "8"})
            with patch.object(updater.urllib.request, "urlopen", lambda request, timeout=None: response):
                result = updater.download(self._info("b" * 64), Path(tmp))
            self.assertFalse(result["ok"])
            self.assertIn("sha256", result["error"])
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_download_without_url_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = updater.download({"tag": "v1"}, Path(tmp))
            self.assertFalse(result["ok"])
            self.assertIn("下载地址", result["error"])

    def test_download_survives_on_progress_callback_that_raises(self):
        import hashlib

        body = b"x" * (300 * 1024)
        digest = hashlib.sha256(body).hexdigest()
        calls = []

        def broken_progress(done, total):
            calls.append(done)
            raise RuntimeError("UI 崩了")

        with tempfile.TemporaryDirectory() as tmp:
            response = FakeResponse(body, headers={"Content-Length": str(len(body))})
            with patch.object(updater.urllib.request, "urlopen", lambda request, timeout=None: response):
                result = updater.download(self._info(digest), Path(tmp), on_progress=broken_progress)
            self.assertTrue(result["ok"], result["error"])
            self.assertTrue(calls)

    def test_download_reports_network_error_and_removes_partial_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(updater.urllib.request, "urlopen", side_effect=TimeoutError("slow")):
                result = updater.download(self._info("c" * 64), Path(tmp))
            self.assertFalse(result["ok"])
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_download_filename_cannot_escape_dest_dir(self):
        import hashlib

        body = b"zip"
        digest = hashlib.sha256(body).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "updates"
            response = FakeResponse(body, headers={"Content-Length": "3"})
            with patch.object(updater.urllib.request, "urlopen", lambda request, timeout=None: response):
                result = updater.download(self._info(digest, tag="../../evil"), dest)
            self.assertTrue(result["ok"], result["error"])
            self.assertEqual(Path(result["path"]).parent, dest)
            name = Path(result["path"]).name
            self.assertNotIn("/", name)
            self.assertNotIn("\\", name)

    def test_download_uses_mirror_after_primary_network_failure_and_verifies_hash(self):
        import hashlib

        body = b"mirror archive"
        digest = hashlib.sha256(body).hexdigest()
        seen = []

        def fake_urlopen(request, timeout=None):
            seen.append(request.full_url)
            if request.full_url.endswith("primary.zip"):
                raise urllib.error.URLError("offline")
            return FakeResponse(body, headers={"Content-Length": str(len(body))})

        with tempfile.TemporaryDirectory() as tmp:
            info = self._info(digest)
            info["download_url"] = "https://github.example/primary.zip"
            info["fallbacks"] = ["https://gitee.example/mirror.zip"]
            with patch.object(updater.urllib.request, "urlopen", fake_urlopen):
                result = updater.download(info, Path(tmp))
            self.assertTrue(result["ok"], result["error"])
            self.assertEqual(result["sha256"], digest)
            self.assertEqual(Path(result["path"]).read_bytes(), body)
        self.assertEqual(seen, ["https://github.example/primary.zip", "https://gitee.example/mirror.zip"])

    def test_download_uses_mirror_after_content_length_mismatch(self):
        import hashlib

        body = b"complete zip"
        digest = hashlib.sha256(body).hexdigest()
        responses = [
            FakeResponse(b"short", headers={"Content-Length": "20"}),
            FakeResponse(body, headers={"Content-Length": str(len(body))}),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            info = self._info(digest)
            info["fallbacks"] = ["https://gitee.example/mirror.zip"]
            with patch.object(updater.urllib.request, "urlopen", side_effect=responses):
                result = updater.download(info, Path(tmp))
            self.assertTrue(result["ok"], result["error"])
            self.assertEqual(Path(result["path"]).read_bytes(), body)

    def test_download_clears_partial_file_when_all_urls_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = self._info("a" * 64)
            info["fallbacks"] = ["https://gitee.example/mirror.zip"]
            first = FakeResponse(b"short", headers={"Content-Length": "30"})
            with patch.object(updater.urllib.request, "urlopen", side_effect=[first, urllib.error.URLError("offline")]):
                result = updater.download(info, Path(tmp))
            self.assertFalse(result["ok"])
            self.assertIn("主下载源", result["error"])
            self.assertIn("备用源", result["error"])
            self.assertEqual(list(Path(tmp).iterdir()), [])


class PendingUpdateTests(unittest.TestCase):
    def test_pending_record_round_trips_and_can_be_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "update.zip"
            archive.write_bytes(b"zip")
            result = updater.mark_pending(root / "data", zip_path=archive, version="2.0.0")
            self.assertEqual(result, {"ok": True, "error": ""})
            record = updater.pending_update(root / "data")
            self.assertEqual(record["zip"], str(archive.resolve()))
            self.assertEqual(record["version"], "2.0.0")
            self.assertTrue(record["at"])
            updater.clear_pending(root / "data")
            self.assertIsNone(updater.pending_update(root / "data"))

    def test_missing_or_malformed_pending_record_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            self.assertIsNone(updater.pending_update(data))
            updates = data / "updates"
            updates.mkdir()
            (updates / updater.PENDING_UPDATE).write_text("not json", encoding="utf-8")
            self.assertIsNone(updater.pending_update(data))


class ApplyAsyncTests(unittest.TestCase):
    def _make_zip(self, tmp):
        archive = Path(tmp) / "QQ-Notice-Hub-v1.1.0.zip"
        archive.write_bytes(b"PK\x03\x04 pretend zip")
        return archive

    @unittest.skipUnless(os.name == "nt", "PowerShell apply script is Windows-only")
    def test_script_contains_required_behaviour_and_flag_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            root = tmp / "install"
            data = tmp / "data"
            root.mkdir()
            archive = self._make_zip(tmp)
            with patch.object(updater.subprocess, "Popen") as popen:
                result = updater.apply_async(root, archive, 4321, data_dir=data)
            self.assertTrue(result["ok"], result["error"])
            script_path = Path(result["script"])
            try:
                text = script_path.read_text(encoding="utf-8-sig")
                # 1) 先请求托盘优雅退出，再等目标进程；超时才按安装目录兜底。
                self.assertIn("Start-Process -FilePath $launcher -ArgumentList '--quit'", text)
                self.assertLess(text.index("--quit"), text.index("AddSeconds(60)"))
                self.assertIn("Get-Process -Id $pidToWait", text)
                self.assertIn("AddSeconds(60)", text)
                self.assertIn("优雅退出等待超时", text)
                self.assertIn("Start-Sleep -Milliseconds 500", text)
                # 2) 只杀本安装目录下的进程，不许按镜像名杀
                self.assertIn("Get-CimInstance Win32_Process", text)
                self.assertIn("StartsWith($prefix", text)
                self.assertIn("OrdinalIgnoreCase", text)
                self.assertIn("$root.TrimEnd('\\') + '\\'", text)
                self.assertIn("Stop-Process", text)
                # 不许按镜像名杀进程（taskkill /IM 或 Get-Process -Name）
                self.assertNotIn("/im ", text.lower())
                self.assertNotIn("get-process -name", text.lower())
                # 3) 失败也要把启动器拉起来
                self.assertIn("Expand-Archive -LiteralPath $zip -DestinationPath $root -Force", text)
                self.assertIn("Start-Process -FilePath $launcher", text)
                self.assertIn("$launcher = " + updater._ps_quote(root / updater.LAUNCHER_EXE), text)
                # 4) 全程写日志，最后一行是「完成」
                self.assertIn("update.log", text)
                self.assertIn("Write-UpdateLog", text)
                self.assertIn("Write-UpdateLog '完成'", text)
                # 只读属性清理（历史坑 C-03）与无条件 flag 清理
                self.assertIn("FileAttributes]::ReadOnly", text)
                self.assertIn("try { Remove-Item -LiteralPath $flag -Force -ErrorAction SilentlyContinue } catch { }", text)
                self.assertNotIn("if ($started) {\n    try { Remove-Item", text)
            finally:
                script_path.unlink(missing_ok=True)
            flag = data / "updates" / "applying.flag"
            self.assertTrue(flag.is_file())
            popen.assert_called_once()
            argv = popen.call_args[0][0]
            self.assertEqual(argv[0], "powershell.exe")
            self.assertIn("-File", argv)
            self.assertEqual(argv[-1], str(script_path))

    def test_posix_apply_returns_manual_instructions_without_writing_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            root = tmp / "install"
            data = tmp / "data"
            root.mkdir()
            archive = self._make_zip(tmp)
            updates = data / "updates"
            updates.mkdir(parents=True)
            release_url = "https://gitee.com/ExpertKT/notice-hub-mirror/releases/tag/v1.1.0"
            (updates / "state.json").write_text(
                json.dumps({"update_result": {"latest": {"html_url": release_url}}}),
                encoding="utf-8",
            )
            with patch.object(updater, "os", SimpleNamespace(name="posix")), patch.object(
                updater.subprocess, "Popen"
            ) as popen, patch.object(updater.tempfile, "gettempdir", return_value=str(tmp)):
                result = updater.apply_async(root, archive, 1, data_dir=data)
                self.assertFalse(result["ok"])
                self.assertEqual(result["script"], "")
                self.assertIn("请手动下载安装", result["error"])
                self.assertIn(release_url, result["error"])
                self.assertIn(str(archive), result["error"])
                self.assertFalse((data / "updates" / "applying.flag").exists())
                self.assertEqual(list(tmp.glob("notice-hub-update-*.ps1")), [])
                popen.assert_not_called()

    def test_missing_zip_fails_without_writing_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            root = tmp / "install"
            data = tmp / "data"
            root.mkdir()
            with patch.object(updater.subprocess, "Popen") as popen:
                result = updater.apply_async(root, tmp / "nope.zip", 1, data_dir=data)
            self.assertFalse(result["ok"])
            self.assertIn("不存在", result["error"])
            popen.assert_not_called()
            self.assertFalse((data / "updates" / "applying.flag").exists())

    @unittest.skipUnless(os.name == "nt", "PowerShell launch is Windows-only")
    def test_launch_failure_clears_flag_so_watchdog_can_recover(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            root = tmp / "install"
            data = tmp / "data"
            root.mkdir()
            archive = self._make_zip(tmp)
            with patch.object(updater.subprocess, "Popen", side_effect=OSError("no powershell")):
                result = updater.apply_async(root, archive, 1, data_dir=data)
            self.assertFalse(result["ok"])
            self.assertFalse((data / "updates" / "applying.flag").exists())


class LauncherWatchdogUpdateTests(unittest.TestCase):
    class FakeProcess:
        def __init__(self, polls, pid=4321):
            self.polls = iter(polls)
            self.returncode = None
            self.pid = pid

        def poll(self):
            value = next(self.polls)
            if value is not None:
                self.returncode = value
            return value

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

    def _host(self, tmp, process):
        from qq_live_digest import hosting

        settings = type("Settings", (), {"data_dir": Path(tmp) / "data"})()
        with patch.object(hosting, "hosting_status", return_value={"hosting_active": False}):
            host = launcher.TrayHost(Path(tmp), Path(tmp) / ".env", settings, process, "http://127.0.0.1:1")
        host._notify = lambda message: None
        return host

    def test_running_service_is_untouched_when_flag_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host(tmp, self.FakeProcess([None]))
            with patch.object(launcher.threading, "Thread") as thread:
                self.assertTrue(host.watchdog_tick())
                thread.assert_not_called()

    def test_fresh_applying_flag_stops_watchdog(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            updates = data / "updates"
            updates.mkdir(parents=True)
            at = (datetime.now() - timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S")
            (updates / "applying.flag").write_text("pid=1\nat=" + at + "\n", encoding="utf-8")
            host = self._host(tmp, self.FakeProcess([1]))
            with patch.object(launcher.threading, "Thread") as thread:
                self.assertFalse(host.watchdog_tick())
                thread.assert_not_called()
            self.assertTrue(host._watchdog_stop.is_set())
            self.assertEqual(host.restart_failures, 0)
            self.assertTrue((updates / "applying.flag").exists())

    def test_stale_applying_flag_is_removed_and_restart_is_scheduled(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            updates = data / "updates"
            updates.mkdir(parents=True)
            at = (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
            flag = updates / "applying.flag"
            flag.write_text("pid=1\nat=" + at + "\n", encoding="utf-8")
            host = self._host(tmp, self.FakeProcess([1]))
            with patch.object(launcher.threading, "Thread") as thread:
                self.assertFalse(host.watchdog_tick())
                thread.assert_called_once()
                thread.return_value.start.assert_called_once()
                self.assertEqual(thread.call_args.kwargs["target"], host._restart_after_delay)
            self.assertFalse(host._watchdog_stop.is_set())
            self.assertEqual(host.restart_failures, 1)
            self.assertFalse(flag.exists())


class LauncherUpdateCheckTests(unittest.TestCase):
    def test_channel_off_skips_the_whole_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("QQ_DIGEST_UPDATE_CHANNEL=off\n", encoding="utf-8")
            self.assertFalse(launcher._update_check_enabled(env_file))
            self.assertEqual(launcher._update_channel(env_file), "off")
            env_file.write_text("QQ_DIGEST_UPDATE_CHANNEL=OFF\n", encoding="utf-8")
            self.assertFalse(launcher._update_check_enabled(env_file))

    def test_channel_beta_and_stable_are_forwarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("QQ_DIGEST_UPDATE_CHANNEL=beta\n", encoding="utf-8")
            self.assertTrue(launcher._update_check_enabled(env_file))
            self.assertEqual(launcher._update_channel(env_file), "beta")
            env_file.write_text("QQ_DIGEST_UPDATE_CHANNEL=stable\n", encoding="utf-8")
            self.assertEqual(launcher._update_channel(env_file), "stable")
            env_file.write_text("QQ_DIGEST_UPDATE_CHANNEL=nonsense\n", encoding="utf-8")
            self.assertIsNone(launcher._update_channel(env_file))

    def test_new_version_is_announced_once_and_state_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            info = {
                "ok": True,
                "checked_at": "2026-10-11 13:00:00",
                "has_update": True,
                "error": "",
                "latest": {"tag": "v1.1.0", "name": "群务台 1.1.0"},
            }
            shown = []
            with patch.object(updater, "check", return_value=info):
                # _notify_info 现在带 title 关键字（更新通知显式传标题）。这个替身必须收得下它，
                # 否则调用抛 TypeError 被吞掉、shown 变空 —— CI 上就是这么红的。
                with patch.object(launcher, "_notify_info", lambda root, message, title="": shown.append((message, title))):
                    launcher._update_check_worker(root, data, "beta")
                    launcher._update_check_worker(root, data, "beta")
            self.assertEqual(len(shown), 1)
            self.assertIn("v1.1.0", shown[0][0])
            self.assertEqual(shown[0][1], "群务台 有新版本")
            state = json.loads((data / "updates" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["notified_tag"], "v1.1.0")
            self.assertEqual(state["last_check"], "2026-10-11 13:00:00")
            self.assertTrue(state["last_result"]["has_update"])

    def test_check_failure_is_swallowed_and_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            with patch.object(updater, "check", side_effect=RuntimeError("boom")):
                launcher._update_check_worker(root, data, None)  # 不许抛
            self.assertFalse((data / "updates" / "state.json").exists())

    @unittest.skipUnless(os.name == "nt", "Windows MessageBoxW")
    def test_notify_info_never_raises(self):
        # 必须把信息框换成假的：MessageBoxW 是模态的，真弹出来会把整个测试进程
        # 挂死到人去点确定为止（2026-10-11 全量测试两次卡死在 16:49 就是这个）。
        with tempfile.TemporaryDirectory() as tmp:
            with patch("ctypes.windll.user32.MessageBoxW", return_value=1) as box:
                launcher._notify_info(Path(tmp), "发现新版本 v1.1.0")
        box.assert_called_once()


if __name__ == "__main__":
    unittest.main()
