"""更新器：查 GitHub release、下载并校验、写脚本覆盖安装。

网页「设置 → 版本更新」和启动器共用这一份逻辑，避免两套真相源。只用标准库。

约定：公开函数**绝不抛异常**，失败一律返回 ``{"ok": False, "error": "人话"}``。
GitHub 返回的 JSON 一律当不可信输入处理：缺字段、类型不对、没有 sha256
都只降级并在 error 里说明，不让页面崩、也不卡启动。
"""

from __future__ import annotations

from datetime import datetime, timezone

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote, urlsplit

from noticehub import update_signing, update_sources
from noticehub.version import APP_CHANNEL, APP_PATCH, APP_VERSION

DEFAULT_REPO = update_sources.DEFAULT_REPO
ASSET_NAME = "QQ-Notice-Hub.zip"
LOGGER = logging.getLogger(__name__)
LAUNCHER_EXE = "QQ-Notice-Hub.exe"
USER_AGENT = "notice-hub-updater"
ACCEPT_HEADER = "application/vnd.github+json"
API_ROOT = update_sources.GITHUB_API
ENV_CHANNEL = "NOTICEHUB_UPDATE_CHANNEL"
LEGACY_ENV_CHANNEL = "QQ_DIGEST_UPDATE_CHANNEL"
PROGRESS_STEP = 256 * 1024  # on_progress 每 256 KB 调一次
READ_CHUNK = 64 * 1024
MAX_JSON_BYTES = 4 * 1024 * 1024
APPLYING_FLAG = "applying.flag"
UPDATE_LOG_NAME = "update.log"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 装包/回退脚本只有 Windows 能跑；单独拎出来，非 Windows 分支才能在任意平台被用例锁住。
_WIN = os.name == "nt"

# 下载源白名单：只信任 GitHub / Gitee 的官方域名（含其子域）。
# 清单里的 download_url 是从上游 JSON 读来的不可信输入，谁都能写个镜像地址。
TRUSTED_DOWNLOAD_HOSTS = (
    "github.com",
    "objects.githubusercontent.com",
    "raw.githubusercontent.com",
    "gitee.com",
)

# 代码内哈希锚点：{tag: 64 位 hex}。
# release 元数据（含 sha256）可以被上游改写，只有写死在代码里的值才算可信。
# 空表**不是**「拒绝一切自动安装」：旧版本发布时不可能预知下一个版本的真实哈希，
# 按 fail-closed 做会让所有用户永远升不了级。空表时安全边界退化为三条——
# 下载域名白名单（TRUSTED_DOWNLOAD_HOSTS）、清单里 sha256 必须存在、下载后与装机前各复算一次，
# 并记一条 warning（见 download()）。要拦住某个已知有问题的版本，就在这里登记它的哈希。
PINNED_SHA256: dict[str, str] = {}


def _trusted_download_url(url: str) -> bool:
    """host 必须等于白名单域名或是它的子域；解析不出来一律按不可信处理。"""
    try:
        host = (urlsplit(str(url)).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in TRUSTED_DOWNLOAD_HOSTS)


def _pinned_sha256(tag: Any) -> str:
    return str(PINNED_SHA256.get(str(tag or "").strip()) or "").strip().lower()


__all__ = ["DEFAULT_REPO", "current_version", "current_patch", "resolve_channel", "check", "download", "mark_pending", "pending_update", "clear_pending", "clear_stale_pending", "apply_async", "rollback_async", "skip_patch", "patch_skip", "clear_patch_skip"]


def current_version() -> str:
    """当前安装的版本号。旧版安装没有这个模块，网页端会显示「未知」。"""
    return APP_VERSION


def current_patch() -> int:
    """当前安装的修复包号。旧版 version.py 没有 APP_PATCH，按 0 处理。"""
    patch = globals().get("APP_PATCH", 0)
    if isinstance(patch, bool) or not isinstance(patch, int) or patch < 0:
        return 0
    return patch


# ---------------------------------------------------------------- check


def _strip_v(tag: Any) -> str:
    text = str(tag or "").strip()
    return text[1:] if text[:1] in {"v", "V"} else text


def _empty_latest() -> dict[str, Any]:
    return {
        "tag": "",
        "version": "",
        "name": "",
        "notes": "",
        "published_at": "",
        "asset_name": "",
        "size": 0,
        "sha256": "",
        "download_url": "",
        "html_url": "",
        "fallbacks": [],
    }


def _pick_asset(release: dict[str, Any]) -> dict[str, Any]:
    assets = release.get("assets")
    if not isinstance(assets, list):
        return {}
    for asset in assets:
        if isinstance(asset, dict) and str(asset.get("name") or "") == ASSET_NAME:
            return asset
    return {}


def _asset_sha256(asset: dict[str, Any]) -> str:
    """GitHub 在 assets[].digest 里给形如 `sha256:abcd…`；老 API 没有这个字段。"""
    digest = str(asset.get("digest") or "").strip().lower()
    if digest.startswith("sha256:"):
        return digest.split(":", 1)[1].strip()
    return ""


def _latest_from_release(release: dict[str, Any]) -> dict[str, Any]:
    tag = str(release.get("tag_name") or "").strip()
    asset = _pick_asset(release)
    try:
        size = int(asset.get("size"))
    except (TypeError, ValueError):
        size = 0
    return {
        "tag": tag,
        "version": _strip_v(tag),
        "name": str(release.get("name") or tag),
        "notes": str(release.get("body") or ""),
        "published_at": str(release.get("published_at") or ""),
        "asset_name": str(asset.get("name") or ""),
        "size": size,
        "sha256": _asset_sha256(asset),
        "download_url": str(asset.get("browser_download_url") or ""),
        "html_url": str(release.get("html_url") or ""),
    }


def _published_at_key(release: dict[str, Any]) -> datetime:
    value = release.get("published_at")
    if not isinstance(value, str) or not value.strip():
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        published_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        return published_at.astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def _first_published(releases: Any, *, stable_only: bool = False) -> dict[str, Any]:
    """取发布得最晚的非 draft release；stable_only 会排除含连字符的测试标签。"""
    if not isinstance(releases, list):
        return {}
    candidates = [
        release
        for release in releases
        if isinstance(release, dict)
        and not release.get("draft")
        and release.get("tag_name")
        and (not stable_only or "-" not in _strip_v(release["tag_name"]))
    ]
    if not candidates:
        return {}
    return max(candidates, key=_published_at_key)


def _degraded_note(latest: dict[str, Any]) -> str:
    """ok=True 但仍要告诉用户「能查到，却不能一键更新」的原因。"""
    if not latest.get("asset_name"):
        return "这个版本没有 %s 附件，暂时不能一键更新" % ASSET_NAME
    if not latest.get("sha256"):
        return "这个版本没有提供 sha256，下载后无法校验完整性"
    return ""


class ManifestSignatureError(ValueError):
    """更新清单验签失败：文案已经是对用户说的话，不要再套一层「检查失败」。"""


def _friendly_error(error: BaseException, source_name: str = "GitHub") -> str:
    if isinstance(error, ManifestSignatureError):
        return str(error)
    if isinstance(error, urllib.error.HTTPError):
        if error.code == 403:
            return "%s 返回 403（请求太频繁被限流，或者被网络环境拦了），稍后再试" % source_name
        if error.code == 404:
            return "%s 返回 404（仓库或版本不存在）" % source_name
        return "%s 返回 HTTP %s" % (source_name, error.code)
    if isinstance(error, urllib.error.URLError):
        reason = str(getattr(error, "reason", "") or error).replace("\n", " ")[:160]
        return "%s 网络不通或被墙：%s" % (source_name, reason)
    if isinstance(error, TimeoutError):
        return "连接 %s 超时，稍后再试" % source_name
    if isinstance(error, json.JSONDecodeError):
        return "%s 返回的内容不是合法 JSON（可能被中间设备拦截了）" % source_name
    if isinstance(error, OSError):
        return "%s 网络出问题了：%s" % (source_name, str(error).replace("\n", " ")[:160])
    return "%s 检查失败：%s" % (source_name, str(error).replace("\n", " ")[:160])


def _fetch_json(url: str, timeout: float) -> Any:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": ACCEPT_HEADER, "Cache-Control": "no-cache"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(MAX_JSON_BYTES)
    return json.loads(raw.decode("utf-8", errors="replace"))


