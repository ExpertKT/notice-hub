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
import time
import zipfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .win_text import decode_console_output

NAPCAT_DOWNLOAD_URL = "https://github.com/NapNeko/NapCatQQ/releases"
# The OneKey zip is only a 994 KB installer stub. Install the actual shell payload.
NAPCAT_SHELL_ZIP_URL = "https://github.com/NapNeko/NapCatQQ/releases/download/v4.18.33/NapCat.Shell.zip"
_QQ_REQUIRED_ERROR = (
    "未找到电脑版 QQ NT。NapCat Shell 本身不含 QQ.exe，它是挂在官方 QQ 上的外挂引擎，"
    "所以必须先装官方 QQ。三步：① 点「下载官方 QQ」装好官方 QQ NT；② 装完不用打开 QQ；"
    "③ 回到本页点「重新检测」。"
)
_QQ_ENV_KEY = "QQ_DIGEST_QQ_EXE"
_QQ_SCAN_DEPTH = 3
_QQ_SCAN_SKIP = frozenset({"node_modules", "windowsapps", "$recycle.bin"})
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
            # Take bytes and decode ourselves: netstat writes GBK on Chinese Windows, and
            # text=True would decode as UTF-8 whenever UTF-8 mode is on (PYTHONUTF8=1),
            # raising UnicodeDecodeError and breaking NapCat install/start. See win_text.py.
            raw = subprocess.check_output(["netstat", "-ano"], stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
            text = decode_console_output(raw)
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
    bases: list[Path] = []
    for raw in (os.environ.get("APPDATA", ""), os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")):
        if raw:
            bases.append(Path(raw))
    env_data = os.environ.get("QQ_DIGEST_DATA_DIR", "")
    env_napcat = os.environ.get("QQ_DIGEST_NAPCAT_DIR", "")
    if env_data:
        bases.append(Path(env_data) / "napcat")
    if env_napcat:
        bases.append(Path(env_napcat))
    qq_exe = _registry_qq_exe()
    if qq_exe is not None:
        bases.extend((qq_exe.parent, qq_exe.parent.parent))
        try:
            bases.extend(qq_exe.parent.glob("**/NapCat.*/versions/*/resources/app/napcat"))
        except OSError:
            pass

    config_roots: list[Path] = []
    seen: set[str] = set()
    for base in bases:
        if not base.is_dir():
            continue
        candidates = [base, base / "napcat"]
        try:
            packages = list(base.glob("NapCat*"))
            nested_base = base / "napcat"
            if nested_base.is_dir():
                packages.extend(nested_base.glob("NapCat*"))
        except OSError:
            packages = []
        for package in packages:
            candidates.append(package)
            try:
                for version in package.glob("versions/*"):
                    candidates.append(version / "resources" / "app" / "napcat")
            except OSError:
                pass
        for root in candidates:
            config = root / "config"
            if not root.is_dir() or not config.is_dir():
                continue
            if not any((config / name).is_file() for name in ("webui.json", "napcat.json")):
                continue
            if not (root / "NapCatWinBootMain.exe").is_file() or not (root / "NapCatWinBootHook.dll").is_file():
                continue
            key = os.path.normcase(str(root.resolve()))
            if key not in seen:
                seen.add(key)
                config_roots.append(root)

    ports = _ports() if config_roots else set()
    roots: list[NapcatRoot] = []
    for root in config_roots:
        webui = root / "config" / "webui.json"
        cfg = _json(webui) if webui.is_file() else {}
        port = int(cfg.get("port", 6099) or 6099)
        uins = sorted(p.stem.split("_", 1)[1] for p in (root / "config").glob("onebot11_*.json") if "_" in p.stem)
        roots.append(NapcatRoot(root, port, str(cfg.get("token", "")), uins, port in ports))
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
    if path.exists():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"OneBot 配置不是有效 JSON：{path}") from exc
        if not isinstance(cfg, dict):
            raise ValueError(f"OneBot 配置必须是 JSON 对象：{path}")
        backup = path.with_name(path.name + f".bak-{time.time_ns()}")
        shutil.copy2(path, backup)
    else:
        cfg = {}
    network = cfg.setdefault("network", {})
    if not isinstance(network, dict):
        raise ValueError(f"OneBot 配置 network 必须是对象：{path}")
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
    try:
        written = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"写入 OneBot 配置后校验失败：{path}") from exc
    if not isinstance(written, dict):
        raise ValueError(f"写入 OneBot 配置后不是 JSON 对象：{path}")


def _known_uins(engine: Path) -> list[str]:
    """引擎配置目录里出现过的 QQ 号（NapCat 按 uin 命名 OneBot 配置）。"""
    uins: list[str] = []
    for path in (engine / "config").glob("onebot11_*.json"):
        stem = path.stem
        if "_" in stem:
            uin = stem.split("_", 1)[1].strip()
            if uin:
                uins.append(uin)
    return sorted(set(uins))


