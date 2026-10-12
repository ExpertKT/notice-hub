from __future__ import annotations

import hashlib
import hmac
import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.wechat_probe import PAGE_SIZE, SQLITE_MAGIC, cbc_crypt, decrypt_page, main, reference_key, reference_page, sqlcipher_page_candidates, valid_page, wal_generation


class WechatProbeTest(unittest.TestCase):
    def test_sqlite_magic_accepts_only_correct_page(self) -> None:
        good = SQLITE_MAGIC + b"\0" * (PAGE_SIZE - len(SQLITE_MAGIC))
        bad = b"not sqlite" + b"\0" * (PAGE_SIZE - 10)
        self.assertTrue(valid_page(good))
        self.assertFalse(valid_page(bad))

    def test_wal_generation_discards_old_salt_and_tail(self) -> None:
        current, old = (11, 22), (1, 2)
        header = b"\x37\x7f\x06\x82" + b"\0" * 12 + struct.pack(">II", *current)
        def frame(salt: tuple[int, int], page: int) -> bytes:
            return struct.pack(">IIIIII", page, 0, *salt, 0, 0) + bytes([page]) * PAGE_SIZE
        salt, frames = wal_generation(header + frame(current, 1) + frame(old, 2) + frame(current, 3) + b"tail")
        self.assertEqual(salt, current)
        self.assertEqual([page for page, _ in frames], [1])

    @unittest.skipUnless(os.name == "nt", "Windows CNG test")
    def test_cng_aes_cbc_known_vector_round_trip(self) -> None:
        key = bytes.fromhex("603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4")
        iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        plain = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
        cipher = cbc_crypt(plain, key, iv, decrypt=False)
        self.assertEqual(cipher.hex(), "f58c4c04d6e5f1ba779eabfb5f7bfbd6")
        self.assertEqual(cbc_crypt(cipher, key, iv, decrypt=True), plain)

    def test_64_hex_candidate_decoded_and_raw_kdf_path_included(self) -> None:
        page = b"X" * PAGE_SIZE
        labels = []
        with patch("tools.wechat_probe.cbc_crypt", side_effect=lambda data, key, iv, decrypt: labels.append(len(key)) or b"X" * len(data)):
            list(sqlcipher_page_candidates(page, b"00" * 32))
        self.assertEqual(len(labels), 16)
        self.assertTrue(all(length == 32 for length in labels))

    def test_reference_per_database_kdf_and_hmac_validation(self) -> None:
        master, salt = b"M" * 32, bytes(range(16))
        db_key = reference_key(master, salt)
        mac_key = hashlib.pbkdf2_hmac("sha512", db_key, bytes(value ^ 0x3A for value in salt), 2, 32)
        iv = b"I" * 16
        encrypted = b"E" * (PAGE_SIZE - 16 - 80)
        mac = hmac.new(mac_key, encrypted + iv + struct.pack("<I", 1), hashlib.sha512).digest()
        page = salt + encrypted + iv + mac
        with patch("tools.wechat_probe.cbc_crypt", return_value=b"\0" * len(encrypted)):
            passed, plain = reference_page(page, master)
        self.assertTrue(passed)
        self.assertEqual(plain[:16], SQLITE_MAGIC)
        damaged = page[:-1] + bytes([page[-1] ^ 1])
        self.assertFalse(reference_page(damaged, master)[0])

    def test_decrypt_page_restores_header_and_reserve(self) -> None:
        encrypted = b"S" * PAGE_SIZE
        with patch("tools.wechat_probe._cbc_with_aes", side_effect=lambda data, *_args, **_kwargs: b"P" * len(data)) as cbc:
            first = decrypt_page(encrypted, b"K" * 32, 1, object())
            other = decrypt_page(encrypted, b"K" * 32, 2, object())
        self.assertEqual(first[:16], SQLITE_MAGIC)
        self.assertEqual(first[-80:], bytes(80))
        self.assertEqual(other[:PAGE_SIZE - 80], b"P" * (PAGE_SIZE - 80))
        self.assertEqual(cbc.call_count, 2)

    def test_wrong_key_does_not_pass_magic(self) -> None:
        page = b"X" * PAGE_SIZE
        with patch("tools.wechat_probe.cbc_crypt", return_value=b"X" * 4032):
            plains = [plain for _, plain in sqlcipher_page_candidates(page, b"wrong")]
        self.assertTrue(plains)
        self.assertFalse(any(valid_page(plain) for plain in plains))


    def test_optional_database_failure_keeps_required_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage, out = Path(tmp), Path(tmp) / "out"
            markers = {"message/message_0.db": 0x11, "session/session.db": 0x22, "contact/contact.db": 0x33}
            for relative, marker in markers.items():
                path = storage / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(bytes([marker]) * (PAGE_SIZE * 2))
            candidate = b"K" * 32

            def fake_check(page: bytes, key: bytes):
                accepted = page[0] == markers["message/message_0.db"] and key == candidate
                return accepted, SQLITE_MAGIC + bytes(PAGE_SIZE - 16) if accepted else None

            def fake_decrypt(source: Path, destination: Path, key: bytes) -> dict:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"decrypted copy")
                return {"source_bytes": source.stat().st_size}

            with patch.multiple(
                "tools.wechat_probe",
                find_pids=lambda: [4242],
                scan_process=lambda pid: ({candidate}, 1, 1, 1),
                locate_storage=lambda: storage,
                check_page_key=fake_check,
                decrypt_database=fake_decrypt,
            ):
                code = main(["--out", str(out)])
            payload = json.loads((out / "probe.json").read_text(encoding="utf-8"))
            results = {item["database"]: item for item in payload["database_results"]}
            self.assertEqual(code, 0)
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["required_matched"])
            self.assertEqual(payload["required_database"], "message/message_0.db")
            self.assertTrue(results["message/message_0.db"]["match"])
            self.assertTrue(results["message/message_0.db"]["required"])
            self.assertFalse(results["session/session.db"]["match"])
            self.assertFalse(results["session/session.db"]["required"])
            self.assertIsNotNone(results["session/session.db"]["reason"])
            self.assertIn("message/message_0.db", payload["decrypted_databases"])
            self.assertNotIn("session/session.db", payload["decrypted_databases"])
            self.assertTrue((out / "message" / "message_0.db").is_file())


if __name__ == "__main__":
    unittest.main()
