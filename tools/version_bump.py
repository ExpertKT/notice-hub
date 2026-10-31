"""一处改版本号，其余落点跟着走。

落点：noticehub/version.py（唯一真源）、README.md（发布链接/标题）、
android/app/build.gradle（versionName）。announcements.json 只提示不动——
公告内容要人判断。

用法：
    python tools/version_bump.py --check
    python tools/version_bump.py --set 1.1.0-beta.12 [--dry-run]

只用标准库，写文件保留原换行、UTF-8 无 BOM。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

VERSION_PY = Path("noticehub/version.py")
README = Path("README.md")
GRADLE = Path("android/app/build.gradle")
ANNOUNCEMENTS = Path("announcements.json")
MANIFEST = Path("release/update-manifest.json")

# 前缀/后缀各留一组，替换时只换中间的值，别把 `APP_VERSION =` 一起吃掉。
_VERSION_RE = re.compile(r"^([ \t]*APP_VERSION[ \t]*=[ \t]*[\"'])([^\"']+)([\"'])", re.M)
_GRADLE_RE = re.compile(r"(versionName[ \t]+[\"'])([^\"']+)([\"'])")
# README 里「明显的版本串」只有两种写法：完整 tag 与标题里的短式括号。
_FULL_TOKEN_RE = re.compile(r"\d+\.\d+\.\d+-beta\.\d+")
_SHORT_TOKEN_RE = re.compile(r"测试版（(beta\.\d+)）")
_NEW_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def _read(path: Path) -> str:
    # newline="" 原样保留 CRLF/LF，写回时不会整文件改行尾。
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _short(version: str) -> str | None:
    """1.1.0-beta.11 -> beta.11；稳定版没有短式。"""
    match = re.search(r"-([0-9A-Za-z]+\.\d+)$", version)
    return match.group(1) if match else None


def read_app_version(root: Path) -> str | None:
    try:
        match = _VERSION_RE.search(_read(root / VERSION_PY))
    except OSError:
        return None
    return match.group(2) if match else None


def _announcement_beta(root: Path) -> str | None:
    """公告里最后一条带 beta.N 的标题里的短式版本号（纯提示）。"""
    try:
        data = json.loads(_read(root / ANNOUNCEMENTS))
    except (OSError, ValueError):
        return None
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return None
    latest = None
    for item in items:
        if not isinstance(item, dict):
            continue
        match = _SHORT_TOKEN_RE.search(str(item.get("title", "")))
        if match:
            latest = match.group(1)
    return latest


def _plan(root: Path, new: str) -> list[tuple[Path, str, str]]:
    """返回 (文件, 旧内容, 新内容)；只含真正会变的文件。"""
    plans: list[tuple[Path, str, str]] = []
    new_short = _short(new)

    version_path = root / VERSION_PY
    old_text = _read(version_path)
    new_text, count = _VERSION_RE.subn(
        lambda m: m.group(1) + new + m.group(3), old_text, count=1
    )
    if count and new_text != old_text:
        plans.append((version_path, old_text, new_text))

    readme_path = root / README
    old_text = _read(readme_path)
    new_text = _FULL_TOKEN_RE.sub(new, old_text)
    if new_short:
        new_text = _SHORT_TOKEN_RE.sub(lambda m: "测试版（%s）" % new_short, new_text)
    if new_text != old_text:
        plans.append((readme_path, old_text, new_text))

    gradle_path = root / GRADLE
    old_text = _read(gradle_path)
    new_text, count = _GRADLE_RE.subn(
        lambda m: m.group(1) + new + m.group(3), old_text, count=1
    )
    if count and new_text != old_text:
        plans.append((gradle_path, old_text, new_text))

    return plans


def _describe(rel: str, old_text: str, new_text: str, new: str) -> list[str]:
    lines: list[str] = []
    if rel == VERSION_PY.as_posix():
        old = _VERSION_RE.search(old_text)
        lines.append("%s: APP_VERSION %s → %s" % (rel, old.group(2), new))
    elif rel == README.as_posix():
        olds_full = _FULL_TOKEN_RE.findall(old_text)
        olds_short = sorted(set(_SHORT_TOKEN_RE.findall(old_text)))
        parts = []
        if olds_full:
            parts.append("完整版本串 %d 处（%s）→ %s" % (len(olds_full), "、".join(sorted(set(olds_full))), new))
        if olds_short:
            parts.append("标题短式 %s → %s" % ("、".join(olds_short), _short(new) or "（稳定版不写短式）"))
        lines.append("%s: %s" % (rel, "；".join(parts) or "无变化"))
    elif rel == GRADLE.as_posix():
        old = _GRADLE_RE.search(old_text)
        lines.append(
            "%s: versionName %s → %s（versionCode 不能自动加，记得手动 +1）"
            % (rel, old.group(2), new)
        )
    return lines


def _check_manifest(root: Path, version: str) -> list[str]:
    """release/update-manifest.json 的最新条目必须对上 APP_VERSION。

    size/sha256 只能校验形状：它们描述的是已发布的安装包，版本一改就得重新打包填。
    """
    rel = MANIFEST.as_posix()
    try:
        data = json.loads(_read(root / MANIFEST))
    except OSError:
        return ["%s:%s:（文件不存在，读不到）" % (rel, version)]
    except ValueError as exc:
        return ["%s:%s:（不是合法 JSON：%s）" % (rel, version, exc)]
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        return ["%s:%s:（没有 items 或 items 为空）" % (rel, version)]
    latest = items[0] if isinstance(items[0], dict) else {}
    actual = str(latest.get("version") or "（缺 version）")
    problems = []
    if actual != version:
        problems.append("%s:%s:%s" % (rel, version, actual))
    sha = latest.get("sha256")
    if not isinstance(sha, str) or not _HEX64_RE.match(sha):
        problems.append("%s:%s:sha256 不是 64 位十六进制（%s）" % (rel, version, sha))
    size = latest.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        problems.append("%s:%s:size 不是正整数（%s）" % (rel, version, size))
    return problems


def check(root: Path, skip_manifest: bool = False) -> int:
    version = read_app_version(root)
    if not version:
        print("%s: 读不到 APP_VERSION（文件缺失或格式变了）" % VERSION_PY.as_posix())
        return 1

    problems: list[str] = []

    try:
        readme = _read(root / README)
    except OSError:
        problems.append("%s:%s:（文件不存在，读不到）" % (README.as_posix(), version))
    else:
        for token in sorted(set(_FULL_TOKEN_RE.findall(readme))):
            if token != version:
                problems.append("%s:%s:%s" % (README.as_posix(), version, token))
        want_short = _short(version)
        for token in sorted(set(_SHORT_TOKEN_RE.findall(readme))):
            if token != want_short:
                problems.append("%s:%s:%s" % (README.as_posix(), want_short or version, token))

    try:
        gradle = _read(root / GRADLE)
    except OSError:
        problems.append("%s:%s:（文件不存在，读不到）" % (GRADLE.as_posix(), version))
    else:
        match = _GRADLE_RE.search(gradle)
        if not match:
            problems.append("%s:%s:（找不到 versionName）" % (GRADLE.as_posix(), version))
        elif match.group(2) != version:
            problems.append("%s:%s:%s" % (GRADLE.as_posix(), version, match.group(2)))

    if not skip_manifest:
        problems.extend(_check_manifest(root, version))

    for line in problems:
        print(line)

    announced = _announcement_beta(root)
    if announced:
        if announced != _short(version):
            print("公告里最新一条是 %s，与 APP_VERSION（%s）不一致" % (announced, version))
        else:
            print("公告里最新一条是 %s，与 APP_VERSION 一致" % announced)
    else:
        print("公告里没找到版本号，未检查（不影响退出码）")

    if problems:
        return 1
    print("版本号一致：%s" % version)
    return 0


def apply(root: Path, new: str, dry_run: bool) -> int:
    old = read_app_version(root)
    if not old:
        print("%s: 读不到 APP_VERSION（文件缺失或格式变了）" % VERSION_PY.as_posix())
        return 1
    if old == new:
        print("已经是 %s，无需修改" % new)
        return check(root)

    plans = _plan(root, new)
    if not plans:
        print("没有任何落点需要修改（README/gradle 里没找到版本串？）")
        return check(root)

    for path, old_text, new_text in plans:
        rel = path.relative_to(root).as_posix()
        for line in _describe(rel, old_text, new_text, new):
            print(line)

    if dry_run:
        print("dry-run：未写入任何文件")
        return 0

    for path, _old_text, new_text in plans:
        _write(path, new_text)
    print("已写入 %d 个文件，开始复核：" % len(plans))
    # 清单里的 size/sha256 属于已发布的包，脚本不猜；只提醒重新打包后补最新条目。
    print("%s: 记得重新打包后更新最新条目（version/tag/size/sha256）" % MANIFEST.as_posix())
    return check(root, skip_manifest=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="统一版本号：一处改，其余落点跟着走。")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="只校验，不一致退出码 1")
    mode.add_argument("--set", metavar="VERSION", help="写入新版本号并自动复核")
    parser.add_argument("--dry-run", action="store_true", help="配合 --set 只打印不写文件")
    parser.add_argument("--root", default=str(REPO_ROOT), help="仓库根目录（默认脚本所在仓库）")
    args = parser.parse_args(argv)

    root = Path(args.root)
    if args.check:
        return check(root)

    if not _NEW_VERSION_RE.match(args.set):
        print("版本号格式不对：%s（示例 1.1.0-beta.12）" % args.set)
        return 1
    return apply(root, args.set, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