def onebot_config_servers(engine: Path) -> list[dict[str, Any]]:
    """引擎目录里所有 onebot11*.json 中启用的 httpServers（用来判断配置到底有没有写进去）。"""
    servers: list[dict[str, Any]] = []
    for path in sorted((engine / "config").glob("onebot11*.json")):
        try:
            cfg = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        network = cfg.get("network") if isinstance(cfg, dict) else None
        if not isinstance(network, dict):
            continue
        for server in network.get("httpServers") or []:
            if isinstance(server, dict) and server.get("enable"):
                servers.append({"file": path.name, "port": server.get("port"), "host": server.get("host")})
    return servers


def ensure_onebot_config(settings: Any, *, engine: Path | None = None, uin: str | None = None) -> dict[str, Any]:
    """保证 NapCat 启动时读到的 OneBot 配置里真的有 HTTP 服务（3001）。

    NapCat 只在启动时读 ``config/onebot11_<uin>.json``；那个文件的 network 里没有
    httpServers，它就**根本不会开 OneBot 的 HTTP 接口**，3001 永远不会监听。而本程序
    「登录成功没有」完全靠这个接口判断 —— 于是扫码登录成功也什么都看不到，页面连一句
    反馈都给不出来（用户实测报的「扫码登录后无事发生，也没有反馈」）。

    所以任何一次启动之前都必须把配置补齐：已出现过的 uin 各写一份，加上不带 uin 的兜底，
    以及调用方明确指定的 uin。
    """
    root = Path(engine) if engine is not None else None
    if root is None:
        boot = detect_boot(Path(settings.data_dir))
        root = Path(boot["data_dir"]) if boot and boot.get("data_dir") else None
    if root is None or not root.is_dir():
        return {"ok": False, "written": [], "error": "找不到 NapCat 引擎目录"}
    targets = _known_uins(root)
    if uin and str(uin).strip() and str(uin).strip() not in targets:
        targets.append(str(uin).strip())
    targets.append("")  # 不带 uin 的兜底文件：NapCat 还没有为某个号建过配置时也得有可用的
    written: list[str] = []
    for target in dict.fromkeys(targets):
        try:
            _file_config(root, target, settings)
        except (OSError, ValueError) as exc:
            return {"ok": False, "written": written, "error": str(exc)}
        written.append("onebot11_%s.json" % target if target else "onebot11.json")
    port = _ports_from_url(getattr(settings, "napcat_api_url", ""), 3001)
    enabled = [item for item in onebot_config_servers(root) if item.get("port") == port]
    return {"ok": bool(enabled), "written": written, "port": port, "servers": enabled, "error": None if enabled else "配置里没有启用 %s 端口的 httpServer" % port}


def diagnose(settings: Any) -> dict[str, Any]:
    """把「能不能收消息」这条链拆开逐段检查，并指出第一处断点。

    以前「服务自检」只看 ``/api/napcat/status``，而那个接口本身依赖 OneBot API：
    API 起不来的原因（配置里压根没有 httpServers）它永远查不出来，用户只看到
    「扫码登录了但什么都没发生」。所以这里按链路顺序查，第一处断点就是要修的地方。
    """
    steps: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, hint: str = "") -> None:
        steps.append({"name": name, "ok": bool(ok), "detail": detail, "hint": hint})

    boot = detect_boot(Path(settings.data_dir))
    if boot is None:
        add("找到 NapCat 引擎", False, "没找到引擎目录", "点「自动下载并安装 NapCat」，或在本目录 .env 里把 QQ_DIGEST_NAPCAT_DIR 指向 NapCat 引擎目录（里面有 NapCatWinBootMain.exe 的那个）")
        return {"ok": False, "steps": steps, "broken": "找到 NapCat 引擎", "hint": steps[0]["hint"]}
    engine = Path(boot["data_dir"])
    add("找到 NapCat 引擎", True, str(engine))

    port = _ports_from_url(getattr(settings, "napcat_api_url", ""), 3001)
    servers = [item for item in onebot_config_servers(engine) if item.get("port") == port]
    if not servers:
        add("OneBot 配置里有 %s 端口的 httpServer" % port, False,
            "config/onebot11*.json 的 network.httpServers 里没有启用 %s 端口的服务" % port,
            "点「重启引擎」会自动补齐配置再启动；这之前无论怎么扫码登录都不会有反应")
        return {"ok": False, "steps": steps, "broken": "OneBot 配置里有 %s 端口的 httpServer" % port, "hint": steps[-1]["hint"]}
    add("OneBot 配置里有 %s 端口的 httpServer" % port, True, json.dumps(servers, ensure_ascii=False))

    pids = running_pids(settings)
    if not pids:
        add("本安装的 NapCat 进程在运行", False, "没有找到本安装的 NapCat 启动器进程",
            "点「启动并登录」拉起 NapCat，再用手机扫码")
        return {"ok": False, "steps": steps, "broken": "本安装的 NapCat 进程在运行", "hint": steps[-1]["hint"]}
    add("本安装的 NapCat 进程在运行", True, "pid " + ",".join(str(p) for p in pids))

    if port not in _ports():
        add("OneBot 接口端口 %s 在监听" % port, False,
            "端口没开。NapCat 要等手机扫码登录成功之后才会启动 OneBot 的 HTTP 服务",
            "用手机 QQ 扫下面的二维码；二维码一直不变就点「重启引擎」，再重新扫一次")
        return {"ok": False, "steps": steps, "broken": "OneBot 接口端口 %s 在监听" % port, "hint": steps[-1]["hint"]}
    add("OneBot 接口端口 %s 在监听" % port, True, "listening")

    return {"ok": True, "steps": steps, "broken": "", "hint": ""}


