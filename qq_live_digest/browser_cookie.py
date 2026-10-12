"""Extract Chaoxing cookies from Windows browsers (Firefox, Edge, Chrome)."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


class _AUTH_CIPHER_MODE_INFO(ctypes.Structure):
    """BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO（Windows CNG，字段顺序照 MSDN）。

    cbData（ULONGLONG）夹在 cbAAD 和 dwFlags 之间：漏掉它 cbSize 会算成 80 而不是 88，
    dwFlags 还会落进 cbData 的槽位，BCryptEncrypt/Decrypt 一律返回
    STATUS_INVALID_PARAMETER（0xC000000D）。加回来之后 McGrew AES-GCM 测试向量逐字节吻合。
    """

    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("dwInfoVersion", ctypes.c_ulong),
        ("pbNonce", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbNonce", ctypes.c_ulong),
        ("pbAuthData", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbAuthData", ctypes.c_ulong),
        ("pbTag", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbTag", ctypes.c_ulong),
        ("pbMacContext", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbMacContext", ctypes.c_ulong),
        ("cbAAD", ctypes.c_ulong),
        ("cbData", ctypes.c_ulonglong),
        ("dwFlags", ctypes.c_ulong),
    ]


_BROWSER_LABELS = {
    "edge": "Edge",
    "chrome": "Chrome",
    "brave": "Brave",
    "vivaldi": "Vivaldi",
    "opera": "Opera",
    "firefox": "Firefox",
}


def _next_step(category: str, source: str) -> str:
    """Return the concrete user action for one browser failure category."""
    browser = _BROWSER_LABELS.get(source, source or "浏览器")
    if category == "browser_locked":
        return f"请完全退出 {browser}（含托盘图标和后台进程），再回来点“重试自动获取”；浏览器运行时会独占 Cookie 库。"
    if category == "no_chaoxing_cookie":
        return f"请用 {browser} 打开 https://i.chaoxing.com/ 并登录 chaoxing.com；登录成功后完全退出浏览器，再回来重试。"
    if category == "decrypt_failed":
        return "请改用另一个受支持浏览器登录学习通后重试；如果仍失败，可用“手工粘贴 Cookie”。"
    return f"请确认已安装并启动过 {browser}；用它登录 chaoxing.com 后完全退出浏览器，再回来重试。"


def _with_guidance(value: dict[str, Any]) -> dict[str, Any]:
    """Normalize one browser attempt without exposing cookie values in diagnostics."""
    result = {
        "ok": False,
        "cookie": "",
        "source": "",
        "category": "",
        "error": "",
        "detail": "",
        "next_step": "",
    }
    result.update(value)
    if result["ok"]:
        result["category"] = ""
        result["next_step"] = ""
    else:
        category = str(result.get("category") or "browser_unavailable")
        result["category"] = category
        if not result.get("next_step"):
            result["next_step"] = _next_step(category, str(result.get("source") or ""))
    return result


def read_chaoxing_cookie() -> dict[str, Any]:
    """
    Extract Chaoxing cookies from browser storage.

    Returns one result plus ``attempts`` in actual probe order. Every failed attempt
    has one of four categories: browser_unavailable, browser_locked,
    no_chaoxing_cookie, or decrypt_failed.

    Never raises; always returns dict. Never logs cookie values.
    """
    result: dict[str, Any] = {
        "ok": False,
        "cookie": "",
        "source": "",
        "category": "",
        "error": "",
        "detail": "",
        "next_step": "",
        "attempts": [],
    }

    if os.name != "nt":
        result.update(_with_guidance({
            "source": "",
            "category": "browser_unavailable",
            "error": "自动读取浏览器 Cookie 仅支持 Windows",
        }))
        return result

    attempts: list[dict[str, Any]] = []
    # The order is user-facing: Edge, Chrome, then other Chromium browsers.
    for browser_name, browser_path_fn in (
        ("edge", _edge_user_data_path),
        ("chrome", _chrome_user_data_path),
        ("brave", _brave_user_data_path),
        ("vivaldi", _vivaldi_user_data_path),
        ("opera", _opera_user_data_path),
    ):
        browser = _BROWSER_LABELS[browser_name]
        try:
            browser_path = browser_path_fn()
            if browser_path.exists():
                attempt = _try_chrome_like(browser_path, browser_name)
            else:
                attempt = {
                    "ok": False,
                    "cookie": "",
                    "source": browser_name,
                    "category": "browser_unavailable",
                    "error": f"未找到 {browser} Cookie 库",
                    "detail": "浏览器未安装，或尚未创建用户配置目录",
                }
        except Exception as error:  # noqa: BLE001 - public entry must never raise
            attempt = {
                "ok": False,
                "cookie": "",
                "source": browser_name,
                "category": "browser_unavailable",
                "error": f"{browser} Cookie 库读取失败",
                "detail": type(error).__name__,
            }
        attempt = _with_guidance(attempt)
        attempts.append(attempt)
        if attempt["ok"]:
            result.update(attempt)
            result["attempts"] = attempts
            return result

    try:
        firefox_attempt = _with_guidance(_try_firefox())
    except Exception as error:  # noqa: BLE001 - public entry must never raise
        firefox_attempt = _with_guidance({
            "source": "firefox",
            "category": "browser_unavailable",
            "error": "Firefox Cookie 库读取失败",
            "detail": type(error).__name__,
        })
    attempts.append(firefox_attempt)
    if firefox_attempt["ok"]:
        result.update(firefox_attempt)
        result["attempts"] = attempts
        return result

    for category in ("browser_locked", "decrypt_failed", "no_chaoxing_cookie", "browser_unavailable"):
        selected = next((item for item in attempts if item["category"] == category), None)
        if selected is not None:
            result.update(selected)
            break
    result["attempts"] = attempts
    return result


def _try_firefox() -> dict[str, Any]:
    """Try to read Firefox cookies (plaintext)."""
    base = {
        "ok": False,
        "cookie": "",
        "source": "firefox",
        "category": "browser_unavailable",
        "error": "",
        "detail": "",
        "next_step": "",
    }

    appdata = Path(os.environ.get("APPDATA", ""))
    profiles_dir = appdata / "Mozilla" / "Firefox" / "Profiles"
    if not profiles_dir.exists():
        base["error"] = "未找到 Firefox Cookie 库"
        return _with_guidance(base)

    found_db = False
    readable_db = False
    locked_profile = ""
    for profile_dir in profiles_dir.iterdir():
        if not profile_dir.is_dir():
            continue
        cookies_db = profile_dir / "cookies.sqlite"
        if not cookies_db.exists():
            continue
        found_db = True
        try:
            with closing(sqlite3.connect(f"file:{cookies_db}?mode=ro", uri=True, timeout=1)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT host, name, value FROM moz_cookies WHERE host LIKE ?",
                    ("%chaoxing.com",),
                ).fetchall()
            readable_db = True
            if not rows:
                continue
            base.update({
                "ok": True,
                "cookie": "; ".join(f"{row['name']}={row['value']}" for row in rows),
                "category": "",
                "detail": f"Firefox 配置: {profile_dir.name}",
            })
            return _with_guidance(base)
        except (PermissionError, OSError):
            locked_profile = profile_dir.name
        except sqlite3.OperationalError as error:
            message = str(error).lower()
            if any(marker in message for marker in ("locked", "busy", "unable to open")):
                locked_profile = profile_dir.name
            else:
                base["detail"] = f"{profile_dir.name}: {type(error).__name__}"
        except sqlite3.Error as error:
            base["detail"] = f"{profile_dir.name}: {type(error).__name__}"

    if locked_profile:
        base.update({
            "category": "browser_locked",
            "error": "Firefox Cookie 库被占用",
            "detail": f"配置: {locked_profile}",
        })
    elif readable_db:
        base.update({
            "category": "no_chaoxing_cookie",
            "error": "Firefox 中未找到学习通 Cookie",
            "detail": "已检查所有可读配置",
        })
    elif found_db:
        base["error"] = "Firefox Cookie 库无法读取"
    else:
        base["error"] = "未找到 Firefox Cookie 库文件"
    return _with_guidance(base)


def _chrome_user_data_path() -> Path:
    """Return Chrome User Data path."""
    localappdata = Path(os.environ.get("LOCALAPPDATA", ""))
    return localappdata / "Google" / "Chrome" / "User Data"


def _edge_user_data_path() -> Path:
    """Return Edge User Data path."""
    localappdata = Path(os.environ.get("LOCALAPPDATA", ""))
    return localappdata / "Microsoft" / "Edge" / "User Data"


def _brave_user_data_path() -> Path:
    localappdata = Path(os.environ.get("LOCALAPPDATA", ""))
    return localappdata / "BraveSoftware" / "Brave-Browser" / "User Data"


def _vivaldi_user_data_path() -> Path:
    localappdata = Path(os.environ.get("LOCALAPPDATA", ""))
    return localappdata / "Vivaldi" / "User Data"


def _opera_user_data_path() -> Path:
    roaming = Path(os.environ.get("APPDATA", ""))
    return roaming / "Opera Software" / "Opera Stable"


def _try_chrome_like(user_data_path: Path, browser_name: str) -> dict[str, Any]:
    """Try one Chromium browser across all of its profiles."""
    browser = _BROWSER_LABELS.get(browser_name, browser_name)
    base = {
        "ok": False,
        "cookie": "",
        "source": browser_name,
        "category": "browser_unavailable",
        "error": "",
        "detail": "",
        "next_step": "",
    }

    local_state_path = user_data_path / "Local State"
    if not local_state_path.exists():
        base["error"] = f"未找到 {browser} Cookie 库所需的 Local State"
        return _with_guidance(base)

    try:
        with open(local_state_path, "r", encoding="utf-8") as file:
            local_state = json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        base.update({
            "category": "decrypt_failed",
            "error": f"{browser} 加密配置无法读取",
            "detail": type(error).__name__,
        })
        return _with_guidance(base)

    encrypted_key_b64 = local_state.get("os_crypt", {}).get("encrypted_key")
    if not encrypted_key_b64:
        base.update({
            "category": "decrypt_failed",
            "error": f"{browser} 加密配置中没有可用密钥",
            "detail": "Local State 缺少 os_crypt.encrypted_key",
        })
        return _with_guidance(base)

    try:
        encrypted_key = base64.b64decode(encrypted_key_b64)
        if encrypted_key[:5] != b"DPAPI":
            base.update({
                "category": "decrypt_failed",
                "error": f"{browser} 密钥格式不受支持",
                "detail": "加密密钥没有 DPAPI 前缀",
            })
            return _with_guidance(base)
        master_key = _dpapi_unprotect(encrypted_key[5:])
        if not master_key or len(master_key) != 32:
            base.update({
                "category": "decrypt_failed",
                "error": f"{browser} DPAPI 密钥解密失败",
                "detail": "未得到 32 字节 AES 密钥",
            })
            return _with_guidance(base)
    except Exception as error:  # noqa: BLE001 - report type, never secret bytes
        base.update({
            "category": "decrypt_failed",
            "error": f"{browser} 密钥处理失败",
            "detail": type(error).__name__,
        })
        return _with_guidance(base)

    profile_results: list[dict[str, Any]] = []
    found_cookie_db = False
    profile_dirs = [user_data_path, *user_data_path.iterdir()]
    for profile_dir in profile_dirs:
        if not profile_dir.is_dir() or profile_dir.name in ("System Profile", "Guest Profile", "supervised-user"):
            continue
        cookies_db = profile_dir / "Network" / "Cookies"
        if not cookies_db.exists():
            continue
        found_cookie_db = True
        profile_result = _with_guidance(
            _read_chrome_cookies_db(cookies_db, master_key, browser_name, profile_dir.name)
        )
        if profile_result["ok"]:
            return profile_result
        profile_results.append(profile_result)

    if not found_cookie_db:
        base.update({
            "category": "browser_unavailable",
            "error": f"未找到 {browser} Cookie 库文件",
            "detail": "已检查所有用户配置目录",
        })
        return _with_guidance(base)

    for category in ("browser_locked", "decrypt_failed", "no_chaoxing_cookie", "browser_unavailable"):
        selected = next((item for item in profile_results if item["category"] == category), None)
        if selected is not None:
            return selected
    return _with_guidance(base)


def _read_chrome_cookies_db(db_path: Path, master_key: bytes, browser_name: str, profile_name: str) -> dict[str, Any]:
    """Read one Chromium Cookie DB and preserve why every target row failed."""
    browser = _BROWSER_LABELS.get(browser_name, browser_name)
    base = {
        "ok": False,
        "cookie": "",
        "source": browser_name,
        "category": "",
        "error": "",
        "detail": f"配置: {profile_name}",
        "next_step": "",
    }

    try:
        with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT host_key, name, encrypted_value FROM cookies WHERE host_key LIKE ?",
                ("%chaoxing.com",),
            ).fetchall()

        if not rows:
            base.update({
                "category": "no_chaoxing_cookie",
                "error": f"{browser} 中未找到学习通 Cookie",
            })
            return _with_guidance(base)

        cookies: list[str] = []
        v20_count = 0
        failed_count = 0
        failure_types: set[str] = set()
        for row in rows:
            encrypted_value = row["encrypted_value"]
            if not encrypted_value or len(encrypted_value) < 3:
                failed_count += 1
                failure_types.add("空或损坏的密文")
                continue

            prefix = encrypted_value[:3]
            if prefix in (b"v10", b"v11"):
                try:
                    decrypted = _decrypt_chrome_cookie_gcm(encrypted_value, master_key)
                except Exception as error:  # noqa: BLE001 - keep the failure type, never the value
                    failed_count += 1
                    failure_types.add(type(error).__name__)
                    continue
                if decrypted is None:
                    failed_count += 1
                    failure_types.add("AES-GCM 校验失败")
                    continue
                cookies.append(f"{row['name']}={decrypted}")
            elif prefix == b"v20":
                v20_count += 1
            else:
                failed_count += 1
                failure_types.add("不支持的密文版本")

        if cookies:
            base.update({
                "ok": True,
                "cookie": "; ".join(cookies),
                "detail": (
                    f"{profile_name}: 成功解密 {len(cookies)} 个 Cookie"
                    + (f"，跳过 {v20_count} 个 v20" if v20_count else "")
                    + (f"，另有 {failed_count} 个失败" if failed_count else "")
                ),
            })
            return _with_guidance(base)

        reasons = []
        if v20_count:
            reasons.append(f"{v20_count} 个 v20 应用级加密 Cookie")
        if failed_count:
            kinds = "、".join(sorted(failure_types))
            reasons.append(f"{failed_count} 个解密失败（{kinds}）")
        base.update({
            "category": "decrypt_failed",
            "error": (
                f"{browser} 中所有学习通 Cookie 均为 v20，无法自动解密"
                if v20_count and not failed_count
                else f"{browser} 中无法解密任何学习通 Cookie"
            ),
            "detail": f"{profile_name}: " + "；".join(reasons),
        })
        return _with_guidance(base)

    except (PermissionError, OSError) as error:
        base.update({
            "category": "browser_locked",
            "error": f"{browser} Cookie 库被占用",
            "detail": f"{profile_name}: {type(error).__name__}",
        })
        return _with_guidance(base)
    except sqlite3.OperationalError as error:
        message = str(error).lower()
        if any(marker in message for marker in ("locked", "busy", "unable to open")):
            category = "browser_locked"
            text = f"{browser} Cookie 库被占用"
        else:
            category = "decrypt_failed"
            text = f"{browser} Cookie 库结构或版本不兼容"
        base.update({
            "category": category,
            "error": text,
            "detail": f"{profile_name}: {type(error).__name__}",
        })
        return _with_guidance(base)
    except sqlite3.Error as error:
        base.update({
            "category": "decrypt_failed",
            "error": f"{browser} Cookie 库结构或版本不兼容",
            "detail": f"{profile_name}: {type(error).__name__}",
        })
        return _with_guidance(base)


def _dpapi_unprotect(data: bytes) -> bytes | None:
    """Decrypt data using Windows DPAPI (CryptUnprotectData only)."""
    if os.name != "nt":
        return None

    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p

        # Define DATA_BLOB structure
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

        # Setup input blob: hold data lifetime by creating buffer
        data_buffer = ctypes.create_string_buffer(data)
        input_blob = DATA_BLOB()
        input_blob.pbData = ctypes.cast(data_buffer, ctypes.POINTER(ctypes.c_ubyte))
        input_blob.cbData = len(data)

        output_blob = DATA_BLOB()

        # Call CryptUnprotectData
        crypt_unprotect = crypt32.CryptUnprotectData
        crypt_unprotect.argtypes = [
            ctypes.POINTER(DATA_BLOB),
            ctypes.c_wchar_p,
            ctypes.POINTER(DATA_BLOB),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(DATA_BLOB),
        ]
        crypt_unprotect.restype = ctypes.c_bool

        success = crypt_unprotect(
            ctypes.byref(input_blob),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(output_blob),
        )

        if not success:
            # Get Windows error code for debugging
            error_code = ctypes.get_last_error()
            print(f"[_dpapi_unprotect] CryptUnprotectData failed: get_last_error()=0x{error_code:08X}")
            return None

        decrypted = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        # Free output blob memory
        kernel32.LocalFree(output_blob.pbData)

        return decrypted

    except Exception as e:
        import traceback
        print(f"[_dpapi_unprotect] Exception: {type(e).__name__}: {e}")
        traceback.print_exc()
        raise


def _dpapi_protect(data: bytes) -> bytes | None:
    """Encrypt data using Windows DPAPI (CryptProtectData) — for testing roundtrip."""
    if os.name != "nt":
        return None

    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p

        # Define DATA_BLOB structure
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

        # Setup input blob
        data_buffer = ctypes.create_string_buffer(data)
        input_blob = DATA_BLOB()
        input_blob.pbData = ctypes.cast(data_buffer, ctypes.POINTER(ctypes.c_ubyte))
        input_blob.cbData = len(data)

        output_blob = DATA_BLOB()

        # Call CryptProtectData
        crypt_protect = crypt32.CryptProtectData
        crypt_protect.argtypes = [
            ctypes.POINTER(DATA_BLOB),
            ctypes.c_wchar_p,
            ctypes.POINTER(DATA_BLOB),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(DATA_BLOB),
        ]
        crypt_protect.restype = ctypes.c_bool

        CRYPTPROTECT_UI_FORBIDDEN = 0x01
        success = crypt_protect(
            ctypes.byref(input_blob),
            None,
            None,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )

        if not success:
            error_code = ctypes.get_last_error()
            print(f"[_dpapi_protect] CryptProtectData failed: get_last_error()=0x{error_code:08X}")
            return None

        encrypted = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        # Free output blob memory using kernel32.LocalFree
        kernel32.LocalFree(output_blob.pbData)

        return encrypted

    except Exception as e:
        import traceback
        print(f"[_dpapi_protect] Exception: {type(e).__name__}: {e}")
        traceback.print_exc()
        raise


def _extract_key(local_state: dict) -> bytes | None:
    """Extract and decrypt master key from browser Local State."""
    try:
        os_crypt = local_state.get("os_crypt", {})
        encrypted_key_b64 = os_crypt.get("encrypted_key")
        if not encrypted_key_b64:
            return None

        encrypted_key = base64.b64decode(encrypted_key_b64)
        if encrypted_key[:5] != b"DPAPI":
            return None

        # Remove DPAPI prefix and decrypt
        dpapi_data = encrypted_key[5:]
        master_key = _dpapi_unprotect(dpapi_data)
        if not master_key or len(master_key) != 32:
            return None

        return master_key

    except Exception:
        return None


def _decrypt_chrome_cookie_gcm(encrypted_value: bytes, master_key: bytes) -> str | None:
    """Decrypt Chrome v10/v11 cookie using AES-256-GCM. Returns plaintext string or None."""
    plaintext = _aes_gcm_decrypt(master_key, encrypted_value)
    if plaintext:
        try:
            return plaintext.decode("utf-8", errors="ignore")
        except Exception:
            return None
    return None


def _aes_gcm_decrypt(key: bytes, payload: bytes) -> bytes | None:
    """
    Decrypt Chrome v10/v11 cookie using AES-256-GCM.
    Payload structure: [3 bytes "v10"/"v11"][12 bytes nonce][ciphertext][16 bytes tag]
    """
    if len(payload) < 31:  # 3 + 12 + 0 + 16 minimum
        return None

    try:
        # Extract components
        nonce = payload[3:15]
        ciphertext_with_tag = payload[15:]

        if len(ciphertext_with_tag) < 16:
            return None

        ciphertext = ciphertext_with_tag[:-16]
        tag = ciphertext_with_tag[-16:]

        # Use CNG for GCM decryption
        return _bcrypt_gcm_decrypt(ciphertext, key, nonce, tag)

    except Exception:
        return None


def _aes_gcm_encrypt(key: bytes, plaintext: bytes) -> bytes | None:
    """
    Encrypt plaintext using AES-256-GCM (Windows CNG).
    Returns: [3 bytes "v10"][12 bytes nonce][ciphertext][16 bytes tag]
    
    NOTE: CNG GCM implementation complex; for testing, consider using external tool
    or mock data with known-good decryption validation.
    """
    import secrets

    if len(key) != 32:
        return None

    try:
        nonce = secrets.token_bytes(12)
        ciphertext, tag = _bcrypt_gcm_encrypt(key, nonce, plaintext)
        if ciphertext is None or tag is None:
            return None

        return b"v10" + nonce + ciphertext + tag

    except Exception:
        return None


def _bcrypt_gcm_encrypt(key: bytes, nonce: bytes, plaintext: bytes) -> tuple[bytes | None, bytes | None]:
    """Encrypt using Windows CNG AES-256-GCM. Returns (ciphertext, tag) or (None, None)."""
    if os.name != "nt":
        return None, None

    try:
        import ctypes
        bcrypt = ctypes.WinDLL("bcrypt", use_last_error=True)

        # ponytail: CNG 的 GCM 参数只能靠结构体对齐；实现在 encrypt/decrypt 之间共用一份。

        # Define struct EARLY (needed for BCryptEncrypt argtypes)
        BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO = _AUTH_CIPHER_MODE_INFO

        # Bind CNG functions (matching dingtalk_receiver.py style)
        handle = ctypes.c_void_p
        byte_ptr = ctypes.c_void_p
        ulong = ctypes.c_ulong
        bcrypt.BCryptOpenAlgorithmProvider.argtypes = [ctypes.POINTER(handle), ctypes.c_wchar_p, ctypes.c_wchar_p, ulong]
        bcrypt.BCryptOpenAlgorithmProvider.restype = ctypes.c_long
        bcrypt.BCryptSetProperty.argtypes = [handle, ctypes.c_wchar_p, byte_ptr, ulong, ulong]
        bcrypt.BCryptSetProperty.restype = ctypes.c_long
        bcrypt.BCryptGetProperty.argtypes = [handle, ctypes.c_wchar_p, byte_ptr, ulong, ctypes.POINTER(ulong), ulong]
        bcrypt.BCryptGetProperty.restype = ctypes.c_long
        bcrypt.BCryptGenerateSymmetricKey.argtypes = [handle, ctypes.POINTER(handle), byte_ptr, ulong, byte_ptr, ulong, ulong]
        bcrypt.BCryptGenerateSymmetricKey.restype = ctypes.c_long
        # CRITICAL FIX: authInfo param must be POINTER(BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO), not generic void_ptr
        bcrypt.BCryptEncrypt.argtypes = [handle, byte_ptr, ulong, ctypes.POINTER(BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO), byte_ptr, ulong, byte_ptr, ulong, ctypes.POINTER(ulong), ulong]
        bcrypt.BCryptEncrypt.restype = ctypes.c_long
        bcrypt.BCryptDestroyKey.argtypes = [handle]
        bcrypt.BCryptDestroyKey.restype = ctypes.c_long
        bcrypt.BCryptCloseAlgorithmProvider.argtypes = [handle, ulong]
        bcrypt.BCryptCloseAlgorithmProvider.restype = ctypes.c_long
        
        # Open AES algorithm
        alg = ctypes.c_void_p()
        status = bcrypt.BCryptOpenAlgorithmProvider(
            ctypes.byref(alg), "AES", None, 0
        )
        if status != 0:
            return None, None

        try:
            # Set GCM mode: use wide string format matching dingtalk_receiver.py
            mode_str = "ChainingModeGCM"
            status = bcrypt.BCryptSetProperty(
                alg,
                "ChainingMode",
                mode_str,
                len(mode_str) * 2,  # Wide char length
                0,
            )
            if status != 0:
                return None, None

            # Get key object size
            key_obj_len = ctypes.c_ulong()
            result_len = ctypes.c_ulong()
            status = bcrypt.BCryptGetProperty(
                alg,
                "ObjectLength",
                ctypes.byref(key_obj_len),
                ctypes.sizeof(key_obj_len),
                ctypes.byref(result_len),
                0,
            )
            if status != 0:
                return None, None

            # Create key
            key_obj = ctypes.create_string_buffer(key_obj_len.value)
            key_buf = ctypes.create_string_buffer(key, len(key))
            key_handle = ctypes.c_void_p()

            status = bcrypt.BCryptGenerateSymmetricKey(
                alg,
                ctypes.byref(key_handle),
                key_obj,
                key_obj_len.value,
                key_buf,
                len(key),
                0,
            )
            if status != 0:
                return None, None

            try:
                # Query tag length requirement from CNG (Lead m00547 hard rule)
                tag_lengths = ctypes.c_ulong()
                status = bcrypt.BCryptGetProperty(
                    alg,
                    ctypes.create_unicode_buffer("AuthTagLength"),
                    ctypes.byref(tag_lengths),
                    ctypes.sizeof(tag_lengths),
                    ctypes.byref(ctypes.c_ulong()),
                    0,
                )
                
                # Setup GCM parameters with pre-defined struct (x64)
                gcm_info = BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO()
                gcm_info.cbSize = ctypes.sizeof(BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO)
                gcm_info.dwInfoVersion = 1

                # Nonce buffer (keep alive during encrypt)
                nonce_buf = ctypes.create_string_buffer(nonce)
                gcm_info.pbNonce = ctypes.cast(nonce_buf, ctypes.POINTER(ctypes.c_ubyte))
                gcm_info.cbNonce = len(nonce)

                gcm_info.pbAuthData = None
                gcm_info.cbAuthData = 0

                # Tag buffer (output during encrypt) — use queried length if available
                tag_len = tag_lengths.value if status == 0 and tag_lengths.value > 0 else 16
                tag_buf = ctypes.create_string_buffer(tag_len)
                gcm_info.pbTag = ctypes.cast(tag_buf, ctypes.POINTER(ctypes.c_ubyte))
                gcm_info.cbTag = tag_len

                gcm_info.pbMacContext = None
                gcm_info.cbMacContext = 0
                gcm_info.cbAAD = 0
                gcm_info.dwFlags = 0

                # Encrypt: plaintext → ciphertext (no expansion), tag in gcm_info.pbTag
                plaintext_buf = ctypes.create_string_buffer(plaintext, len(plaintext))
                output = ctypes.create_string_buffer(len(plaintext))
                output_len = ctypes.c_ulong()

                # BCryptEncrypt: pbIV=None, cbIV=0 (IV goes via authInfo)
                status = bcrypt.BCryptEncrypt(
                    key_handle,
                    ctypes.cast(plaintext_buf, ctypes.POINTER(ctypes.c_ubyte)),
                    len(plaintext),
                    ctypes.byref(gcm_info),
                    None,
                    0,
                    ctypes.cast(output, ctypes.POINTER(ctypes.c_ubyte)),
                    len(plaintext),
                    ctypes.byref(output_len),
                    0,
                )
                if status != 0:
                    return None, None

                ciphertext = output.raw[: output_len.value]
                tag = tag_buf.raw[:tag_len]

                return ciphertext, tag

            finally:
                bcrypt.BCryptDestroyKey(key_handle)

        finally:
            bcrypt.BCryptCloseAlgorithmProvider(alg, 0)

    except Exception:
        raise



def _bcrypt_gcm_decrypt(ciphertext: bytes, key: bytes, nonce: bytes, tag: bytes) -> bytes | None:
    """Decrypt using Windows CNG AES-256-GCM."""
    if os.name != "nt":
        return None

    try:
        bcrypt = ctypes.WinDLL("bcrypt", use_last_error=True)

        # Open AES algorithm
        alg = ctypes.c_void_p()
        status = bcrypt.BCryptOpenAlgorithmProvider(
            ctypes.byref(alg), "AES", None, 0
        )
        if status < 0:
            return None

        try:
            # Set GCM mode using wide string
            mode_str = "ChainingModeGCM"
            mode_wide = ctypes.create_unicode_buffer(mode_str)
            status = bcrypt.BCryptSetProperty(
                alg,
                ctypes.create_unicode_buffer("ChainingMode"),
                mode_wide,
                ctypes.sizeof(mode_wide),
                0,
            )
            if status < 0:
                return None

            # Get key object size
            key_obj_len = ctypes.c_ulong()
            result_len = ctypes.c_ulong()
            status = bcrypt.BCryptGetProperty(
                alg,
                ctypes.create_unicode_buffer("ObjectLength"),
                ctypes.byref(key_obj_len),
                ctypes.sizeof(key_obj_len),
                ctypes.byref(result_len),
                0,
            )
            if status < 0:
                return None

            # Create key
            key_obj = ctypes.create_string_buffer(key_obj_len.value)
            key_buf = ctypes.create_string_buffer(key, len(key))
            key_handle = ctypes.c_void_p()

            status = bcrypt.BCryptGenerateSymmetricKey(
                alg,
                ctypes.byref(key_handle),
                key_obj,
                key_obj_len.value,
                key_buf,
                len(key),
                0,
            )
            if status < 0:
                return None

            try:
                # Setup GCM parameters (shared struct; a locally-defined copy here would
                # raise NameError, which the bare except below used to swallow silently)
                tag_buf = ctypes.create_string_buffer(tag, len(tag))
                gcm_info = _AUTH_CIPHER_MODE_INFO()
                gcm_info.cbSize = ctypes.sizeof(_AUTH_CIPHER_MODE_INFO)
                gcm_info.dwInfoVersion = 1
                gcm_info.pbNonce = ctypes.cast(ctypes.create_string_buffer(nonce), ctypes.POINTER(ctypes.c_ubyte))
                gcm_info.cbNonce = len(nonce)
                gcm_info.pbAuthData = None
                gcm_info.cbAuthData = 0
                gcm_info.pbTag = ctypes.cast(tag_buf, ctypes.POINTER(ctypes.c_ubyte))
                gcm_info.cbTag = 16

                # Decrypt
                output = ctypes.create_string_buffer(len(ciphertext))
                output_len = ctypes.c_ulong()

                status = bcrypt.BCryptDecrypt(
                    key_handle,
                    ctypes.c_char_p(ciphertext),
                    len(ciphertext),
                    ctypes.byref(gcm_info),
                    None,
                    0,
                    output,
                    len(ciphertext),
                    ctypes.byref(output_len),
                    0,
                )

                if status < 0:
                    return None

                return output.raw[: output_len.value]

            finally:
                bcrypt.BCryptDestroyKey(key_handle)

        finally:
            bcrypt.BCryptCloseAlgorithmProvider(alg, 0)

    except Exception:
        return None
