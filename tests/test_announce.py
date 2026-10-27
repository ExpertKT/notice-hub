"""Tests for qq_live_digest/announce.py — offline only, mock all network calls."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import urllib.error

from qq_live_digest.announce import fetch, parse, active, _ver_tuple, _version_in_range


class VersionTupleTest(unittest.TestCase):
    """Test version tuple parsing and comparison."""
    
    def test_release_version(self):
        """Release versions should parse to numeric tuple with (1,) marker for sorting."""
        self.assertEqual(_ver_tuple("1.0.0"), (1, 0, 0, 1))
        self.assertEqual(_ver_tuple("1.1.0"), (1, 1, 0, 1))
        self.assertEqual(_ver_tuple("2.3.4"), (2, 3, 4, 1))
    
    def test_prerelease_version(self):
        """Pre-release versions sort before release (pre-release gets 0 marker, release gets 1)."""
        pre = _ver_tuple("1.0.0-beta.1")
        rel = _ver_tuple("1.0.0")
        self.assertLess(pre, rel)  # (1,0,0,0,'beta',1) < (1,0,0,1)
    
    def test_version_comparison(self):
        """Various version comparisons."""
        self.assertLess(_ver_tuple("1.0.0-beta"), _ver_tuple("1.0.0"))
        self.assertLess(_ver_tuple("1.0.0-rc.1"), _ver_tuple("1.0.0-rc.2"))
        self.assertLess(_ver_tuple("1.0.0"), _ver_tuple("1.0.1"))
    
    def test_empty_version(self):
        """Empty or invalid versions return empty tuple."""
        self.assertEqual(_ver_tuple(""), ())
        self.assertEqual(_ver_tuple(None), ())
        self.assertEqual(_ver_tuple("invalid"), ())


class VersionRangeTest(unittest.TestCase):
    """Test min_version / max_version filtering."""
    
    def test_no_limits(self):
        """Empty min/max means all versions pass."""
        self.assertTrue(_version_in_range("1.0.0", "", ""))
        self.assertTrue(_version_in_range("2.3.4", "", ""))
    
    def test_min_version_check(self):
        """Only versions >= min_version pass."""
        self.assertFalse(_version_in_range("1.0.0", "1.0.1", ""))
        self.assertTrue(_version_in_range("1.0.1", "1.0.0", ""))
        self.assertTrue(_version_in_range("1.0.0", "1.0.0", ""))
    
    def test_max_version_check(self):
        """Only versions <= max_version pass."""
        self.assertFalse(_version_in_range("1.1.0", "", "1.0.0"))
        self.assertTrue(_version_in_range("1.0.0", "", "1.1.0"))
        self.assertTrue(_version_in_range("1.0.0", "", "1.0.0"))
    
    def test_range_both_limits(self):
        """Version must fall within [min, max]."""
        self.assertTrue(_version_in_range("1.0.5", "1.0.0", "1.1.0"))
        self.assertFalse(_version_in_range("1.2.0", "1.0.0", "1.1.0"))
        self.assertFalse(_version_in_range("0.9.0", "1.0.0", "1.1.0"))


class ParseTest(unittest.TestCase):
    """Test parse function — filtering and sorting."""
    
    def test_empty_payload(self):
        """Empty or non-dict payloads return []."""
        self.assertEqual(parse({}, current_version="1.0.0"), [])
        self.assertEqual(parse({"items": []}, current_version="1.0.0"), [])
        self.assertEqual(parse(None, current_version="1.0.0"), [])
        self.assertEqual(parse("not a dict", current_version="1.0.0"), [])
    
    def test_skip_malformed_items(self):
        """Skip items missing id or title."""
        payload = {
            "items": [
                {"id": "item1", "title": "Good"},  # OK
                {"id": "item2"},  # Missing title
                {"title": "No ID"},  # Missing id
                {"id": "", "title": "Empty id"},  # Empty id
                "not a dict",  # Not dict
                None,
            ]
        }
        result = parse(payload, current_version="1.0.0")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "item1")
    
    def test_seen_required_sticky(self):
        """sticky=true → seen_required=true."""
        payload = {
            "items": [{"id": "a", "title": "Title", "sticky": True}]
        }
        result = parse(payload, current_version="1.0.0")
        self.assertTrue(result[0]["seen_required"])
    
    def test_seen_required_critical(self):
        """level=critical → seen_required=true."""
        payload = {
            "items": [{"id": "a", "title": "Title", "level": "critical"}]
        }
        result = parse(payload, current_version="1.0.0")
        self.assertTrue(result[0]["seen_required"])
    
    def test_seen_required_info(self):
        """level=info, sticky=false → seen_required=false."""
        payload = {
            "items": [{"id": "a", "title": "Title", "level": "info", "sticky": False}]
        }
        result = parse(payload, current_version="1.0.0")
        self.assertFalse(result[0]["seen_required"])
    
    def test_sort_by_date_descending(self):
        """Items sorted by date descending (most recent first)."""
        payload = {
            "items": [
                {"id": "a", "title": "A", "date": "2026-10-09"},
                {"id": "b", "title": "B", "date": "2026-10-11"},
                {"id": "c", "title": "C", "date": "2026-10-10"},
            ]
        }
        result = parse(payload, current_version="1.0.0")
        self.assertEqual([r["id"] for r in result], ["b", "c", "a"])
    
    def test_version_range_filter(self):
        """Filter by min_version / max_version."""
        payload = {
            "items": [
                {"id": "a", "title": "A", "min_version": "1.1.0"},
                {"id": "b", "title": "B", "min_version": "1.0.0", "max_version": "1.0.9"},
                {"id": "c", "title": "C", "min_version": "", "max_version": ""},
            ]
        }
        result = parse(payload, current_version="1.0.5")
        # a filtered out (1.0.5 < 1.1.0)
        # b in range
        # c no limits
        ids = {r["id"] for r in result}
        self.assertNotIn("a", ids)
        self.assertIn("b", ids)
        self.assertIn("c", ids)
    
    def test_older_github_release_announcement_is_filtered(self):
        payload = {"items": [{
            "id": "beta1", "title": "beta.1", "url": "https://github.com/ExpertKT/notice-hub/releases/tag/v1.1.0-beta.1",
        }]}
        self.assertEqual(parse(payload, current_version="1.1.0-beta.4"), [])

    def test_current_github_release_announcement_is_kept(self):
        payload = {"items": [{
            "id": "beta4", "title": "beta.4", "url": "https://github.com/ExpertKT/notice-hub/releases/tag/v1.1.0-beta.4",
        }]}
        self.assertEqual([item["id"] for item in parse(payload, current_version="1.1.0-beta.4")], ["beta4"])

    def test_beta9_release_announcement_visibility(self):
        """beta.9：任何旧版本用户都看「beta.9 已发布」；beta.8 自己那条退场。

        同前几轮退场模式：beta.9 公告 max=1.1.0-beta.9，
        beta.8 公告压到 max=1.1.0-beta.7。
        白天与夜间两批合并进同一条公告（id 与日期保持 2026-10-23，避免已读状态错乱），
        所以 beta.9 用户只看到这一条。
        """
        path = Path(__file__).resolve().parent.parent / "announcements.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        # beta.9 只看这一条
        result_beta9 = parse(payload, current_version="1.1.0-beta.9", channel="beta")
        ids_beta9 = [item["id"] for item in result_beta9]
        self.assertIn("2026-10-23-release-1-1-0-beta-9", ids_beta9)
        # 常驻的「手机连不上排查」公告对所有版本可见，不算版本公告
        self.assertIn("2026-10-24-mobile-connect-help", ids_beta9)
        self.assertNotIn("2026-10-23-release-1-1-0-beta-8", ids_beta9)
        # beta.8 看 beta.9、不看 beta.8（退场）
        result_beta8 = parse(payload, current_version="1.1.0-beta.8", channel="beta")
        ids_beta8 = [item["id"] for item in result_beta8]
        self.assertIn("2026-10-23-release-1-1-0-beta-9", ids_beta8)
        self.assertIn("2026-10-24-mobile-connect-help", ids_beta8)
        self.assertNotIn("2026-10-23-release-1-1-0-beta-8", ids_beta8)
        # beta.7 看 beta.8
        result_beta7 = parse(payload, current_version="1.1.0-beta.7", channel="beta")
        ids_beta7 = [item["id"] for item in result_beta7]
        self.assertIn("2026-10-23-release-1-1-0-beta-8", ids_beta7)
        self.assertIn("2026-10-24-mobile-connect-help", ids_beta7)

    def test_beta9_announcement_merges_day_and_night_batches(self):
        """合并后的 beta.9 公告必须同时含白天那批修复与夜间这批，且不重复 beta.5 已修过的二维码问题。"""
        path = Path(__file__).resolve().parent.parent / "announcements.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        item = next(
            entry for entry in payload["items"]
            if entry["id"] == "2026-10-23-release-1-1-0-beta-9"
        )
        self.assertEqual(item["max_version"], "1.1.0-beta.9")
        self.assertEqual(item["channel"], "beta")
        self.assertEqual(item["date"], "2026-10-23")
        body = item["body"]
        # 已合并：不应再有单独的「夜间补齐」条目
        self.assertNotIn(
            "2026-10-24-release-1-1-0-beta-9-repack",
            [entry["id"] for entry in payload["items"]],
        )
        # 两组标题
        self.assertIn("【修复】", body)
        self.assertIn("【新增】", body)
        # 白天那批仍在
        self.assertIn("泛雅子站的 Cookie", body)
        self.assertIn("重复生成两条任务", body)
        self.assertIn("另有 K 条未展开", body)
        self.assertIn("800 条", body)
        self.assertIn("自动重试 3 次", body)
        # 夜间这批也在
        self.assertIn("启动 NapCat 并获取二维码", body)
        self.assertIn("首次使用？先完成 QQ 接入", body)
        self.assertIn("四个实验功能", body)
        for flag in (
            "QQ_DIGEST_LAB_SEARCH",
            "QQ_DIGEST_LAB_REPLIES",
            "QQ_DIGEST_LAB_EVENTS",
            "QQ_DIGEST_LAB_RULES",
        ):
            self.assertIn(flag, body)
        self.assertIn("实验室", body)
        self.assertIn("doctor --bundle", body)
        self.assertIn("原子写", body)
        self.assertIn("日志自动轮转", body)
        self.assertIn("16:1", body)
        # 不能再写「修复二维码」：beta.5 已修过，重复会让用户以为又坏了
        self.assertNotIn("修复二维码", body)
        self.assertNotIn("修复登录二维码", body)
    
    def test_beta10_release_announcement_visibility(self):
        """beta.10：三档可见性（beta.10 看自己、beta.9 看 beta.9、beta.8 看 beta.9）+ 正文关键词。

        版本门控语义见 `qq_live_digest/announce.py:64-83`：min/max 都是闭区间
        （`current > max_version` 才排除），另有 GitHub release tag 兜底（`announce.py:237-241`）。
        所以「beta.10 公告只给 beta.10 用户看」必须靠 min_version=1.1.0-beta.10；
        而 beta.9 公告保持 max_version=1.1.0-beta.9 不动，beta.9 用户仍能看到它，
        beta.10 用户则因 announce.py:80 的 `current > max` 被排除。
        """
        path = Path(__file__).resolve().parent.parent / "announcements.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        item = next(
            entry for entry in payload["items"]
            if entry["id"] == "2026-10-24-release-1-1-0-beta-10"
        )
        self.assertEqual(item["max_version"], "1.1.0-beta.10")
        self.assertEqual(item["min_version"], "1.1.0-beta.10")
        self.assertEqual(item["channel"], "beta")
        self.assertEqual(item["level"], "info")
        self.assertEqual(item["date"], "2026-10-24")
        self.assertFalse(item["sticky"])
        body = item["body"]
        for keyword in ("90 秒", "铃铛", "15 分钟", "Tailscale",
                        "防火墙", "全部标为已读", "Funnel"):
            self.assertIn(keyword, body)

        # beta.10：看自己，不看 beta.9 那条
        ids_beta10 = [i["id"] for i in parse(payload, current_version="1.1.0-beta.10", channel="beta")]
        self.assertIn("2026-10-24-release-1-1-0-beta-10", ids_beta10)
        self.assertNotIn("2026-10-23-release-1-1-0-beta-9", ids_beta10)

        # beta.9：看 beta.9 那条（没被挤掉），不看 beta.10
        ids_beta9 = [i["id"] for i in parse(payload, current_version="1.1.0-beta.9", channel="beta")]
        self.assertIn("2026-10-23-release-1-1-0-beta-9", ids_beta9)
        self.assertNotIn("2026-10-24-release-1-1-0-beta-10", ids_beta9)

        # beta.8：看 beta.9，不看 beta.10
        ids_beta8 = [i["id"] for i in parse(payload, current_version="1.1.0-beta.8", channel="beta")]
        self.assertIn("2026-10-23-release-1-1-0-beta-9", ids_beta8)
        self.assertNotIn("2026-10-24-release-1-1-0-beta-10", ids_beta8)

        # 常驻的「手机连不上排查」公告（sticky，无版本上限）对三档都可见
        for ids in (ids_beta10, ids_beta9, ids_beta8):
            self.assertIn("2026-10-24-mobile-connect-help", ids)

    def test_beta11_release_announcement_visibility(self):
        """beta.11：三档可见性（beta.11 看自己、beta.10 看 beta.10、beta.9 看 beta.9）+ 正文关键词。"""
        path = Path(__file__).resolve().parent.parent / "announcements.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        item = next(
            entry for entry in payload["items"]
            if entry["id"] == "2026-10-24-release-1-1-0-beta-11"
        )
        self.assertEqual(item["max_version"], "1.1.0-beta.11")
        self.assertEqual(item["min_version"], "1.1.0-beta.11")
        self.assertEqual(item["channel"], "beta")
        self.assertEqual(item["level"], "info")
        self.assertFalse(item["sticky"])
        body = item["body"]
        for keyword in ("电脑版 QQ", "NapCat", "重新检测", "二维码", "我知道了"):
            self.assertIn(keyword, body)

        ids_beta11 = [i["id"] for i in parse(payload, current_version="1.1.0-beta.11", channel="beta")]
        self.assertIn("2026-10-24-release-1-1-0-beta-11", ids_beta11)
        self.assertNotIn("2026-10-24-release-1-1-0-beta-10", ids_beta11)

        ids_beta10 = [i["id"] for i in parse(payload, current_version="1.1.0-beta.10", channel="beta")]
        self.assertIn("2026-10-24-release-1-1-0-beta-10", ids_beta10)
        self.assertNotIn("2026-10-24-release-1-1-0-beta-11", ids_beta10)

        ids_beta9 = [i["id"] for i in parse(payload, current_version="1.1.0-beta.9", channel="beta")]
        self.assertIn("2026-10-23-release-1-1-0-beta-9", ids_beta9)
        self.assertNotIn("2026-10-24-release-1-1-0-beta-11", ids_beta9)

        for ids in (ids_beta11, ids_beta10, ids_beta9):
            self.assertIn("2026-10-24-mobile-connect-help", ids)

    def test_beta13_release_announcement_visibility(self):
        """beta.13：三档可见性（beta.13 看自己、beta.12 看 beta.12、beta.11 看 beta.11）+ 正文关键词。"""
        path = Path(__file__).resolve().parent.parent / "announcements.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        item = next(
            entry for entry in payload["items"]
            if entry["id"] == "2026-10-28-release-1-1-0-beta-13"
        )
        self.assertEqual(item["max_version"], "1.1.0-beta.13")
        self.assertEqual(item["min_version"], "1.1.0-beta.13")
        self.assertEqual(item["channel"], "beta")
        self.assertEqual(item["level"], "info")
        self.assertFalse(item["sticky"])
        body = item["body"]
        for keyword in ("自动整理规则", "公网", "截止时间", "自检", "实验室"):
            self.assertIn(keyword, body)

        ids_beta13 = [i["id"] for i in parse(payload, current_version="1.1.0-beta.13", channel="beta")]
        self.assertIn("2026-10-28-release-1-1-0-beta-13", ids_beta13)
        self.assertNotIn("2026-10-26-release-1-1-0-beta-12", ids_beta13)

        ids_beta12 = [i["id"] for i in parse(payload, current_version="1.1.0-beta.12", channel="beta")]
        self.assertIn("2026-10-26-release-1-1-0-beta-12", ids_beta12)
        self.assertNotIn("2026-10-28-release-1-1-0-beta-13", ids_beta12)

        ids_beta11 = [i["id"] for i in parse(payload, current_version="1.1.0-beta.11", channel="beta")]
        self.assertIn("2026-10-24-release-1-1-0-beta-11", ids_beta11)
        self.assertNotIn("2026-10-28-release-1-1-0-beta-13", ids_beta11)

        for ids in (ids_beta13, ids_beta12, ids_beta11):
            self.assertIn("2026-10-24-mobile-connect-help", ids)

    def test_beta5_announcement_retired_in_beta6(self):
        """beta.5 announcement max_version=1.1.0-beta.4 means beta.5 and later don't see it."""
        path = Path(__file__).resolve().parent.parent / "announcements.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        # beta.4 sees beta.5 announcement (max_version=1.1.0-beta.4 includes beta.4)
        result_beta4 = parse(payload, current_version="1.1.0-beta.4", channel="beta")
        self.assertIn("2026-10-22-release-1-1-0-beta-5", [item["id"] for item in result_beta4])
        # beta.5 doesn't see beta.5 announcement (max_version=1.1.0-beta.4 excludes beta.5)
        result_beta5 = parse(payload, current_version="1.1.0-beta.5", channel="beta")
        self.assertNotIn("2026-10-22-release-1-1-0-beta-5", [item["id"] for item in result_beta5])
        # beta.6 doesn't see beta.5 announcement (max_version=1.1.0-beta.4 excludes beta.6)
        result_beta6 = parse(payload, current_version="1.1.0-beta.6", channel="beta")
        self.assertNotIn("2026-10-22-release-1-1-0-beta-5", [item["id"] for item in result_beta6])

    def test_explicit_max_version_filter_still_applies(self):
        payload = {"items": [{
            "id": "capped", "title": "capped", "url": "https://github.com/ExpertKT/notice-hub/releases/tag/v1.1.0-beta.4",
            "max_version": "1.1.0-beta.3",
        }]}
        self.assertEqual(parse(payload, current_version="1.1.0-beta.4"), [])

    def test_channel_filter(self):
        """Filter by channel (empty = all)."""
        payload = {
            "items": [
                {"id": "a", "title": "A", "channel": "stable"},
                {"id": "b", "title": "B", "channel": "beta"},
                {"id": "c", "title": "C", "channel": ""},
            ]
        }
        # Requesting beta channel
        result_beta = parse(payload, current_version="1.0.0", channel="beta")
        beta_ids = {r["id"] for r in result_beta}
        self.assertNotIn("a", beta_ids)  # stable only
        self.assertIn("b", beta_ids)  # beta
        self.assertIn("c", beta_ids)  # all channels
        
        # Requesting stable channel
        result_stable = parse(payload, current_version="1.0.0", channel="stable")
        stable_ids = {r["id"] for r in result_stable}
        self.assertIn("a", stable_ids)  # stable
        self.assertNotIn("b", stable_ids)  # beta only
        self.assertIn("c", stable_ids)  # all channels