download_url = NAPCAT_DOWNLOAD_URL


def download_url() -> str:
    return NAPCAT_SHELL_ZIP_URL


def _path_from_registry_value(value: Any) -> Path | None:
    text = os.path.expandvars(str(value or "")).strip()
    match = re.match(r'^"([^\"]+\.exe)"(?:,\d+)?', text, re.IGNORECASE)
    if not match:
        match = re.match(r"^(.+?\.exe)(?:,\d+)?(?:\s|$)", text, re.IGNORECASE)
    if match:
        return Path(match.group(1))
    if text:
        return Path(text) / "QQ.exe"
    return None


def _registry_qq_exe() -> Path | None:
    """Read the installed QQ NT path from known registry values, without scanning disks."""
    try:
        import winreg
    except ImportError:
        return None

    known_keys = (
        (winreg.HKEY_CURRENT_USER, r"Software\Tencent\QQNT"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Tencent\QQNT"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Tencent\QQNT"),
    )
    uninstall_keys = (
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    candidates: list[Path] = []

    def values_for(hive: Any, key_path: str) -> dict[str, Any]:
        values: dict[str, Any] = {}
        try:
            with winreg.OpenKey(hive, key_path) as key:
                index = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    values[name.casefold()] = value
                    index += 1
        except OSError:
            pass
        return values

    for hive, key_path in known_keys:
        values = values_for(hive, key_path)
        # "install" 是腾讯 QQNT 自己写的值名（HKLM\...\WOW6432Node\Tencent\QQNT 里
        # 就是 Install=D:\QQ）；漏了它会把装在非系统盘的 QQ 判成「没装」。
        for name in ("install", "installpath", "installdir", "installlocation", "path", "exepath"):
            path = _path_from_registry_value(values.get(name))
            if path is not None:
                candidates.append(path)

    for hive, root in uninstall_keys:
        try:
            with winreg.OpenKey(hive, root) as key:
                index = 0
                while True:
                    try:
                        subkey = winreg.EnumKey(key, index)
                    except OSError:
                        break
                    index += 1
                    values = values_for(hive, root + "\\" + subkey)
                    display = str(values.get("displayname", "")).casefold()
                    if "qq" not in display and "腾讯" not in display and "qq" not in subkey.casefold():
                        continue
                    for name in ("installlocation", "displayicon"):
                        path = _path_from_registry_value(values.get(name))
                        if path is not None:
                            candidates.append(path)
        except OSError:
            pass

    for path in candidates:
        if path.name.casefold() == "qq.exe" and path.is_file():
            return path.resolve()
    return None


def _running_qq_exe() -> Path | None:
    """正在运行的 QQ.exe 的完整路径；QQ 没开着或查不到时返回 None。

    这是最便宜也最准的一条：用户既然在用 QQ，进程路径就摆在那里，不用猜注册表也不用扫盘。
    """
    if os.name != "nt":
        return None
    query = "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'QQ.exe' } | Select-Object -First 1 -ExpandProperty ExecutablePath"
    try:
        raw = subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-Command", query],
            stderr=subprocess.DEVNULL,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in decode_console_output(raw).splitlines():
        text = line.strip().strip('"')
        if not text:
            continue
        try:
            path = Path(text)
            if path.name.casefold() == "qq.exe" and path.is_file():
                return path.resolve()
        except OSError:
            continue
    return None


def _drive_root_qq_candidates() -> list[Path]:
    """各盘根目录下最常见的几种 QQ 安装位置（只看固定几个点，不遍历全盘）。"""
    candidates: list[Path] = []
    if os.name != "nt":
        return candidates
    for letter in "CDEFGH":
        base = Path(f"{letter}:\\")
        try:
            if not base.is_dir():
                continue
        except OSError:
            continue
        for relative in (
            ("QQ", "QQ.exe"),
            ("Tencent", "QQ", "QQ.exe"),
            ("Tencent", "QQNT", "QQ.exe"),
            ("Program Files", "Tencent", "QQNT", "QQ.exe"),
            ("Program Files (x86)", "Tencent", "QQNT", "QQ.exe"),
        ):
            candidates.append(base.joinpath(*relative))
    return candidates


def find_qq_exe(preferred_roots: tuple[Path, ...] = ()) -> Path | None:
    """Find QQ NT locally: package siblings first, then registry, then standard paths."""
    candidates: list[Path] = []
    for root in preferred_roots:
        root = Path(root)
        candidates.append(root if root.name.casefold() == "qq.exe" else root / "QQ.exe")
    env_path = os.environ.get(_QQ_ENV_KEY, "").strip().strip('"')
    if env_path:
        candidates.append(Path(env_path))
    registered = _registry_qq_exe()
    if registered is not None:
        candidates.append(registered)
    running = _running_qq_exe()
    if running is not None:
        candidates.append(running)

    program_roots = [
        os.environ.get("ProgramFiles", r"C:\\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
        os.environ.get("APPDATA", ""),
    ]
    for raw in program_roots:
        if not raw:
            continue
        base = Path(raw)
        candidates.extend((
            base / "Tencent" / "QQ" / "QQ.exe",
            base / "Tencent" / "QQNT" / "QQ.exe",
            base / "Programs" / "Tencent" / "QQ" / "QQ.exe",
            base / "Programs" / "Tencent" / "QQNT" / "QQ.exe",
        ))
    candidates.extend(_drive_root_qq_candidates())
    seen: set[str] = set()
    for path in candidates:
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        try:
            if path.name.casefold() == "qq.exe" and path.is_file():
                return path.resolve()
        except OSError:
            continue
    return None


def _within(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError):
        return False


def _install_step(callback: Callable[[dict[str, Any]], None] | None, name: str, detail: str) -> None:
    if callback:
        callback({"name": name, "ok": True, "detail": detail})


def install(settings: Any, *, on_step: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    global _LAST_INSTALL_ROOT
    data_dir = Path(getattr(settings, "data_dir", "")).expanduser()
    target = (data_dir / "napcat").resolve()
    result: dict[str, Any] = {"ok": False, "installed": False, "already_installed": False, "root": None, "version": None, "bytes": 0, "error": None}
    stage_dir: Path | None = None
    old_dir: Path | None = None
    previous_root = _LAST_INSTALL_ROOT
    try:
        if not data_dir or not data_dir.is_absolute():
            raise ValueError("data_dir 必须是绝对路径")
        data_dir.mkdir(parents=True, exist_ok=True)
        existing = detect_boot(data_dir)
        if existing and Path(existing["boot_exe"]).resolve().is_relative_to(target):
            result.update(ok=True, already_installed=True, root=str(target))
            return result

        if find_qq_exe((target,)) is None:
            raise ValueError(_QQ_REQUIRED_ERROR)

        stage_dir = Path(tempfile.mkdtemp(prefix="napcat-stage-", dir=data_dir))
        archive = stage_dir / "NapCat.zip"
        # resolve() 让 payload 与下面 destination.resolve() 的物理路径口径一致：
        # 若 data_dir/TEMP 里含 8.3 短名（CI 的 RUNNER~1）或 junction/symlink 组件，
        # is_relative_to 会因「一边已解析、一边未解析」而误判越界，导致合法压缩包被拒。
        payload = (stage_dir / "payload").resolve()
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
            if downloaded.read(4) != b"PK\x03\x04":
                raise ValueError("下载内容不是有效的压缩包（不是 ZIP 文件）")
        _install_step(on_step, "extract", "解压 NapCat")
        payload.mkdir()
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                path = Path(name)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError("压缩包包含不安全路径")
                destination = (payload / path).resolve()
                if not destination.is_relative_to(payload):
                    raise ValueError("压缩包路径越界")
            zf.extractall(payload)

        _LAST_INSTALL_ROOT = payload
        boot = detect_boot(data_dir)
        if not boot or not Path(boot["boot_exe"]).resolve().is_relative_to(payload):
            raise ValueError("解压完成但未找到有效 NapCat 启动文件")

        if target.exists():
            old_dir = data_dir / f"napcat-old-{time.time_ns()}"
            target.replace(old_dir)
        try:
            payload.replace(target)
        except Exception:
            if old_dir is not None and not target.exists():
                old_dir.replace(target)
            raise
        _LAST_INSTALL_ROOT = target
        result.update(ok=True, installed=True, root=str(target), version="v4.18.33")
        if old_dir is not None:
            shutil.rmtree(old_dir, ignore_errors=True)
        _install_step(on_step, "complete", "NapCat 安装完成")
    except Exception as exc:
        if old_dir is not None and old_dir.exists() and not target.exists():
            try:
                old_dir.replace(target)
            except OSError:
                pass
        _LAST_INSTALL_ROOT = previous_root
        result["error"] = f"NapCat 安装失败：{exc}"
    finally:
        if stage_dir is not None:
            try:
                shutil.rmtree(stage_dir, ignore_errors=True)
            except OSError:
                pass
    return result


def detect_boot(data_dir: Path | None = None) -> dict[str, str] | None:
    roots = find_roots()
    env_data = os.environ.get("QQ_DIGEST_DATA_DIR", "")
    env_napcat = os.environ.get("QQ_DIGEST_NAPCAT_DIR", "")
    managed_roots: list[Path | None] = [_LAST_INSTALL_ROOT]
    if data_dir is not None:
        managed_roots.append(Path(data_dir).expanduser() / "napcat")
    elif env_data:
        managed_roots.append(Path(env_data).expanduser() / "napcat")
    if env_napcat:
        managed_roots.append(Path(env_napcat).expanduser())

    bases = [root for root in managed_roots if root is not None]
    bases.extend(root.root for root in roots)
    boots: list[Path] = []
    seen: set[str] = set()
    for base in bases:
        if not base.is_dir():
            continue
        candidates = [base / "NapCatWinBootMain.exe", base / "napcat" / "NapCatWinBootMain.exe"]
        packages = [base, base / "napcat"]
        try:
            packages.extend(base.glob("NapCat*"))
            nested = base / "napcat"
            if nested.is_dir():
                packages.extend(nested.glob("NapCat*"))
        except OSError:
            pass
        for package in packages:
            if not package.is_dir():
                continue
            try:
                for version in package.glob("versions/*"):
                    candidates.append(version / "resources" / "app" / "napcat" / "NapCatWinBootMain.exe")
            except OSError:
                pass
        for boot in candidates:
            if boot.is_file():
                key = os.path.normcase(str(boot.resolve()))
                if key not in seen:
                    seen.add(key)
                    boots.append(boot)

    for boot in boots:
        hook = boot.with_name("NapCatWinBootHook.dll")
        if not hook.is_file():
            continue
        shell_dir = boot.parent
        qq = find_qq_exe((shell_dir, *boot.parents))
        if qq is None:
            continue
        patch = shell_dir / "qqnt.json"
        if patch.is_file() and _json(patch).get("isPureShell") is False:
            continue

        managed = next((root for root in managed_roots if root is not None and _within(boot, root)), None)
        config_root = next((parent for parent in (shell_dir, *boot.parents) if (parent / "config").is_dir()), boot.parent)
        if managed is not None:
            data = next((root.root for root in roots if root.root.resolve() == config_root.resolve()), config_root)
        else:
            data = next((root.root for root in roots if root.root.resolve() == shell_dir.resolve()), None)
            if data is None:
                data = max(roots, key=lambda root: (_qr_mtime(root), _root_mtime(root))).root if roots else boot.parent
        return {"boot_exe": str(boot.resolve()), "qq_exe": str(qq), "hook_dll": str(hook.resolve()), "data_dir": str(data.resolve())}
    return None


def _ensure_qq_loader(shell_dir: Path, qq_exe: Path) -> str | None:
    """把 qqnt.json 指定的加载器复制进 QQ 的 resources\\app\\；不需要处理时返回 None。

    NapCatWinBootHook.dll 会把 qqnt.json 的 main（./loadNapCat.js）当成 QQ 的主入口，
    于是 Electron 去 <QQ>\\versions\\<版本>\\resources\\app\\loadNapCat.js 找它；文件不在就弹
    「Uncaught Exception: Cannot find module ...loadNapCat.js」，QQ 起不来（用户 2026-10-11 撞上）。
    引擎目录里那份是给人看的，QQ 只认自己目录里的那份。
    """
    patch = shell_dir / "qqnt.json"
    if not patch.is_file():
        return None
    relative = Path(str(_json(patch).get("main") or ""))
    if not relative.name or relative.is_absolute() or ".." in relative.parts:
        return None
    source = shell_dir / relative
    if not source.is_file():
        return None
    qq_dir = qq_exe.parent
    apps = [app for app in (qq_dir / "resources" / "app", *sorted((qq_dir / "versions").glob("*/resources/app"))) if app.is_dir() and not (app / relative).is_file()]
    for app in apps:
        target = app / relative
        # 先写临时文件再原子改名：copy2 中途失败会留下半截文件，而上面的 apps 过滤只判断
        # 「文件在不在」，下次启动就会跳过它，让 QQ 去加载一个损坏的入口。
        staging = target.with_name(target.name + ".nh-part")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, staging)
            os.replace(staging, target)
        except OSError as exc:
            try:
                staging.unlink(missing_ok=True)
            except OSError:
                pass
            return f"无法把 {relative.name} 复制到 QQ 目录（{target}）：{exc}。请退出后用管理员身份运行一次，或手动把 {source} 复制过去。"
    return None


def _onebot_config_failure(detail: Any, *, stopped: bool = False) -> str:
    state = "旧的 NapCat 已经停了，新的没有起来。" if stopped else "NapCat 还没有启动。"
    return f"引擎配置没写成功：{detail or '未知原因'}。{state}请检查 NapCat 目录权限和 .env 里的 QQ_DIGEST_NAPCAT_DIR，然后重试「启动并登录」或「重启引擎」。"


def launch(settings: Any, *, uin: str | None = None, profile_dir: str | None = None) -> dict[str, Any]:
    boot = detect_boot(Path(settings.data_dir))
    profile = _profile_dir(settings, profile_dir)
    result = {"ok": False, "already_running": False, "pid": None, "command": [], "profile_dir": str(profile), "boot": boot, "error": None}
    # 只认 OneBot HTTP API 端口，且必须跟 .env 里的 QQ_DIGEST_NAPCAT_API_URL 走：写死 3001
    # 的话，用户改过端口就会误判「已在运行」而拒绝启动（本文件别处都按配置读）。
    # 6099 是 QQNT 自带 WebUI 的端口，用户开着日常 QQ 也会占用它；把它算进去会把
    # 「QQ 在跑」误判成「NapCat 在跑」，页面上一边说已经在运行、一边又说连不上 API。
    if _ports_from_url(getattr(settings, "napcat_api_url", ""), 3001) in _ports():
        result.update(ok=True, already_running=True)
        return result
    # 端口没起来 ≠ 没在运行：NapCat 的 OneBot 端口要等手机扫码登录成功之后才监听，
    # 「等扫码」可能持续好几分钟。以前只按端口判断，于是这段等待期里每点一次「启动并登录」
    # 就再拉一套 NapCat + QQ；两套实例抢同一个 --user-data-dir，谁都登不进去、端口永远不开，
    # 表现就是「二维码不刷新、点了也没反应」。这里再按进程兜一次：同一个 profile 已经有
    # 启动器在跑，就只报告状态，绝不再拉第二个。
    running = _matching_pids(str(profile))
    if running:
        result.update(ok=True, already_running=True, pid=running[0], starting=True)
        return result
    if boot is None:
        result["error"] = _QQ_REQUIRED_ERROR if find_qq_exe() is None else "没找到 NapCat。请点本页面的「自动下载并安装 NapCat」；若下载不通，可把已有的 NapCat 整个放进 <数据目录>\\napcat\\ 后重试。"
        return result
    loader_error = _ensure_qq_loader(Path(boot["boot_exe"]).parent, Path(boot["qq_exe"]))
    if loader_error:
        result["error"] = loader_error
        return result
    # 先补齐 OneBot 配置再启动。NapCat 只在启动时读 config/onebot11_<uin>.json，
    # 那里没有启用的 httpServers 就永远不会开 3001 —— 用户扫码登录成功也什么都不发生。
    result["onebot_config"] = ensure_onebot_config(settings, engine=Path(boot["data_dir"]), uin=uin)
    if not result["onebot_config"].get("ok"):
        result["error"] = _onebot_config_failure(result["onebot_config"].get("error"))
        return result
    profile.mkdir(parents=True, exist_ok=True)
    cwd_dir = Path(boot["data_dir"])
    if not cwd_dir.is_dir():
        cwd_dir = Path(boot["boot_exe"]).parent  # 兜底：工作目录必须真实存在，否则 Popen 报 WinError 267
    command = [boot["boot_exe"], boot["qq_exe"], boot["hook_dll"]]
    if uin:
        command += ["-q", str(uin)]
    command += [f"--user-data-dir={profile}"]
    env = os.environ.copy()
    env.update({"NAPCAT_PATCH_PACKAGE": str(cwd_dir / "qqnt.json"), "NAPCAT_LOAD_PATH": str(cwd_dir / "loadNapCat.js"), "NAPCAT_MAIN_PATH": str(cwd_dir / "napcat.mjs"), "DATA_DIR": str(cwd_dir), "NAPCAT_WORKDIR": str(cwd_dir)})
    result["command"] = command
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        child = subprocess.Popen(command, cwd=cwd_dir, env=env, creationflags=flags)
        result.update(ok=True, pid=child.pid)
    except OSError as exc:
        result["error"] = f"{exc}。NapCat 引擎目录不可用，请在本目录 .env 里把 QQ_DIGEST_NAPCAT_DIR 指向 NapCat 引擎目录（里面有 NapCatWinBootMain.exe 的那个）后重启。"
    return result


def _matching_pids(pattern: str) -> list[int]:
    """正在运行的、命令行里含 pattern 的 NapCat 启动器进程号（只查不杀）。

    必须按进程判断「在不在跑」，不能只看端口：NapCat 的 OneBot 端口要等扫码登录成功
    才监听，等待期间端口是空的。
    """
    safe = pattern.replace("'", "''")
    query = (
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'NapCatWinBootMain.exe' "
        f"-and $_.CommandLine -like '*{safe}*' }} | Select-Object -ExpandProperty ProcessId"
    )
    raw = subprocess.check_output(["powershell.exe", "-NoProfile", "-Command", query], stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    # Same GBK-vs-UTF-8 hazard as _ports(); the query output can carry Chinese install paths,
    # so it must be decoded properly rather than dropped with errors="ignore".
    return [int(x) for x in decode_console_output(raw).split() if x.isdigit()]


def _profile_dir(settings: Any, profile_dir: str | None = None) -> Path:
    """本安装给 NapCat 用的 --user-data-dir。launch() 与状态查询共用这一个定义。"""
    profile = Path(profile_dir) if profile_dir else Path(settings.data_dir) / "napcat-profile"
    return profile.resolve()


def running_pids(settings: Any, *, profile_dir: str | None = None) -> list[int]:
    """本安装自己的 NapCat 启动器进程号；没有就是空列表。

    按 profile 目录匹配，绝不按进程名：NapCatWinBootMain.exe 的命令行里必然含它自己的
    文件名，按名字匹配会把同机上别人的实例也算进来。
    """
    try:
        return _matching_pids(str(_profile_dir(settings, profile_dir)))
    except (OSError, ValueError):
        return []


def _kill_matching(pattern: str) -> list[int]:
    pids = _matching_pids(pattern)
    for pid in pids:
        subprocess.run(["taskkill.exe", "/PID", str(pid), "/T", "/F"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    return pids


def stop(settings: Any) -> dict[str, Any]:
    boot = detect_boot(Path(settings.data_dir))
    result = {"ok": False, "stopped": [], "already_stopped": False, "error": None}
    if boot is None:
        result.update(ok=True, already_stopped=True)
        return result
    try:
        pids = _kill_matching(str(Path(boot["boot_exe"]).parent))
        if not pids:
            # 换过安装目录时启动文件路径对不上，这里只允许回退到「本安装自己的 profile 目录」：
            # launch() 把它写进 --user-data-dir=，是这套安装独有的标记。
            # 绝不能退回按进程名停 —— NapCatWinBootMain.exe 的命令行里必然含它自己的文件名，
            # 按名字匹配等于杀掉这台机器上所有 NapCat 实例（别人的账号也会被一起踢下线）。
            pids = _kill_matching(str((Path(settings.data_dir) / "napcat-profile").resolve()))
        if not pids:
            result.update(ok=True, already_stopped=True)
            return result
        result.update(ok=True, stopped=pids)
    except (OSError, ValueError) as exc:
        result["error"] = str(exc)
    return result


def restart(settings: Any, *, uin: str | None = None, wait_seconds: float = 30) -> dict[str, Any]:
    """停掉正在运行的 NapCat 再启动一次：onebot11_*.json 只在 NapCat 启动时读取。"""
    result: dict[str, Any] = {"ok": False, "stopped": [], "already_stopped": False, "pid": None, "api_up": False, "waiting_for_login": False, "error": None}
    stopped = stop(settings)
    result["stopped"] = list(stopped.get("stopped") or [])
    result["already_stopped"] = bool(stopped.get("already_stopped"))
    if stopped.get("error"):
        result["error"] = stopped["error"]
        return result
    stop_port = _ports_from_url(getattr(settings, "napcat_api_url", ""), 3001)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and stop_port in _ports():
        time.sleep(0.5)
    qr = _qr_file(settings)
    stamp = _qr_token(qr)
    launched = launch(settings, uin=uin)
    result["pid"] = launched.get("pid")
    if not launched.get("ok"):
        result["error"] = _onebot_config_failure(launched.get("error") or "启动 NapCat 失败", stopped=bool(result["stopped"]))
        return result
    port = _ports_from_url(getattr(settings, "napcat_api_url", ""), 3001)
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while not result["api_up"] and time.monotonic() < deadline and _qr_token(qr) == stamp:
        result["api_up"] = wait_for_port(port, 0.5)
    # OneBot 的 HTTP 服务在 InitOneBot 里才启动，而那是登录成功之后的事：
    # 新实例写出登录二维码 ⇒ 它在等扫码，端口这会儿本来就不会开，别当成失败。
    result["waiting_for_login"] = bool(not result["api_up"] and _qr_token(qr) != stamp)
    result["ok"] = True
    return result


def _qr_file(settings: Any) -> Path | None:
    """Return the newest QR among the active engine, data cache, and configured path."""
    data_dir = Path(settings.data_dir)
    boot = detect_boot(data_dir)
    candidates: list[Path] = []
    if boot:
        candidates.append(Path(boot["data_dir"]) / "cache" / "qrcode.png")
        candidates.append(Path(boot["boot_exe"]).parent / "cache" / "qrcode.png")
    candidates.append(data_dir / "cache" / "qrcode.png")
    candidates.append(Path(getattr(settings, "napcat_qr_path", "")))
    found = [path for path in candidates if str(path) and path.is_file()]
    return max(found, key=lambda path: path.stat().st_mtime_ns) if found else None


def qrcode_bytes(settings: Any) -> bytes | None:
    path = _qr_file(settings)
    if path is None:
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _qr_token(path: Path | None) -> str:
    """二维码文件的变化标记（mtime+大小）；文件不在时为空串。"""
    try:
        info = path.stat()
    except (OSError, AttributeError):
        return ""
    return f"{info.st_mtime_ns}-{info.st_size}"


def qr_stamp(settings: Any) -> str:
    """页面只在标记变化时才重新取图，避免每 5 秒闪一次。"""
    return _qr_token(_qr_file(settings))


def wait_for_port(port: int, timeout: float = 30) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if port in _ports():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def _probe_api(api: str, token: str) -> tuple[bool, str]:
    """OneBot HTTP 服务有应答就算接通；是否已登录交给页面轮询状态去显示。"""
    try:
        result = _request(api, "POST", {}, token)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    return True, json.dumps(result, ensure_ascii=False)


def _wait_for_new_qr(path: Path, before: int, timeout: float = 20) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        try:
            if path.is_file() and path.stat().st_mtime_ns != before:
                return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


def auto_setup(settings: Any, *, on_step: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    def add(n: str, ok: bool, d: str):
        _step(steps, n, ok, d)
        if on_step: on_step(steps[-1])
    roots = find_roots()
    if not roots:
        add("find_roots", False, "未找到 NapCat 配置根。请点本页面的「自动下载并安装 NapCat」；若下载不通，可把已有的 NapCat 整个放进 <数据目录>\\napcat\\ 后重试。")
        return {"ok": False, "steps": steps, "qrcode_path": None, "uin": None, "applied_via": None, "restart_required": False, "error": "NapCat not found"}
    boot = detect_boot(Path(settings.data_dir))
    preferred = Path(boot["data_dir"]).resolve() if boot and boot.get("data_dir") else None
    root = max(roots, key=lambda r: (preferred is not None and r.root.resolve() == preferred, r.live, _qr_mtime(r), _root_mtime(r)))
    add("find_roots", True, str(root.root))
    uin = root.uins[0] if root.uins else ""
    applied = None
    try:
        endpoint = f"http://127.0.0.1:{root.webui_port}/api/OB11Config/GetConfig"
        _request(endpoint, token=root.webui_token)
        # WebUI auth is version-specific; file fallback remains deterministic.
        raise RuntimeError("WebUI mutation not available")
    except Exception as exc:
        try:
            _file_config(root.root, uin, settings)
        except Exception as config_exc:
            detail = _onebot_config_failure(config_exc)
            add("configure_onebot", False, detail)
            return {"ok": False, "steps": steps, "qrcode_path": None, "uin": uin or None, "applied_via": None, "restart_required": False, "error": detail}
        applied = "file"
        add("configure_onebot", True, f"file:{root.root / 'config' / ('onebot11_' + uin + '.json' if uin else 'onebot11.json')} ({exc})")
    qr = root.root / "cache" / "qrcode.png"
    try:
        qr_before = qr.stat().st_mtime_ns if qr.is_file() else 0
    except OSError:
        qr_before = 0
    api = f"http://127.0.0.1:{_ports_from_url(getattr(settings, 'napcat_api_url', ''), 3001)}/get_login_info"
    api_token = getattr(settings, "napcat_api_token", "")
    listening, detail = _probe_api(api, api_token)
    restarted = False
    restart_error = None
    waiting = False
    if not listening and applied == "file":
        # NapCat 只在启动时读 onebot11_*.json：配置写完不重启，API 端口永远不会开。
        info = restart(settings, uin=uin)
        restarted = bool(info.get("ok"))
        restart_error = info.get("error")
        add("restart", restarted, f"已重启 NapCat（进程 {info.get('pid')}），需要用手机重新扫码登录" if restarted else f"重启 NapCat 失败：{restart_error or '未知错误'}")
        if restarted:
            listening, detail = _probe_api(api, api_token)
            qr_changed = _wait_for_new_qr(qr, qr_before)
            waiting = bool((qr_changed or info.get("waiting_for_login")) and not listening)
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
    connected = listening or waiting
    connect_detail = detail if listening else ("正在等手机扫码登录 QQ：扫码成功后会自动连上，不用再点一键接入。" if waiting else detail)
    add("connect", connected, connect_detail)
    error = None if connected else _onebot_config_failure((restart_error if not restarted else detail) or "NapCat 接入端口没有起来")
    return {"ok": bool(applied and connected), "steps": steps, "qrcode_path": qr_path, "uin": uin or None, "applied_via": applied, "restarted": restarted, "waiting_for_login": waiting, "restart_required": bool(applied == "file" and not connected), "error": error}