def _coerce_patch(value: Any) -> int:
    """把清单里的补丁号收成非负整数；缺失或类型不对一律按 0（宽松版，只用于取值）。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _manifest_entries(manifest: Any) -> list[tuple[tuple, dict[str, Any]]]:
    """验签 + 逐条校验清单，返回 [(版本排序键, 规范化条目)]。

    验签在解析之后、取任何字段之前：不通过就当整份清单不可用，绝不使用它的
    sha256 / download_url（否则「改仓库的人同时换包和哈希」这条路仍然通）。
    """
    if not isinstance(manifest, dict) or type(manifest.get("version")) is not int or manifest["version"] != 1:
        raise ValueError("镜像清单版本或结构无效")
    verified, reason = update_signing.verify_manifest_signature(manifest)
    if not verified:
        raise ManifestSignatureError(
            "更新清单签名校验失败（%s），已拒绝自动更新。请从官网手动下载安装包。" % reason
        )
    items = manifest.get("items")
    if not isinstance(items, list):
        raise ValueError("镜像清单 items 不是数组")
    required_strings = ("tag", "version", "name", "notes", "published_at", "asset_name", "sha256", "download_url")
    entries: list[tuple[tuple, dict[str, Any]]] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("镜像清单包含非对象条目")
        for key in required_strings:
            if key not in item or not isinstance(item[key], str):
                raise ValueError("镜像清单条目缺少有效字段：%s" % key)
        size = item.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("镜像清单条目的 size 类型无效")
        tag = item["tag"].strip()
        version = item["version"].strip()
        if not tag or not version or item["asset_name"] != ASSET_NAME or not item["download_url"].strip():
            raise ValueError("镜像清单条目缺少版本或安装包地址")
        sha = item["sha256"].strip().lower()
        if sha and not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError("镜像清单 sha256 格式无效")
        # 修复号可选；一旦给了就必须是干净的非负整数，否则这一条不可信，整条跳过。
        # 跳过而不是抛错：一条坏条目不该让其它版本一起不可用。
        patch_value = item.get("patch", 0)
        if isinstance(patch_value, bool) or not isinstance(patch_value, int) or patch_value < 0:
            LOGGER.warning("镜像清单条目的修复号无效，已跳过：%s", tag)
            continue
        version_key = _version_tuple(version)
        if not version_key:
            continue
        entries.append((version_key, {
            "tag": tag,
            "version": version,
            "name": item["name"],
            "notes": item["notes"],
            "published_at": item["published_at"],
            "asset_name": item["asset_name"],
            "size": size,
            "sha256": sha,
            "download_url": item["download_url"].strip(),
            "html_url": "",
            "fallbacks": [],
            "patch": patch_value,
        }))
    return entries


def _latest_from_manifest(manifest: Any, source: dict[str, str], *, stable_only: bool) -> dict[str, Any]:
    candidates = [
        (version_key, dict(entry))
        for version_key, entry in _manifest_entries(manifest)
        if not (stable_only and "-" in entry["tag"])
    ]
    if not candidates:
        raise ValueError("镜像清单没有适用于当前通道的有效版本")
    try:
        latest = max(candidates, key=lambda candidate: candidate[0])[1]
    except TypeError as error:
        raise ValueError("镜像清单版本号无法排序") from error
    latest["html_url"] = "https://gitee.com/%s/releases/tag/%s" % (
        source["repo"], quote(latest["tag"], safe="-._")
    )
    return latest


def _manifest_authority(manifest: Any, tag: str) -> dict[str, Any] | None:
    """取清单里与 release 同一个 tag 的条目；没有就返回 None（调用方按失败关闭处理）。"""
    for _version_key, entry in _manifest_entries(manifest):
        if entry["tag"] == tag:
            return entry
    return None



def _fetch_manifest(source: dict[str, str], timeout: float) -> Any:
    """拉镜像清单：release/ 下的新位置优先，404 再退根目录（兼容挪位前/后的仓库）。

    两个地址共用同一份超时预算：退避那次只能用剩下的时间，不会把 30 秒悄悄放大成 60 秒。
    """
    urls = update_sources.manifest_urls(source)
    deadline = time.monotonic() + float(timeout)
    last_error: Exception | None = None
    for index, url in enumerate(urls):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            return _fetch_json(url, remaining)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            last_error = error
        if index == len(urls) - 1:
            raise last_error  # type: ignore[misc]
    raise last_error or ValueError("镜像清单地址都不可用")  # type: ignore[misc]


def _latest_for_source(source: dict[str, str], timeout: float, *, stable_only: bool) -> dict[str, Any]:
    if source["id"] == "github":
        releases = _fetch_json(
            "%s/repos/%s/releases?per_page=100" % (source["api_root"], source["repo"]), timeout
        )
        release = _first_published(releases, stable_only=stable_only)
        if not release.get("tag_name"):
            raise ValueError("没有可用的 Release")
        latest = _latest_from_release(release)
        if not latest.get("asset_name") or not latest.get("sha256"):
            # 没有可安装的资产、或清单没给哈希：本来就无法一键更新，交给降级文案说明。
            # 这种版本也没有「可被替换的安装包」，不需要再要求清单权威。
            return latest
        # 安装权威只认签名清单：Release API 只负责「给人看的」标题/正文，
        # 它的资产 size / sha256 一律不用于安装判断。
        authority = _manifest_authority(_fetch_manifest(source, timeout), latest["tag"])
        if authority is None:
            raise ValueError("镜像清单可能还没同步到这一版，过几分钟再点一次检查")
        for key in ("size", "sha256", "download_url", "asset_name", "fallbacks", "patch"):
            latest[key] = authority[key]
        return latest
    manifest = _fetch_manifest(source, timeout)
    return _latest_from_manifest(manifest, source, stable_only=stable_only)


def _version_tuple(value: Any) -> tuple:
    """Reuse announce.py's canonical version ordering for update decisions."""
    try:
        from .announce import _ver_tuple
        return _ver_tuple(str(value or "").strip())
    except Exception:
        return ()


def _normalize_channel(value: Any) -> str:
    """归一成 stable / beta / off；其它任何值都当 beta（契约要求）。"""
    text = str(value or "").strip().lower()
    return text if text in {"stable", "beta", "off"} else "beta"


def resolve_channel(channel: str | None = None) -> str:
    """解析更新通道：显式参数 > 环境变量 ``NOTICEHUB_UPDATE_CHANNEL`` > ``version.APP_CHANNEL``。

    大小写不敏感，非法值当 ``beta``；``off`` 表示不检查、不通知。
    """
    if channel is not None and str(channel).strip():
        return _normalize_channel(channel)
    from_env = os.environ.get(ENV_CHANNEL, os.environ.get(LEGACY_ENV_CHANNEL))
    if from_env is not None and str(from_env).strip():
        return _normalize_channel(from_env)
    return _normalize_channel(APP_CHANNEL)


