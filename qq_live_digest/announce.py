"""公告通道（后端）：fetch、parse、active 三个函数，绝不抛异常。"""

from __future__ import annotations

import json
import re
import socket
import tempfile
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from qq_live_digest import update_sources


ANNOUNCEMENTS_URL = update_sources.announcements_url(update_sources.sources()[0])


def _ver_tuple(ver_str: str) -> tuple[int | str, ...]:
    """Parse version string like '1.1.0-beta.1' into comparable tuple.
    
    Pre-release segments sort BEFORE release: '1.0.0-beta' < '1.0.0'.
    Strategy: pre-release gets empty tuple appended; release gets (1,) appended.
    This makes (1, 0, 0) < (1, 0, 0, 1) because tuple comparison is lexicographic.
    
    Examples:
    - '1.1.0' → (1, 1, 0, 1)  # release
    - '1.1.0-beta.1' → (1, 1, 0, 0, 'beta', 1)  # pre-release sorts first
    - '1.1.0-rc.2' → (1, 1, 0, 0, 'rc', 2)  # pre-release sorts first
    """
    if not ver_str or not isinstance(ver_str, str):
        return ()
    
    if "-" not in ver_str:
        # Release version: base numbers + (1,) to sort AFTER pre-release
        try:
            base = tuple(int(x) for x in ver_str.split("."))
            return base + (1,)
        except (ValueError, AttributeError):
            return ()
    
    # Pre-release: split on "-"
    parts = ver_str.split("-", 1)
    try:
        base = tuple(int(x) for x in parts[0].split("."))
    except (ValueError, AttributeError):
        return ()
    
    # Pre-release identifier: (0,) prefix sorts before release's (1,)
    pre_part = parts[1] if len(parts) > 1 else ""
    pre_tokens = [0]  # 0 sorts before 1
    for token in pre_part.split("."):
        try:
            pre_tokens.append(int(token))
        except ValueError:
            pre_tokens.append(token)
    
    return base + tuple(pre_tokens)


def _version_in_range(current: str, min_version: str, max_version: str) -> bool:
    """Check if current version falls within [min_version, max_version].
    
    Empty string means no limit.
    """
    current_tuple = _ver_tuple(current)
    if not current_tuple:
        return True  # Can't parse current, allow
    
    if min_version:
        min_tuple = _ver_tuple(min_version)
        if min_tuple and current_tuple < min_tuple:
            return False
    
    if max_version:
        max_tuple = _ver_tuple(max_version)
        if max_tuple and current_tuple > max_tuple:
            return False
    
    return True


def _release_tag_version(url: Any) -> tuple[int | str, ...] | None:
    """Return the parsed version only for a standard GitHub release-tag URL."""
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or parsed.hostname != "github.com":
        return None
    match = re.fullmatch(
        r"/[^/]+/[^/]+/releases/tag/v(\d+\.\d+\.\d+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)",
        parsed.path,
    )
    if not match:
        return None
    version = _ver_tuple(match.group(1))
    return version or None


def _get_current_version() -> str:
    """Get APP_VERSION from version.py, with ImportError fallback."""
    try:
        from qq_live_digest.version import APP_VERSION
        return APP_VERSION
    except (ImportError, AttributeError):
        return "未知"


def _get_data_dir() -> Path:
    """Get default data_dir from settings, with fallback to tempfile."""
    try:
        from qq_live_digest.config import Settings
        settings = Settings()
        return Path(settings.data_dir) if settings.data_dir else Path(tempfile.gettempdir())
    except (ImportError, AttributeError, TypeError):
        return Path(tempfile.gettempdir())


