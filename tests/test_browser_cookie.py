"""Tests for browser_cookie.py: extract Chaoxing cookies from browser storage."""

import base64
import json
import os
import secrets
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from qq_live_digest.browser_cookie import (
    read_chaoxing_cookie,
    _try_firefox,
    _try_chrome_like,
    _read_chrome_cookies_db,
    _dpapi_unprotect,
    _dpapi_protect,
    _decrypt_chrome_cookie_gcm,
    _aes_gcm_encrypt,
    _aes_gcm_decrypt,
    _extract_key,
    _edge_user_data_path,
)


class TestReadChaoxingCookie(unittest.TestCase):
    """Test the main entry point: read_chaoxing_cookie()."""

    def test_returns_dict_never_raises(self):
        """read_chaoxing_cookie() always returns dict, never raises."""
        result = read_chaoxing_cookie()
        self.assertIsInstance(result, dict)
        self.assertIn("ok", result)
        self.assertIn("cookie", result)
        self.assertIn("source", result)
        self.assertIn("error", result)
        self.assertIn("detail", result)

    def test_no_cookie_value_in_error(self):
        """Error messages must not contain full Cookie values."""
        result = read_chaoxing_cookie()
        if result.get("cookie"):
            # If we have a cookie, error should be empty or not contain the cookie
            self.assertNotIn(result["cookie"], result.get("error", ""))
        # Even if Firefox/Chrome fails, error message must not leak cookie
        self.assertNotIn("=", result.get("error", ""))  # No key=value pairs


class TestFirefoxReader(unittest.TestCase):
    """Test Firefox cookie reading (plaintext)."""

    def test_firefox_nonexistent_returns_error(self):
        """Firefox reader returns ok=False when profiles dir doesn't exist."""
        with patch("qq_live_digest.browser_cookie.Path") as mock_path:
            mock_path.return_value.exists.return_value = False
            result = _try_firefox()
            self.assertFalse(result["ok"])
            self.assertIn("Firefox", result["error"])

    def test_firefox_cookie_string_format(self):
        """Firefox cookie result has correct format: 'key=value; key2=value2'."""
        # This test would require a real Firefox installation or mocking sqlite3
        # For now, we test the return format contract
        result = _try_firefox()
        if result["ok"]:
            # If we have a cookie, it must be a string
            self.assertIsInstance(result["cookie"], str)
            # It should contain = and ; separators
            if result["cookie"]:
                self.assertIn("=", result["cookie"])