class FetchTest(unittest.TestCase):
    """Test fetch function — mocked network calls only."""
    
    def setUp(self):
        """Create temporary cache directory for each test."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.temp_dir.name)
    
    def tearDown(self):
        """Clean up temporary directory."""
        self.temp_dir.cleanup()
    
    @patch("urllib.request.urlopen")
    def test_fetch_success(self, mock_urlopen):
        """Successful fetch writes to cache."""
        payload = {"version": 1, "items": [{"id": "a", "title": "A"}]}
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(payload).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_response.__exit__.return_value = None
        mock_urlopen.return_value = mock_response
        
        result = fetch(timeout=1.0, cache_dir=self.cache_dir)
        
        self.assertTrue(result["ok"])
        self.assertEqual(result["payload"], payload)
        self.assertFalse(result["from_cache"])
        self.assertEqual(result["error"], "")
        
        # Cache should be written
        cache_file = self.cache_dir / "announcements.json"
        self.assertTrue(cache_file.is_file())
    
    @patch("urllib.request.urlopen")
    def test_fetch_network_error_with_cache(self, mock_urlopen):
        """Network error falls back to cache."""
        mock_urlopen.side_effect = urllib.error.URLError("Connection failed")
        
        # Pre-populate cache
        cache_file = self.cache_dir / "announcements.json"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cached_payload = {"version": 1, "items": [{"id": "cached", "title": "Cached"}]}
        cache_file.write_text(json.dumps(cached_payload), encoding="utf-8")
        
        result = fetch(timeout=1.0, cache_dir=self.cache_dir)
        
        self.assertTrue(result["ok"])
        self.assertEqual(result["payload"], cached_payload)
        self.assertTrue(result["from_cache"])
    
    @patch("urllib.request.urlopen")
    def test_fetch_network_error_no_cache(self, mock_urlopen):
        """Network error with no cache returns error."""
        mock_urlopen.side_effect = urllib.error.URLError("Connection failed")
        
        result = fetch(timeout=1.0, cache_dir=self.cache_dir)
        
        self.assertFalse(result["ok"])
        self.assertIsNone(result["payload"])
        self.assertTrue(result["error"])  # Non-empty error message
    
    @patch("urllib.request.urlopen")
    def test_fetch_json_decode_error(self, mock_urlopen):
        """Malformed JSON returns error."""
        mock_response = MagicMock()
        mock_response.read.return_value = b"not valid json"
        mock_response.__enter__.return_value = mock_response
        mock_response.__exit__.return_value = None
        mock_urlopen.return_value = mock_response
        
        result = fetch(timeout=1.0, cache_dir=self.cache_dir)
        
        self.assertFalse(result["ok"])
        self.assertIsNone(result["payload"])
        self.assertTrue(result["error"])

    @patch("urllib.request.urlopen")
    def test_fetch_tries_gitee_after_github_404_and_caches_success(self, mock_urlopen):
        sources = [
            {"id": "github", "repo": "ExpertKT/notice-hub", "api_root": "https://api.github.com", "raw_base": "https://raw.githubusercontent.com/ExpertKT/notice-hub/main"},
            {"id": "gitee", "repo": "ExpertKT/notice-hub", "api_root": "https://gitee.com/api/v5", "raw_base": "https://gitee.com/ExpertKT/notice-hub/raw/master"},
        ]
        payload = {"version": 1, "items": [{"id": "mirror", "title": "镜像公告"}]}
        response = MagicMock()
        response.read.return_value = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        response.__enter__.return_value = response
        response.__exit__.return_value = None
        mock_urlopen.side_effect = [
            urllib.error.HTTPError("https://raw.example/github/announcements.json", 404, "Not Found", {}, None),
            response,
        ]

        with patch("qq_live_digest.announce.update_sources.sources", return_value=sources):
            result = fetch(timeout=1.0, cache_dir=self.cache_dir)

        self.assertTrue(result["ok"])
        self.assertEqual(result["payload"], payload)
        self.assertFalse(result["from_cache"])
        self.assertEqual(mock_urlopen.call_count, 2)
        self.assertEqual(mock_urlopen.call_args_list[1].args[0].full_url, sources[1]["raw_base"] + "/announcements.json")
        self.assertEqual(json.loads((self.cache_dir / "announcements.json").read_text(encoding="utf-8")), payload)

    @patch("urllib.request.urlopen")
    def test_fetch_404_means_no_announcements_yet(self, mock_urlopen):
        """main 上还没有 announcements.json 时是「暂无公告」，不是错误。

        否则每次开页面，设置里的公告卡片都挂一句「无法获取公告：HTTPError」
        —— 发布者一条公告都没发过是正常状态。
        """
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://raw.githubusercontent.com/ExpertKT/notice-hub/main/announcements.json",
            404, "Not Found", {}, None,
        )

        result = fetch(timeout=1.0, cache_dir=self.cache_dir)

        self.assertTrue(result["ok"])
        self.assertEqual(result["payload"], {"version": 1, "items": []})
        self.assertEqual(result["error"], "")


class ActiveTest(unittest.TestCase):
    """Test active function — combines fetch + parse."""
    
    @patch("urllib.request.urlopen")
    def test_active_success(self, mock_urlopen):
        """Active fetches and parses successfully."""
        payload = {
            "version": 1,
            "items": [
                {"id": "a", "title": "A", "date": "2026-10-11", "level": "info", "sticky": False}
            ]
        }
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(payload).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_response.__exit__.return_value = None
        mock_urlopen.return_value = mock_response
        
        with tempfile.TemporaryDirectory() as tmpdir:
            result = active(cache_dir=Path(tmpdir), current_version="1.0.0", timeout=1.0)
        
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["id"], "a")
        self.assertEqual(result["current_version"], "1.0.0")
        self.assertEqual(result["channel"], "beta")
        self.assertFalse(result["from_cache"])
        self.assertTrue(result["checked_at"])


if __name__ == "__main__":
    unittest.main()