def check(
    repo: str | None = None,
    *,
    channel: str | None = None,
    timeout: float = 8.0,
    settings: Any = None,
) -> dict[str, Any]:
    """查一次最新版本。

    通道按 :func:`resolve_channel` 解析；``beta`` 走 release 列表（允许 prerelease），
    ``stable`` 走 release 列表并排除测试标签，``off`` 直接返回空结果、不联网、不算错。

    每次都真查（不缓存），缓存由调用方负责（网页端做 6 小时缓存）。
    绝不抛异常：网络/限流/JSON 坏都返回 ``ok=False`` + 人话 ``error``。
    """
    effective_channel = resolve_channel(channel)
    target_repo = str(repo or DEFAULT_REPO).strip() or DEFAULT_REPO
    result: dict[str, Any] = {
        "ok": False,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "channel": effective_channel,
        "current": {"version": current_version(), "tag": "v" + current_version()},
        "latest": _empty_latest(),
        "has_update": False,
        "patch_current": current_patch(),
        "patch_latest": 0,
        "patch_update": False,
        "error": "",
        "source": "",
        "sources_tried": [],
        "warning": "",
    }
    if effective_channel == "off":
        result["ok"] = True
        return result

    try:
        source_list = update_sources.sources(settings)
        if repo:
            source_list = [dict(source) for source in source_list]
            source_list[0]["repo"] = target_repo
            source_list[0]["raw_base"] = update_sources.GITHUB_RAW % target_repo
        selected: tuple[dict[str, str], dict[str, Any]] | None = None
        available: list[tuple[dict[str, str], dict[str, Any]]] = []
        for source in source_list:
            source_name = "GitHub" if source["id"] == "github" else "Gitee"
            try:
                latest = _latest_for_source(
                    source, timeout, stable_only=effective_channel == "stable"
                )
                latest.setdefault("fallbacks", [])
                available.append((source, latest))
                result["sources_tried"].append({"id": source["id"], "ok": True, "error": ""})
                if selected is None:
                    selected = (source, latest)
            except Exception as error:  # noqa: BLE001 - 每个源独立降级
                result["sources_tried"].append({
                    "id": source["id"],
                    "ok": False,
                    "error": _friendly_error(error, source_name),
                })

        if selected is None:
            # 验签失败是「有人动过清单」级别的信号，直接把这句原话给用户，
            # 不要再裹一层「更新源均不可用」把重点冲掉。
            signature_failure = next(
                (
                    entry["error"]
                    for entry in result["sources_tried"]
                    if "签名校验失败" in str(entry.get("error") or "")
                ),
                "",
            )
            if signature_failure:
                result["error"] = signature_failure
                return result
            details = ["%s：%s" % (entry["id"], entry["error"]) for entry in result["sources_tried"]]
            result["error"] = "更新源均不可用：" + ("；".join(details) or "没有配置可用源")
            return result

        selected_source, latest = selected
        target_asset_name = latest.get("asset_name") or ASSET_NAME
        matching = [
            (source, candidate) for source, candidate in available
            if candidate.get("tag") == latest.get("tag")
            and candidate.get("asset_name") == target_asset_name
        ]
        if not latest.get("asset_name") and matching:
            latest["asset_name"] = target_asset_name
            if not latest.get("size"):
                latest["size"] = next((candidate.get("size", 0) for _, candidate in matching), 0)
        github_sha = next((str(item.get("sha256") or "").strip().lower()
                           for source, item in matching if source["id"] == "github" and item.get("sha256")), "")
        manifest_sha = next((str(item.get("sha256") or "").strip().lower()
                             for source, item in matching if source["id"] == "gitee" and item.get("sha256")), "")
        latest["sha256"] = github_sha or manifest_sha or str(latest.get("sha256") or "")

        # Build download URL list: prefer Gitee when both sources have matching sha256
        download_urls = []
        gitee_url = ""
        github_url = ""
        for source, candidate in matching:
            if candidate.get("download_url"):
                url = str(candidate["download_url"])
                if source["id"] == "gitee":
                    gitee_url = url
                elif source["id"] == "github":
                    github_url = url

        # Use Gitee as primary only when both sources have the same non-empty sha256
        # Fallback source only added when sha256 match or either side is empty (safety: avoid wrong package)
        sha_match = github_sha and manifest_sha and github_sha == manifest_sha
        sha_unknown = not github_sha or not manifest_sha
        if github_sha and manifest_sha and github_sha != manifest_sha:
            # 两个清单都给了哈希却对不上，是最该报警的信号：静默按 GitHub 继续
            # 会把「镜像被改过 / 还没同步完」藏起来。sha_match 语义不动，只加提示。
            warning = "GitHub 与 Gitee 清单对 %s 的 sha256 不一致，已按 GitHub 清单继续" % (latest.get("tag") or "")
            result["warning"] = warning
            LOGGER.warning("%s", warning)
        if gitee_url and github_url and sha_match:
            latest["download_url"] = gitee_url
            download_urls.append(gitee_url)
            download_urls.append(github_url)
        elif github_url:
            download_urls.append(github_url)
            if gitee_url and sha_unknown:
                download_urls.append(gitee_url)
        elif gitee_url:
            download_urls.append(gitee_url)

        # Fallbacks are all URLs except the primary
        primary = latest.get("download_url", "")
        latest["fallbacks"] = [url for url in download_urls if url != primary]
        result["source"] = selected_source["id"]
        result["latest"] = latest
        result["ok"] = True
        # 补丁号只看「同一版本」的条目：版本不同时 patch_latest 没有可比意义，按 0。
        # 先算补丁，再决定 error：同版本有补丁可装时不能对外说「无需更新」。
        patch_current = current_patch()
        patch_latest = 0
        if str(latest.get("version") or "").strip() == current_version():
            patch_latest = _coerce_patch(latest.get("patch"))
        patch_update = patch_latest > patch_current
        result["patch_current"] = patch_current
        result["patch_latest"] = patch_latest
        result["patch_update"] = patch_update
        current_tuple = _version_tuple(current_version())
        latest_tuple = _version_tuple(latest.get("version"))
        if current_tuple and latest_tuple:
            result["has_update"] = latest_tuple > current_tuple
            if latest_tuple <= current_tuple and not patch_update:
                result["error"] = "线上 Release 比本机版本旧（这一版还没发布），无需更新"
        else:
            result["error"] = "线上或本机版本号无法比较，无需更新"
        if not result["error"]:
            result["error"] = _degraded_note(latest)
        return result
    except Exception as error:  # noqa: BLE001 - 契约要求绝不抛异常
        result["error"] = "更新检查失败：%s" % str(error).replace("\n", " ")[:200]
        return result


# ------------------------------------------------------------- download


def _download_filename(tag: Any) -> str:
    """标签来自 GitHub，是不可信输入：必须过滤，别让它把文件写到目录外面去。"""
    safe = re.sub(r"[^0-9A-Za-z._-]", "_", str(tag or "").strip()).lstrip(".")
    return "QQ-Notice-Hub-%s.zip" % (safe or "unknown")


def _content_length(response: Any) -> int:
    try:
        value = response.headers.get("Content-Length")
        return int(value) if value else 0
    except (TypeError, ValueError, AttributeError):
        return 0


def _call_progress(callback: Optional[Callable[[int, int], Any]], done: int, total: int) -> None:
    if callback is None:
        return
    try:
        callback(done, total)
    except Exception:  # noqa: BLE001 - 回调是 UI 的，崩了不能拖垮下载
        pass


def _remove_quietly(path: Optional[Path]) -> None:
    if path is None:
        return
    try:
        Path(path).unlink()
    except OSError:
        pass


def download(
    info: dict[str, Any],
    dest_dir: Path,
    *,
    timeout: float = 30.0,
    on_progress: Optional[Callable[[int, int], Any]] = None,
) -> dict[str, Any]:
    """下载到 ``dest_dir/QQ-Notice-Hub-<tag>.zip``，边下边算 sha256。

    sha256 与 ``info["sha256"]`` 不符就删掉半截文件并返回 ``ok=False``。
    """
    outcome: dict[str, Any] = {"ok": False, "path": "", "bytes": 0, "sha256": "", "error": ""}
    destination: Optional[Path] = None
    try:
        data = info if isinstance(info, dict) else {}
        primary_url = str(data.get("download_url") or "").strip()
        urls: list[tuple[str, str]] = []
        if primary_url:
            urls.append((primary_url, "主下载源"))
        fallbacks = data.get("fallbacks")
        if isinstance(fallbacks, list):
            for fallback in fallbacks:
                if isinstance(fallback, dict):
                    url = str(fallback.get("download_url") or fallback.get("url") or "").strip()
                    name = str(fallback.get("id") or fallback.get("source") or "备用源")
                else:
                    url, name = str(fallback or "").strip(), "备用源"
                if url and all(url != existing for existing, _ in urls):
                    urls.append((url, name))
        if not urls:
            outcome["error"] = "没有下载地址（这个版本可能没有 %s 附件）" % ASSET_NAME
            return outcome
        trusted: list[tuple[str, str]] = []
        rejected: list[str] = []
        for url, source_name in urls:
            if _trusted_download_url(url):
                trusted.append((url, source_name))
            else:
                rejected.append("%s：域名不在信任白名单（%s）" % (source_name, url))
        if not trusted:
            outcome["error"] = "下载源不可信，已拒绝"
            return outcome
        expected = str(data.get("sha256") or "").strip().lower()
        if not expected:
            outcome["error"] = "这个版本没有提供 sha256 校验值，为安全起见不自动安装；请手动下载"
            return outcome
        pinned = _pinned_sha256(data.get("tag"))
        if pinned and pinned != expected:
            # 有登记就必须相等：清单被换掉而代码 pin 没变，说明包/清单不同源。
            outcome["error"] = "清单里的校验值与代码内登记的不一致，已拒绝自动安装"
            return outcome
        if not pinned:
            # 旧版本发出去时不可能预知下一个版本的真实哈希，空表不能等于「禁止升级」。
            # 此时安全边界退化为：下载域名白名单 + 清单 sha256 必须存在 + 下载与装机两处复算。
            LOGGER.warning(
                "版本 %s 没有代码内登记哈希，仅依赖域名白名单、清单 sha256 与安装前复算",
                data.get("tag"),
            )
        try:
            metadata_size = int(data.get("size") or 0)
        except (TypeError, ValueError, OverflowError):
            metadata_size = 0
        if metadata_size < 0:
            metadata_size = 0
        directory = Path(dest_dir)
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / _download_filename(data.get("tag"))
        failures: list[str] = list(rejected)

        for url, source_name in trusted:
            digest = hashlib.sha256()
            done = 0
            reported = 0
            total = 0
            try:
                request = urllib.request.Request(
                    url,
                    headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
                    method="GET",
                )
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    total = _content_length(response)
                    if total and metadata_size and total != metadata_size:
                        raise ValueError("响应长度与版本清单 size 不符")
                    expected_length = total or metadata_size
                    with destination.open("wb") as stream:
                        while True:
                            chunk = response.read(READ_CHUNK)
                            if not chunk:
                                break
                            stream.write(chunk)
                            digest.update(chunk)
                            done += len(chunk)
                            if done - reported >= PROGRESS_STEP and done != reported:
                                reported = done
                                _call_progress(on_progress, done, total)
                if expected_length and done != expected_length:
                    raise ValueError("下载长度与服务器或清单声明不符（%d/%d 字节）" % (done, expected_length))
                if done and reported != done:
                    _call_progress(on_progress, done, total)

                actual = digest.hexdigest()
                outcome.update({"path": str(destination), "bytes": done, "sha256": actual})
                if actual != expected:
                    _remove_quietly(destination)
                    outcome.update({"path": "", "ok": False, "error": "下载的文件 sha256 校验不过，已删除，请重试"})
                    return outcome
                outcome["ok"] = True
                return outcome
            except Exception as error:  # noqa: BLE001 - 失败时尝试同版本备用地址
                _remove_quietly(destination)
                failures.append("%s：%s" % (source_name, _friendly_error(error, source_name)))
                outcome.update({"path": "", "bytes": 0, "sha256": ""})

        outcome["error"] = "所有下载地址均失败：" + "；".join(failures)
        return outcome
    except Exception as error:  # noqa: BLE001 - 契约要求绝不抛异常
        _remove_quietly(destination)
        outcome["path"] = ""
        outcome["error"] = _friendly_error(error)
        return outcome


