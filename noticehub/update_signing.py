"""更新清单签名验签（RS256 = RSA PKCS#1 v1.5 + SHA-256），纯标准库。

为什么要签名：更新清单和安装包来自同一个可被篡改的来源（GitHub/Gitee 仓库），
只比对清单里的 sha256 等于「自己跟自己比对」——能改仓库的人可以同时换包和哈希。
清单带上数字签名、程序内置公钥验签后，没有私钥就伪造不出能通过的清单。

口径：
- 待签字节由本模块 :func:`canonical_payload` 在本地重算，与服务器发来的空白、换行、
  键顺序无关；离线签名脚本 ``release/sign-manifest.py`` 也必须 import 这一个函数，
  两份实现漂移就是漏洞。
- 验签失败一律失败关闭，调用方不得回退到「没有签名就当没这回事」。
- 只做验签，不做签名：私钥不进仓库、不进程序。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from typing import Any

# RS256 的 DigestInfo 前缀（RFC 8017 附录 A.2.1，SHA-256）。
SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")

# key_id -> (n, e)。轮换时把新公钥并列加进来：先发带新公钥的版本，再用新私钥签后续清单。
# 当前这把与 release/manifest-pubkey.json 完全一致（3072 位，e = 0x10001）。
KNOWN_KEYS: dict[str, tuple[int, int]] = {
    "nh-2026-1": (
        int(
            "c3d9d5f44b93d95316d4591b430203c554896e87e47ca07fc84564a0afa1b64181462146d583ff4fa622ce8cc7155cc0"
            "d7be0bc07c8a5256f5851d22eac639505f71982cc938eaff6e928bd9dda4ce959b39ebbcf37b58d778bb5d68518846d6"
            "ec5a8c2b1c1b49e58757683db2c827cc1fa566fe034b031f38a7838521f8cd0ec31a522f0e624fb59064be2b447b78ef"
            "93aab755f6bd5660b44f6c687801b0a149d05a1caadcdfd24777f7f7c37679c60ccb991ae4236864e598ca816033d3ce"
            "5dc628d3054abdc7e603c5b445447671af33277c214ce8def7df732e8fa76dd2a62107f86224fb2ce9b90ae2de5d27fb"
            "987cd26923fc35914b2a91513e1263247a98473c657c4a4136c062ee499d11c75f1c784a2822cc0d888d0383387c4200"
            "f04307f7f0a1f7709c30e2a72323ea3f1deb5580b032d1b0474f1d177bb4a3c5fd3eb8f99024e78c0678ed1c72c60fe6"
            "badecdbb8b852e05ed9224bcf1572279614db6e14fc3de254dee994f89e2edcb51390d17d06b7a5ec195da74d7ff16e9",
            16,
        ),
        0x10001,
    ),
}


def canonical_payload(document: dict) -> bytes:
    """签名与验签共用的待签字节。

    排除 ``signature`` 自身（否则永远验不过）；键排序 + 无空格分隔，与来源排版无关；
    UTF-8 + ``ensure_ascii=False``，中文正文按 UTF-8 参与签名。
    """
    body = {k: v for k, v in document.items() if k != "signature"}
    return json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _decode_base64url(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _check_padding(em: bytes, expected_tail: bytes) -> str:
    """校验 EMSA-PKCS1-v1_5 结构，返回空串表示通过，否则返回原因。"""
    if len(em) < 2 or em[0:2] != b"\x00\x01":
        return "签名格式不合法"
    index = 2
    while index < len(em) and em[index] == 0xFF:
        index += 1
    # PS 至少 8 个 0xff，且后面必须紧跟 0x00 分隔符。
    if index - 2 < 8:
        return "签名格式不合法"
    if index >= len(em) or em[index] != 0x00:
        return "签名格式不合法"
    tail = em[index + 1:]
    # 严格等长：不允许宽松匹配（宽松比较能被拼出通过的结构）。
    if len(tail) != len(expected_tail):
        return "签名与公钥不匹配"
    if not hmac.compare_digest(tail, expected_tail):
        return "签名与公钥不匹配"
    return ""


def verify_manifest_signature(document: Any) -> tuple[bool, str]:
    """验证更新清单签名。

    返回 ``(ok, reason)``；``reason`` 是能直接给用户看的一句话（也用于日志）。
    任何一步失败都返回具体原因，不返回笼统的 False。
    """
    if not isinstance(document, dict):
        return False, "更新清单不是对象"
    signature = document.get("signature")
    if not isinstance(signature, dict):
        return False, "清单没有签名"
    alg = signature.get("alg")
    if not isinstance(alg, str) or alg != "RS256":
        return False, "不支持的签名算法"
    key_id = signature.get("key_id")
    if not isinstance(key_id, str) or key_id not in KNOWN_KEYS:
        return False, "未知的签名公钥"
    value = signature.get("value")
    if not isinstance(value, str) or not value:
        return False, "签名格式不合法"
    modulus, exponent = KNOWN_KEYS[key_id]
    size = (modulus.bit_length() + 7) // 8
    try:
        raw = _decode_base64url(value)
    except (ValueError, TypeError, binascii.Error):
        return False, "签名编码不合法"
    # 长度必须恰好等于模长：截断/加长都直接否掉。
    if len(raw) != size:
        return False, "签名长度不合法"
    number = int.from_bytes(raw, "big")
    if not 0 < number < modulus:
        return False, "签名数值不合法"
    try:
        em = pow(number, exponent, modulus).to_bytes(size, "big")
    except (ValueError, OverflowError):
        return False, "签名格式不合法"
    digest = hashlib.sha256(canonical_payload(document)).digest()
    reason = _check_padding(em, SHA256_DIGEST_INFO + digest)
    if reason:
        return False, reason
    return True, ""
