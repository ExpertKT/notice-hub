"""冷装：解 zip → 随机端口/令牌 → 起 QQ-Notice-Hub.exe → 等 /api/health → teardown。

设计约束（负责人定的红线）：
* 只在 `%TEMP%\\nh-e2e-<guid>` 里解包，绝不碰 `F:\\notice-hub` 或 8765-8768。
* 端口随机且避开 8765-8768；令牌随机。
* 全程隐藏窗口（CREATE_NO_WINDOW），不弹浏览器（BROWSER 指向不存在的程序）。
* 关掉 LLM / 视觉 / 更新检查 / 社区遥测，E2E 不依赖外网。
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .cdp import RESERVED_PORTS, free_port
from .paths import ensure_outside_repo, temp_root

CREATE_NO_WINDOW = 0x08000000
DEFAULT_ZIP = Path(__file__).resolve().parents[3] / "packaging" / "QQ-Notice-Hub.zip"

# 冷装环境：一条都不依赖外网，也不给用户机器留自动更新/开机启动的钩子。
ISOLATION_ENV = {
    "QQ_DIGEST_LLM": "0",
    "QQ_DIGEST_VISION": "0",
    "QQ_DIGEST_UPDATE_CHANNEL": "off",
    "QQ_DIGEST_COMMUNITY_TELEMETRY": "0",
    "QQ_DIGEST_AUTOSTART": "0",
}


class ColdInstallError(RuntimeError):
    pass


@dataclass
class Install:
    root: Path
    port: int
    token: str
    process: subprocess.Popen | None = None
    keep: bool = False
    keep_reason: str = ""
    env_extra: dict[str, str] = field(default_factory=dict)
    # 源码模式：直接从仓库复制最小源码树、用系统 python 起 main.py run。
    # 报告里要标明本次是 zip 还是 source，免得拿旧包数据当结论。
    mode: str = "zip"
    _stopped: bool = field(default=False, init=False)

    def has_env(self, key: str) -> bool:
        if key in self.env_extra:
            return bool(str(self.env_extra[key]).strip())
        return False

    # ------------------------------------------------------------------ 派生
    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def install_dir(self) -> Path:
        return self.root / "install"

    @property
    def data_dir(self) -> Path:
        return self.install_dir / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "digest.sqlite3"

    @property
    def exe(self) -> Path:
        return self.install_dir / "QQ-Notice-Hub.exe"

    def alive(self) -> bool:
        if self._stopped or self.process is None:
            return False
        if self.process.poll() is not None:
            return False
        try:
            return health(self.base_url, self.token, timeout=3)[0] == 200
        except OSError:
            return False

    # ------------------------------------------------------------------ 停止
    def teardown(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self.process is not None and self.process.poll() is None:
            # 先走托盘「退出」的官方入口，让 launcher 自己收掉服务子进程。
            if self.mode != "source":
                try:
                    subprocess.run(
                        [str(self.exe), "--quit"],
                        cwd=str(self.install_dir),
                        timeout=30,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=CREATE_NO_WINDOW,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
            _wait_port_closed(self.port, 15)
            if self.process.poll() is None:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                )
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:  # pragma: no cover
                pass
        self.process = None
        if self.keep:
            print(f"[keep] 冷装目录保留在：{self.root}")
            return
        for attempt in range(3):
            try:
                shutil.rmtree(self.root, ignore_errors=False)
                return
            except OSError:
                time.sleep(1.0 + attempt)
        shutil.rmtree(self.root, ignore_errors=True)


def health(base_url: str, token: str = "", *, timeout: float = 5.0) -> tuple[int, str]:
    """GET /api/health（免鉴权），返回 (status, body)。"""
    request = urllib.request.Request(f"{base_url}/api/health")
    if token:
        request.add_header("X-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


def _wait_port_closed(port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return
        time.sleep(0.25)


def _extract(zip_path: Path, target: Path) -> int:
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    count = 0
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            # M3：压缩包里的 ../ 或绝对路径会把文件写到 target 外面，先按解析后的真实路径拦。
            out = (target / info.filename.replace("/", os.sep)).resolve()
            if root not in out.parents:
                raise ColdInstallError(f"安装包里有越界路径，拒绝解压：{info.filename}")
            out.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, open(out, "wb") as dest:
                shutil.copyfileobj(source, dest, length=1 << 20)
            count += 1
    return count


def _seed_env(install_dir: Path, port: int, token: str, extra_env: dict[str, str] | None = None) -> Path:
    data_dir = install_dir / "data"
    log_dir = install_dir / "logs"
    lines = [
        "QQ_DIGEST_WEB=1",
        "QQ_DIGEST_WEB_HOST=127.0.0.1",
        f"QQ_DIGEST_WEB_PORT={port}",
        f"QQ_DIGEST_WEB_TOKEN={token}",
        f"QQ_DIGEST_DATA_DIR={data_dir}",
        f"QQ_DIGEST_LOG_DIR={log_dir}",
    ]
    lines += [f"{key}={value}" for key, value in ISOLATION_ENV.items()]
    for key, value in (extra_env or {}).items():
        if str(key).strip():
            lines.append(f"{key}={value}")
    env_path = install_dir / ".env"
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return env_path


def cold_install(
    zip_path: str | os.PathLike | None = None,
    *,
    keep: bool = False,
    health_timeout: float = 90.0,
    extra_env: dict[str, str] | None = None,
    port: int | None = None,
    token: str | None = None,
    root: str | os.PathLike | None = None,
    from_source: bool = False,
) -> Install:
    """一个全新冷装实例。调用方负责 teardown()。

    端口/令牌/根目录可以预分配（先起平台替身、再把替身给的 env 写进 `.env` 的场景需要）。
    `from_source=True` 时不解包，直接复制仓库最小源码树、用系统 python 起 `main.py run`：
    发版前验「真实安装包」仍用默认的 zip 模式，改完代码要立刻验证时用源码模式。
    """
    package = Path(zip_path) if zip_path else DEFAULT_ZIP
    if not from_source:
        if not package.is_file():
            raise ColdInstallError(f"找不到安装包：{package}（先打包，或传 --zip）")
        if package.stat().st_size < 1024 * 1024:
            raise ColdInstallError(f"安装包只有 {package.stat().st_size} B，像是打包没完成：{package}")

    if root is not None:
        run_root = ensure_outside_repo(Path(root))
        run_root.mkdir(parents=True, exist_ok=True)
        # 每个用例自己的子目录：解包 + Edge profile + 数据都在里面，删起来干净。
        root_path = Path(tempfile.mkdtemp(prefix="case-", dir=str(run_root)))
    else:
        root_path = ensure_outside_repo(Path(tempfile.mkdtemp(prefix="nh-e2e-", dir=str(temp_root()))))
    chosen_port = int(port) if port else free_port(exclude=RESERVED_PORTS)
    chosen_token = token or secrets.token_hex(24)
    install_dir = root_path / "install"
    install: Install | None = None
    try:
        if from_source:
            entries = _copy_source_tree(install_dir)
            if entries < 10:
                raise ColdInstallError(f"源码树只复制到 {entries} 个文件，仓库不完整")
            mode = "source"
        else:
            entries = _extract(package, install_dir)
            if entries < 10:
                raise ColdInstallError(f"解包只得到 {entries} 个文件，包不完整：{package}")
            mode = "zip"
        install = Install(root=root_path, port=chosen_port, token=chosen_token, keep=keep, mode=mode,
                          env_extra={str(k): str(v) for k, v in (extra_env or {}).items()})
        if mode == "zip" and not install.exe.is_file():
            raise ColdInstallError(f"解包后没有 QQ-Notice-Hub.exe：{install_dir}")
        env_path = _seed_env(install_dir, chosen_port, chosen_token, extra_env)
        # B1：子进程环境必须「白名单化」。config.py 用 os.environ.setdefault 读 .env，
        # 父 shell 里任何一个 QQ_DIGEST_*（比如开发机上的真实 QQ_DIGEST_DATA_DIR /
        # QQ_DIGEST_WECHAT_DIR）都会盖掉 .env 里的隔离值，冷装就会写进真实数据、读真实微信库。
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith("QQ_DIGEST_")
        }
        env["BROWSER"] = "nonexistent-browser-probe"  # 别真的弹出浏览器
        env.update(ISOLATION_ENV)
        # 隔离键显式写进环境，不只靠 .env（两种模式都生效）。
        env.update({
            "QQ_DIGEST_WEB": "1",
            "QQ_DIGEST_WEB_HOST": "127.0.0.1",
            "QQ_DIGEST_WEB_PORT": str(chosen_port),
            "QQ_DIGEST_WEB_TOKEN": chosen_token,
            "QQ_DIGEST_DATA_DIR": str(install.data_dir),
            "QQ_DIGEST_LOG_DIR": str(install_dir / "logs"),
        })
        env.update({str(key): str(value) for key, value in (extra_env or {}).items()})
        command = (
            [sys.executable, "-X", "utf8", "main.py", "--env", str(env_path), "run"]
            if mode == "source"
            else [str(install.exe)]
        )
        install.process = subprocess.Popen(
            command,
            cwd=str(install_dir),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        _wait_health(install, timeout=health_timeout)
        _assert_isolated(install)
        return install
    except BaseException:
        _teardown_failed_install(install, root_path)
        raise


# 源码模式只复制跑 Web 服务要的东西（含 webapp 用的 RUNTIME_ROOT/assets）。
# 之前是「整树复制再排除」，仓库根稍有点测试残留（带被锁的 sqlite）就会复制失败；
# 需要新的根文件就加进这个名单，别回到整树复制。
SOURCE_COPY = ("main.py", "qq_digest.py", "qq_live_digest", "assets")
SOURCE_SKIP_SUFFIXES = (".pyc", ".pyo")


def _copy_source_tree(dest: Path) -> int:
    """把最小源码树复制到 dest（只 SOURCE_COPY 里的条目，跳过缓存/数据/日志）。"""
    repo = Path(__file__).resolve().parents[3]
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    ignore = shutil.ignore_patterns(
        "__pycache__", "data", "logs", ".env*", *[f"*{suffix}" for suffix in SOURCE_SKIP_SUFFIXES]
    )
    for name in SOURCE_COPY:
        item = repo / name
        if not item.exists():
            continue
        if item.is_dir():
            target = dest / name
            shutil.copytree(item, target, ignore=ignore, dirs_exist_ok=True)
            count += sum(1 for path in target.rglob("*") if path.is_file())
        else:
            shutil.copy2(item, dest / name)
            count += 1
    return count


def _assert_isolated(install: "Install") -> None:
    """启动后自证隔离：数据目录必须落在本轮 run dir 内，否则立刻停下（别写进真实数据）。"""
    data_dir = Path(str(install.data_dir)).resolve()
    root = Path(str(install.root)).resolve()
    if root not in data_dir.parents and data_dir != root:
        raise ColdInstallError(
            f"冷装实例的数据目录跑出本轮目录了：{data_dir}（应在 {root} 下）。"
            "检查父进程环境里是不是有 QQ_DIGEST_DATA_DIR 之类没被清掉的变量。"
        )
    status, body = health(install.base_url, install.token, timeout=5)
    if status != 200:
        raise ColdInstallError(f"冷装实例 /api/health 复查失败：HTTP {status} {body[:120]}")
    try:
        payload = json.loads(body)
    except ValueError:
        return
    reported = str(payload.get("data_dir") or payload.get("data") or "").strip()
    if reported and Path(reported).resolve() != data_dir:
        raise ColdInstallError(f"冷装实例自报的数据目录不是隔离目录：{reported}（应为 {data_dir}）")


def _teardown_failed_install(install: "Install | None", root_path: Path) -> None:
    """B2：启动失败/健康检查超时也要把进程杀掉、等端口关，再删目录；删不掉要留日志。

    只动本轮那个 PID（`--quit` 再 `taskkill /PID`），绝不按镜像名全机杀——
    用户自己那份 F:\\notice-hub 也是同名 exe。
    """
    process = getattr(install, "process", None)
    pid = int(process.pid) if process is not None and process.poll() is None else 0
    if pid:
        try:
            subprocess.run(
                [str(install.exe), "--quit"], timeout=10, capture_output=True, creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    timeout=15, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
                )
            except (OSError, subprocess.SubprocessError) as error:
                print(f"[e2e] 强杀冷装进程 {pid} 失败：{error}", file=sys.stderr)
        _wait_port_closed(install.port, 10)
    if root_path.exists():
        try:
            shutil.rmtree(root_path)
        except OSError as error:
            print(f"[e2e] 冷装清理失败（{root_path}）：{error}", file=sys.stderr)
        if root_path.exists():
            print(f"[e2e] 冷装目录仍存在，需手动清理：{root_path}", file=sys.stderr)


def _wait_health(install: Install, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last = "未发起请求"
    while time.monotonic() < deadline:
        if install.process is not None and install.process.poll() is not None:
            raise ColdInstallError(
                f"QQ-Notice-Hub.exe 在健康检查前就退出了（exit={install.process.returncode}）；"
                f"日志：{install.install_dir / 'logs'}"
            )
        try:
            status, body = health(install.base_url, install.token, timeout=5)
            if status == 200:
                return
            last = f"HTTP {status} {body[:120]}"
        except (OSError, urllib.error.URLError) as error:
            last = str(error)
        time.sleep(0.5)
    raise ColdInstallError(
        f"{timeout:.0f}s 内 /api/health 没到 200（端口 {install.port}，最后结果：{last}）；"
        f"冷装目录：{install.root}"
    )