# ------------------------------------------------------------ apply


PENDING_UPDATE = "pending.json"


def mark_pending(
    data_dir: Path, *, zip_path: Path, version: str, sha256: str = "", patch: int = 0
) -> dict[str, Any]:
    """原子记录一个等待托盘安装的更新包（带下载时算出的哈希，装机前要复算）。"""
    outcome: dict[str, Any] = {"ok": False, "error": ""}
    path = Path(zip_path)
    try:
        if not path.is_file():
            outcome["error"] = "更新包不存在：%s" % path
            return outcome
        record = {
            "zip": str(path.resolve()),
            "version": str(version).strip(),
            "sha256": str(sha256 or "").strip().lower(),
            "patch": _coerce_patch(patch),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if not record["version"]:
            outcome["error"] = "更新版本号为空"
            return outcome
        updates = Path(data_dir) / "updates"
        updates.mkdir(parents=True, exist_ok=True)
        destination = updates / PENDING_UPDATE
        # 唯一临时名：并发/重复调用不会互踩同一个 pending.json.tmp。
        temporary = updates / f"{PENDING_UPDATE}.{os.getpid()}-{threading.get_ident()}.tmp"
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        # Windows 上目标文件可能被托盘短暂占用，replace 失败重试几次。
        for attempt in range(5):
            try:
                temporary.replace(destination)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.01)
        outcome["ok"] = True
    except (OSError, ValueError, TypeError) as error:
        outcome["error"] = "写入待安装更新失败：%s" % error
        try:
            temporary.unlink(missing_ok=True)
        except (OSError, UnboundLocalError):
            pass
    return outcome


