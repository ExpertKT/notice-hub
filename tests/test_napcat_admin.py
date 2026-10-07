import io
import json
import os
import tempfile
import zipfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qq_live_digest import napcat_admin as na

class S:
    onebot_port = 8765
    onebot_token = "one"
    napcat_api_url = "http://127.0.0.1:3001"
    napcat_api_token = "api"
    napcat_qr_path = Path(tempfile.gettempdir()) / "qq-notice-hub-test-qr.png"
    data_dir = Path(tempfile.gettempdir()) / "qq-notice-hub-test-data"

class NapcatAdminTests(unittest.TestCase):
    def test_ports_accept_any_listening_local_address(self):
        text = "  TCP    0.0.0.0:6099    0.0.0.0:0    LISTENING    10\n  TCP    [::]:6099       [::]:0       LISTENING    10\n"
        self.assertIn(6099, na._ports(text))

    def test_live_root_wins_even_when_later(self):
        with tempfile.TemporaryDirectory() as td:
            first, second = Path(td) / "first", Path(td) / "second"
            for root in (first, second):
                (root / "config").mkdir(parents=True)
                (root / "config" / "webui.json").write_text('{"port":6199}', encoding="utf-8")
            (second / "config" / "onebot11_2.json").write_text(json.dumps({"network":{"httpServers":[],"httpClients":[]}}), encoding="utf-8")
            roots = [na.NapcatRoot(first, 6199, "", ["1"], False), na.NapcatRoot(second, 6199, "", ["2"], True)]
            with patch.object(na, "find_roots", return_value=roots), patch.object(na, "_request", side_effect=OSError("offline")):
                result = na.auto_setup(S())
            self.assertEqual(result["uin"], "2")

    def test_file_mtime_beats_directory_mtime(self):
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td) / "a", Path(td) / "b"
            for root in (a, b):
                (root / "config").mkdir(parents=True)
                (root / "config" / "webui.json").write_text('{"port":6199}', encoding="utf-8")
                (root / "config" / "onebot11_x.json").write_text('{"network": {}}', encoding="utf-8")
            import os
            os.utime(a / "config", (30, 30)); os.utime(a / "config" / "webui.json", (5, 5)); os.utime(a / "config" / "onebot11_x.json", (10, 10))
            os.utime(b / "config", (10, 10)); os.utime(b / "config" / "webui.json", (5, 5)); os.utime(b / "config" / "onebot11_x.json", (30, 30))
            self.assertGreater(na._root_mtime(na.NapcatRoot(b, 6199, "", ["x"], True)), na._root_mtime(na.NapcatRoot(a, 6199, "", ["x"], True)))

    def test_qrcode_mtime_breaks_tie(self):
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td) / "a", Path(td) / "b"
            for root in (a, b):
                (root / "config").mkdir(parents=True); (root / "cache").mkdir()
                (root / "config" / "onebot11_x.json").write_text('{}')
            (b / "cache" / "qrcode.png").write_bytes(b"qr")
            self.assertGreater(na._qr_mtime(na.NapcatRoot(b, 6199, "", [], True)), na._qr_mtime(na.NapcatRoot(a, 6199, "", [], True)))

    def test_launch_skips_when_already_running(self):
        with patch.object(na, "_ports", return_value={6099}), patch.object(na.subprocess, "Popen") as popen:
            result = na.launch(S(), uin="123")
        self.assertTrue(result["already_running"])
        popen.assert_not_called()

    def test_launch_scan_mode_omits_quick_login(self):
        boot = {"boot_exe": "F:/boot.exe", "qq_exe": "F:/QQ.exe", "hook_dll": "F:/hook.dll", "data_dir": tempfile.gettempdir()}
        child = type("Child", (), {"pid": 7})()
        with patch.object(na, "_ports", return_value=set()), patch.object(na, "detect_boot", return_value=boot), patch.object(na.subprocess, "Popen", return_value=child) as popen:
            result = na.launch(S(), uin=None, profile_dir=str(Path(tempfile.gettempdir()) / "napcat-test-profile"))
        self.assertTrue(result["ok"])
        self.assertNotIn("-q", result["command"])
        self.assertTrue(any(x.startswith("--user-data-dir=") for x in result["command"]))
        popen.assert_called_once()

    def _make_fake_tree(self, data_dir):
        root = Path(data_dir) / "napcat"
        napcat = root / "versions" / "1" / "resources" / "app" / "napcat"
        napcat.mkdir(parents=True)
        (napcat / "NapCatWinBootMain.exe").write_bytes(b"boot")
        (napcat / "NapCatWinBootHook.dll").write_bytes(b"hook")
        (napcat / "qqnt.json").write_text("{}")
        (root / "QQ.exe").write_bytes(b"shell")
        (Path(data_dir) / "D-QQ.exe").write_bytes(b"user")
        return root

    def test_detect_boot_never_returns_user_qq(self):
        with tempfile.TemporaryDirectory() as td:
            self._make_fake_tree(td)
            na._LAST_INSTALL_ROOT = None
            with patch.dict(os.environ, {"QQ_DIGEST_DATA_DIR": td}, clear=False):
                result = na.detect_boot()
            self.assertIsNotNone(result)
            self.assertNotEqual(result["qq_exe"].lower(), r"d:\qq\qq.exe")
            self.assertNotIn(r"d:\qq", result["qq_exe"].lower())

    def test_missing_root(self):
        with patch.object(na, "find_roots", return_value=[]):
            result = na.auto_setup(S())
        self.assertFalse(result["ok"])
        self.assertIn(na.NAPCAT_DOWNLOAD_URL, result["steps"][0]["detail"])

    def test_file_fallback_preserves_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); (root / "config").mkdir(); (root / "cache").mkdir()
            (root / "config" / "webui.json").write_text('{"port":6199,"token":"t"}', encoding="utf-8")
            cfg = {"network":{"httpServers":[],"httpClients":[{"url":"http://127.0.0.1:8765","old":1}]},"keep":True}
            (root / "config" / "onebot11_123.json").write_text(json.dumps(cfg), encoding="utf-8")
            (root / "cache" / "qrcode.png").write_bytes(b"png")
            fake = na.NapcatRoot(root, 6199, "t", ["123"], False)
            with patch.object(na, "find_roots", return_value=[fake]), patch.object(na, "_request", side_effect=OSError("offline")):
                result = na.auto_setup(S())
            self.assertEqual(result["applied_via"], "file")
            self.assertTrue(result["restart_required"])
            out = json.loads((root / "config" / "onebot11_123.json").read_text())
            self.assertTrue(out["keep"])
            self.assertEqual(len(out["network"]["httpClients"]), 1)
            self.assertEqual(out["network"]["httpClients"][0]["url"], "http://127.0.0.1:8765")
            self.assertEqual(out["network"]["httpServers"][0]["port"], 3001)
            self.assertFalse(Path(__file__).resolve().parents[1].joinpath("qr.png").exists())

    def _fake_zip(self, malicious=False):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as zf:
            zf.writestr("../evil.txt" if malicious else "versions/1/resources/app/napcat/NapCatWinBootMain.exe", b"x")
            if not malicious:
                zf.writestr("versions/1/resources/app/napcat/NapCatWinBootHook.dll", b"x")
                zf.writestr("QQ.exe", b"x")
                zf.writestr("versions/1/resources/app/napcat/qqnt.json", b"{}")
        return stream.getvalue()

    def test_install_downloads_to_data_dir_and_detects(self):
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): pass
        with tempfile.TemporaryDirectory() as td:
            settings = type("Settings", (), {"data_dir": Path(td)})()
            payload = self._fake_zip()
            na._LAST_INSTALL_ROOT = None
            with patch.dict(os.environ, {"QQ_DIGEST_DATA_DIR": td}, clear=False), patch.object(na.urllib.request, "urlopen", return_value=Response(payload)):
                result = na.install(settings)
            self.assertTrue(result["ok"])
            self.assertTrue(Path(result["root"]).is_relative_to(Path(td)))
            self.assertTrue(na.detect_boot()["boot_exe"].startswith(str(Path(td))))

    def test_install_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            settings = type("Settings", (), {"data_dir": Path(td)})()
            na._LAST_INSTALL_ROOT = Path(td) / "napcat"
            na._LAST_INSTALL_ROOT.mkdir(parents=True)
            with patch.object(na, "detect_boot", return_value={"boot_exe": str(na._LAST_INSTALL_ROOT / "x")} ), patch.object(na.urllib.request, "urlopen") as fetch:
                result = na.install(settings)
            self.assertTrue(result["already_installed"])
            fetch.assert_not_called()

    def test_install_download_error_is_returned(self):
        with tempfile.TemporaryDirectory() as td:
            settings = type("Settings", (), {"data_dir": Path(td)})()
            with patch.object(na.urllib.request, "urlopen", side_effect=OSError("offline")):
                result = na.install(settings)
            self.assertFalse(result["ok"])
            self.assertIn("失败", result["error"])

    def test_install_rejects_html_download(self):
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): pass
        with tempfile.TemporaryDirectory() as td:
            settings = type("Settings", (), {"data_dir": Path(td)})()
            with patch.object(na.urllib.request, "urlopen", return_value=Response(b"<html>not zip</html>")):
                result = na.install(settings)
            self.assertFalse(result["ok"])
            self.assertIn("不是有效的压缩包", result["error"])

    def test_install_rejects_zip_path_traversal(self):
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): pass
        with tempfile.TemporaryDirectory() as td:
            settings = type("Settings", (), {"data_dir": Path(td)})()
            with patch.object(na.urllib.request, "urlopen", return_value=Response(self._fake_zip(True))):
                result = na.install(settings)
            self.assertFalse(result["ok"])
            self.assertFalse((Path(td).parent / "evil.txt").exists())

if __name__ == "__main__":
    unittest.main()
