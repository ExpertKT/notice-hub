"""NapCat discovery and one-click OneBot wiring."""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import sys
import shutil
import tempfile
import zipfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

NAPCAT_DOWNLOAD_URL = "https://github.com/NapNeko/NapCatQQ/releases"
# Pinned v4.18.33 asset; update this URL when selecting a newer official release.
NAPCAT_SHELL_ZIP_URL = "https://github.com/NapNeko/NapCatQQ/releases/download/v4.18.33/NapCat.Shell.Windows.OneKey.zip"
_LAST_INSTALL_ROOT: Path | None = None

@dataclass
class NapcatRoot:
    root: Path
    webui_port: int
    webui_token: str
    uins: list[str]
    live: bool


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _ports(text: str | None = None) -> set[int]:
    if text is None:
        try:
            text = subprocess.check_output(["netstat", "-ano"], text=True, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return set()
    ports: set[int] = set()
    for line in text.splitlines():
        if "LISTENING" not in line.upper():
            continue
        match = re.search(r"(?:\[::\]|[^\s:]+):(\d+)\s+", line)
        if match:
            ports.add(int(match.group(1)))
    return ports


def find_roots() -> list[NapcatRoot]:
    candidates: list[Path] = []
    for raw in (r"F:\NapCat", r"F:\napcat-dl", os.environ.get("APPDATA", ""), os.environ.get("LOCALAPPDATA", ""), "D:\\"):
        if raw:
            candidates.append(Path(raw))
    seen: set[Path] = set()
    roots: list[NapcatRoot] = []
    shallow: list[Path] = []
    for index, base in enumerate(candidates):
        if not base.exists():
            continue
        direct = base / "config" / "webui.json"
        if direct.is_file():
            shallow.append(direct)
        if index < 2:
            try:
                shallow.extend(base.glob("**/NapCat.*/versions/*/resources/app/napcat/config/webui.json"))
            except OSError:
                pass
    files = shallow
    if not files:
        for base in candidates:
            if not base.exists():
                continue
            try:
                files.extend(base.glob("**/config/webui.json"))
            except OSError:
                pass
    for webui in files:
            root = webui.parent.parent
            if root in seen:
                continue
            seen.add(root)
            cfg = _json(webui)
            uins = sorted(p.stem.split("_", 1)[1] for p in (root / "config").glob("onebot11_*.json") if "_" in p.stem)
            roots.append(NapcatRoot(root, int(cfg.get("port", 6099) or 6099), str(cfg.get("token", "")), uins, int(cfg.get("port", 6099) or 6099) in _ports()))
    return roots


def _root_mtime(root: NapcatRoot) -> float:
    try:
        files = [p for p in (root.root / "config").glob("*") if p.is_file()]
        return max((p.stat().st_mtime for p in files), default=0.0)
    except OSError:
        return 0.0


def _qr_mtime(root: NapcatRoot) -> float:
    try:
        return (root.root / "cache" / "qrcode.png").stat().st_mtime
    except OSError:
        return 0.0


def _request(url: str, method: str = "GET", payload: Any = None, token: str = "", timeout: float = 3) -> Any:
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read()
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return raw


def _step(steps: list[dict[str, Any]], name: str, ok: bool, detail: str) -> None:
    steps.append({"name": name, "ok": ok, "detail": detail})


def _ports_from_url(value: Any, default: int) -> int:
    text = str(value or "")
    m = re.search(r":(\d+)(?:/|$)", text)
    return int(m.group(1)) if m else default


def _file_config(root: Path, uin: str, settings: Any) -> None:
    path = root / "config" / (f"onebot11_{uin}.json" if uin else "onebot11.json")
    cfg = _json(path)
    network = cfg.setdefault("network", {})
    network.setdefault("httpServers", [])
    network.setdefault("httpClients", [])
    network.setdefault("httpSseServers", [])
    network.setdefault("websocketServers", [])
    network.setdefault("websocketClients", [])
    network.setdefault("plugins", [])
    api_port = _ports_from_url(getattr(settings, "napcat_api_url", ""), 3001)
    server = next((x for x in network["httpServers"] if isinstance(x, dict) and x.get("port") == api_port), None)
    if server is None:
        server = {"name": "qq-notice-hub-api", "enable": True, "port": api_port, "host": "127.0.0.1", "enableCors": False, "enableWebsocket": False, "messagePostFormat": "array", "token": getattr(settings, "napcat_api_token", ""), "debug": False}
        network["httpServers"].append(server)
    else:
        server.update(enable=True, host="127.0.0.1", token=getattr(settings, "napcat_api_token", ""), messagePostFormat="array")
    url = f"http://127.0.0.1:{int(getattr(settings, 'onebot_port', 8765))}"
    client = next((x for x in network["httpClients"] if isinstance(x, dict) and x.get("url") == url), None)
    if client is None:
        network["httpClients"].append({"name": "qq-notice-hub-events", "enable": True, "url": url, "messagePostFormat": "array", "reportSelfMessage": False, "token": getattr(settings, "onebot_token", ""), "debug": False})
    else:
        client.update(enable=True, token=getattr(settings, "onebot_token", ""), messagePostFormat="array", reportSelfMessage=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


download_url = NAPCAT_DOWNLOAD_URL


def download_url() -> str:
    return NAPCAT_SHELL_ZIP_URL


def _install_step(callback: Callable[[dict[str, Any]], None] | None, name: str, detail: str) -> None:
    if callback:
        callback({"name": name, "ok": True, "detail": detail})


def install(settings: Any, *, on_step: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    data_dir = Path(getattr(settings, "data_dir", "")).expanduser()
    target = (data_dir / "napcat").resolve()
    result: dict[str, Any] = {"ok": False, "installed": False, "already_installed": False, "root": None, "version": None, "bytes": 0, "error": None}
    try:
        if not data_dir or not data_dir.is_absolute():
            raise ValueError("data_dir 必须是绝对路径")
        existing = detect_boot()
        if existing and Path(existing["boot_exe"]).resolve().is_relative_to(target):
            result.update(ok=True, already_installed=True, root=str(target))
            return result
        if target.exists():
            shutil.rmtree(target)
        tmp_dir = data_dir / "napcat-dl-tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        archive = tmp_dir / "NapCat.zip"
        _install_step(on_step, "download", "开始下载官方 NapCat")
        total = 0
        with urllib.request.urlopen(download_url(), timeout=30) as response, archive.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > 500 * 1024 * 1024:
                    raise ValueError("下载文件超过 500 MB 上限")
                output.write(chunk)
                _install_step(on_step, "download", f"已下载 {total} 字节")
        result["bytes"] = total
        with archive.open("rb") as downloaded:
            magic = downloaded.read(4)
        if magic != b"PK\x03\x04":
            raise ValueError("下载内容不是有效的压缩包（不是 ZIP 文件）")
        _install_step(on_step, "extract", "解压 NapCat")
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                path = Path(name)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError("压缩包包含不安全路径")
                destination = (target / path).resolve()
                if not destination.is_relative_to(target):
                    raise ValueError("压缩包路径越界")
            zf.extractall(target)
        global _LAST_INSTALL_ROOT
        _LAST_INSTALL_ROOT = target
        boot = detect_boot()
        if not boot or not Path(boot["boot_exe"]).resolve().is_relative_to(target):
            raise ValueError("解压完成但未找到有效 NapCat 启动文件")
        result.update(ok=True, installed=True, root=str(target), version="v4.18.33")
        _install_step(on_step, "complete", "NapCat 安装完成")
    except Exception as exc:
        result["error"] = f"NapCat 安装失败：{exc}"
    finally:
        try:
            shutil.rmtree(data_dir / "napcat-dl-tmp", ignore_errors=True)
        except OSError:
            pass
    return result


def detect_boot() -> dict[str, str] | None:
    boots: list[Path] = []
    bases = [Path(r"F:\NapCat\napcat"), Path(r"F:\napcat-dl"), Path(r"F:\NapCat")]
    if _LAST_INSTALL_ROOT is not None:
        bases.insert(0, _LAST_INSTALL_ROOT)
    env_data = os.environ.get("QQ_DIGEST_DATA_DIR")
    if env_data:
        bases.insert(0, Path(env_data) / "napcat")
    for base in bases:
        if base.is_dir():
            try:
                boots.extend(base.rglob("NapCatWinBootMain.exe"))
            except OSError:
                pass
    for boot in boots:
        shell = boot.parents[5] if len(boot.parents) > 5 else boot.parent
        qq = shell / "QQ.exe"
        hook = boot.with_name("NapCatWinBootHook.dll")
        if not (qq.is_file() and hook.is_file()):
            continue
        patch = boot.parent / "qqnt.json"
        if patch.is_file() and _json(patch).get("isPureShell") is False:
            continue
        roots = find_roots()
        data = next((r.root for r in roots if r.root == Path(r"F:\NapCat")), None)
        data = data or (max(roots, key=lambda r: (_qr_mtime(r), _root_mtime(r))).root if roots else Path(r"F:\NapCat"))
        return {"boot_exe": str(boot), "qq_exe": str(qq), "hook_dll": str(hook), "data_dir": str(data)}
    return None


def launch(settings: Any, *, uin: str | None = None, profile_dir: str | None = None) -> dict[str, Any]:
    boot = detect_boot()
    profile = Path(profile_dir) if profile_dir else Path(settings.data_dir) / "napcat-profile"
    profile = profile.resolve()
    result = {"ok": False, "already_running": False, "pid": None, "command": [], "profile_dir": str(profile), "boot": boot, "error": None}
    if _ports() & {3001, 6099}:
        result.update(ok=True, already_running=True)
        return result
    if boot is None:
        result["error"] = "没找到 NapCat，请先安装：" + NAPCAT_DOWNLOAD_URL
        return result
    profile.mkdir(parents=True, exist_ok=True)
    command = [boot["boot_exe"], boot["qq_exe"], boot["hook_dll"]]
    if uin:
        command += ["-q", str(uin)]
    command += [f"--user-data-dir={profile}"]
    env = os.environ.copy()
    env.update({"NAPCAT_PATCH_PACKAGE": str(Path(boot["data_dir"]) / "qqnt.json"), "NAPCAT_LOAD_PATH": str(Path(boot["data_dir"]) / "loadNapCat.js"), "NAPCAT_MAIN_PATH": str(Path(boot["data_dir"]) / "napcat.mjs"), "DATA_DIR": boot["data_dir"]})
    result["command"] = command
    try:
        flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        child = subprocess.Popen(command, cwd=boot["data_dir"], env=env, creationflags=flags)
        result.update(ok=True, pid=child.pid)
    except OSError as exc:
        result["error"] = str(exc)
    return result


def stop(settings: Any) -> dict[str, Any]:
    boot = detect_boot()
    result = {"ok": False, "stopped": [], "already_stopped": False, "error": None}
    if boot is None:
        result.update(ok=True, already_stopped=True)
        return result
    try:
        query = "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*NapCatWinBootMain.exe*' -and $_.CommandLine -like '*F:\\napcat-dl\\onekey*' } | Select-Object -ExpandProperty ProcessId"
        raw = subprocess.check_output(["powershell.exe", "-NoProfile", "-Command", query], text=True, stderr=subprocess.DEVNULL)
        pids = [int(x) for x in raw.split() if x.isdigit()]
        if not pids:
            result.update(ok=True, already_stopped=True)
            return result
        for pid in pids:
            subprocess.run(["taskkill.exe", "/PID", str(pid), "/T", "/F"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        result.update(ok=True, stopped=pids)
    except (OSError, ValueError) as exc:
        result["error"] = str(exc)
    return result


def qrcode_bytes(settings: Any) -> bytes | None:
    candidates = [Path(getattr(settings, "data_dir", "")) / "cache" / "qrcode.png", Path(getattr(settings, "napcat_qr_path", ""))]
    candidates.extend(root.root / "cache" / "qrcode.png" for root in find_roots())
    for path in candidates:
        if path.is_file():
            try: return path.read_bytes()
            except OSError: pass
    return None


def auto_setup(settings: Any, *, on_step: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    def add(n: str, ok: bool, d: str):
        _step(steps, n, ok, d)
        if on_step: on_step(steps[-1])
    roots = find_roots()
    if not roots:
        add("find_roots", False, "未找到 NapCat 配置根；官方下载地址: " + NAPCAT_DOWNLOAD_URL)
        return {"ok": False, "steps": steps, "qrcode_path": None, "uin": None, "applied_via": None, "restart_required": False, "error": "NapCat not found"}
    root = max(roots, key=lambda r: (r.live, _qr_mtime(r), _root_mtime(r)))
    add("find_roots", True, str(root.root))
    uin = root.uins[0] if root.uins else ""
    applied = None
    try:
        endpoint = f"http://127.0.0.1:{root.webui_port}/api/OB11Config/GetConfig"
        _request(endpoint, token=root.webui_token)
        # WebUI auth is version-specific; file fallback remains deterministic.
        raise RuntimeError("WebUI mutation not available")
    except Exception as exc:
        _file_config(root.root, uin, settings)
        applied = "file"
        add("configure_onebot", True, f"file:{root.root / 'config' / ('onebot11_' + uin + '.json' if uin else 'onebot11.json')} ({exc})")
    qr = root.root / "cache" / "qrcode.png"
    qr_path = None
    if qr.is_file():
        target = Path(getattr(settings, "napcat_qr_path", qr))
        if not target.is_absolute():
            target = Path(getattr(settings, "data_dir", Path.cwd())) / target
        target = target.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(qr.read_bytes())
        qr_path = str(target)
    add("qrcode", qr_path is not None, qr_path or "二维码不存在")
    api = f"http://127.0.0.1:{_ports_from_url(getattr(settings, 'napcat_api_url', ''), 3001)}/get_login_info"
    try:
        result = _request(api, "POST", {}, getattr(settings, "napcat_api_token", "")); ok = isinstance(result, dict) and result.get("retcode") == 0
        add("connect", ok, json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        add("connect", False, str(exc))
    return {"ok": bool(applied and any(s["ok"] for s in steps if s["name"] == "connect")), "steps": steps, "qrcode_path": qr_path, "uin": uin or None, "applied_via": applied, "restart_required": applied == "file", "error": None}
