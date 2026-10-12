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
            text = subprocess.check_output(["netstat", "-ano"], text=True, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
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
    raw = subprocess.check_output(["powershell.exe", "-NoProfile", "-Command", query], text=True, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    return [int(x) for x in raw.split() if x.isdigit()]


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
    """引擎目录里所有 onebot11*.json 中启用的 httpServers（判断配置到底有没有写进去）。"""
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
    """保证 NapCat 启动时读到的 OneBot 配置里真的有 HTTP 服务。

    NapCat 只在启动时读 config/onebot11_<uin>.json；那个文件的 network 里没有 httpServers，
    它就根本不会开 OneBot 的 HTTP 接口，端口永远不会监听。而本程序「登录成功没有」完全靠
    这个接口判断 —— 于是扫码登录成功也什么都看不到，页面连一句反馈都给不出来。
    """
    root = Path(engine) if engine is not None else None
    if root is None:
        boot = detect_boot()
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

    以前「服务自检」只看 /api/napcat/status，而那个接口本身依赖 OneBot API：API 起不来的
    原因（配置里压根没有 httpServers）它永远查不出来，用户只看到「扫码登录了但什么都没发生」。
    """
    steps: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, hint: str = "") -> None:
        steps.append({"name": name, "ok": bool(ok), "detail": detail, "hint": hint})

    boot = detect_boot()
    if boot is None:
        add("找到 NapCat 引擎", False, "没找到引擎目录", "点「自动下载并安装 NapCat」，或在本目录 .env 里把 QQ_DIGEST_NAPCAT_DIR 指向含 QQ.exe 的 NapCat 目录")
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
        add("本安装的 NapCat 进程在运行", False, "没有找到本安装的 NapCat 启动器进程", "点「启动并登录」拉起 NapCat，再用手机扫码")
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
        existing = detect_boot()
        if existing and Path(existing["boot_exe"]).resolve().is_relative_to(target):
            result.update(ok=True, already_installed=True, root=str(target))
            return result

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
        boot = detect_boot()
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


def _ensure_qq_loader(shell_dir: Path, qq_exe: Path) -> str | None:
    """把 qqnt.json 指定的加载器复制进 QQ 的 resources\\app\\；不需要处理时返回 None。

    NapCatWinBootHook.dll 会把 qqnt.json 的 main（./loadNapCat.js）当成 QQ 的主入口，
    于是 Electron 去 <QQ>\\versions\\<版本>\\resources\\app\\loadNapCat.js 找它；文件不在就弹
    「Uncaught Exception: Cannot find module ...loadNapCat.js」，QQ 起不来。
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
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        except OSError as exc:
            return f"无法把 {relative.name} 复制到 QQ 目录（{target}）：{exc}。请退出后用管理员身份运行一次，或手动把 {source} 复制过去。"
    return None


def launch(settings: Any, *, uin: str | None = None, profile_dir: str | None = None) -> dict[str, Any]:
    boot = detect_boot()
    profile = _profile_dir(settings, profile_dir)
    result = {"ok": False, "already_running": False, "pid": None, "command": [], "profile_dir": str(profile), "boot": boot, "error": None}
    # 只认 3001（OneBot HTTP API 端口）。6099 是 QQNT 自带 WebUI 的端口，用户开着日常
    # QQ 也会占用它；把它算进去会把「QQ 在跑」误判成「NapCat 在跑」，页面上一边说
    # 已经在运行、一边又说连不上 API。
    if _ports() & {3001}:
        result.update(ok=True, already_running=True)
        return result
    # 端口没起来 != 没在运行：NapCat 的 OneBot 端口要等手机扫码登录成功之后才监听，
    # 「等扫码」可能持续好几分钟。以前只按端口判断，于是这段等待期里每点一次「启动并登录」
    # 就再拉一套 NapCat + QQ；两套实例抢同一个 --user-data-dir，谁都登不进去、端口永远不开。
    # 这里再按进程兜一次：同一个 profile 已经有启动器在跑，就只报告状态，绝不再拉第二个。
    running = _matching_pids(str(profile))
    if running:
        result.update(ok=True, already_running=True, pid=running[0], starting=True)
        return result
    if boot is None:
        result["error"] = "没找到 NapCat，请先安装：" + NAPCAT_DOWNLOAD_URL
        return result
    loader_error = _ensure_qq_loader(Path(boot["boot_exe"]).parent, Path(boot["qq_exe"]))
    if loader_error:
        result["error"] = loader_error
        return result
    # 先补齐 OneBot 配置再启动。NapCat 只在启动时读 config/onebot11_<uin>.json，
    # 那里没有启用的 httpServers 就永远不会开 OneBot 接口 —— 扫码登录成功也什么都不发生。
    result["onebot_config"] = ensure_onebot_config(settings, engine=Path(boot["data_dir"]), uin=uin)
    profile.mkdir(parents=True, exist_ok=True)
    command = [boot["boot_exe"], boot["qq_exe"], boot["hook_dll"]]
    if uin:
        command += ["-q", str(uin)]
    command += [f"--user-data-dir={profile}"]
    env = os.environ.copy()
    env.update({"NAPCAT_PATCH_PACKAGE": str(Path(boot["data_dir"]) / "qqnt.json"), "NAPCAT_LOAD_PATH": str(Path(boot["data_dir"]) / "loadNapCat.js"), "NAPCAT_MAIN_PATH": str(Path(boot["data_dir"]) / "napcat.mjs"), "DATA_DIR": boot["data_dir"]})
    result["command"] = command
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        child = subprocess.Popen(command, cwd=boot["data_dir"], env=env, creationflags=flags)
        result.update(ok=True, pid=child.pid)
    except OSError as exc:
        result["error"] = str(exc)
    return result


def _kill_matching(pattern: str) -> list[int]:
    safe = pattern.replace("'", "''")
    query = (
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'NapCatWinBootMain.exe' "
        f"-and $_.CommandLine -like '*{safe}*' }} | Select-Object -ExpandProperty ProcessId"
    )
    raw = subprocess.check_output(["powershell.exe", "-NoProfile", "-Command", query], text=True, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    pids = [int(x) for x in raw.split() if x.isdigit()]
    for pid in pids:
        subprocess.run(["taskkill.exe", "/PID", str(pid), "/T", "/F"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    return pids


def stop(settings: Any) -> dict[str, Any]:
    boot = detect_boot()
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
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and 3001 in _ports():
        time.sleep(0.5)
    qr = _qr_file(settings)
    stamp = _qr_token(qr)
    launched = launch(settings, uin=uin)
    result["pid"] = launched.get("pid")
    if not launched.get("ok"):
        result["error"] = launched.get("error") or "启动 NapCat 失败"
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
    """二维码图片文件：刷新标记和图片字节必须取自同一个文件，否则页面会在图没变时重载。"""
    candidates = [Path(getattr(settings, "data_dir", "")) / "cache" / "qrcode.png", Path(getattr(settings, "napcat_qr_path", ""))]
    candidates.extend(root.root / "cache" / "qrcode.png" for root in find_roots())
    for path in candidates:
        if str(path) and path.is_file():
            return path
    return None


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
        add("find_roots", False, "未找到 NapCat 配置根；官方下载地址: " + NAPCAT_DOWNLOAD_URL)
        return {"ok": False, "steps": steps, "qrcode_path": None, "uin": None, "applied_via": None, "restart_required": False, "error": "NapCat not found"}
    boot = detect_boot()
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
            add("configure_onebot", False, str(config_exc))
            return {"ok": False, "steps": steps, "qrcode_path": None, "uin": uin or None, "applied_via": None, "restart_required": False, "error": str(config_exc)}
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
    waiting = False
    if not listening and applied == "file":
        # NapCat 只在启动时读 onebot11_*.json：配置写完不重启，API 端口永远不会开。
        info = restart(settings, uin=uin)
        restarted = bool(info.get("ok"))
        add("restart", restarted, f"已重启 NapCat（进程 {info.get('pid')}），需要用手机重新扫码登录" if restarted else f"重启 NapCat 失败：{info.get('error') or '未知错误'}")
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
    add("connect", connected, detail if listening else ("正在等手机扫码登录 QQ：扫码成功后会自动连上，不用再点一键接入。" if waiting else detail))
    return {"ok": bool(applied and connected), "steps": steps, "qrcode_path": qr_path, "uin": uin or None, "applied_via": applied, "restarted": restarted, "waiting_for_login": waiting, "restart_required": bool(applied == "file" and not connected), "error": None}
