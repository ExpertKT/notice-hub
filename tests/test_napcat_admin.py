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
            with patch.object(na, "find_roots", return_value=roots), patch.object(na, "_request", side_effect=OSError("offline")), patch.object(na, "restart", return_value={"ok": False, "error": "test"}):
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
            with patch.object(na, "find_roots", return_value=[fake]), patch.object(na, "_request", side_effect=OSError("offline")), patch.object(na, "restart", return_value={"ok": False, "error": "test"}):
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
            self.assertTrue(Path(result["root"]).resolve().is_relative_to(Path(td).resolve()))
            boot = na.detect_boot()
            self.assertTrue(Path(boot["boot_exe"]).resolve().is_relative_to(Path(td).resolve()))

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

    def test_qr_file_prefers_data_dir_and_stamp_follows_the_file(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / "data"
            (data / "cache").mkdir(parents=True)
            qr = data / "cache" / "qrcode.png"
            qr.write_bytes(b"one")
            settings = type("T", (), {"data_dir": data, "napcat_qr_path": Path(td) / "elsewhere.png"})()
            with patch.object(na, "find_roots", return_value=[]):
                self.assertEqual(na._qr_file(settings), qr)
                first = na.qr_stamp(settings)
                self.assertEqual(first, f"{qr.stat().st_mtime_ns}-3")
                os.utime(qr, ns=(111_111_111_111, 111_111_111_111))
                self.assertNotEqual(na.qr_stamp(settings), first)

    def test_qr_stamp_is_blank_without_a_file(self):
        with tempfile.TemporaryDirectory() as td:
            settings = type("T", (), {"data_dir": Path(td), "napcat_qr_path": Path(td) / "missing.png"})()
            with patch.object(na, "find_roots", return_value=[]):
                self.assertEqual(na.qr_stamp(settings), "")
                self.assertIsNone(na.qrcode_bytes(settings))

    def test_wait_for_port_returns_once_the_port_listens(self):
        seen = {"n": 0}

        def ports():
            seen["n"] += 1
            return {3001} if seen["n"] >= 2 else set()

        with patch.object(na, "_ports", side_effect=ports), patch.object(na.time, "sleep"):
            self.assertTrue(na.wait_for_port(3001, timeout=5))

    def test_wait_for_port_times_out_without_the_port(self):
        with patch.object(na, "_ports", return_value=set()), patch.object(na.time, "sleep"), patch.object(na.time, "monotonic", side_effect=[0, 0, 99]):
            self.assertFalse(na.wait_for_port(3001, timeout=1))

    def test_probe_api_counts_any_http_answer_as_connected(self):
        with patch.object(na, "_request", return_value={"status": "failed", "retcode": 1}):
            ok, detail = na._probe_api("http://127.0.0.1:3001/get_login_info", "t")
        self.assertTrue(ok)
        self.assertIn("failed", detail)
        with patch.object(na, "_request", side_effect=OSError("refused")):
            self.assertFalse(na._probe_api("http://127.0.0.1:3001/get_login_info", "t")[0])

    def test_restart_stops_waits_launches_and_probes(self):
        with patch.object(na, "stop", return_value={"ok": True, "stopped": [5], "already_stopped": False, "error": None}), \
             patch.object(na, "_ports", return_value=set()), \
             patch.object(na, "launch", return_value={"ok": True, "pid": 9}) as launched, \
             patch.object(na, "wait_for_port", return_value=True) as waited:
            result = na.restart(S(), uin="123")
        self.assertTrue(result["ok"])
        self.assertEqual(result["pid"], 9)
        self.assertEqual(result["stopped"], [5])
        self.assertTrue(result["api_up"])
        self.assertFalse(result["waiting_for_login"])
        launched.assert_called_once()
        waited.assert_called_once()

    def test_restart_waits_for_login_when_qr_changes_but_port_stays_closed(self):
        with patch.object(na, "stop", return_value={"ok": True, "stopped": [], "already_stopped": True, "error": None}), \
             patch.object(na, "_ports", return_value=set()), \
             patch.object(na, "launch", return_value={"ok": True, "pid": 9}), \
             patch.object(na, "wait_for_port", return_value=False), \
             patch.object(na, "_qr_token", side_effect=["before", "after", "after"]):
            result = na.restart(S(), uin="123", wait_seconds=5)
        self.assertTrue(result["ok"])
        self.assertFalse(result["api_up"])
        self.assertTrue(result["waiting_for_login"])

    def test_restart_stops_on_stop_error(self):
        with patch.object(na, "stop", return_value={"ok": False, "stopped": [], "already_stopped": False, "error": "boom"}), patch.object(na, "launch") as launched:
            result = na.restart(S())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "boom")
        launched.assert_not_called()

    def test_auto_setup_restarts_napcat_so_the_new_config_is_read(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            (root / "cache").mkdir()
            (root / "config" / "webui.json").write_text('{"port":6199,"token":"t"}', encoding="utf-8")
            (root / "config" / "onebot11_123.json").write_text('{"network":{}}', encoding="utf-8")
            (root / "cache" / "qrcode.png").write_bytes(b"png")
            fake = na.NapcatRoot(root, 6199, "t", ["123"], False)
            answers = [False, True]

            def probe(_api, _token):
                return answers.pop(0), "{}"

            with patch.object(na, "find_roots", return_value=[fake]), patch.object(na, "_request", side_effect=OSError("offline")), \
                 patch.object(na, "_probe_api", side_effect=probe), \
                 patch.object(na, "restart", return_value={"ok": True, "pid": 4}) as restarted, \
                 patch.object(na, "_wait_for_new_qr"):
                result = na.auto_setup(S())
            restarted.assert_called_once()
            self.assertTrue(result["restarted"])
            self.assertTrue(result["ok"])
            self.assertFalse(result["restart_required"])
            self.assertEqual([s["name"] for s in result["steps"]], ["find_roots", "configure_onebot", "restart", "qrcode", "connect"])

    def test_auto_setup_skips_restart_when_the_api_already_answers(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            (root / "cache").mkdir()
            (root / "config" / "webui.json").write_text('{"port":6199,"token":"t"}', encoding="utf-8")
            (root / "config" / "onebot11_123.json").write_text('{"network":{}}', encoding="utf-8")
            fake = na.NapcatRoot(root, 6199, "t", ["123"], False)
            with patch.object(na, "find_roots", return_value=[fake]), patch.object(na, "_request", side_effect=OSError("offline")), \
                 patch.object(na, "_probe_api", return_value=(True, "{}")), patch.object(na, "restart") as restarted:
                result = na.auto_setup(S())
            restarted.assert_not_called()
            self.assertFalse(result["restarted"])
            self.assertTrue(result["ok"])
            self.assertEqual([s["name"] for s in result["steps"]], ["find_roots", "configure_onebot", "qrcode", "connect"])

    def test_auto_setup_reports_waiting_for_login_instead_of_failure(self):
        """扫码前 3001 端口本就不会开：restart 后二维码变了就算等待登录，不能报失败。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            (root / "cache").mkdir()
            (root / "config" / "webui.json").write_text('{"port":6199,"token":"t"}', encoding="utf-8")
            (root / "config" / "onebot11_123.json").write_text('{"network":{}}', encoding="utf-8")
            (root / "cache" / "qrcode.png").write_bytes(b"png")
            fake = na.NapcatRoot(root, 6199, "t", ["123"], False)
            with patch.object(na, "find_roots", return_value=[fake]), patch.object(na, "_request", side_effect=OSError("offline")), \
                 patch.object(na, "_probe_api", return_value=(False, "connect refused")), \
                 patch.object(na, "restart", return_value={"ok": True, "pid": 4, "waiting_for_login": True}), \
                 patch.object(na, "_wait_for_new_qr", return_value=True):
                result = na.auto_setup(S())
            self.assertTrue(result["waiting_for_login"])
            self.assertTrue(result["ok"])
            self.assertFalse(result["restart_required"])
            connect = next(s for s in result["steps"] if s["name"] == "connect")
            self.assertTrue(connect["ok"])
            self.assertIn("扫码", connect["detail"])


if __name__ == "__main__":
    unittest.main()