class TestChromeReader(unittest.TestCase):
    """Test Chrome/Edge cookie reading (encrypted)."""

    def test_v20_returns_ok_false_with_error(self):
        """v20 (app-bound) cookies should return ok=False with descriptive error."""
        # Create a temporary directory structure
        with tempfile.TemporaryDirectory() as tmpdir:
            user_data = Path(tmpdir)

            # Local State 里的密钥必须是**真能被 DPAPI 解开的**：否则代码在碰 Cookie 库之前
            # 就失败，v20 分支根本测不到，报出来的还是 DPAPI 的错（这个用例以前就是这样
            # 假通过的 —— 断言只看到 error 里有 'v20'，其实来自别的分支）。
            protected = _dpapi_protect(bytes(range(32)))
            self.assertIsNotNone(protected)
            local_state = {
                "os_crypt": {
                    "encrypted_key": base64.b64encode(b"DPAPI" + protected).decode("ascii")
                }
            }
            local_state_path = user_data / "Local State"
            with open(local_state_path, "w") as f:
                json.dump(local_state, f)
            
            # Create a profile with v20 cookies
            profile = user_data / "Default" / "Network"
            profile.mkdir(parents=True)
            
            cookies_db = profile / "Cookies"
            conn = sqlite3.connect(cookies_db)
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE cookies (
                    host_key TEXT,
                    name TEXT,
                    encrypted_value BLOB
                )
            """)
            # v20 prefix means app-bound encryption
            cursor.execute(
                "INSERT INTO cookies VALUES (?, ?, ?)",
                ("chaoxing.com", "testcookie", b"v20" + b"x" * 20)
            )
            conn.commit()
            conn.close()
            
            # Try to read
            result = _try_chrome_like(user_data, "chrome")
            
            # Should fail and return ok=False with v20 error
            self.assertFalse(result["ok"])
            self.assertEqual(result["category"], "decrypt_failed")
            self.assertIn("v20", result.get("error", "").lower())
            self.assertIn("另一个", result["next_step"])
            # No cookie should be returned
            self.assertEqual(result["cookie"], "")

    def test_no_cookies_returns_error(self):
        """Empty or missing cookies.db returns ok=False with descriptive error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            user_data = Path(tmpdir)
            
            local_state_path = user_data / "Local State"
            with open(local_state_path, "w") as f:
                json.dump({"os_crypt": {"encrypted_key": "REVQQU0lfVGVzdA=="}}, f)
            
            result = _try_chrome_like(user_data, "chrome")
            self.assertFalse(result["ok"])
            self.assertNotEqual(result["error"], "")

    def test_missing_cookie_db_is_browser_unavailable(self):
        """浏览器目录存在但没有 Cookie 库时，不能误报成「没有登录学习通」。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            user_data = Path(tmpdir)
            protected = _dpapi_protect(bytes(range(32)))
            self.assertIsNotNone(protected)
            local_state = {"os_crypt": {"encrypted_key": base64.b64encode(b"DPAPI" + protected).decode("ascii")}}
            (user_data / "Local State").write_text(json.dumps(local_state), encoding="utf-8")
            result = _try_chrome_like(user_data, "chrome")
        self.assertFalse(result["ok"])
        self.assertEqual(result["category"], "browser_unavailable")
        self.assertIn("Cookie 库", result["error"])
        self.assertNotIn("未找到学习通 Cookie", result["error"])

    def test_decrypt_exception_is_reported_instead_of_swallowed(self):
        """单条 Cookie 解密异常必须汇总成 decrypt_failed，且不得泄露 Cookie 值。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cookies_db = Path(tmpdir) / "Cookies"
            conn = sqlite3.connect(cookies_db)
            try:
                conn.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB)")
                conn.execute(
                    "INSERT INTO cookies VALUES (?, ?, ?)",
                    (".chaoxing.com", "UID", b"v10" + b"secret-ciphertext"),
                )
                conn.commit()
            finally:
                conn.close()
            with patch(
                "qq_live_digest.browser_cookie._decrypt_chrome_cookie_gcm",
                side_effect=RuntimeError("unsupported algorithm"),
            ):
                result = _read_chrome_cookies_db(cookies_db, bytes(range(32)), "edge", "Default")
        self.assertFalse(result["ok"])
        self.assertEqual(result["category"], "decrypt_failed")
        self.assertIn("无法解密", result["error"])
        self.assertIn("RuntimeError", result["detail"])
        self.assertIn("另一个", result["next_step"])
        self.assertNotIn("secret-ciphertext", result["detail"])

    def test_read_tries_edge_then_chrome_and_keeps_each_result(self):
        """Edge 失败后必须继续 Chrome；成功响应也要保留前面每次尝试的结果。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            edge_path = Path(tmpdir) / "edge"
            chrome_path = Path(tmpdir) / "chrome"
            edge_path.mkdir()
            chrome_path.mkdir()
            edge_fail = {
                "ok": False,
                "cookie": "",
                "source": "edge",
                "category": "no_chaoxing_cookie",
                "error": "Edge 中没有学习通登录态",
                "detail": "",
            }
            chrome_ok = {
                "ok": True,
                "cookie": "UID=ok",
                "source": "chrome",
                "error": "",
                "detail": "Chrome 配置: Default",
            }
            with patch("qq_live_digest.browser_cookie._edge_user_data_path", return_value=edge_path), patch(
                "qq_live_digest.browser_cookie._chrome_user_data_path", return_value=chrome_path
            ), patch(
                "qq_live_digest.browser_cookie._try_chrome_like", side_effect=[edge_fail, chrome_ok]
            ) as reader, patch("qq_live_digest.browser_cookie._try_firefox") as firefox:
                result = read_chaoxing_cookie()
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "chrome")
        self.assertEqual([item["source"] for item in result["attempts"]], ["edge", "chrome"])
        self.assertEqual([call.args[1] for call in reader.call_args_list], ["edge", "chrome"])
        firefox.assert_not_called()

    def test_locked_cookie_db_says_browser_is_running(self):
        """浏览器开着时 Cookie 库被独占：必须说清原因，不能报成「未找到学习通 Cookie」。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            user_data = Path(tmpdir)
            protected = _dpapi_protect(bytes(range(32)))
            self.assertIsNotNone(protected)
            local_state = {"os_crypt": {"encrypted_key": base64.b64encode(b"DPAPI" + protected).decode("ascii")}}
            (user_data / "Local State").write_text(json.dumps(local_state), encoding="utf-8")
            (user_data / "Default" / "Network").mkdir(parents=True)
            (user_data / "Default" / "Network" / "Cookies").write_bytes(b"not a real db")
            with patch("qq_live_digest.browser_cookie.sqlite3.connect", side_effect=PermissionError("in use")):
                result = _try_chrome_like(user_data, "edge")
        self.assertFalse(result["ok"])
        self.assertEqual(result["category"], "browser_locked")
        self.assertIn("被占用", result["error"])
        self.assertIn("完全退出", result["next_step"])
        self.assertIn("托盘", result["next_step"])
        self.assertIn("后台进程", result["next_step"])
        self.assertNotIn("未找到学习通 Cookie", result["error"])

    def test_read_chaoxing_cookie_reports_the_real_reason(self):
        """总入口要透出最高优先级真实原因，不能统一报成笼统失败。"""
        chrome_fail = {
            "ok": False,
            "cookie": "",
            "source": "edge",
            "category": "browser_locked",
            "error": "Edge Cookie 库被占用",
            "detail": "",
        }
        firefox_fail = {
            "ok": False,
            "cookie": "",
            "source": "firefox",
            "category": "browser_unavailable",
            "error": "Firefox Cookie 库不存在",
            "detail": "",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            edge_path = Path(tmpdir) / "edge"
            edge_path.mkdir()
            missing = Path(tmpdir) / "missing"
            with patch("qq_live_digest.browser_cookie._edge_user_data_path", return_value=edge_path), patch(
                "qq_live_digest.browser_cookie._chrome_user_data_path", return_value=missing
            ), patch("qq_live_digest.browser_cookie._brave_user_data_path", return_value=missing), patch(
                "qq_live_digest.browser_cookie._vivaldi_user_data_path", return_value=missing
            ), patch("qq_live_digest.browser_cookie._opera_user_data_path", return_value=missing), patch(
                "qq_live_digest.browser_cookie._try_firefox", return_value=firefox_fail
            ), patch("qq_live_digest.browser_cookie._try_chrome_like", return_value=chrome_fail):
                result = read_chaoxing_cookie()
        self.assertFalse(result["ok"])
        self.assertEqual(result["category"], "browser_locked")
        self.assertIn("被占用", result["error"])
        self.assertEqual(len(result["attempts"]), 6)
        self.assertNotEqual(result["error"], "未找到浏览器 Cookie 库或无法解密")

    def test_cookie_result_format(self):
        """Chrome cookie result has correct format."""
        result = _try_chrome_like(Path("."), "chrome")
        if result["ok"]:
            # If successful, cookie must be non-empty string
            self.assertIsInstance(result["cookie"], str)
            self.assertGreater(len(result["cookie"]), 0)


class TestGCMRoundtrip(unittest.TestCase):
    """Test GCM encryption/decryption roundtrip (Step 1-2)."""

    def test_gcm_roundtrip_encrypt_decrypt(self):
        """GCM encrypt plaintext → decrypt → verify plaintext matches."""
        plaintext = b"test_cookie_value"
        key = secrets.token_bytes(32)  # 32 bytes for AES-256

        # Encrypt
        encrypted = _aes_gcm_encrypt(key, plaintext)
        self.assertIsNotNone(encrypted, "Encryption returned None")
        self.assertGreaterEqual(len(encrypted), 3 + 12 + 0 + 16)
        self.assertEqual(encrypted[:3], b"v10", "Missing v10 prefix")

        # Decrypt
        decrypted = _aes_gcm_decrypt(key, encrypted)
        self.assertIsNotNone(decrypted, "Decryption returned None")
        self.assertEqual(decrypted, plaintext, f"Plaintext mismatch: {decrypted!r} != {plaintext!r}")

    def test_gcm_roundtrip_short_message(self):
        """GCM encrypt/decrypt short message (edge case)."""
        plaintext = b"x"
        key = secrets.token_bytes(32)

        encrypted = _aes_gcm_encrypt(key, plaintext)
        self.assertIsNotNone(encrypted, "Encryption failed on short message")

        decrypted = _aes_gcm_decrypt(key, encrypted)
        self.assertIsNotNone(decrypted, "Decryption failed on short message")
        self.assertEqual(decrypted, plaintext)

    def test_gcm_roundtrip_large_message(self):
        """GCM encrypt/decrypt large message."""
        plaintext = b"x" * 10000  # 10KB
        key = secrets.token_bytes(32)

        encrypted = _aes_gcm_encrypt(key, plaintext)
        self.assertIsNotNone(encrypted)

        decrypted = _aes_gcm_decrypt(key, encrypted)
        self.assertIsNotNone(decrypted)
        self.assertEqual(decrypted, plaintext)

    def test_gcm_corruption_detection(self):
        """GCM detects tag corruption (authentication)."""
        plaintext = b"authenticated_data"
        key = secrets.token_bytes(32)

        encrypted = _aes_gcm_encrypt(key, plaintext)
        self.assertIsNotNone(encrypted)

        # Corrupt the tag (last 16 bytes)
        corrupted = bytearray(encrypted)
        corrupted[-1] ^= 0xFF

        decrypted = _aes_gcm_decrypt(key, bytes(corrupted))
        # Decryption should fail or return garbage
        self.assertTrue(decrypted is None or decrypted != plaintext)


class TestDPAPIRoundtrip(unittest.TestCase):
    """Test DPAPI encryption/decryption roundtrip (Step 3)."""

    @unittest.skipIf(os.name != "nt", "DPAPI only on Windows")
    def test_dpapi_roundtrip(self):
        """DPAPI encrypt plaintext → decrypt → verify plaintext matches."""
        plaintext = b"dpapi_test_secret_data"

        # Encrypt
        encrypted = _dpapi_protect(plaintext)
        self.assertIsNotNone(encrypted, "DPAPI protection failed")
        self.assertGreater(len(encrypted), len(plaintext))

        # Decrypt
        decrypted = _dpapi_unprotect(encrypted)
        self.assertIsNotNone(decrypted, "DPAPI unprotect failed")
        self.assertEqual(decrypted, plaintext)

    @unittest.skipIf(os.name != "nt", "DPAPI only on Windows")
    def test_dpapi_empty_data(self):
        """DPAPI handles empty input gracefully."""
        result = _dpapi_unprotect(b"")
        self.assertTrue(result is None or isinstance(result, bytes))

    @unittest.skipIf(os.name != "nt", "DPAPI only on Windows")
    def test_dpapi_invalid_data(self):
        """DPAPI returns None for invalid input."""
        result = _dpapi_unprotect(b"invalid_dpapi_blob")
        self.assertIsNone(result)


class TestExtractKeyFromEdge(unittest.TestCase):
    """Test master key extraction from Edge Local State (Step 5)."""

    @unittest.skipIf(os.name != "nt", "DPAPI only on Windows")
    def test_extract_key_from_edge(self):
        """Extract master key from real Edge Local State (if installed)."""
        edge_path = _edge_user_data_path()
        local_state = edge_path / "Local State"

        if not local_state.exists():
            self.skipTest("Edge not installed")

        try:
            with open(local_state, "r", encoding="utf-8") as f:
                local_state_dict = json.load(f)
        except Exception as e:
            self.skipTest(f"Cannot read Local State: {e}")

        # Extract key
        master_key = _extract_key(local_state_dict)

        if master_key is None:
            self.skipTest("No master key found or decryption failed")

        # Verify it's 32 bytes (AES-256)
        self.assertEqual(len(master_key), 32, f"Master key wrong size: {len(master_key)}")


class TestGCMDecrypt(unittest.TestCase):
    """Test GCM decryption."""

    def test_gcm_returns_string_or_none(self):
        """_decrypt_chrome_cookie_gcm returns string or None, never raises."""
        result = _decrypt_chrome_cookie_gcm(b"v10" + b"x" * 50, b"key" * 10)
        self.assertTrue(result is None or isinstance(result, str))

    def test_gcm_too_short_returns_none(self):
        """GCM with too-short ciphertext returns None."""
        result = _decrypt_chrome_cookie_gcm(b"v10abc", b"key" * 10)
        self.assertIsNone(result)


class TestNoLeakingCookies(unittest.TestCase):
    """Verify that Cookie values never leak into logs/errors/details."""

    def test_read_chaoxing_cookie_no_leak(self):
        """Main function never leaks Cookie values in error/detail."""
        result = read_chaoxing_cookie()
        
        cookie_val = result.get("cookie", "")
        error = result.get("error", "")
        detail = result.get("detail", "")
        
        if cookie_val and len(cookie_val) > 4:
            self.assertNotIn(cookie_val, error)
            self.assertNotIn(cookie_val, detail)

    def test_firefox_no_leak(self):
        """Firefox reader never leaks Cookie values."""
        result = _try_firefox()
        cookie_val = result.get("cookie", "")
        error = result.get("error", "")
        
        if cookie_val:
            self.assertNotIn(cookie_val, error)

    def test_chrome_no_leak(self):
        """Chrome reader never leaks Cookie values."""
        result = _try_chrome_like(Path("."), "chrome")
        cookie_val = result.get("cookie", "")
        error = result.get("error", "")
        
        if cookie_val:
            self.assertNotIn(cookie_val, error)


if __name__ == "__main__":
    unittest.main()

