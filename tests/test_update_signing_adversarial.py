"""对抗性测试：试图骗过更新清单验签（RS256 / PKCS#1 v1.5）。

目的不是重复 ``tests/test_update_signing.py`` 的正向契约，而是站在攻击者角度逐条试：
改字段、重放别人的签名、改算法/公钥名、翻转/截断签名、喂畸形结构——每一条都必须
失败关闭（``ok is False`` 且 ``reason`` 非空）。

用一把**本文件独立的离线测试私钥**（1024 位，只在测试里）通过 patch 公钥表接入，
生产公钥 ``update_signing.KNOWN_KEYS`` 一个字节都不改。另有一条用真实私钥
（仓库外、只读、内容不打印）签发临时清单的互操作测试，用来证明生产公钥与私钥确实配对。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest import update_signing, updater  # noqa: E402

TEST_KEY_ID = "adv-2026"
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
PRODUCTION_KEY_PATH = Path.home() / ".dsh" / "nh-signing" / "update-signing-key.json"
SIGN_SCRIPT = PROJECT_ROOT / "release" / "sign-manifest.py"


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


def _powerpoint(hex_value: int) -> str:
    text = format(hex_value, "x")
    return "0" + text if len(text) % 2 else text


class _PatchedKey(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.dict(update_signing.KNOWN_KEYS, {TEST_KEY_ID: (TEST_N, TEST_E)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_rejected(self, document, *, contains: str | None = None) -> str:
        ok, reason = update_signing.verify_manifest_signature(document)
        self.assertIs(ok, False, "这份清单居然通过了验签")
        self.assertIsInstance(reason, str)
        self.assertTrue(reason.strip(), "拒绝时必须给出非空原因")
        if contains:
            self.assertIn(contains, reason)
        return reason


class FieldTamperingTest(_PatchedKey):
    """1–6：签名覆盖的每个字段，改一个字节都必须验不过。"""

    def test_sha256_changed_to_another_valid_hex_is_rejected(self) -> None:
        doc = signed()
        doc["items"][0]["sha256"] = "c" * 64
        self.assert_rejected(doc, contains="签名与公钥不匹配")

    def test_size_plus_one_byte_is_rejected(self) -> None:
        doc = signed()
        doc["items"][0]["size"] += 1
        self.assert_rejected(doc)

    def test_download_url_same_host_different_path_is_rejected(self) -> None:
        doc = signed()
        doc["items"][0]["download_url"] = doc["items"][0]["download_url"].replace(".zip", "-evil.zip")
        self.assert_rejected(doc)

    def test_one_character_in_notes_is_rejected(self) -> None:
        doc = signed()
        doc["items"][0]["notes"] = doc["items"][0]["notes"].replace("中文", "中交")
        self.assert_rejected(doc)

    def test_version_bumped_is_rejected(self) -> None:
        doc = signed()
        doc["version"] = 2
        self.assert_rejected(doc)

    def test_appending_an_item_is_rejected(self) -> None:
        doc = signed()
        extra = json.loads(json.dumps(doc["items"][0]))
        extra["tag"] = "v1.3.0"
        extra["version"] = "1.3.0"
        doc["items"].append(extra)
        self.assert_rejected(doc)

    def test_dropping_an_item_is_rejected(self) -> None:
        doc = signed()
        extra = json.loads(json.dumps(doc["items"][0]))
        extra["tag"] = "v1.3.0"
        extra["version"] = "1.3.0"
        doc["items"].append(extra)
        resign = signed(doc)
        resign["items"] = resign["items"][:1]
        self.assert_rejected(resign)

    def test_reordering_items_is_rejected(self) -> None:
        doc = signed()
        extra = json.loads(json.dumps(doc["items"][0]))
        extra["tag"] = "v0.9.0"
        extra["version"] = "0.9.0"
        doc["items"].append(extra)
        resign = signed(doc)
        resign["items"] = list(reversed(resign["items"]))
        self.assert_rejected(resign)


class SignatureEnvelopeTest(_PatchedKey):
    """7–15：签名信封本身与文档结构。"""

    def test_missing_signature_is_rejected(self) -> None:
        doc = base_document()
        self.assert_rejected(doc, contains="没有签名")

    def test_algorithm_variants_are_rejected(self) -> None:
        for alg in ("rs256", "RS1", "none", "", None, 256, "RSA"):
            doc = signed()
            doc["signature"]["alg"] = alg
            self.assert_rejected(doc, contains="不支持的签名算法")

    def test_unknown_key_id_variants_are_rejected(self) -> None:
        for key_id in ("nh-2026-2", "", None, 1, ["nh-2026-1"]):
            doc = signed()
            doc["signature"]["key_id"] = key_id
            self.assert_rejected(doc, contains="未知的签名公钥")

    def test_flipping_one_bit_of_the_signature_is_rejected(self) -> None:
        doc = signed()
        raw = bytearray(base64.urlsafe_b64decode(doc["signature"]["value"] + "=="))
        raw[0] ^= 0x01
        doc["signature"]["value"] = _b64(bytes(raw))
        self.assert_rejected(doc)

    def test_flipping_a_bit_in_the_middle_of_the_signature_is_rejected(self) -> None:
        doc = signed()
        raw = bytearray(base64.urlsafe_b64decode(doc["signature"]["value"] + "=="))
        raw[len(raw) // 2] ^= 0x80
        doc["signature"]["value"] = _b64(bytes(raw))
        self.assert_rejected(doc)

    def test_truncated_signature_is_rejected(self) -> None:
        doc = signed()
        raw = base64.urlsafe_b64decode(doc["signature"]["value"] + "==")
        doc["signature"]["value"] = _b64(raw[:-1])
        self.assert_rejected(doc, contains="签名长度不合法")

    def test_extended_signature_is_rejected(self) -> None:
        doc = signed()
        raw = base64.urlsafe_b64decode(doc["signature"]["value"] + "==")
        doc["signature"]["value"] = _b64(raw + b"\x00")
        self.assert_rejected(doc, contains="签名长度不合法")

    def test_signature_replayed_from_another_manifest_is_rejected(self) -> None:
        other = base_document()
        other["version"] = 1
        other["items"][0]["tag"] = "v9.9.9"
        other["items"][0]["version"] = "9.9.9"
        donor = signed(other)
        stolen = signed()
        stolen["signature"] = donor["signature"]
        self.assert_rejected(stolen)

    def test_malformed_signature_values_are_rejected(self) -> None:
        for value in ("!!!!", "", "A" * 5000, "签名", 123, [], {}):
            doc = signed()
            doc["signature"]["value"] = value
            self.assert_rejected(doc)

    def test_signature_envelope_of_wrong_type_is_rejected(self) -> None:
        for signature in ([], "RS256", None, 1, True):
            doc = base_document()
            doc["signature"] = signature
            self.assert_rejected(doc, contains="没有签名")

    def test_document_of_wrong_type_is_rejected(self) -> None:
        for document in ([], "{}", None, 1, True):
            self.assert_rejected(document, contains="不是对象")

    def test_out_of_range_signature_numbers_are_rejected(self) -> None:
        size = (TEST_N.bit_length() + 7) // 8
        for number in (0, TEST_N, TEST_N + 1):
            doc = signed()
            doc["signature"]["value"] = _b64(number.to_bytes(size, "big"))
            self.assert_rejected(doc, contains="签名数值不合法")


class PositiveCasesTest(_PatchedKey):
    """16：合法签名必须过，而且与服务器排版无关（规范形式在本地重算）。"""

    def test_signed_manifest_verifies(self) -> None:
        ok, reason = update_signing.verify_manifest_signature(signed())
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "")

    def test_reformatting_and_key_shuffling_do_not_break_verification(self) -> None:
        original = signed()
        reordered = {key: original[key] for key in reversed(list(original.keys()))}
        for text in (
            json.dumps(reordered, ensure_ascii=False, indent=4),
            json.dumps(reordered, ensure_ascii=True),
            json.dumps(reordered, ensure_ascii=False, separators=(", ", " : ")),
        ):
            reparsed = json.loads(text)
            ok, reason = update_signing.verify_manifest_signature(reparsed)
            self.assertTrue(ok, reason)

    def test_canonical_payload_ignores_signature_field(self) -> None:
        doc = base_document()
        with_signature = dict(doc)
        with_signature["signature"] = {"alg": "RS256", "key_id": "x", "value": "y"}
        self.assertEqual(
            update_signing.canonical_payload(doc),
            update_signing.canonical_payload(with_signature),
        )

    def test_changing_signature_field_alone_does_not_change_payload(self) -> None:
        first = signed()
        second = signed()
        second["signature"]["value"] = "A" * 10
        self.assertEqual(
            update_signing.canonical_payload(first),
            update_signing.canonical_payload(second),
        )


@unittest.skipUnless(os.name == "nt", ".NET 交叉验签只在 Windows 上做")
class DotNetInteropTest(_PatchedKey):
    """17：双向互操作，证明填充与摘要没写歪。"""

    @staticmethod
    def _run_powershell(script: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "interop.ps1"
            path.write_text(script, encoding="utf-8-sig")
            return subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(path)],
                capture_output=True,
                timeout=180,
            )

    def test_python_signature_verifies_under_dotnet(self) -> None:
        doc = base_document()
        payload = update_signing.canonical_payload(doc)
        signature = _sign_raw(payload)
        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp) / "payload.bin"
            data_path.write_bytes(payload)
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
            ) % (_powerpoint(TEST_N), _powerpoint(TEST_E), data_path, sig_path)
            result = self._run_powershell(script)
        self.assertEqual(
            result.returncode,
            0,
            result.stdout.decode("utf-8", "replace") + result.stderr.decode("utf-8", "replace"),
        )

    def test_dotnet_signature_verifies_in_python(self) -> None:
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
                "key_id": "dotnet-adv",
                "value": _b64(sig_path.read_bytes()),
            }
            with patch.dict(
                update_signing.KNOWN_KEYS,
                {"dotnet-adv": (int(modulus_hex, 16), int(exponent_hex, 16))},
            ):
                ok, reason = update_signing.verify_manifest_signature(signed_doc)
        self.assertTrue(ok, reason)


@unittest.skipUnless(PRODUCTION_KEY_PATH.is_file(), "仓库外没有生产私钥（只读），跳过生产钥互操作")
class ProductionKeyInteropTest(unittest.TestCase):
    """用真实离线私钥签一份临时清单，证明程序内置公钥与这把私钥确实配对。

    私钥只通过 ``--key`` 路径传给签名脚本，测试不读、不打印、不复制其内容。
    """

    def test_sign_script_output_verifies_with_builtin_public_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "update-manifest.json"
            manifest_path.write_text(
                json.dumps(base_document(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            result = subprocess.run(
                [sys.executable, "-X", "utf8", str(SIGN_SCRIPT), "--key", str(PRODUCTION_KEY_PATH), "--manifest", str(manifest_path)],
                capture_output=True,
                timeout=120,
                cwd=str(PROJECT_ROOT),
            )
            self.assertEqual(
                result.returncode,
                0,
                result.stdout.decode("utf-8", "replace") + result.stderr.decode("utf-8", "replace"),
            )
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
            signature = document.get("signature") or {}
            self.assertEqual(signature.get("alg"), "RS256")
            self.assertIn(signature.get("key_id"), update_signing.KNOWN_KEYS)
            ok, reason = update_signing.verify_manifest_signature(document)
            self.assertTrue(ok, reason)

            # 改一个字节必须不再通过（证明验的是这份内容，不是别的）
            document["items"][0]["size"] += 1
            ok, reason = update_signing.verify_manifest_signature(document)
            self.assertFalse(ok)
            self.assertTrue(reason)


class FailClosedWiringTest(_PatchedKey):
    """接线检查：验签在取字段之前；ManifestSignatureError 不被上层兜底吞掉。"""

    def test_signature_is_checked_before_any_item_field_is_used(self) -> None:
        # items 结构本身就是坏的（缺少全部必需字段）：如果先解析字段，报的会是 items 错误。
        broken = {"version": 1, "items": [{"tag": 1}], "signature": {"alg": "RS256", "key_id": "nope", "value": "x"}}
        with self.assertRaises(updater.ManifestSignatureError) as context:
            updater._manifest_entries(broken)
        self.assertIn("签名校验失败", str(context.exception))

    def test_manifest_version_is_checked_before_signature(self) -> None:
        # 结构版本在验签之前：不接受「先按未知结构解析」。
        with self.assertRaises(ValueError) as context:
            updater._manifest_entries({"version": 2, "items": [], "signature": {}})
        self.assertNotIsInstance(context.exception, updater.ManifestSignatureError)

    def test_signature_failure_is_not_downgraded_to_a_network_error(self) -> None:
        source_list = [
            {"id": "github", "repo": "ExpertKT/notice-hub", "api_root": "https://api.github.com", "raw_base": "https://raw.githubusercontent.com/ExpertKT/notice-hub/main"},
            {"id": "gitee", "repo": "ExpertKT/notice-hub-mirror", "api_root": "https://gitee.com/api/v5", "raw_base": "https://gitee.com/ExpertKT/notice-hub-mirror/raw/master"},
        ]
        release = {
            "tag_name": "v9.9.9",
            "name": "群务台 9.9.9",
            "body": "notes",
            "published_at": "2026-10-28T00:00:00Z",
            "assets": [
                {
                    "name": updater.ASSET_NAME,
                    "size": 123,
                    "digest": "sha256:" + "d" * 64,
                    "browser_download_url": "https://github.com/ExpertKT/notice-hub/releases/download/v9.9.9/QQ-Notice-Hub.zip",
                }
            ],
        }
        unsigned_manifest = base_document()  # 没有 signature 字段
        with (
            patch.object(updater.update_sources, "sources", lambda settings=None: source_list),
            patch.object(updater, "_fetch_json", return_value=[release]),
            patch.object(updater, "_fetch_manifest", return_value=unsigned_manifest),
        ):
            result = updater.check(channel="beta")
        self.assertFalse(result["ok"])
        error = str(result["error"])
        self.assertIn("签名校验失败", error)
        self.assertNotIn("更新源均不可用", error)
        self.assertNotIn("更新检查失败", error)
        self.assertTrue(all(entry["error"] for entry in result["sources_tried"] if not entry["ok"]))


if __name__ == "__main__":
    unittest.main()