def _read_pending_record(data_dir: Path) -> dict[str, Any] | None:
    """读裸记录，顺带做结构校验；版本是否已经装上不在这里判断。"""
    try:
        record = json.loads((Path(data_dir) / "updates" / PENDING_UPDATE).read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            return None
        zip_path = record.get("zip")
        version = record.get("version")
        at = record.get("at")
        if not isinstance(zip_path, str) or not Path(zip_path).is_absolute():
            return None
        if not isinstance(version, str) or not version.strip() or not isinstance(at, str):
            return None
        sha256 = record.get("sha256")
        if not isinstance(sha256, str):
            sha256 = ""
        return {
            "zip": zip_path,
            "version": version,
            "at": at,
            "sha256": sha256,
            "patch": _coerce_patch(record.get("patch")),
        }
    except (OSError, ValueError, TypeError):
        return None


def _pending_is_stale(version: str, running: str | None = None, patch: int | None = None) -> bool:
    """待装包不比正在跑的版本新 ⇒ 已经装过了。

    相等是最常见的一种（更新脚本装的）。比当前旧也算：用户手动覆装、或者从
    更新包回退过之后，这条记录再留着只会让托盘和页面一直喊「更新包已就绪」，
    点下去还会把旧包装回来。版本号读不出来时一律不动。

    ``patch`` 传进来时才比较修复号：同版本下，待装包的补丁号不比当前新也算装过了。
    不传就完全按老行为（同版本一律过期），别改动既有调用点。
    """
    current = (running if running is not None else current_version()).strip()
    if not current or current == "未知":
        return False
    if version == current:
        if patch is None:
            return True
        return _coerce_patch(patch) <= current_patch()
    try:
        pending_key = _version_tuple(version)
        current_key = _version_tuple(current)
    except Exception:  # noqa: BLE001 - 比不出来就当没过时
        return False
    return bool(pending_key and current_key and pending_key < current_key)


def pending_update(data_dir: Path) -> dict[str, Any] | None:
    """返回仍然待装的记录；损坏、缺失或已经装过的都视为没有待安装更新。"""
    record = _read_pending_record(data_dir)
    if not record:
        return None
    if _pending_is_stale(record["version"], patch=record.get("patch")):
        return None
    return record


def clear_stale_pending(data_dir: Path) -> str:
    """清掉已经装过的待装记录，返回被清掉的版本号（没清就返回空串）。

    只清「不比当前版本新」的那些；比当前新的记录是真没装，不能碰。
    """
    record = _read_pending_record(data_dir)
    if not record:
        return ""
    if not _pending_is_stale(record["version"], patch=record.get("patch")):
        return ""
    clear_pending(data_dir)
    return record["version"]


def clear_pending(data_dir: Path) -> None:
    try:
        (Path(data_dir) / "updates" / PENDING_UPDATE).unlink(missing_ok=True)
    except OSError:
        pass


def _latest_release_page(data_dir: Path) -> str:
    try:
        state = json.loads((Path(data_dir) / "updates" / "state.json").read_text(encoding="utf-8"))
        result = state.get("update_result") if isinstance(state, dict) else None
        latest = result.get("latest") if isinstance(result, dict) else None
        html_url = latest.get("html_url") if isinstance(latest, dict) else None
        if isinstance(html_url, str) and html_url.strip():
            return html_url.strip()
    except (OSError, ValueError, TypeError):
        pass
    return "https://github.com/%s/releases" % DEFAULT_REPO


def _ps_quote(value: Any) -> str:
    """PowerShell 单引号字面量（内部的 ' 需要写两遍）。""" 
    return "'" + str(value).replace("'", "''") + "'"


def _script_name() -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    directory = Path(tempfile.gettempdir())
    candidate = "notice-hub-update-%s.ps1" % stamp
    counter = 1
    while (directory / candidate).exists():
        candidate = "notice-hub-update-%s-%d.ps1" % (stamp, counter)
        counter += 1
    return candidate


def _script_text(
    install_root: Path,
    zip_path: Path,
    pid: int,
    data_dir: Path,
    web_port: int = 8766,
    expected_sha256: str = "",
    patch: int = 0,
    backup_patch: int = 0,
) -> str:
    """生成覆盖安装脚本正文（纯函数，方便测试断言）。

    ``patch`` 是本次即将装上的新补丁号，只用于日志文案；``backup_patch`` 是装机前
    那一版的补丁号，用于回退包文件名 ``prev-<backup_patch>.zip``——里面存的正是
    装机前的文件，回退时才能报出正确的「从 N 回到 M」。
    """
    root = str(install_root)
    launcher = str(Path(install_root) / LAUNCHER_EXE)
    log = str(Path(data_dir) / "logs" / UPDATE_LOG_NAME)
    flag = str(Path(data_dir) / "updates" / APPLYING_FLAG)
    patch_label = str(_coerce_patch(backup_patch))
    backup_name = "%s%d.zip" % (BACKUP_PREFIX, _coerce_patch(backup_patch))

    lines = [
        "# notice-hub 覆盖更新脚本（由 noticehub/updater.py 生成，别手改）。",
        "# 为什么要有这个脚本：覆盖安装必须等本安装目录的进程全部退出，",
        "# 而正在运行的进程锁着自己的 exe，没法给自己换文件。",
        "$ErrorActionPreference = 'Continue'",
        "$root = " + _ps_quote(root),
        "$zip = " + _ps_quote(zip_path),
        "$pidToWait = " + _ps_quote(pid),
        "$log = " + _ps_quote(log),
        "$flag = " + _ps_quote(flag),
        "$launcher = " + _ps_quote(launcher),
        "$data = " + _ps_quote(data_dir),
        "$webPort = " + _ps_quote(int(web_port)),
        "$expectedHash = " + _ps_quote(str(expected_sha256 or "").strip().lower()),
        "$patchLabel = " + _ps_quote(patch_label),  # 装机前那一版的补丁号，备份文件名用
        "$newPatchLabel = " + _ps_quote(str(_coerce_patch(patch))),  # 本次即将装上的补丁号，只写日志
        "",
        "New-Item -ItemType Directory -Force -Path (Split-Path -Parent $log) | Out-Null",
        "function Write-UpdateLog([string]$message) {",
        "    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $message",
        "    try { Add-Content -LiteralPath $log -Value $line -Encoding UTF8 } catch { }",
        "}",
        "",
        "Write-UpdateLog '请求启动器执行优雅退出（--quit）'",
        "try {",
        "    $quit = Start-Process -FilePath $launcher -ArgumentList '--quit' -WorkingDirectory $root -Wait -PassThru -ErrorAction Stop",
        "    Write-UpdateLog ('--quit 返回 {0}' -f $quit.ExitCode)",
        "} catch {",
        "    Write-UpdateLog ('--quit 请求失败，继续等待后走进程兜底：{0}' -f $_)",
        "}",
        "Write-UpdateLog ('等待进程 {0} 自然退出（最多 60 秒）' -f $pidToWait)",
        "$deadline = (Get-Date).AddSeconds(60)",
        "while ((Get-Date) -lt $deadline) {",
        "    if (-not (Get-Process -Id $pidToWait -ErrorAction SilentlyContinue)) { break }",
        "    Start-Sleep -Milliseconds 500",
        "}",
        "if (Get-Process -Id $pidToWait -ErrorAction SilentlyContinue) { Write-UpdateLog '优雅退出等待超时，随后按安装目录兜底结束进程' }",
        "else { Write-UpdateLog '目标进程已自然退出，再等 2 秒让文件句柄释放' }", 
        "Start-Sleep -Seconds 2",
        "",
        "# 结束两类进程：本安装目录下的（按可执行路径前缀），以及本安装自己的 NapCat/QQ",
        "# （按镜像名 + 本安装数据目录匹配）。只按镜像名 taskkill 会杀掉同机别人的实例",
        "# （见 04-task-B-mediums.md B-10），所以名字必须再叠一层「属于本安装」的范围。",
        "$prefix = $root.TrimEnd('\\') + '\\'",
        "$dataPrefix = $data.TrimEnd('\\') + '\\'",
        "$names = @('qq.exe', 'napcatwinbootmain.exe', 'napcat.exe')",
        "Write-UpdateLog ('结束本安装相关进程（目录 {0}*，名称 {1}）' -f $prefix, ($names -join '/'))",
        "try {",
        "    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |",
        "        Where-Object {",
        "            if (-not $_.ExecutablePath) { return $false }",
        "            if ($_.ExecutablePath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) { return $true }",
        "            if ($names -notcontains $_.Name.ToLowerInvariant()) { return $false }",
        "            if ($_.ExecutablePath.StartsWith($dataPrefix, [System.StringComparison]::OrdinalIgnoreCase)) { return $true }",
        "            if ($_.CommandLine -and $_.CommandLine.IndexOf($dataPrefix, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) { return $true }",
        "            return $false",
        "        } |",
        "        ForEach-Object {",
        "            Write-UpdateLog ('  结束 {0} (pid {1})' -f $_.ExecutablePath, $_.ProcessId)",
        "            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue",
        "        }",
        "} catch {",
        "    Write-UpdateLog ('结束进程时出错（继续解压）：{0}' -f $_)",
        "}",
        "Start-Sleep -Seconds 1",
        "",
        "# 装机目录里可能有只读文件（历史坑 bug-hunt-packaging C-03：",
        "# assets\\donate-wechat.jpg 带 ReadOnly），Expand-Archive -Force 会因此报",
        "# PermissionError，先清掉只读位，否则覆盖更新必然失败。",
        "try {",
        "    Get-ChildItem -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue |",
        "        Where-Object { $_.Attributes -band [System.IO.FileAttributes]::ReadOnly } |",
        "        ForEach-Object { $_.Attributes = $_.Attributes -bxor [System.IO.FileAttributes]::ReadOnly }",
        "} catch {",
        "    Write-UpdateLog ('清理只读属性时出错（继续）：{0}' -f $_)",
        "}",
        "",
        "# 装机前复算一次包哈希（TOCTOU）：下载时校验过，但从落盘到执行之间包仍可能被换掉。",
        "# 哈希不符就一个文件都不动，绝不把来路不明的包解进安装目录。",
        "$actualHash = ''",
        "try { $actualHash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant() } catch { Write-UpdateLog ('复算更新包哈希失败：{0}' -f $_) }",
        "$hashOk = ($expectedHash -ne '' -and $actualHash -eq $expectedHash)",
        "if (-not $hashOk) { Write-UpdateLog ('更新包哈希不符，拒绝解压：期望 {0}，实际 {1}' -f $expectedHash, $actualHash) }",
        "",
        "# 解压前先探测目标文件有没有被占用（典型：VCRUNTIME140.dll 被 NapCat 子进程锁住）。",
        "# Expand-Archive -Force 是边解边覆盖，中途撞上占用就留下半新半旧的装机；",
        "# 这里宁可一个文件都不动，也不要装一半。",
        "$expanded = $false",
        # 备份成败要在整段脚本都能读到：哈希不符/占用时压根不会进备份分支，
        # 收尾判断不能因为变量没定义就把「哈希不符」说成「备份失败」。
        "$backupOk = $false",
        "$blocked = @()",
        "$probeOk = $true",
        "try {",
        "    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop",
        "    $archive = [System.IO.Compression.ZipFile]::OpenRead($zip)",
        "    try {",
        "        foreach ($entry in $archive.Entries) {",
        "            if ([string]::IsNullOrEmpty($entry.Name)) { continue }",
        "            $relative = $entry.FullName.Replace('/', '\\')",
        # .. 必须是完整一段才算穿越（分隔符分段判断），my..file.txt 这类合法名不拦
        "            if (($relative -split '\\\\') -contains '..') {",
        "                Write-UpdateLog ('跳过可疑路径：{0}' -f $entry.FullName)",
        "                continue",
        "            }",
        "            $target = Join-Path $root $relative",
        "            if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { continue }",
        "            try {",
        "                $stream = [System.IO.File]::Open($target, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)",
        "                $stream.Dispose()",
        "            } catch {",
        "                $blocked += $target",
        "            }",
        "        }",
        "    } finally { $archive.Dispose() }",
        "} catch {",
        "    $probeOk = $false",
        "    Write-UpdateLog ('占用探测没能执行，为免装一半，本次不覆盖：{0}' -f $_)",
        "}",
        "",
        "if (-not $hashOk) {",
        "    Write-UpdateLog '跳过解压：更新包哈希不符'",
        "} elseif (-not $probeOk) {",
        "    Write-UpdateLog '跳过解压：占用探测失败，未覆盖任何文件'",
        "} elseif ($blocked.Count -gt 0) {",
        "    Write-UpdateLog ('以下文件被占用，未开始覆盖（一个文件都没改）：{0}' -f ($blocked -join '; '))",
        "} else {",
        "    # 覆盖前先把「本次会被换掉的已装文件」备份一份：同版本修复包装坏了，",
        "    # 只能靠这份快照退回去。按 zip 条目列表逐个取，不整目录打包（数据目录在装机根下，",
        "    # 整目录会把用户待办一起卷进去）。",
        "    $backupDir = Join-Path $data 'updates\\rollback'",
        "    $backupZip = Join-Path $backupDir " + _ps_quote(backup_name),
        "    try {",
        "        New-Item -ItemType Directory -Force -Path $backupDir | Out-Null",
        "        # 只留最近一份回退包，旧的先删：多份堆积既占地方，也分不清该退哪一份。",
        "        Get-ChildItem -LiteralPath $backupDir -Filter 'prev-*.zip' -File -ErrorAction SilentlyContinue |",
        "            Remove-Item -Force -ErrorAction SilentlyContinue",
        "        $staging = Join-Path $backupDir 'staging'",
        "        if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue }",
        "        New-Item -ItemType Directory -Force -Path $staging | Out-Null",
        "        $saved = 0",
        "        $archive = [System.IO.Compression.ZipFile]::OpenRead($zip)",
        "        try {",
        "            foreach ($entry in $archive.Entries) {",
        "                if ([string]::IsNullOrEmpty($entry.Name)) { continue }",
        "                $relative = $entry.FullName.Replace('/', '\\')",
        "                if (($relative -split '\\\\') -contains '..') { continue }",
        "                $target = Join-Path $root $relative",
        "                if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { continue }",
        "                $copy = Join-Path $staging $relative",
        "                New-Item -ItemType Directory -Force -Path (Split-Path -Parent $copy) | Out-Null",
        "                Copy-Item -LiteralPath $target -Destination $copy -Force -ErrorAction Stop",
        "                $saved++",
        "            }",
        "        } finally { $archive.Dispose() }",
        "        if ($saved -gt 0) {",
        "            Compress-Archive -Path (Join-Path $staging '*') -DestinationPath $backupZip -Force -ErrorAction Stop",
        "            Write-UpdateLog ('已备份补丁 {0} 的 {1} 个将被覆盖的文件到 {2}（本次装补丁 {3}）' -f $patchLabel, $saved, $backupZip, $newPatchLabel)",
        "        } else {",
        "            Write-UpdateLog '没有已装文件会被覆盖（全新安装），无需备份'",
        "        }",
        "        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue",
        "        $backupOk = $true",
        "    } catch {",
        "        Write-UpdateLog ('备份失败，本次未安装：{0}' -f $_)",
        "    }",
        "    if (-not $backupOk) {",
        "        Write-UpdateLog '备份失败，本次未安装：没有动任何已装文件'",
        "    } else {",
        "        try {",
        "            Write-UpdateLog ('解压覆盖：{0} -> {1}' -f $zip, $root)",
        "            Expand-Archive -LiteralPath $zip -DestinationPath $root -Force -ErrorAction Stop",
        "            $expanded = $true",
        "            Write-UpdateLog '解压覆盖完成'",
        "        } catch {",
        "            Write-UpdateLog ('解压失败（继续拉起旧版）：{0}。若是装在 Program Files，可能需要用管理员权限再点一次更新' -f $_)",
        "        }",
        "    }",
        "}",
        "",
        "# 出货包的顶层若与安装目录布局不同（zip 里是 qq-live-digest\\...，装机是扁平目录），",
        "# 解压只会多出一层子目录，真正在跑的旧版不会被替换。打包布局问题不在这里修，",
        "# 但必须记下来，否则用户只看到「点了更新却没变化」。",
        "$nestedLauncher = Join-Path $root 'qq-live-digest\\QQ-Notice-Hub.exe'",
        "if ($expanded -and (Test-Path -LiteralPath $nestedLauncher)) {",
        "    Write-UpdateLog ('注意：解压出的程序在子目录 {0}，而本安装目录是扁平布局，计划任务仍会启动 {1}' -f $nestedLauncher, $launcher)",
        "}",
        "",
        "Write-UpdateLog '无论解压成败，都拉起启动器，不能让用户面对一个打不开的目录'",
        "$started = $false",
        "try {",
        "    # 自动更新后的重启不该抢用户屏幕：带 --no-browser，别在浏览器里再开一个标签。",
        "    Start-Process -FilePath $launcher -ArgumentList '--no-browser' -WorkingDirectory $root",
        "    $started = $true",
        "    Write-UpdateLog ('已拉起 {0}' -f $launcher)",
        "} catch {",
        "    Write-UpdateLog ('拉起启动器失败：{0}' -f $_)",
        "}",
        "",
        "if ($started) { Write-UpdateLog '启动器进程已创建' } else { Write-UpdateLog '启动器未能启动' }",
        "",
        "# 启动器进程创建成功不代表服务真的起来了：新版首次启动要初始化数据库、端口可能还在监听中。",
        "# 只探一次就放开，用户会遇到「脚本跑完了但网页打不开，只能自己再双击一次」。",
        "# 这里最多重试 3 次、间隔 2 秒；仍失败就写清人工兜底动作，绝不死循环。",
        "$healthy = $false",
        "if ($started) {",
        "    for ($i = 1; $i -le 3; $i++) {",
        "        try {",
        "            $req = [System.Net.HttpWebRequest]::Create(('http://127.0.0.1:{0}/health' -f $webPort))",
        "            $req.Proxy = $null",
        "            $req.Timeout = 3000",
        "            $req.ReadWriteTimeout = 3000",
        "            $resp = $req.GetResponse()",
        "            $code = [int]$resp.StatusCode",
        "            $resp.Close()",
        "            if ($code -eq 200) { $healthy = $true; break }",
        "        } catch { }",
        "        if ($i -lt 3) { Start-Sleep -Seconds 2 }",
        "    }",
        "}",
        "if ($healthy) {",
        "    Write-UpdateLog ('健康检查通过：http://127.0.0.1:{0}/health' -f $webPort)",
        "} elseif ($started) {",
        "    Write-UpdateLog ('警告：3 次健康检查（间隔 2 秒）均失败，http://127.0.0.1:{0}/health 无响应。请手动双击 QQ-Notice-Hub.exe 启动。' -f $webPort)",
        "}",
        "# 脚本结束后无论更新或启动结果如何，都恢复看门狗；失败路径已尝试拉起旧版。",
        "try { Remove-Item -LiteralPath $flag -Force -ErrorAction SilentlyContinue } catch { }",
        "",
        "# 装完即清待装记录：不清的话托盘一直显示「重启并更新」、页面一直提示「更新包已就绪」，",
        "# 用户再点一次就再装一遍，还会再多开一个标签。只有真的覆盖成功才清——失败要留着让用户重试。",
        "if ($expanded) {",
        "    try { Remove-Item -LiteralPath (Join-Path (Split-Path -Parent $zip) 'pending.json') -Force -ErrorAction SilentlyContinue } catch { }",
        "}",
        "",
        "if ($expanded) { Write-UpdateLog '更新完成' } elseif (-not $hashOk) { Write-UpdateLog '更新未开始：更新包哈希不符，未覆盖任何文件' } elseif ($blocked.Count -gt 0) { Write-UpdateLog '更新未开始：有文件被占用，未覆盖任何文件' } elseif (-not $probeOk) { Write-UpdateLog '更新未开始：占用探测失败，未覆盖任何文件' } elseif (-not $backupOk) { Write-UpdateLog '备份失败，本次未安装：没有动任何已装文件，请检查数据目录权限后重试' } else { Write-UpdateLog '更新未完成，文件可能已部分覆盖，请重装或手动覆盖安装（若是装在 Program Files，可能需要用管理员权限再点一次更新）' }",
        "Write-UpdateLog '完成'",
        "",
    ]
    return "\n".join(lines)


def apply_async(
    install_root: Path,
    zip_path: Path,
    pid: int,
    *,
    data_dir: Path,
    web_port: int = 8766,
    expected_sha256: str = "",
) -> dict[str, Any]:
    """Windows 写覆盖安装脚本；POSIX 返回手动下载安装指引，不生成脚本。

    Windows 先落 ``<data_dir>/updates/applying.flag``（看门狗据此停止拉起服务），
    再启动脚本。脚本失败/启动失败都会把标记清掉，避免服务永远起不来。
    ``web_port`` 只用于脚本里的启动后健康检查（探 /health）。
    """
    outcome: dict[str, Any] = {"ok": False, "script": "", "error": ""}
    root = Path(install_root)
    archive = Path(zip_path)
    updates_dir = Path(data_dir) / "updates"
    flag = updates_dir / APPLYING_FLAG

    try:
        if not archive.is_file():
            outcome["error"] = "更新包不存在：%s" % archive
            return outcome
    except OSError as error:
        outcome["error"] = "读取更新包失败：%s" % error
        return outcome

    if not _WIN:
        outcome["error"] = (
            "这个平台不能自动装更新，请手动下载安装。\n"
            "更新说明：%s\n更新包：%s" % (_latest_release_page(Path(data_dir)), archive)
        )
        return outcome

    expected = str(expected_sha256 or "").strip().lower()
    if not expected:
        # 没有可信哈希就不装：脚本里的复算没有基准等于没校验。
        outcome["error"] = "没有可信的更新包哈希，拒绝自动安装；请重新下载或手动安装"
        return outcome

    try:
        updates_dir.mkdir(parents=True, exist_ok=True)
        flag.write_text(
            "pid=%s\nat=%s\n" % (pid, time.strftime("%Y-%m-%d %H:%M:%S")),
            encoding="utf-8",
        )
    except OSError as error:
        outcome["error"] = "写更新标记失败（请检查安装目录权限）：%s" % error
        return outcome

    try:
        script_path = Path(tempfile.gettempdir()) / _script_name()
        # 新补丁号从 pending.json 里取：启动器调用 apply_async 时不传 patch（它也不知道），
        # 取不到才退回当前补丁号。这个值只写进日志，不参与备份命名。
        record = _read_pending_record(Path(data_dir)) or {}
        # _read_pending_record 会把缺失的 patch 归一成 0，所以不能靠 dict.get 的默认值兜底：
        # 0 也当作「取不到」，退回当前补丁号，日志才不会写着「本次装补丁 0」。
        patch = _coerce_patch(record.get("patch")) or current_patch()
        # 备份里装的是「装机前」的文件，文件名必须用装机前的补丁号，回退才能
        # 正确报出 from_patch（新）-> to_patch（旧）。启动器一行都不用改。
        backup_patch = current_patch()
        # utf-8-sig：Windows PowerShell 5.1 只认 BOM，否则脚本里的中文会按 GBK 解码成乱码。
        script_path.write_text(
            _script_text(root, archive, int(pid), Path(data_dir), web_port, expected, patch, backup_patch),
            encoding="utf-8-sig",
        )
        outcome["script"] = str(script_path)
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-WindowStyle",
                "Hidden",
                "-File",
                str(script_path),
            ],
            creationflags=CREATE_NO_WINDOW,
        )
        outcome["ok"] = True
        return outcome
    except Exception as error:  # noqa: BLE001 - 契约要求绝不抛异常
        _remove_quietly(flag)
        outcome["error"] = "启动更新脚本失败：%s" % error
        return outcome


