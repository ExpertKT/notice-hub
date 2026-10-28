"""版本更新与公告（原 webapp.py 顶层 updates/announce 域，B 方案按域拆分第一刀）。

为什么单独一个模块：webapp.py 原先 4400 行，更新/公告这条链路自包含（只碰
``<data_dir>/updates/state.json`` 与 updater/announce 两个模块）。**迁移口径**：
函数体逐字未改，只把「测试会 patch 的 webapp 模块级名字」（``_current_version``、
``_refresh_update``、``_webapp().updater``、``RUNTIME_ROOT``）改成运行时从 webapp 模块取，
所以 ``patch("qq_live_digest.webapp._current_version")`` 这类补丁点继续有效。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import announce

LOGGER = logging.getLogger("qq_live_digest.webapp")


def _webapp():
    """webapp 模块对象；只在调用时取（webapp 末尾才 import 本模块，导入期循环安全）。"""
    return sys.modules[__package__ + ".webapp"]


# 对外公告：改动要尽快到达用户，15 分钟是「够快」与 GitHub raw 限速的折中。
ANNOUNCEMENT_REFRESH_SECONDS = 15 * 60
# 失败的公告结果只压 10 分钟：够挡住刷屏式重试，又不会让「公告源刚修好」或「刚联网」的用户
# 对着一条 6 小时的旧错误发呆（升级还会把 data\updates\state.json 里那条旧错误一起带过来）。
ANNOUNCEMENT_ERROR_RETRY_SECONDS = 10 * 60

def _updates_state_path(server: ThreadingHTTPServer) -> Path:
    """`<data_dir>\\updates\\state.json`：公告与更新检查共用这一份（启动器也在写它）。"""
    return Path(server.settings.data_dir) / "updates" / "state.json"  # type: ignore[attr-defined]


def _read_updates_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_updates_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Use unique temp file to avoid cross-thread/cross-process collision
    temp_name = f"{path.name}.{os.getpid()}-{threading.get_ident()}.tmp"
    temporary = path.with_name(temp_name)
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    # Retry replace() on Windows PermissionError - file may be temporarily locked by another thread
    delays = (0.01, 0.02, 0.04, 0.08, 0.16)
    for attempt in range(len(delays) + 1):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == len(delays):
                LOGGER.warning("更新状态写入失败（重试 %d 次后仍被占用）：%s", attempt, path)
                raise
            time.sleep(delays[attempt])


def _normalize_announcement_response(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"ok": False, "items": [], "from_cache": False, "checked_at": "", "error": "公告暂时不可用", "current_version": _webapp()._current_version()}
    items = value.get("items")
    return {
        "ok": bool(value.get("ok")),
        "items": [dict(item) for item in items if isinstance(item, dict)] if isinstance(items, list) else [],
        "from_cache": bool(value.get("from_cache")),
        "checked_at": str(value.get("checked_at") or ""),
        "error": "" if value.get("ok") else str(value.get("error") or "公告暂时不可用"),
        # 弹窗要显示「当前版本 vX」；拿不到就「未知」，不能让旧安装报 500。
        "current_version": str(value.get("current_version") or "") or _webapp()._current_version(),
    }


def _announcement_is_fresh(checked_at: str, ttl_seconds: float = ANNOUNCEMENT_REFRESH_SECONDS) -> bool:
    try:
        checked = dt.datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=dt.timezone.utc)
        age = (dt.datetime.now(dt.timezone.utc) - checked).total_seconds()
        return -300 <= age < ttl_seconds
    except (TypeError, ValueError):
        return False


def _refresh_announcements(server: ThreadingHTTPServer) -> None:
    updates = Path(server.settings.data_dir) / "updates"  # type: ignore[attr-defined]
    state_path = _updates_state_path(server)
    try:
        channel = _webapp().updater.resolve_channel(server.settings.update_channel) if _webapp().updater is not None else None  # type: ignore[attr-defined]
        response = _normalize_announcement_response(announce.active(cache_dir=updates, channel=channel))
    except Exception:  # noqa: BLE001 - announcement failures must not break the web app
        response = {"ok": False, "items": [], "from_cache": False, "checked_at": "", "error": "公告暂时不可用"}
    if not response["checked_at"]:
        response["checked_at"] = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        with server.state_lock:  # type: ignore[attr-defined]
            state = _read_updates_state(state_path)
            state["announce_checked_at"] = response["checked_at"]
            state["announce_response"] = response
            _write_updates_state(state_path, state)
    except OSError:
        LOGGER.warning("公告状态缓存写入失败", exc_info=True)
    finally:
        with server.announce_lock:  # type: ignore[attr-defined]
            server.announce_running = False  # type: ignore[attr-defined]


def _announcement_cache_is_stale(server: ThreadingHTTPServer) -> bool:
    """公告缓存是否该重取（含「版本变了」的失效判断），与 `_announcement_response` 同一套 TTL。"""
    with server.state_lock:  # type: ignore[attr-defined]
        state = _read_updates_state(_updates_state_path(server))
        raw = state.get("announce_response")
        cached = _normalize_announcement_response(raw) if raw is not None else None
        checked_at = str(state.get("announce_checked_at") or "")
    if cached is None:
        return True
    cached_version = str(cached.get("current_version") or "").strip()
    running_version = _webapp()._current_version().strip()
    if (
        cached_version
        and running_version
        and cached_version != "未知"
        and running_version != "未知"
        and cached_version != running_version
    ):
        return True
    ttl = ANNOUNCEMENT_REFRESH_SECONDS if (cached["ok"] and not cached["from_cache"]) else ANNOUNCEMENT_ERROR_RETRY_SECONDS
    return not _announcement_is_fresh(checked_at, ttl)


def _start_announcement_refresh(server: ThreadingHTTPServer) -> bool:
    """起一个后台公告刷新线程；已经有一个在跑就复用。返回是否新起了线程。"""
    with server.announce_lock:  # type: ignore[attr-defined]
        if server.announce_running:  # type: ignore[attr-defined]
            return False
        server.announce_running = True  # type: ignore[attr-defined]
        worker = threading.Thread(target=_refresh_announcements, args=(server,), name="announcement-refresh", daemon=True)
        try:
            worker.start()
        except Exception:  # noqa: BLE001
            server.announce_running = False  # type: ignore[attr-defined]
            LOGGER.warning("公告后台检查无法启动", exc_info=True)
            return False
        return True


def _announcement_response(server: ThreadingHTTPServer, *, force: bool = False) -> dict[str, Any]:
    state_path = _updates_state_path(server)
    started_refresh = False
    if force:
        # `?refresh=1`：这次要真结果（8 秒超时），别又给一份缓存。但已有刷新在跑时
        # 不再叠一次 GitHub 请求（连点铃铛、或刚被「检查更新」触发过），直接给缓存标
        # checking 返回，前端轮询会拿到那次刷新的结果。
        with server.announce_lock:  # type: ignore[attr-defined]
            busy = bool(server.announce_running)  # type: ignore[attr-defined]
            if not busy:
                server.announce_running = True  # type: ignore[attr-defined]
        if busy:
            with server.state_lock:  # type: ignore[attr-defined]
                cached = _normalize_announcement_response(_read_updates_state(state_path).get("announce_response") or {})
            cached["from_cache"] = True
            cached["checking"] = True
            return cached
        _refresh_announcements(server)  # 同步等结果；它自己的 finally 会清 announce_running
        with server.state_lock:  # type: ignore[attr-defined]
            fresh = _normalize_announcement_response(_read_updates_state(state_path).get("announce_response") or {})
        fresh["from_cache"] = False
        fresh["checking"] = False
        return fresh
    with server.state_lock:  # type: ignore[attr-defined]
        state = _read_updates_state(state_path)
        response = state.get("announce_response")
        cached = _normalize_announcement_response(response) if response is not None else None
        if cached is not None:
            cached_version = str(cached.get("current_version") or "").strip()
            running_version = _webapp()._current_version().strip()
            if (
                cached_version
                and running_version
                and cached_version != "未知"
                and running_version != "未知"
                and cached_version != running_version
            ):
                cached = None
        checked_at = str(state.get("announce_checked_at") or "")
        # 缓存里的**失败**结果只算 10 分钟新鲜：公告源刚修好、或用户刚联网时，不该等满 6 小时
        # 才看到第一条公告；同时 10 分钟也足够挡住页面对失败源的重试风暴。
        ttl = ANNOUNCEMENT_REFRESH_SECONDS if (cached is None or (cached["ok"] and not cached["from_cache"])) else ANNOUNCEMENT_ERROR_RETRY_SECONDS
        if cached is not None and _announcement_is_fresh(checked_at, ttl):
            cached["from_cache"] = True
            cached["checking"] = False
            return cached
    with server.announce_lock:  # type: ignore[attr-defined]
        if not server.announce_running:  # type: ignore[attr-defined]
            server.announce_running = True  # type: ignore[attr-defined]
            started_refresh = True
            worker = threading.Thread(target=_refresh_announcements, args=(server,), name="announcement-refresh", daemon=True)
            try:
                worker.start()
            except Exception:  # noqa: BLE001
                server.announce_running = False  # type: ignore[attr-defined]
                started_refresh = False
                LOGGER.warning("公告后台检查无法启动", exc_info=True)
        checking = started_refresh or bool(server.announce_running)  # type: ignore[attr-defined]
    if cached is not None:
        cached["from_cache"] = True
        cached["checking"] = checking
        return cached
    return {"ok": False, "items": [], "from_cache": True, "checked_at": checked_at, "error": "公告正在检查", "checking": checking, "current_version": _webapp()._current_version()}


# ---- 版本更新（网页「设置 → 版本更新」）----
# 查/下载/覆盖安装的实现在 qq_live_digest/updater.py（启动器也用它），这里只做缓存、
# 进度状态与「退出自己去重启」这三件网页才有的事。

UPDATE_CHECK_SECONDS = 6 * 60 * 60
_APPLYING_FLAG_NAME = "applying.flag"

_FEATURE_HEADS = {"新增功能", "新增", "new", "features", "feature"}
_FIX_HEADS = {"修复问题", "修复", "fixed", "fixes", "fix", "bugfix", "bugfixes"}


def _install_root() -> Path:
    """onedir zip 在安装根目录平铺两个 exe，更新应覆盖当前 executable 的目录。"""
    return Path(_webapp().RUNTIME_ROOT)


def _current_version() -> str:
    """旧版安装没有 version 常量：这里兜成「未知」，绝不让页面 500。"""
    try:
        if _webapp().updater is None:
            return "未知"
        return str(_webapp().updater.current_version() or "") or "未知"
    except Exception:  # noqa: BLE001 - 兜底路径，不能因为它把设置页弄挂
        return "未知"






def _update_channel_name(settings: Any = None) -> str:
    try:
        configured = getattr(settings, "update_channel", None) if settings is not None else None
        return str(_webapp().updater.resolve_channel(configured) or "") if _webapp().updater is not None else ""
    except Exception:  # noqa: BLE001
        return ""






def _notes_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def split_release_notes(notes: str) -> dict[str, list[str]]:
    """把 release 正文按 `## 新增功能` / `## 修复问题` 分节。

    标题匹配有意放宽（`## 新增`、`## Fixes`、大小写不限）；没分节就全归到 other，
    老 release 没有这个结构也不能报错。条目里的 `- `/`* ` 前缀在这里去掉。
    """
    sections: dict[str, list[str]] = {"features": [], "fixes": [], "other": []}
    bucket = "other"
    for raw in str(notes or "").splitlines():
        line = raw.strip()
        if line.startswith("#"):
            head = line.lstrip("#").strip().lower()
            if head in _FEATURE_HEADS:
                bucket = "features"
            elif head in _FIX_HEADS:
                bucket = "fixes"
            else:
                bucket = "other"  # 其它小标题（如「已知问题」）也算更新内容
            continue
        if not line:
            continue
        if line[:2] in {"- ", "* "}:
            line = line[2:].strip()
        sections[bucket].append(line)
    return sections


def _release_notes_inline(line: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", _notes_escape(line))


def _release_notes_html(notes: str) -> str:
    """自己渲染：**先转义再套标记**，正文里写 `<script>` 也只会被当文本显示。"""
    sections = split_release_notes(notes)
    blocks: list[str] = []
    for key, title in (("features", "新增功能"), ("fixes", "修复问题"), ("other", "更新内容")):
        lines = sections[key]
        if not lines:
            continue
        items = "".join("<li>%s</li>" % _release_notes_inline(line) for line in lines)
        blocks.append('<div class="update-notes-block"><h4>%s</h4><ul>%s</ul></div>' % (title, items))
    return "".join(blocks)


def _normalize_update(value: Any) -> dict[str, Any]:
    """裁剪成页面要用的形状：GitHub 的返回是不可信输入，缺字段一律降级。"""
    data = value if isinstance(value, dict) else {}
    latest = data.get("latest") if isinstance(data.get("latest"), dict) else {}
    current = data.get("current") if isinstance(data.get("current"), dict) else {}
    try:
        size = int(latest.get("size"))
    except (TypeError, ValueError):
        size = 0
    notes = str(latest.get("notes") or "")
    fallback_urls = latest.get("fallbacks")
    if not isinstance(fallback_urls, list):
        fallback_urls = []
    version = str(current.get("version") or "") or _webapp()._current_version()
    # notice 是「不是失败」的说明（如无需更新、规则不同），error 只留给真失败；
    # 页面把两者分开渲染，避免把「无需更新」显示成「检查更新失败」。
    notice = str(data.get("notice") or "")
    result: dict[str, Any] = {
        "ok": bool(data.get("ok")),
        "checked_at": str(data.get("checked_at") or ""),
        "channel": str(data.get("channel") or ""),
        "current": {"version": version, "tag": str(current.get("tag") or "")},
        "latest": {
            "tag": str(latest.get("tag") or ""),
            "version": str(latest.get("version") or ""),
            "name": str(latest.get("name") or ""),
            "notes": notes,
            "notes_html": _release_notes_html(notes),
            "published_at": str(latest.get("published_at") or ""),
            "asset_name": str(latest.get("asset_name") or ""),
            "size": size,
            "sha256": str(latest.get("sha256") or ""),
            "download_url": str(latest.get("download_url") or ""),
            "html_url": str(latest.get("html_url") or ""),
            "fallbacks": [url for url in fallback_urls if isinstance(url, str)][:5],
        },
        "has_update": bool(data.get("has_update")),
        "source": str(data.get("source") or ""),
        "error": "" if data.get("ok") else str(data.get("error") or "检查更新失败"),
        "notice": notice,
    }
    if result["has_update"] and result["latest"]["tag"] and version.strip().lower() != "未知":
        latest_version = result["latest"]["version"]
        current_kind = _version_kind(version)
        latest_kind = _version_kind(latest_version)
        if current_kind and latest_kind and current_kind != latest_kind:
            result["has_update"] = False
            online_label = result["latest"]["tag"] or latest_version
            result["notice"] = (
                f"线上 Release 与本机版本号规则不同（本机 {version} / 线上 {online_label}），"
                "不自动判断，请到 Release 页面手工确认"
            )
        elif current_kind and current_kind == latest_kind and _is_downgrade(version, latest_version):
            # _webapp().updater 只做「tag 不等于当前版本」的字符串比较，如果线上最新的 release 比手上这份还旧
            # （本地版还没发布），它会算成「有更新」——那会诱导用户把新版换回旧版。这里压掉。
            result["has_update"] = False
            result["notice"] = result["notice"] or "线上 Release 比本机版本旧（这一版还没发布），无需更新"
    return result


def _version_kind(value: str) -> str:
    """识别可比较的日期式或受限 SemVer 版本；日期式优先处理相同外观的歧义。"""
    value = value.strip()
    date = re.fullmatch(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", value)
    if date and 1 <= int(date.group(2)) <= 12 and 1 <= int(date.group(3)) <= 31:
        # YYYY.MM.DD 与 major=YYYY 的 SemVer 外观相同；本项目按日期式优先解释。
        return "date"
    if re.fullmatch(r"\d+\.\d+\.\d+(?:-(?:beta|rc)\.\d+)?", value):
        return "semver"
    return ""


def _is_downgrade(current: str, latest: str) -> bool:
    """本机版本比线上最新 release 还新？用公告模块里那份版本排序（含 prerelease 规则），不重复造。"""
    try:
        current_key = announce._ver_tuple(current)
        latest_key = announce._ver_tuple(latest)
    except Exception:  # noqa: BLE001
        return False
    if not current_key or not latest_key:
        return False
    return current_key > latest_key


def _failed_update(error: str) -> dict[str, Any]:
    return {
        "ok": False,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "channel": _update_channel_name(),
        "current": {"version": _webapp()._current_version(), "tag": ""},
        "latest": {},
        "has_update": False,
        "error": error,
        "notice": "",
    }


def _persist_update(server: ThreadingHTTPServer, info: Any) -> dict[str, Any]:
    normalized = _normalize_update(info)
    state_path = _updates_state_path(server)
    try:
        with server.state_lock:  # type: ignore[attr-defined]
            state = _read_updates_state(state_path)
            state["update_checked_epoch"] = time.time()
            state["update_result"] = normalized
            _write_updates_state(state_path, state)
    except OSError:
        LOGGER.warning("更新状态缓存写入失败", exc_info=True)
    return normalized


def _refresh_update(server: ThreadingHTTPServer) -> None:
    """后台真查一次（不阻塞页面）。"""
    try:
        info = _webapp().updater.check(channel=_update_channel_name(server.settings), timeout=8.0) if _webapp().updater is not None else _failed_update("更新模块不可用")
    except Exception:  # noqa: BLE001 - _webapp().updater 契约是绝不抛，这里再兜一层
        LOGGER.warning("检查更新失败", exc_info=True)
        info = _failed_update("检查更新失败")
    _persist_update(server, info)
    with server.update_lock:  # type: ignore[attr-defined]
        server.update_running = False  # type: ignore[attr-defined]


def _update_download_snapshot(server: ThreadingHTTPServer) -> dict[str, Any]:
    with server.update_lock:  # type: ignore[attr-defined]
        return dict(server.update_download)  # type: ignore[attr-defined]


def _applying_flag_present(server: ThreadingHTTPServer) -> bool:
    try:
        return (_updates_state_path(server).parent / _APPLYING_FLAG_NAME).is_file()
    except OSError:
        return False


def _update_payload(server: ThreadingHTTPServer, cached: dict[str, Any] | None, *, checking: bool) -> dict[str, Any]:
    cached = cached if isinstance(cached, dict) else None
    if cached is None:
        payload: dict[str, Any] = {
            "ok": False,
            "current": {"version": _webapp()._current_version(), "tag": ""},
            "latest": None,
            "has_update": False,
            "channel": _update_channel_name(),
            "checking": bool(checking),
            "checked_at": "",
            "error": "" if checking else "",
            "notice": "" if checking else "还没有检查过更新",
        }
    else:
        payload = dict(cached)
        payload["checking"] = bool(checking)
    payload["download"] = _update_download_snapshot(server)
    payload["applying"] = _applying_flag_present(server)
    pending = None
    pending_update = getattr(_webapp().updater, "pending_update", None)
    if callable(pending_update):
        try:
            record = pending_update(Path(server.settings.data_dir))  # type: ignore[attr-defined]
            if isinstance(record, dict):
                version = record.get("version")
                at = record.get("at")
                if isinstance(version, str) and version.strip() and isinstance(at, str):
                    pending = {"version": version, "at": at}
        except Exception:  # noqa: BLE001
            LOGGER.warning("读取待安装更新失败", exc_info=True)
    payload["pending"] = pending
    return payload


def _update_cache_version_changed(cached: Any) -> bool:
    current = cached.get("current") if isinstance(cached, dict) else None
    cached_version = str(current.get("version") or "").strip() if isinstance(current, dict) else ""
    running_version = _webapp()._current_version().strip()
    if not cached_version or not running_version or cached_version == "未知" or running_version == "未知":
        return False
    return cached_version != running_version


def _update_response(server: ThreadingHTTPServer, *, force: bool = False) -> dict[str, Any]:
    state_path = _updates_state_path(server)
    if force:
        # 用户点了「检查更新」：这次要真结果，等它（8 秒超时）比再给一个缓存好。
        try:
            info = _webapp().updater.check(channel=_update_channel_name(server.settings), timeout=8.0) if _webapp().updater is not None else _failed_update("更新模块不可用")
        except Exception:  # noqa: BLE001
            LOGGER.warning("检查更新失败", exc_info=True)
            info = _failed_update("检查更新失败")
        return _update_payload(server, _persist_update(server, info), checking=False)
    with server.state_lock:  # type: ignore[attr-defined]
        state = _read_updates_state(state_path)
        cached = state.get("update_result") if isinstance(state.get("update_result"), dict) else None
        if _update_cache_version_changed(cached):
            state.pop("update_result", None)
            state.pop("update_checked_epoch", None)
            try:
                _write_updates_state(state_path, state)
            except OSError:
                LOGGER.warning("版本变化后的更新缓存清理失败", exc_info=True)
            cached = None
        try:
            checked_epoch = float(state.get("update_checked_epoch") or 0)
        except (TypeError, ValueError):
            checked_epoch = 0.0
        stale = cached is None or (time.time() - checked_epoch) >= UPDATE_CHECK_SECONDS
    with server.update_lock:  # type: ignore[attr-defined]
        checking = False
        if stale:
            if not server.update_running:  # type: ignore[attr-defined]
                server.update_running = True  # type: ignore[attr-defined]
                worker = threading.Thread(target=_webapp()._refresh_update, args=(server,), name="update-check", daemon=True)
                try:
                    worker.start()
                    checking = True
                except Exception:  # noqa: BLE001
                    server.update_running = False  # type: ignore[attr-defined]
                    LOGGER.warning("更新检查线程无法启动", exc_info=True)
            else:
                checking = True
    return _update_payload(server, cached, checking=checking)


def _start_update_download(server: ThreadingHTTPServer) -> dict[str, Any]:
    if _webapp().updater is None:
        return {"ok": False, "started": False, "error": "更新模块不可用"}
    with server.state_lock:  # type: ignore[attr-defined]
        state = _read_updates_state(_updates_state_path(server))
        cached = state.get("update_result") if isinstance(state.get("update_result"), dict) else {}
        if _update_cache_version_changed(cached):
            return {"ok": False, "started": False, "error": "还没有可下载的新版本，请先点「检查更新」"}
        latest = cached.get("latest") if isinstance(cached.get("latest"), dict) else {}
        if not latest.get("download_url"):
            return {"ok": False, "started": False, "error": "还没有可下载的新版本，请先点「检查更新」"}
        info = dict(latest)
    with server.update_lock:  # type: ignore[attr-defined]
        if server.update_download.get("state") == "downloading":  # type: ignore[attr-defined]
            return {"ok": True, "started": False}
        server.update_download = {  # type: ignore[attr-defined]
            "state": "downloading",
            "done": 0,
            "total": int(latest.get("size") or 0),
            "path": "",
            "error": "",
            "tag": str(latest.get("tag") or ""),
        }
    try:
        worker = threading.Thread(target=_download_update, args=(server, info), name="update-download", daemon=True)
        worker.start()
    except Exception as error:  # noqa: BLE001
        with server.update_lock:  # type: ignore[attr-defined]
            server.update_download.update({"state": "error", "error": "下载线程无法启动：%s" % error})  # type: ignore[attr-defined]
        return {"ok": False, "started": False, "error": "下载线程无法启动：%s" % error}
    return {"ok": True, "started": True}


def _download_update(server: ThreadingHTTPServer, latest: dict[str, Any]) -> None:
    def on_progress(done: int, total: int) -> None:
        with server.update_lock:  # type: ignore[attr-defined]
            if server.update_download.get("state") == "downloading":  # type: ignore[attr-defined]
                server.update_download["done"] = int(done)  # type: ignore[attr-defined]
                if total:
                    server.update_download["total"] = int(total)  # type: ignore[attr-defined]

    try:
        result = _webapp().updater.download(latest, _updates_state_path(server).parent, timeout=60.0, on_progress=on_progress)  # type: ignore[union-attr]
    except Exception as error:  # noqa: BLE001
        result = {"ok": False, "error": str(error)}
    if result.get("ok"):
        try:
            archive = Path(str(result.get("path") or ""))
            version = str(latest.get("version") or latest.get("tag") or "").strip()
            if not version or not archive.is_file():
                raise RuntimeError("下载完成但更新包无效")
            outcome = _webapp().updater.mark_pending(Path(server.settings.data_dir), zip_path=archive, version=version, sha256=str(result.get("sha256") or ""))  # type: ignore[attr-defined]
            # 网页「重启并更新」按钮和托盘文案都以这条记录为准（页面 JS：applyButton.hidden = !showLatest || !!pending），
            # 落盘失败还报成功的话，两个入口会一起静默消失，所以这里当失败处理、让用户重试。
            if isinstance(outcome, dict) and not outcome.get("ok"):
                raise RuntimeError("下载完成但待安装记录写入失败：%s" % (outcome.get("error") or "未知原因"))
        except Exception as error:  # noqa: BLE001
            result = {"ok": False, "error": str(error)}
    with server.update_lock:  # type: ignore[attr-defined]
        if result.get("ok"):
            archive = Path(str(result.get("path") or ""))
            server.update_download.update({"state": "done", "path": str(archive), "error": ""})  # type: ignore[attr-defined]
            if result.get("bytes"):
                server.update_download["done"] = int(result["bytes"])  # type: ignore[attr-defined]
        else:
            server.update_download.update({"state": "error", "path": "", "error": str(result.get("error") or "下载失败")})  # type: ignore[attr-defined]


def _apply_update(server: ThreadingHTTPServer) -> dict[str, Any]:
    if _webapp().updater is None:
        return {"ok": False, "restarting": False, "error": "更新模块不可用"}
    snapshot = _update_download_snapshot(server)
    if snapshot.get("state") != "done" or not snapshot.get("path"):
        return {"ok": False, "restarting": False, "error": "还没下载好更新包，请先点「一键更新」等它下载完"}
    archive = Path(str(snapshot["path"]))
    try:
        exists = archive.is_file()
    except OSError:
        exists = False
    if not exists:
        return {"ok": False, "restarting": False, "error": "更新包不见了（%s），请重新点「一键更新」" % archive.name}
    # 装机脚本要在解压前复算哈希，基准取下载时校验过的那个值（pending 记录）。
    pending = _webapp().updater.pending_update(Path(server.settings.data_dir)) or {}  # type: ignore[attr-defined]
    result = _webapp().updater.apply_async(_install_root(), archive, os.getpid(), data_dir=Path(server.settings.data_dir), web_port=int(server.settings.web_port), expected_sha256=str(pending.get("sha256") or ""))  # type: ignore[attr-defined]
    if not result.get("ok"):
        return {"ok": False, "restarting": False, "error": str(result.get("error") or "启动更新脚本失败")}
    # 脚本几秒内就会杀掉本安装目录的进程；先把响应发出去，再自己退（看门狗已被 applying.flag 压住）。
    threading.Timer(2.0, lambda: os._exit(0)).start()
    return {"ok": True, "restarting": True, "script": str(result.get("script") or "")}
