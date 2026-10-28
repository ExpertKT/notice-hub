"""更新清单签名的验签契约（RS256 / PKCS#1 v1.5，纯标准库）。

测试用一把独立的 1024 位测试私钥（只在这份测试文件里，生产公钥见
``qq_live_digest/update_signing.py`` 的 ``KNOWN_KEYS``），通过 patch 公钥表接入。
真实私钥永不入库、不进测试。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qq_live_digest import update_signing

TEST_KEY_ID = "test-2026"
TEST_N = int(
    "c16bc63d71c2a986cf593d9afd2bc2c3b93972e4f824f0f78c6e4832ea90999a"
    "c9a58b92894438be00f218895ccd2cd9fc300d5e327d8a3073f8e3ab2f7abbd0"
    "f614afe2c6d887804d4445110fff1c620d66c0409fb1fe6ccc7acb8d7d09efb2"
    "c6032a762922af6481ae4de24cd956afdf2b32bd4b323f4855bef0afabf37b5f",
    16,
)
TEST_E = 0x10001
TEST_D = int(
    "12b61e636b51ed9c810a9d0f1556da6c9cca95f6d89a9a45186cc2c74a31daea"
    "427fa554f3ba89d2b7bcb3eaf5f35357733222d8ee017f10d8e746ff1484c70e"
    "70a65a0703af6970962c2fe6556da6f69af63acb588afe8429d3a87dc7cd54b4"
    "fc10b84837d5397b833ca7b46ab03e9af6e59b065d2894469de43fabd5fa0f81",
    16,
)


def _sign_raw(payload: bytes) -> bytes:
    size = (TEST_N.bit_length() + 7) // 8
    digest_info = update_signing.SHA256_DIGEST_INFO + hashlib.sha256(payload).digest()
    padding = size - 3 - len(digest_info)
    em = b"\x00\x01" + b"\xff" * padding + b"\x00" + digest_info
    return pow(int.from_bytes(em, "big"), TEST_D, TEST_N).to_bytes(size, "big")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def base_document() -> dict:
    return {
        "version": 1,
        "items": [
            {
                "tag": "v1.2.0",
                "version": "1.2.0",
                "name": "群务台 1.2.0",
                "notes": "中文说明也要参与签名",
                "published_at": "2026-10-28T00:00:00Z",
                "asset_name": "QQ-Notice-Hub.zip",
                "size": 50883826,
                "sha256": "b" * 64,
                "download_url": "https://github.com/ExpertKT/notice-hub/releases/download/v1.2.0/QQ-Notice-Hub.zip",
            }
        ],
    }


def signed(document: dict | None = None) -> dict:
    doc = base_document() if document is None else document
    out = json.loads(json.dumps(doc))
    out["signature"] = {
        "alg": "RS256",
        "key_id": TEST_KEY_ID,
        "value": _b64(_sign_raw(update_signing.canonical_payload(doc))),
    }
    return out


class SignatureTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.dict(update_signing.KNOWN_KEYS, {TEST_KEY_ID: (TEST_N, TEST_E)})
        patcher.start()
        self.addCleanup(patcher.stop)

    # 1
    def test_signed_manifest_verifies(self) -> None:
        ok, reason = update_signing.verify_manifest_signature(signed())
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "")

    # 2
    def test_any_field_change_invalidates_signature(self) -> None:
        doc = signed()

        def mutate_sha(doc):
            doc["items"][0]["sha256"] = "c" * 64

        def mutate_size(doc):
            doc["items"][0]["size"] = 1

        def mutate_url(doc):
            doc["items"][0]["download_url"] = "https://evil.example/x.zip"

        def mutate_notes(doc):
            doc["items"][0]["notes"] = "改了一个字"

        def mutate_version(doc):
            doc["version"] = 2

        def add_item(doc):
            extra = json.loads(json.dumps(doc["items"][0]))
            extra["tag"] = "v1.2.1"
            extra["version"] = "1.2.1"
            doc["items"].append(extra)

        def remove_item(doc):
            doc["items"].pop()

        for label, mutate in (
            ("sha256", mutate_sha),
            ("size", mutate_size),
            ("download_url", mutate_url),
            ("notes", mutate_notes),
            ("version", mutate_version),
            ("add_item", add_item),
            ("remove_item", remove_item),
        ):
            with self.subTest(field=label):
                broken = json.loads(json.dumps(doc))
                mutate(broken)
                ok, reason = update_signing.verify_manifest_signature(broken)
                self.assertFalse(ok, label)
                self.assertEqual(reason, "签名与公钥不匹配")

    # 3
    def test_missing_signature_is_rejected(self) -> None:
        doc = base_document()
        ok, reason = update_signing.verify_manifest_signature(doc)
        self.assertFalse(ok)
        self.assertEqual(reason, "清单没有签名")
        ok, reason = update_signing.verify_manifest_signature(None)
        self.assertFalse(ok)
        self.assertEqual(reason, "更新清单不是对象")

    # 4
    def test_unsupported_algorithm_is_rejected(self) -> None:
        for alg in ("RS1", "none", "rs256", "RS512", ""):
            with self.subTest(alg=alg):
                doc = signed()
                doc["signature"]["alg"] = alg
                ok, reason = update_signing.verify_manifest_signature(doc)
                self.assertFalse(ok, alg)
                self.assertEqual(reason, "不支持的签名算法")

    # 5
    def test_unknown_key_id_is_rejected(self) -> None:
        doc = signed()
        doc["signature"]["key_id"] = "nh-1999"
        ok, reason = update_signing.verify_manifest_signature(doc)
        self.assertFalse(ok)
        self.assertEqual(reason, "未知的签名公钥")

    # 6
    def test_flipped_signature_bit_is_rejected(self) -> None:
        doc = signed()
        raw = bytearray(base64.urlsafe_b64decode(doc["signature"]["value"] + "=="))
        raw[10] ^= 0x01
        doc["signature"]["value"] = _b64(bytes(raw))
        ok, _reason = update_signing.verify_manifest_signature(doc)
        self.assertFalse(ok)

    # 7
    def test_truncated_or_extended_signature_is_rejected(self) -> None:
        doc = signed()
        raw = base64.urlsafe_b64decode(doc["signature"]["value"] + "==")
        for label, value in (("截断", raw[:-1]), ("加长", raw + b"\x00")):
            with self.subTest(case=label):
                broken = json.loads(json.dumps(doc))
                broken["signature"]["value"] = _b64(value)
                ok, reason = update_signing.verify_manifest_signature(broken)
                self.assertFalse(ok, label)
                self.assertEqual(reason, "签名长度不合法")

    # 8
    def test_signature_from_another_version_is_rejected(self) -> None:
        other = base_document()
        other["version"] = 2
        other["items"][0]["tag"] = "v1.3.0"
        other["items"][0]["version"] = "1.3.0"
        replay = base_document()
        replay["signature"] = signed(other)["signature"]
        ok, _reason = update_signing.verify_manifest_signature(replay)
        self.assertFalse(ok)

    # 9
    def test_reformatting_does_not_break_signature(self) -> None:
        doc = signed()
        # 打乱键序 + indent=4 + 加尾随空行，再解析回对象：规范形式必须仍然对得上。
        shuffled = dict(reversed(list(doc.items())))
        text = json.dumps(shuffled, ensure_ascii=False, indent=4) + "\n\n"
        reparsed = json.loads(text)
        ok, reason = update_signing.verify_manifest_signature(reparsed)
        self.assertTrue(ok, reason)

    # 10
    def test_out_of_range_signature_values_are_rejected(self) -> None:
        size = (TEST_N.bit_length() + 7) // 8
        for label, number in (("s=0", 0), ("s=n", TEST_N), ("s>n", TEST_N + 1)):
            with self.subTest(case=label):
                doc = signed()
                doc["signature"]["value"] = _b64(number.to_bytes(size, "big"))
                ok, reason = update_signing.verify_manifest_signature(doc)
                self.assertFalse(ok, label)
                self.assertEqual(reason, "签名数值不合法")

    def test_bad_base64_is_rejected(self) -> None:
        doc = signed()
        doc["signature"]["value"] = "@@@not-base64@@@"
        ok, reason = update_signing.verify_manifest_signature(doc)
        self.assertFalse(ok)
        self.assertEqual(reason, "签名编码不合法")


@unittest.skipUnless(os.name == "nt", ".NET 交叉验签只在 Windows 上做")
class DotNetInteropTests(unittest.TestCase):
    """证明填充没写歪：Python 签的 .NET 能验，.NET 签的 Python 能验。"""

    def setUp(self) -> None:
        patcher = patch.dict(update_signing.KNOWN_KEYS, {TEST_KEY_ID: (TEST_N, TEST_E)})
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _run_powershell(script: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "interop.ps1"
            path.write_text(script, encoding="utf-8-sig")
            return subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(path)],
                capture_output=True,
                timeout=120,
            )

    def _hex(self, value: int) -> str:
        text = format(value, "x")
        return "0" + text if len(text) % 2 else text

    def test_python_signature_verifies_under_dotnet(self) -> None:
        doc = base_document()
        signature = _sign_raw(update_signing.canonical_payload(doc))
        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp) / "payload.bin"
            data_path.write_bytes(update_signing.canonical_payload(doc))
            sig_path = Path(tmp) / "sig.bin"
            sig_path.write_bytes(signature)
            script = (
                "$ErrorActionPreference='Stop'\n"
                "$params = New-Object System.Security.Cryptography.RSAParameters\n"
                "$params.Modulus = [byte[]]('%s' -split '(..)' | Where-Object {$_} | ForEach-Object {[Convert]::ToByte($_,16)})\n"
                "$params.Exponent = [byte[]]('%s' -split '(..)' | Where-Object {$_} | ForEach-Object {[Convert]::ToByte($_,16)})\n"
                "$rsa = New-Object System.Security.Cryptography.RSACryptoServiceProvider\n"
                "$rsa.ImportParameters($params)\n"
                "$data = [IO.File]::ReadAllBytes('%s')\n"
                "$sig = [IO.File]::ReadAllBytes('%s')\n"
                "if ($rsa.VerifyData($data, [System.Security.Cryptography.SHA256]::Create(), $sig)) { exit 0 } else { exit 2 }\n"
            ) % (self._hex(TEST_N), self._hex(TEST_E), data_path, sig_path)
            result = self._run_powershell(script)
        self.assertEqual(result.returncode, 0, result.stdout.decode("utf-8", "replace") + result.stderr.decode("utf-8", "replace"))

    def test_dotnet_signature_verifies_in_python(self) -> None:
        """反方向：.NET 自己生成密钥并签名，Python 用导出的公钥验。

        不在 .NET 里塞我们这把测试私钥——.NET Framework 的 RSAParameters 做私钥运算
        要求 P/Q/DP/DQ/InverseQ，只给 n/e/d 会抛异常。让 .NET 自己出钥匙同样能证明
        填充和摘要写法一致，且不需要多传任何私钥材料。
        """
        doc = base_document()
        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp) / "payload.bin"
            data_path.write_bytes(update_signing.canonical_payload(doc))
            sig_path = Path(tmp) / "sig.bin"
            key_path = Path(tmp) / "key.txt"
            script = (
                "$ErrorActionPreference='Stop'\n"
                "$rsa = New-Object System.Security.Cryptography.RSACryptoServiceProvider(2048)\n"
                "$data = [IO.File]::ReadAllBytes('%s')\n"
                "$sig = $rsa.SignData($data, [System.Security.Cryptography.SHA256]::Create())\n"
                "[IO.File]::WriteAllBytes('%s', $sig)\n"
                "$p = $rsa.ExportParameters($false)\n"
                "function Hex($b) { ($b | ForEach-Object { $_.ToString('x2') }) -join '' }\n"
                "[IO.File]::WriteAllText('%s', ((Hex $p.Modulus) + '|' + (Hex $p.Exponent)))\n"
            ) % (data_path, sig_path, key_path)
            result = self._run_powershell(script)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
            modulus_hex, exponent_hex = key_path.read_text(encoding="utf-8").strip().split("|")
            signed_doc = json.loads(json.dumps(doc))
            signed_doc["signature"] = {
                "alg": "RS256",
                "key_id": "dotnet-test",
                "value": _b64(sig_path.read_bytes()),
            }
            with patch.dict(
                update_signing.KNOWN_KEYS,
                {"dotnet-test": (int(modulus_hex, 16), int(exponent_hex, 16))},
            ):
                ok, reason = update_signing.verify_manifest_signature(signed_doc)
        self.assertTrue(ok, reason)


if __name__ == "__main__":
    unittest.main()