# ---------------------------------------------------------------- 修复包回退
#
# 同版本修复包是「同一个版本号、不同补丁号」。装坏了以后版本号没变，
# check() 不会提示任何更新，用户只能靠手动覆装。所以装机前留一份回退包
# （``<data_dir>/updates/rollback/prev-<patch>.zip``），装坏了就退回去。

PATCH_SKIP = "patch-skip.json"
ROLLBACK_DIRNAME = "rollback"
BACKUP_PREFIX = "prev-"


def _patch_skip_path(data_dir: Path) -> Path:
    return Path(data_dir) / "updates" / PATCH_SKIP


def _backup_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "updates" / ROLLBACK_DIRNAME


def _latest_backup(data_dir: Path) -> Path | None:
    """最近一份回退包；没有就返回 None。只认 ``prev-*.zip``，staging 之类不算。"""
    directory = _backup_dir(data_dir)
    try:
        candidates = [p for p in directory.glob(BACKUP_PREFIX + "*.zip") if p.is_file()]
    except OSError:
        return None
    if not candidates:
        return None
    try:
        return max(candidates, key=lambda p: p.stat().st_mtime)
    except OSError:
        return candidates[0]


def _patch_from_backup(path: Path) -> int:
    """从 ``prev-<patch>.zip`` 的文件名里取补丁号，取不到按 0。"""
    name = path.name
    if not (name.startswith(BACKUP_PREFIX) and name.endswith(".zip")):
        return 0
    middle = name[len(BACKUP_PREFIX) : -len(".zip")]
    try:
        return _coerce_patch(int(middle))
    except (TypeError, ValueError):
        return 0


