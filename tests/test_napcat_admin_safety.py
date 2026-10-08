import io
import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from qq_live_digest import napcat_admin as na


class Settings:
    onebot_port = 8765
    onebot_token = "event-token"
    napcat_api_url = "http://127.0.0.1:3001"
    napcat_api_token = "api-token"


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def archive_bytes(*, malicious=False, valid=True):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as zf:
        if malicious:
            zf.writestr("../escape.txt", b"escape")
        elif valid:
            zf.writestr("versions/1/resources/app/napcat/NapCatWinBootMain.exe", b"boot")
            zf.writestr("versions/1/resources/app/napcat/NapCatWinBootHook.dll", b"hook")
            zf.writestr("versions/1/resources/app/napcat/qqnt.json", b"{}")
            zf.writestr("QQ.exe", b"qq")
        else:
            zf.writestr("readme.txt", b"not a bootable install")
    return stream.getvalue()


class NapcatInstallSafetyTests(unittest.TestCase):
    def setUp(self):
        na._LAST_INSTALL_ROOT = None

    def test_download_failure_preserves_existing_tree_and_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td)
            target = data / "napcat"
            target.mkdir()
            original = target / "config.bin"
            original.write_bytes(b"old-install-bytes")
            with patch.object(na.urllib.request, "urlopen", side_effect=OSError("offline")):
                result = na.install(type("S", (), {"data_dir": data})())
            self.assertFalse(result["ok"])
            self.assertEqual(original.read_bytes(), b"old-install-bytes")
            self.assertTrue(target.is_dir())

    def test_path_traversal_is_rejected_before_replacement(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td)
            target = data / "napcat"
            target.mkdir()
            original = target / "keep.txt"
            original.write_bytes(b"keep")
            with patch.object(na.urllib.request, "urlopen", return_value=Response(archive_bytes(malicious=True))):
                result = na.install(type("S", (), {"data_dir": data})())
            self.assertFalse(result["ok"])
            self.assertEqual(original.read_bytes(), b"keep")
            self.assertFalse((data / "escape.txt").exists())

    def test_success_replaces_old_tree_and_points_last_root_to_target(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td)
            target = data / "napcat"
            target.mkdir()
            (target / "old.txt").write_bytes(b"old")
            with patch.dict("os.environ", {"QQ_DIGEST_DATA_DIR": td}, clear=False), patch.object(na.urllib.request, "urlopen", return_value=Response(archive_bytes())):
                result = na.install(type("S", (), {"data_dir": data})())
            self.assertTrue(result["ok"], result)
            self.assertEqual(na._LAST_INSTALL_ROOT, target.resolve())
            self.assertTrue((target / "versions/1/resources/app/napcat/NapCatWinBootMain.exe").is_file())
            self.assertFalse((target / "old.txt").exists())
            self.assertEqual(list(data.glob("napcat-old-*")), [])

    def test_path_check_is_not_fooled_by_unresolved_data_dir(self):
        """data_dir 落在 junction/symlink 或 8.3 短名路径下时，合法压缩包仍须安装成功。

        GitHub Actions 的 TEMP 是 C:\\Users\\RUNNER~1\\...，未解析的 payload 与已解析的
        destination 口径不一致，会误报「压缩包路径越界」——本用例是本机可复现的等价场景。
        """
        with tempfile.TemporaryDirectory() as td:
            real = Path(td) / "real"
            real.mkdir()
            link = Path(td) / "link"
            made = False
            if os.name == "nt":
                made = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(real)],
                    capture_output=True,
                    text=True,
                ).returncode == 0
            if not made:
                try:
                    link.symlink_to(real, target_is_directory=True)
                    made = True
                except (OSError, NotImplementedError):
                    made = False
            if not made:
                self.skipTest("本机无法创建 junction/symlink")
            data = link / "data"
            data.mkdir()
            with patch.object(na.urllib.request, "urlopen", return_value=Response(archive_bytes())):
                result = na.install(type("S", (), {"data_dir": data})())
            self.assertTrue(result["ok"], result)
            self.assertTrue((data / "napcat" / "QQ.exe").is_file())

    def test_file_config_creates_timestamped_backup_before_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_dir = root / "config"
            config_dir.mkdir()
            path = config_dir / "onebot11_42.json"
            original = {"keep": "original", "network": {"httpServers": [], "httpClients": []}}
            path.write_text(json.dumps(original), encoding="utf-8")
            na._file_config(root, "42", Settings())
            backups = list(config_dir.glob("onebot11_42.json.bak-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8")), original)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["keep"], "original")

    def test_bad_json_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_dir = root / "config"
            config_dir.mkdir()
            path = config_dir / "onebot11_42.json"
            path.write_text("{bad json", encoding="utf-8")
            with self.assertRaises(ValueError):
                na._file_config(root, "42", Settings())
            self.assertEqual(path.read_text(encoding="utf-8"), "{bad json")
            self.assertEqual(list(config_dir.glob("onebot11_42.json.bak-*")), [])


if __name__ == "__main__":
    unittest.main()
