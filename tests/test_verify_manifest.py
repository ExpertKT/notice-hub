"""tools/verify_manifest.py 的用例：全部在临时目录里造假 manifest，不碰真仓库文件。"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "verify_manifest.py"
_SPEC = importlib.util.spec_from_file_location("verify_manifest", _SCRIPT)
verify_manifest = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(verify_manifest)

_SHA = "2ade3385c928a7e18f179751e7173b834ed4a31ef405598275dd535394d583d8"


def _item(version, sha=_SHA, size=50757062):
    return {
        "tag": "v" + version,
        "version": version,
        "name": "群务台 %s" % version,
        "size": size,
        "sha256": sha,
        "download_url": "https://gitee.com/ExpertKT/notice-hub-mirror/releases/download/v%s/QQ-Notice-Hub.zip" % version,
    }


def _manifest(*versions):
    return {"version": 1, "items": [_item(version) for version in versions]}


class VerifyManifestTests(unittest.TestCase):
    def _run(self, payload):
        """把 payload 写成临时文件后跑一遍，返回 (退出码, 输出)。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "update-manifest.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            code = verify_manifest.main(["verify_manifest.py", str(path)])
        return code

    def test_valid_manifest_exits_zero(self):
        self.assertEqual(0, self._run(_manifest("1.1.0-beta.11", "1.1.0-beta.9", "1.0.0")))

    def test_sha256_wrong_length_exits_one(self):
        payload = _manifest("1.1.0-beta.11")
        payload["items"][0]["sha256"] = _SHA[:40]
        self.assertEqual(1, self._run(payload))

    def test_wrong_order_exits_one(self):
        # 字符串排序会把 beta.9 排在 beta.11 前面，这里必须按数字比。
        self.assertEqual(1, self._run(_manifest("1.1.0-beta.9", "1.1.0-beta.11")))

    def test_missing_field_exits_one(self):
        payload = _manifest("1.1.0-beta.11")
        del payload["items"][0]["download_url"]
        self.assertEqual(1, self._run(payload))

    def test_repo_manifest_passes(self):
        repo_manifest = _SCRIPT.parent.parent / "release" / "update-manifest.json"
        self.assertEqual(0, verify_manifest.main(["verify_manifest.py", str(repo_manifest)]))


if __name__ == "__main__":
    unittest.main()
