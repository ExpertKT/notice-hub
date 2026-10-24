"""校验 release/update-manifest.json：结构、字段、排序、下载地址。

用法：python -X utf8 tools/verify_manifest.py [manifest 路径]
逐条打印检查结果；全过退出码 0，任一失败退出码 1。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "release" / "update-manifest.json"

TOP_LEVEL = {"version": int, "items": list}
ITEM_FIELDS = ("version", "download_url", "size", "sha256")
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-beta\.(\d+))?$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def version_key(text):
    """排序键：1.1.0-beta.11 > 1.1.0-beta.9；同号正式版高于 beta。"""
    match = VERSION_RE.match(text) if isinstance(text, str) else None
    if not match:
        return None
    major, minor, patch, beta = match.groups()
    return (int(major), int(minor), int(patch), beta is None, int(beta or 0))


def is_gitee_url(url):
    if not isinstance(url, str):
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "gitee.com" or host.endswith(".gitee.com"))


def check(data):
    """返回 (失败信息列表, 已检查条数)。"""
    failures = []
    checks = 0

    def expect(ok, message):
        nonlocal checks
        checks += 1
        if not ok:
            failures.append(message)
        return ok

    if not expect(isinstance(data, dict), "顶层必须是 JSON 对象"):
        return failures, checks

    for name, kind in TOP_LEVEL.items():
        expect(isinstance(data.get(name), kind), "顶层字段 %s 缺失或类型不对" % name)

    items = data.get("items")
    if not isinstance(items, list):
        return failures, checks
    if not expect(bool(items), "items 至少一条"):
        return failures, checks

    keys = []
    for index, item in enumerate(items):
        where = "items[%d]" % index
        if not expect(isinstance(item, dict), "%s 必须是对象" % where):
            continue
        for field in ITEM_FIELDS:
            expect(field in item, "%s 缺字段 %s" % (where, field))
        version = item.get("version")
        key = version_key(version)
        expect(key is not None, "%s version 格式不是 x.y.z[-beta.N]：%r" % (where, version))
        if key is not None:
            keys.append((key, version))
        sha = item.get("sha256")
        expect(
            isinstance(sha, str) and SHA_RE.match(sha) is not None,
            "%s sha256 不是 64 位十六进制：%r" % (where, sha),
        )
        size = item.get("size")
        expect(
            isinstance(size, int) and not isinstance(size, bool) and size > 0,
            "%s size 不是正整数：%r" % (where, size),
        )
        url = item.get("download_url")
        expect(is_gitee_url(url), "%s download_url 不是 https 的 Gitee 地址：%r" % (where, url))

    if len(keys) == len(items):
        for (previous, _), (current, name) in zip(keys, keys[1:]):
            expect(previous > current, "items 没按版本号倒序：%s 排在了更旧的版本前面" % name)

    return failures, checks


def main(argv):
    path = Path(argv[1]) if len(argv) > 1 else DEFAULT_PATH
    print("校验 %s" % path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        print("FAIL 读不到文件：%s" % error)
        return 1
    except ValueError as error:
        print("FAIL 不是合法 JSON：%s" % error)
        return 1

    failures, checks = check(data)
    for message in failures:
        print("FAIL %s" % message)
    print("%d 项检查，%d 项失败" % (checks, len(failures)))
    if failures:
        return 1
    print("OK 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
