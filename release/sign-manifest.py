#!/usr/bin/env python3
"""离线签发 / 校验更新清单签名（RS256 / PKCS#1 v1.5）。

用法：
    python release/sign-manifest.py --key <私钥 json 路径>   # 写入签名
    python release/sign-manifest.py --check                  # 只验签，发版门禁用（退出码 0/1）

待签字节**必须**来自 ``qq_live_digest.update_signing.canonical_payload``，不在本脚本里
复制第二份实现——两份实现漂移，签出来的东西程序就验不过（或更糟：验得过但不一致）。

私钥只从路径参数读，不进仓库、不进日志。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qq_live_digest.update_signing import (  # noqa: E402 - 需要先补 sys.path
    KNOWN_KEYS,
    SHA256_DIGEST_INFO,
    canonical_payload,
    verify_manifest_signature,
)

DEFAULT_KEY = Path.home() / ".dsh" / "nh-signing" / "update-signing-key.json"
DEFAULT_MANIFEST = ROOT / "release" / "update-manifest.json"


def load_key(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as error:
        raise SystemExit("读不到私钥文件：%s" % error)
    except ValueError as error:
        raise SystemExit("私钥文件不是合法 JSON：%s" % error)
    for field in ("n", "e", "d"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise SystemExit("私钥缺少字段：%s" % field)
    return data


def sign_payload(payload: bytes, modulus: int, private_exponent: int) -> bytes:
    """PKCS#1 v1.5 签名：EM = 0x00 | 0x01 | PS(0xff) | 0x00 | DigestInfo||digest。"""
    size = (modulus.bit_length() + 7) // 8
    digest_info = SHA256_DIGEST_INFO + hashlib.sha256(payload).digest()
    padding = size - 3 - len(digest_info)
    if padding < 8:
        raise SystemExit("密钥长度不足，无法做 PKCS#1 v1.5 填充")
    em = b"\x00\x01" + b"\xff" * padding + b"\x00" + digest_info
    return pow(int.from_bytes(em, "big"), private_exponent, modulus).to_bytes(size, "big")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="签发或校验 release/update-manifest.json 的签名")
    parser.add_argument("--key", default=str(DEFAULT_KEY), help="私钥 JSON 路径（离线保存，勿入库）")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="清单路径")
    parser.add_argument("--check", action="store_true", help="只验签，不写文件；失败退出码 1")
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest)
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as error:
        print("读不到清单：%s" % error, file=sys.stderr)
        return 1
    except ValueError as error:
        print("清单不是合法 JSON：%s" % error, file=sys.stderr)
        return 1
    if not isinstance(document, dict):
        print("清单顶层不是对象", file=sys.stderr)
        return 1

    if args.check:
        ok, reason = verify_manifest_signature(document)
        if ok:
            print("签名校验通过：%s" % manifest_path)
            return 0
        print("签名校验失败：%s" % reason, file=sys.stderr)
        return 1

    key = load_key(Path(args.key))
    modulus = int(key["n"], 16)
    exponent = int(key["e"], 16)
    private_exponent = int(key["d"], 16)
    key_id = str(key.get("key_id") or "")
    # 私钥必须与程序内置的公钥配对，否则签出来的清单用户端一定验不过。
    known = KNOWN_KEYS.get(key_id)
    if known is None:
        raise SystemExit("私钥的 key_id=%r 不在程序内置公钥表里（先发一版带新公钥的版本）" % key_id)
    if known[0] != modulus or known[1] != exponent:
        raise SystemExit("私钥与程序内置公钥不匹配（key_id=%s）" % key_id)

    payload = canonical_payload(document)
    raw = sign_payload(payload, modulus, private_exponent)
    signature = {
        "alg": "RS256",
        "key_id": key_id,
        "value": base64.urlsafe_b64encode(raw).decode("ascii").rstrip("="),
    }
    signed = dict(document)
    signed["signature"] = signature
    ok, reason = verify_manifest_signature(signed)
    if not ok:
        raise SystemExit("签出来的清单自验不过（%s），未写入" % reason)
    manifest_path.write_text(
        json.dumps(signed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("已写入签名：%s（key_id=%s）" % (manifest_path, key_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