def skip_patch(data_dir: Path, patch: int) -> dict[str, Any]:
    """记下「这个补丁号别自动装」。

    回退成功之后由调用方写：否则自动下载线程下一轮又把装坏的那个包拉回来，
    用户会陷在「装坏 → 回退 → 又装坏」的循环里。
    """
    outcome: dict[str, Any] = {"ok": False, "error": ""}
    target = _patch_skip_path(data_dir)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "patch": _coerce_patch(patch),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        temporary.replace(target)
        outcome["ok"] = True
        return outcome
    except (OSError, ValueError, TypeError) as error:
        outcome["error"] = "记录跳过的修复包失败：%s" % error
        return outcome


def patch_skip(data_dir: Path) -> dict[str, Any] | None:
    """读回「被跳过的补丁号」；没有或读不懂都返回 None。"""
    target = _patch_skip_path(data_dir)
    try:
        record = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(record, dict):
        return None
    patch = record.get("patch")
    if isinstance(patch, bool) or not isinstance(patch, int) or patch < 0:
        return None
    at = record.get("at")
    if not isinstance(at, str):
        at = ""
    return {"patch": patch, "at": at}


def clear_patch_skip(data_dir: Path) -> dict[str, Any]:
    """清掉跳过记录（比如用户手动又装回了那个修复包）。"""
    outcome: dict[str, Any] = {"ok": False, "error": ""}
    try:
        _patch_skip_path(data_dir).unlink()
        outcome["ok"] = True
    except FileNotFoundError:
        outcome["ok"] = True
    except OSError as error:
        outcome["error"] = "清除跳过的修复包失败：%s" % error
    return outcome


def _rollback_script_name() -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    directory = Path(tempfile.gettempdir())
    candidate = "notice-hub-rollback-%s.ps1" % stamp
    counter = 1
    while (directory / candidate).exists():
        candidate = "notice-hub-rollback-%s-%d.ps1" % (stamp, counter)
        counter += 1
    return candidate