def fetch(*, timeout: float = 8.0, cache_dir: Path | None = None, settings: Any = None) -> dict:
    """Fetch announcements from GitHub, then the configured mirror.
    
    Returns: {
        "ok": bool,
        "payload": dict or None,
        "from_cache": bool,
        "error": str (human-readable, empty if ok)
    }
    
    - Success: writes only the successful source payload to cache_dir/announcements.json
    - Network failure: reads cache, returns from_cache=True
    - Cache missing: returns ok=False with error message
    - Never raises exceptions
    """
    if cache_dir is None:
        try:
            if settings is None:
                from qq_live_digest.config import Settings
                settings = Settings()
            cache_dir = Path(settings.data_dir) / "updates" if settings.data_dir else Path(tempfile.gettempdir())
        except (ImportError, AttributeError, TypeError):
            cache_dir = Path(tempfile.gettempdir())
    else:
        cache_dir = Path(cache_dir)
    
    cache_path = cache_dir / "announcements.json"
    
    failures: list[str] = []
    not_found = 0
    try:
        source_list = update_sources.sources(settings)
    except Exception as error:  # noqa: BLE001 - 公告接口绝不抛异常
        source_list = []
        failures.append("源配置：%s" % type(error).__name__)

    for source in source_list:
        try:
            url = update_sources.announcements_url(source)
            req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=timeout) as response:
                payload_text = response.read().decode("utf-8")
                payload = json.loads(payload_text)

            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except (OSError, IOError):
                pass
            return {"ok": True, "payload": payload, "from_cache": False, "error": ""}
        except urllib.error.HTTPError as error:
            if error.code == 404:
                not_found += 1
                failures.append("%s：HTTP 404" % source["id"])
                continue
            failures.append("%s：HTTP %s" % (source["id"], error.code))
        except (urllib.error.URLError, OSError, socket.error, TimeoutError) as error:
            reason = str(getattr(error, "reason", "") or error).replace("\n", " ")[:160]
            failures.append("%s：网络错误（%s）" % (source["id"], reason))
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as error:
            failures.append("%s：公告数据格式错误（%s）" % (source["id"], type(error).__name__))
        except Exception as error:  # noqa: BLE001 - 单源异常继续下一个源
            failures.append("%s：获取失败（%s）" % (source.get("id", "unknown"), type(error).__name__))

    if source_list and not failures:
        failures.append("没有可用公告源")
    if source_list and not_found == len(source_list):
        return {"ok": True, "payload": {"version": 1, "items": []}, "from_cache": False, "error": ""}

    try:
        if cache_path.is_file():
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            return {"ok": True, "payload": payload, "from_cache": True, "error": ""}
    except (OSError, IOError, json.JSONDecodeError, ValueError):
        pass

    details = "；".join(failures) or "未配置可用公告源"
    return {"ok": False, "payload": None, "from_cache": False, "error": "无法获取公告：" + details}


def parse(payload: Any, *, current_version: str, channel: str = "beta") -> list[dict]:
    """Parse and filter announcements.
    
    - Filter by version range (min_version, max_version)
    - Filter by channel (empty = all channels)
    - Sort by date descending
    - Add "seen_required" field (True if sticky or level=="critical")
    - Skip malformed items (missing id/title, non-dict, etc.)
    - Return [] on payload not dict or items not list
    """
    if not isinstance(payload, dict):
        return []
    
    items = payload.get("items", [])
    if not isinstance(items, list):
        return []
    
    filtered = []
    for item in items:
        if not isinstance(item, dict):
            continue
        
        # Required fields
        if not item.get("id") or not item.get("title"):
            continue
        
        # Version range check
        min_ver = item.get("min_version", "")
        max_ver = item.get("max_version", "")
        if not _version_in_range(current_version, min_ver, max_ver):
            continue

        release_version = _release_tag_version(item.get("url"))
        current_tuple = _ver_tuple(current_version)
        # 公告数据在远端，人肉填写 max_version 容易漏；GitHub release tag 作为旧公告过滤兜底。
        if release_version is not None and current_tuple and release_version < current_tuple:
            continue
        
        # Channel check (empty = all)
        item_channel = item.get("channel", "")
        if item_channel and item_channel != channel:
            continue
        
        # Add seen_required
        level = item.get("level", "info")
        sticky = item.get("sticky", False)
        seen_required = sticky or level == "critical"

        # 字段白名单：远端 JSON 里的未知字段一律丢弃，前端只消费这些键。
        allowed = ("id", "title", "level", "body", "content", "description",
                   "url", "date", "min_version", "max_version", "channel", "sticky")
        item_copy = {key: item[key] for key in allowed if key in item}
        item_copy["seen_required"] = seen_required
        filtered.append(item_copy)
    
    # Sort by date descending (most recent first)
    filtered.sort(key=lambda x: x.get("date", ""), reverse=True)
    return filtered


def active(
    *,
    cache_dir: Path | None = None,
    current_version: str | None = None,
    channel: str | None = None,
    timeout: float = 8.0,
    settings: Any = None,
) -> dict:
    """Fetch + parse announcements in one call.
    
    Returns: {
        "ok": bool,
        "items": list[dict],
        "from_cache": bool,
        "checked_at": str (ISO format),
        "current_version": str,
        "channel": str,
        "error": str (human-readable, empty if ok)
    }
    """
    if current_version is None:
        current_version = _get_current_version()
    
    if channel is None:
        try:
            if settings is None:
                from qq_live_digest.config import Settings
                settings = Settings()
            channel = getattr(settings, "update_channel", "beta") or "beta"
        except (ImportError, AttributeError, TypeError):
            channel = "beta"
    
    fetch_result = fetch(timeout=timeout, cache_dir=cache_dir, settings=settings)
    
    items = []
    if fetch_result["ok"] and fetch_result["payload"]:
        items = parse(fetch_result["payload"], current_version=current_version, channel=channel)
    
    return {
        "ok": fetch_result["ok"],
        "items": items,
        "from_cache": fetch_result["from_cache"],
        "checked_at": datetime.utcnow().isoformat() + "Z",
        "current_version": current_version,
        "channel": channel,
        "error": fetch_result["error"]
    }
