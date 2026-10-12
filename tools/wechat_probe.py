"""Read-only WeChat 4.x SQLCipher probe (Windows, no injection)."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import struct
from pathlib import Path
from typing import Iterable

PAGE_SIZE = 4096
SQLITE_MAGIC = b"SQLite format 3\x00"
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
READABLE = {0x02, 0x04, 0x20, 0x40, 0x80, 0x01}  # R, RW, RX, RWX, WRITECOPY


def _check(status: int, label: str) -> None:
    if status < 0:
        raise OSError(f"BCrypt failed: {label} ({status:#x})")


class CngAesEcb:
    def __init__(self, key: bytes):
        if os.name != "nt":
            raise OSError("CNG requires Windows")
        self.dll = ctypes.WinDLL("bcrypt", use_last_error=True)
        self.alg = ctypes.c_void_p()
        self.handle = ctypes.c_void_p()
        self.obj = None
        h = ctypes.c_void_p
        u = ctypes.c_ulong
        self.dll.BCryptOpenAlgorithmProvider.argtypes = [ctypes.POINTER(h), ctypes.c_wchar_p, ctypes.c_wchar_p, u]
        self.dll.BCryptOpenAlgorithmProvider.restype = ctypes.c_long
        self.dll.BCryptSetProperty.argtypes = [h, ctypes.c_wchar_p, ctypes.c_void_p, u, u]
        self.dll.BCryptSetProperty.restype = ctypes.c_long
        self.dll.BCryptGetProperty.argtypes = [h, ctypes.c_wchar_p, ctypes.c_void_p, u, ctypes.POINTER(u), u]
        self.dll.BCryptGetProperty.restype = ctypes.c_long
        self.dll.BCryptGenerateSymmetricKey.argtypes = [h, ctypes.POINTER(h), ctypes.c_void_p, u, ctypes.c_void_p, u, u]
        self.dll.BCryptGenerateSymmetricKey.restype = ctypes.c_long
        self.dll.BCryptDecrypt.argtypes = [h, ctypes.c_void_p, u, ctypes.c_void_p, ctypes.c_void_p, u, ctypes.c_void_p, u, ctypes.POINTER(u), u]
        self.dll.BCryptDecrypt.restype = ctypes.c_long
        self.dll.BCryptEncrypt.argtypes = [h, ctypes.c_void_p, u, ctypes.c_void_p, ctypes.c_void_p, u, ctypes.c_void_p, u, ctypes.POINTER(u), u]
        self.dll.BCryptEncrypt.restype = ctypes.c_long
        self.dll.BCryptDestroyKey.argtypes = [h]
        self.dll.BCryptCloseAlgorithmProvider.argtypes = [h, u]
        _check(self.dll.BCryptOpenAlgorithmProvider(ctypes.byref(self.alg), "AES", None, 0), "open")
        mode = ctypes.create_unicode_buffer("ChainingModeECB")
        _check(self.dll.BCryptSetProperty(self.alg, "ChainingMode", mode, len("ChainingModeECB") * 2, 0), "ECB")
        length, out = ctypes.c_ulong(), ctypes.c_ulong()
        _check(self.dll.BCryptGetProperty(self.alg, "ObjectLength", ctypes.byref(length), 4, ctypes.byref(out), 0), "length")
        self.obj = ctypes.create_string_buffer(length.value)
        secret = ctypes.create_string_buffer(key, len(key))
        _check(self.dll.BCryptGenerateSymmetricKey(self.alg, ctypes.byref(self.handle), self.obj, length.value, secret, len(key), 0), "key")

    def block_decrypt(self, block: bytes) -> bytes:
        return self._crypt(block, False)

    def block_encrypt(self, block: bytes) -> bytes:
        return self._crypt(block, True)

    def _crypt(self, block: bytes, encrypt: bool) -> bytes:
        if len(block) != 16:
            raise ValueError("AES block must be 16 bytes")
        src, dst, n = ctypes.create_string_buffer(block), ctypes.create_string_buffer(16), ctypes.c_ulong()
        fn = self.dll.BCryptEncrypt if encrypt else self.dll.BCryptDecrypt
        _check(fn(self.handle, src, 16, None, None, 0, dst, 16, ctypes.byref(n), 0), "crypt")
        return dst.raw[:16]

    def close(self) -> None:
        if self.handle and self.handle.value:
            self.dll.BCryptDestroyKey(self.handle)
            self.handle = ctypes.c_void_p()
        if self.alg and self.alg.value:
            self.dll.BCryptCloseAlgorithmProvider(self.alg, 0)
            self.alg = ctypes.c_void_p()

    def __enter__(self): return self
    def __exit__(self, *_): self.close()


def _cbc_with_aes(data: bytes, aes: CngAesEcb, iv: bytes, *, decrypt: bool) -> bytes:
    if len(data) % 16 or len(iv) != 16:
        raise ValueError("CBC data and IV must be block aligned")
    result, previous = [], iv
    for offset in range(0, len(data), 16):
        block = data[offset:offset + 16]
        if decrypt:
            plain = bytes(a ^ b for a, b in zip(aes.block_decrypt(block), previous))
            previous, result = block, result + [plain]
        else:
            mixed = bytes(a ^ b for a, b in zip(block, previous))
            cipher = aes.block_encrypt(mixed)
            previous, result = cipher, result + [cipher]
    return b"".join(result)


def cbc_crypt(data: bytes, key: bytes, iv: bytes, *, decrypt: bool) -> bytes:
    with CngAesEcb(key) as aes:
        return _cbc_with_aes(data, aes, iv, decrypt=decrypt)


def sqlcipher_page_candidates(page: bytes, key_material: bytes) -> Iterable[tuple[str, bytes]]:
    """Try the documented SQLCipher key derivation alternatives."""
    if len(page) < PAGE_SIZE:
        return
    salt = page[:16]
    key32 = key_material if len(key_material) == 32 else bytes.fromhex(key_material.decode("ascii")) if len(key_material) == 64 else key_material
    derivations = [
        ("raw-key32", key32),
        ("sha512-256000-key32", hashlib.pbkdf2_hmac("sha512", key32, salt, 256000, 32)),
        ("sha1-64000-key32", hashlib.pbkdf2_hmac("sha1", key32, salt, 64000, 32)),
    ]
    if key_material != key32:
        derivations.append(("sha512-256000-ascii64", hashlib.pbkdf2_hmac("sha512", key_material, salt, 256000, 32)))
    elif len(key_material) == 32:
        derivations.append(("sha512-256000-ascii32-control", hashlib.pbkdf2_hmac("sha512", key_material, salt, 256000, 32)))
    for derivation_name, key in derivations:
        for reserve in (48, 80):
            end = PAGE_SIZE - reserve
            ciphertext = page[16:end]
            for iv_name, iv in (("salt", salt), ("reserve", page[end:end + 16])):
                if len(ciphertext) % 16:
                    continue
                try:
                    plain = cbc_crypt(ciphertext, key, iv, decrypt=True)
                except (OSError, ValueError):
                    continue
                yield f"{derivation_name}-page4096-reserve{reserve}-iv{iv_name}", plain


def reference_key(master: bytes, salt: bytes) -> bytes:
    """Reference spec: derive per-database key from 32-byte master + file salt."""
    return hashlib.pbkdf2_hmac("sha512", master, salt, 256000, dklen=32)


def check_page_key(page: bytes, key: bytes) -> tuple[bool, bytes | None]:
    """Verify SQLCipher v4 HMAC and decrypt page one with an already-derived key."""
    if len(page) != PAGE_SIZE or len(key) != 32:
        return False, None
    salt = page[:16]
    mac_salt = bytes(byte ^ 0x3A for byte in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", key, mac_salt, 2, dklen=32)
    mac_data = page[16:PAGE_SIZE - 80 + 16]
    stored_mac = page[PAGE_SIZE - 64:PAGE_SIZE]
    import hmac
    if not hmac.compare_digest(hmac.new(mac_key, mac_data + struct.pack("<I", 1), hashlib.sha512).digest(), stored_mac):
        return False, None
    iv = page[PAGE_SIZE - 80:PAGE_SIZE - 64]
    encrypted = page[16:PAGE_SIZE - 80]
    plain = SQLITE_MAGIC + cbc_crypt(encrypted, key, iv, decrypt=True) + b"\0" * 80
    return valid_page(plain), plain


def reference_page(page: bytes, master: bytes) -> tuple[bool, bytes | None]:
    return check_page_key(page, reference_key(master, page[:16]))


def valid_page(page: bytes) -> bool:
    return page[:16] == SQLITE_MAGIC


def decrypt_page(page: bytes, key: bytes, page_no: int, aes: CngAesEcb) -> bytes:
    if len(page) != PAGE_SIZE:
        raise ValueError("Encrypted page must be 4096 bytes")
    iv = page[PAGE_SIZE - 80:PAGE_SIZE - 64]
    if page_no == 1:
        return SQLITE_MAGIC + _cbc_with_aes(page[16:PAGE_SIZE - 80], aes, iv, decrypt=True) + bytes(80)
    return _cbc_with_aes(page[:PAGE_SIZE - 80], aes, iv, decrypt=True) + bytes(80)


def decrypt_database(source: Path, destination: Path, key: bytes) -> dict:
    raw_size = source.stat().st_size
    with source.open("rb") as stream:
        first = stream.read(PAGE_SIZE)
    passed, _ = check_page_key(first, key)
    if not passed:
        raise ValueError(f"Page-1 HMAC/magic failed: {source.name}")
    wal_path = Path(str(source) + "-wal")
    wal_data = wal_path.read_bytes() if wal_path.is_file() else b""
    wal_salt, frames = wal_generation(wal_data)
    last_commit_size = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with CngAesEcb(key) as aes, source.open("rb") as src, destination.open("wb") as dst:
        page_no = 0
        while encrypted := src.read(PAGE_SIZE):
            page_no += 1
            if len(encrypted) != PAGE_SIZE:
                break
            dst.write(decrypt_page(encrypted, key, page_no, aes))
        for index in range(len(frames)):
            frame_head = wal_data[24 + index * 4120:48 + index * 4120]
            db_size = struct.unpack(">I", frame_head[4:8])[0]
            frame_page_no, encrypted = frames[index]
            dst.seek((frame_page_no - 1) * PAGE_SIZE)
            dst.write(decrypt_page(encrypted, key, frame_page_no, aes))
            if db_size:
                last_commit_size = db_size
        if last_commit_size:
            dst.truncate(last_commit_size * PAGE_SIZE)
    return {"wal_salt": "<redacted>", "wal_frames": len(frames), "wal_commit_pages": last_commit_size, "source_bytes": raw_size, "output_bytes": destination.stat().st_size}


def wal_generation(data: bytes) -> tuple[tuple[int, int], list[tuple[int, bytes]]]:
    if len(data) < 24:
        return (0, 0), []
    salt = struct.unpack(">II", data[16:24])
    frames: list[tuple[int, bytes]] = []
    offset = 24
    while offset + 4120 <= len(data):
        header = data[offset:offset + 24]
        page_no = struct.unpack(">I", header[:4])[0]
        if struct.unpack(">II", header[8:16]) != salt:
            break
        frames.append((page_no, data[offset + 24:offset + 4120]))
        offset += 4120
    return salt, frames


def _open_process(pid: int):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    handle = kernel.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return kernel, handle


WINDOWS_CONFIG_XOR_MASK = bytes.fromhex("d2c7442458020000004889442450488b450048844c2448488944254048584c24")
CONFIG_LITERAL_RE = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")


def _candidate_keys(blob: bytes) -> set[bytes]:
    """Decode 64-hex key candidates from a Config.Cipher payload."""
    out: set[bytes] = set()
    decoded = bytes(value ^ WINDOWS_CONFIG_XOR_MASK[i % len(WINDOWS_CONFIG_XOR_MASK)] for i, value in enumerate(blob))
    for match in CONFIG_LITERAL_RE.finditer(decoded):
        run = match.group(1)
        starts = [0]
        if len(run) > 64:
            starts.extend(range(0, len(run) - 63, 32))
            starts.append(len(run) - 64)
        for start in dict.fromkeys(starts):
            try:
                key = bytes.fromhex(run[start:start + 64].decode("ascii"))
            except (ValueError, UnicodeDecodeError):
                continue
            if len(key) == 32 and len(set(key)) >= 15 and key not in (bytes(32), b"\xff" * 32):
                out.add(key)
    return out


def scan_direct_key_candidates(pid: int, page: bytes) -> set[bytes]:
    """Search writable non-executable heaps near copied page salts for raw keys."""
    from ctypes import wintypes
    kernel, handle = _open_process(pid); salt = page[:16]; found: set[bytes] = set()
    class MBI(ctypes.Structure):
        _fields_ = [("base", ctypes.c_void_p), ("alloc", ctypes.c_void_p), ("alloc_protect", wintypes.DWORD), ("region_size", ctypes.c_size_t), ("state", wintypes.DWORD), ("protect", wintypes.DWORD), ("type", wintypes.DWORD)]
    kernel.VirtualQueryEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]; kernel.VirtualQueryEx.restype = ctypes.c_size_t
    kernel.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]; kernel.ReadProcessMemory.restype = ctypes.c_int
    address = 0; mbi = MBI(); ptr_size = ctypes.sizeof(ctypes.c_void_p)
    try:
        while address < (1 << (47 if ptr_size == 8 else 31)):
            if not kernel.VirtualQueryEx(handle, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi)): break
            base = int(mbi.base or 0); size = int(mbi.region_size or 0); protect = int(mbi.protect or 0)
            writable = bool(protect & (0x04 | 0x40 | 0x80)); executable = bool(protect & (0x10 | 0x20 | 0x40))
            if mbi.state == MEM_COMMIT and writable and not executable and not protect & (PAGE_GUARD | PAGE_NOACCESS):
                for offset in range(0, size, 1024 * 1024):
                    amount = min(1024 * 1024, size - offset); buf = ctypes.create_string_buffer(amount); got = ctypes.c_size_t()
                    if not kernel.ReadProcessMemory(handle, ctypes.c_void_p(base + offset), buf, amount, ctypes.byref(got)): continue
                    blob = buf.raw[:got.value]; start = 0
                    while True:
                        hit = blob.find(salt, start)
                        if hit < 0: break
                        lo = max(0, hit - 4096); hi = min(len(blob) - 32, hit + 4096)
                        for pos in range(lo & ~15, hi + 1, 16): found.add(blob[pos:pos + 32])
                        start = hit + 1
            address = base + size if base + size > address else address + 0x1000
    finally: kernel.CloseHandle(handle)
    return found


def scan_process(pid: int) -> tuple[set[bytes], int, int, int]:
    """Scan readable committed memory; carry preserves Config.Cipher blobs across chunks."""
    from ctypes import wintypes
    kernel, handle = _open_process(pid)

    class MBI(ctypes.Structure):
        _fields_ = [("base", ctypes.c_void_p), ("alloc", ctypes.c_void_p), ("alloc_protect", wintypes.DWORD), ("region_size", ctypes.c_size_t), ("state", wintypes.DWORD), ("protect", wintypes.DWORD), ("type", wintypes.DWORD)]

    kernel.VirtualQueryEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]
    kernel.VirtualQueryEx.restype = ctypes.c_size_t
    kernel.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    kernel.ReadProcessMemory.restype = ctypes.c_int
    candidates: set[bytes] = set(); regions = bytes_read = 0; address = 0; mbi = MBI(); ptr_size = ctypes.sizeof(ctypes.c_void_p)
    try:
        while address < (1 << (47 if ptr_size == 8 else 31)):
            if not kernel.VirtualQueryEx(handle, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi)):
                break
            base = int(mbi.base or 0); size = int(mbi.region_size or 0); protect = int(mbi.protect or 0)
            if mbi.state == MEM_COMMIT and not protect & (PAGE_GUARD | PAGE_NOACCESS) and protect & 0xFF in READABLE:
                regions += 1; carry = b""
                for offset in range(0, size, 1024 * 1024):
                    amount = min(1024 * 1024, size - offset); buf = ctypes.create_string_buffer(amount); got = ctypes.c_size_t()
                    if kernel.ReadProcessMemory(handle, ctypes.c_void_p(base + offset), buf, amount, ctypes.byref(got)):
                        chunk = buf.raw[:got.value]; bytes_read += len(chunk); data = carry + chunk
                        candidates.update(_candidate_keys(data)); carry = data[-256:]
                    else:
                        carry = b""
            address = base + size if base + size > address else address + 0x1000
    finally:
        kernel.CloseHandle(handle)
    return candidates, regions, bytes_read, len(candidates)


def find_pids() -> list[int]:
    import subprocess
    out = subprocess.check_output(["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"], text=True, errors="replace")
    pids: list[int] = []
    for line in out.splitlines():
        fields = [x.strip('"') for x in line.split('","')]
        if len(fields) >= 2 and fields[0].lower() == "weixin.exe":
            try:
                pids.append(int(fields[1]))
            except ValueError:
                pass
    if not pids:
        raise RuntimeError("Weixin.exe not found")
    return pids


def locate_storage() -> Path:
    root = Path(r"D:\Wechat files\xwechat_files")
    choices = [p / "db_storage" for p in root.glob("*") if (p / "db_storage").is_dir()]
    if not choices:
        raise FileNotFoundError("WeChat db_storage not found")
    return choices[0]


def inspect_database(path: Path) -> dict:
    """Read-only SQLite summary of a decrypted copy (required-database evidence)."""
    import sqlite3
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%' ORDER BY name")]
        rows = sum(connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in tables)
    finally:
        connection.close()
    return {"integrity_check": integrity, "msg_table_count": len(tables), "msg_row_count": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--out", required=True, type=Path); args = parser.parse_args(argv); args.out.mkdir(parents=True, exist_ok=True)
    storage = locate_storage(); db_files = sorted((storage / "message").glob("message_[0-9]*.db"), key=lambda p: p.stat().st_size)
    db_files = [p for p in db_files if p.stat().st_size >= PAGE_SIZE]
    if not db_files: raise RuntimeError("No usable message database")
    source = db_files[0]
    all_db_files = sorted(storage.rglob("*.db"), key=lambda p: p.stat().st_size)
    all_db_files = [p for p in all_db_files if p.stat().st_size >= PAGE_SIZE]
    pids = find_pids(); pid_stats = []; candidates: set[bytes] = set()
    for pid in pids:
        found, regions, scanned, count = scan_process(pid)
        candidates.update(found)
        pid_stats.append({"pid": pid, "readable_regions": regions, "bytes_scanned": scanned, "candidate_count": count})
    required_path = source
    required_rel = required_path.relative_to(storage).as_posix()
    ordered_paths: list[Path] = [required_path]
    for name in ("session.db", "contact.db"):  # optional metadata databases; never gate success
        path = next((p for p in all_db_files if p.name == name), None)
        if path is not None and path not in ordered_paths:
            ordered_paths.append(path)
    trial_results = []; matched_keys: dict[Path, bytes] = {}
    tested_keys = 0
    for path in ordered_paths:
        page = path.read_bytes()[:PAGE_SIZE]; matched = None
        for candidate in candidates:
            tested_keys += 1
            routes = [("raw", candidate), ("pbkdf2-sha512-256000", hashlib.pbkdf2_hmac("sha512", candidate, page[:16], 256000, 32)), ("pbkdf2-sha1-64000", hashlib.pbkdf2_hmac("sha1", candidate, page[:16], 64000, 32))]
            for name, key in routes:
                ok, _ = check_page_key(page, key)
                if ok:
                    matched = {"method": name, "page1_prefix_hex": SQLITE_MAGIC.hex(), "hmac": True}; matched_keys[path] = key
                    break
            if matched: break
        trial_results.append({"database": path.relative_to(storage).as_posix(), "required": path == required_path, "match": bool(matched), "method": matched["method"] if matched else None, "page1_prefix_hex": SQLITE_MAGIC.hex() if matched else None, "hmac": bool(matched), "reason": None if matched else "no scanned candidate key passed page-1 SQLite magic + SQLCipher HMAC (database likely not encrypted with the key that opens the required message database)"})
    decrypt_results = {}
    for path, key in matched_keys.items():
        rel = path.relative_to(storage).as_posix()
        info = decrypt_database(path, args.out / path.relative_to(storage), key)
        info["output"] = rel
        try:
            info.update(inspect_database(args.out / path.relative_to(storage)))
        except Exception as exc:
            info["inspect_error"] = repr(exc)
        decrypt_results[rel] = info
    required_matched = required_path in matched_keys
    result = {"storage": "<redacted>", "database": source.name, "required_database": required_rel, "required_matched": required_matched, "ok": required_matched, "match": required_matched, "pids": pid_stats, "candidate_count": len(candidates), "candidate_key_trials_per_database": tested_keys, "database_results": trial_results, "decrypted_databases": decrypt_results, "required_integrity_check": decrypt_results.get(required_rel, {}).get("integrity_check"), "required_msg_table_count": decrypt_results.get(required_rel, {}).get("msg_table_count"), "required_msg_row_count": decrypt_results.get(required_rel, {}).get("msg_row_count"), "tested": "XOR-decoded in-memory 64-hex candidates; raw key first; PBKDF2-HMAC-SHA512/256000 and PBKDF2-HMAC-SHA1/64000; SQLCipher HMAC-SHA512 reserve80 IV page tail; page-1 SQLite magic; required message database judged independently of optional session/contact databases"}
    (args.out / "probe.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    if required_matched:
        print(f"REQUIRED {required_rel}: integrity_check={result['required_integrity_check']} msg_tables={result['required_msg_table_count']} msg_rows={result['required_msg_row_count']}")
    return 0 if required_matched else 2


if __name__ == "__main__": raise SystemExit(main())