def _rollback_script_text(
    install_root: Path,
    backup_zip: Path,
    pid: int,
    data_dir: Path,
    web_port: int = 8766,
    from_patch: int = 0,
    to_patch: int = 0,
) -> str:
    """生成回退脚本正文（纯函数，方便测试断言）。套路与覆盖安装脚本一致。"""
    root = str(install_root)
    launcher = str(Path(install_root) / LAUNCHER_EXE)
    log = str(Path(data_dir) / "logs" / UPDATE_LOG_NAME)
    flag = str(Path(data_dir) / "updates" / APPLYING_FLAG)
    pending = str(Path(data_dir) / "updates" / PENDING_UPDATE)

    lines = [
        "# notice-hub 修复包回退脚本（由 noticehub/updater.py 生成，别手改）。",
        "# 同版本修复包装坏了，版本号没变，只能靠装机前留的快照退回去。",
        "$ErrorActionPreference = 'Continue'",
        "$root = " + _ps_quote(root),
        "$backup = " + _ps_quote(backup_zip),
        "$pidToWait = " + _ps_quote(pid),
        "$log = " + _ps_quote(log),
        "$flag = " + _ps_quote(flag),
        "$launcher = " + _ps_quote(launcher),
        "$data = " + _ps_quote(data_dir),
        "$pending = " + _ps_quote(pending),
        "$webPort = " + _ps_quote(int(web_port)),
        "$fromPatch = " + _ps_quote(int(from_patch)),
        "$toPatch = " + _ps_quote(int(to_patch)),
        "",
        "New-Item -ItemType Directory -Force -Path (Split-Path -Parent $log) | Out-Null",
        "function Write-UpdateLog([string]$message) {",
        "    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $message",
        "    try { Add-Content -LiteralPath $log -Value $line -Encoding UTF8 } catch { }",
        "}",
        "",
        "Write-UpdateLog ('开始回退修复包：{0} -> {1}' -f $fromPatch, $toPatch)",
        "Write-UpdateLog '请求启动器执行优雅退出（--quit）'",
        "try {",
        "    $quit = Start-Process -FilePath $launcher -ArgumentList '--quit' -WorkingDirectory $root -Wait -PassThru -ErrorAction Stop",
        "    Write-UpdateLog ('--quit 返回 {0}' -f $quit.ExitCode)",
        "} catch {",
        "    Write-UpdateLog ('--quit 请求失败，继续等待后走进程兜底：{0}' -f $_)",
        "}",
        "",
        "Write-UpdateLog '等待旧进程退出（最多 60 秒）'",
        "$deadline = (Get-Date).AddSeconds(60)",
        "while ((Get-Date) -lt $deadline) {",
        "    if (-not (Get-Process -Id $pidToWait -ErrorAction SilentlyContinue)) { break }",
        "    Start-Sleep -Milliseconds 500",
        "}",
        "Start-Sleep -Seconds 2",
        "",
        "# 进程兜底：只认「属于本安装」的进程 —— 可执行路径在安装目录下，",
        "# 或镜像名是 QQ/NapCat 且可执行路径/命令行带本安装数据目录。绝不按进程名整机架杀。",
        "$prefix = $root.TrimEnd('\\') + '\\'",
        "$dataPrefix = $data.TrimEnd('\\') + '\\'",
        "$names = @('qq.exe', 'napcatwinbootmain.exe', 'napcat.exe')",
        "Write-UpdateLog ('结束本安装相关进程（目录 {0}*，名称 {1}）' -f $prefix, ($names -join '/'))",
        "try {",
        "    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |",
        "        Where-Object {",
        "            if (-not $_.ExecutablePath) { return $false }",
        "            if ($_.ExecutablePath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) { return $true }",
        "            if ($names -notcontains $_.Name.ToLowerInvariant()) { return $false }",
        "            if ($_.ExecutablePath.StartsWith($dataPrefix, [System.StringComparison]::OrdinalIgnoreCase)) { return $true }",
        "            if ($_.CommandLine -and $_.CommandLine.IndexOf($dataPrefix, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) { return $true }",
        "            return $false",
        "        } |",
        "        ForEach-Object {",
        "            Write-UpdateLog ('  结束 {0} (pid {1})' -f $_.ExecutablePath, $_.ProcessId)",
        "            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue",
        "        }",
        "} catch {",
        "    Write-UpdateLog ('结束进程时出错（继续解压）：{0}' -f $_)",
        "}",
        "Start-Sleep -Seconds 1",
        "",
        "try {",
        "    Get-ChildItem -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue |",
        "        Where-Object { $_.Attributes -band [System.IO.FileAttributes]::ReadOnly } |",
        "        ForEach-Object { $_.Attributes = $_.Attributes -bxor [System.IO.FileAttributes]::ReadOnly }",
        "} catch {",
        "    Write-UpdateLog ('清理只读属性时出错（继续）：{0}' -f $_)",
        "}",
        "",
        "$restored = $false",
        "if (-not (Test-Path -LiteralPath $backup -PathType Leaf)) {",
        "    Write-UpdateLog ('回退包不见了，未做任何改动：{0}' -f $backup)",
        "} else {",
        "    try {",
        "        Write-UpdateLog ('回退覆盖：{0} -> {1}' -f $backup, $root)",
        "        Expand-Archive -LiteralPath $backup -DestinationPath $root -Force -ErrorAction Stop",
        "        $restored = $true",
        "        Write-UpdateLog '回退覆盖完成'",
        "    } catch {",
        "        Write-UpdateLog ('回退失败：{0}。若装在 Program Files，可能需要用管理员权限再点一次回退' -f $_)",
        "    }",
        "}",
        "",
        "Write-UpdateLog '无论回退成败，都拉起启动器'",
        "$started = $false",
        "try {",
        "    Start-Process -FilePath $launcher -ArgumentList '--no-browser' -WorkingDirectory $root",
        "    $started = $true",
        "    Write-UpdateLog ('已拉起 {0}' -f $launcher)",
        "} catch {",
        "    Write-UpdateLog ('拉起启动器失败：{0}' -f $_)",
        "}",
        "",
        "# 起来才算退成功：进程创建不等于服务活着，探 3 次 /health 再收工。",
        "$healthy = $false",
        "if ($started) {",
        "    for ($i = 1; $i -le 3; $i++) {",
        "        try {",
        "            $req = [System.Net.HttpWebRequest]::Create(('http://127.0.0.1:{0}/health' -f $webPort))",
        "            $req.Proxy = $null",
        "            $req.Timeout = 3000",
        "            $req.ReadWriteTimeout = 3000",
        "            $resp = $req.GetResponse()",
        "            $code = [int]$resp.StatusCode",
        "            $resp.Close()",
        "            if ($code -eq 200) { $healthy = $true; break }",
        "        } catch { }",
        "        if ($i -lt 3) { Start-Sleep -Seconds 2 }",
        "    }",
        "}",
        "if ($healthy) {",
        "    Write-UpdateLog ('回退后健康检查通过：http://127.0.0.1:{0}/health' -f $webPort)",
        "} elseif ($started) {",
        "    Write-UpdateLog ('警告：3 次健康检查均失败，http://127.0.0.1:{0}/health 无响应。请手动双击 QQ-Notice-Hub.exe 启动。' -f $webPort)",
        "}",
        "",
        "try { Remove-Item -LiteralPath $flag -Force -ErrorAction SilentlyContinue } catch { }",
        "# 只有真的退回去、而且服务活着，才清本次待装标记；否则留着让用户还能再试一次。",
        "if ($restored -and $healthy) {",
        "    try { Remove-Item -LiteralPath $pending -Force -ErrorAction SilentlyContinue } catch { }",
        "    # 已经成功退回这一版，快照别留着：留着会重复回退到同一份，也分不清当前状态。",
        "    # 只有服务真的活着才删；回退失败时备份必须保留，用户还能再试一次。",
        "    try { Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue } catch { }",
        "    Write-UpdateLog ('回退完成，已删除已回退的备份：{0}' -f $backup)",
        "} elseif ($restored) {",
        "    Write-UpdateLog '文件已回退，但服务没起来，请手动启动后确认'",
        "} else {",
        "    Write-UpdateLog '回退未完成，未改动任何已装文件'",
        "}",
        "Write-UpdateLog '完成'",
        "",
    ]
    return "\n".join(lines)


def rollback_async(
    data_dir: Path,
    *,
    install_dir: Path | None = None,
    web_port: int = 8766,
    pid: int | None = None,
) -> dict[str, Any]:
    """把最近一次装机前的快照解回安装目录；没有快照就什么都不写。

    返回 ``from_patch``（被退掉的补丁号）与 ``to_patch``（快照里的补丁号）：
    调用方靠 ``from_patch`` 写 ``skip_patch``，靠 ``to_patch`` 回给前端。
    """
    outcome: dict[str, Any] = {"ok": False, "script": "", "error": ""}
    backup = _latest_backup(Path(data_dir))
    if backup is None:
        outcome["error"] = "没有可回退的备份"
        return outcome

    to_patch = _patch_from_backup(backup)
    from_patch = current_patch()

    if not _WIN:
        outcome["error"] = (
            "这个平台不能自动回退，请手动覆盖安装。\n回退包：%s" % backup
        )
        return outcome

    # 默认安装根＝本模块所在目录的上一级（noticehub/ 就在装机根下）。
    root = Path(install_dir) if install_dir is not None else Path(__file__).resolve().parent.parent
    if pid is None:
        pid = os.getpid()

    updates_dir = Path(data_dir) / "updates"
    flag = updates_dir / APPLYING_FLAG
    try:
        updates_dir.mkdir(parents=True, exist_ok=True)
        flag.write_text(
            "pid=%s\nat=%s\n" % (pid, time.strftime("%Y-%m-%d %H:%M:%S")),
            encoding="utf-8",
        )
    except OSError as error:
        outcome["error"] = "写回退标记失败（请检查安装目录权限）：%s" % error
        return outcome

    try:
        script_path = Path(tempfile.gettempdir()) / _rollback_script_name()
        script_path.write_text(
            _rollback_script_text(root, backup, int(pid), Path(data_dir), web_port, from_patch, to_patch),
            encoding="utf-8-sig",
        )
        outcome["script"] = str(script_path)
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-WindowStyle",
                "Hidden",
                "-File",
                str(script_path),
            ],
            creationflags=CREATE_NO_WINDOW,
        )
        outcome["ok"] = True
        outcome["from_patch"] = from_patch
        outcome["to_patch"] = to_patch
        return outcome
    except Exception as error:  # noqa: BLE001 - 契约要求绝不抛异常
        _remove_quietly(flag)
        outcome["error"] = "启动回退脚本失败：%s" % error
        return outcome
