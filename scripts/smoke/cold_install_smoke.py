"""notice-hub 冷装冒烟脚本（每次发版复用）。

流程：解压 zip 到 %TEMP%\\nh-smoke-<guid> → 注入环境变量 → 启动实例 →
等 /health → 查版本 → 检查主页 → 停止实例 → 删除临时目录。
输出一行 PASS/FAIL（含版本、启动耗时、页面字节数）；FAIL 时写明失败步骤。
CI 可用：非交互，退出码 0=PASS / 1=FAIL。

用法：
    python scripts/smoke/cold_install_smoke.py [--zip PATH] [--port N]
        [--token TOKEN] [--timeout 60]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path

DEFAULT_ZIP = Path(__file__).resolve().parents[2] / "packaging" / "QQ-Notice-Hub.zip"
PAGE_MIN_BYTES = 100_000  # 冷装主页正常约 351 KB，明显小于此说明页面没打进包


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _force_rmtree(path: Path) -> None:
    def onerror(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    shutil.rmtree(path, onerror=onerror)


def _get(url: str, timeout: float = 5.0):
    request = urllib.request.Request(url, headers={"Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read()


class StepFailure(Exception):
    def __init__(self, step: str, detail: str) -> None:
        super().__init__(f"[{step}] {detail}")
        self.step = step
        self.detail = detail


def run_smoke(zip_path: Path, port: int, token: str, timeout: float) -> tuple[bool, str]:
    started = time.monotonic()

    def elapsed() -> str:
        return f"{time.monotonic() - started:.1f}s"

    if not zip_path.is_file():
        raise StepFailure("extract", f"zip 不存在: {zip_path}")
    sha_ok = zip_path.stat().st_size

    tmp_root = Path(tempfile.gettempdir()) / f"nh-smoke-{uuid.uuid4().hex[:8]}"
    extract_start = time.monotonic()
    try:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(tmp_root)
        for path in tmp_root.rglob("*"):
            if path.is_file():
                os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
        exe = tmp_root / "qq-live-digest.exe"
        if not exe.is_file():
            raise StepFailure("extract", f"包里没有 qq-live-digest.exe: {tmp_root}")

        env = dict(os.environ)
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env.pop(key, None)
        env["QQ_DIGEST_WEB_HOST"] = "127.0.0.1"
        env["QQ_DIGEST_WEB_PORT"] = str(port)
        env["QQ_DIGEST_WEB_TOKEN"] = token
        env["NO_PROXY"] = "127.0.0.1,localhost"

        proc = subprocess.Popen(
            [str(exe), "run"],
            cwd=str(tmp_root),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            health_url = f"http://127.0.0.1:{port}/health"
            deadline = time.monotonic() + timeout
            startup = None
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise StepFailure("health", f"进程提前退出，code={proc.returncode}")
                try:
                    status, _ = _get(health_url, timeout=2)
                    if status == 200:
                        startup = time.monotonic() - extract_start
                        break
                except OSError:
                    time.sleep(0.25)
            if startup is None:
                raise StepFailure("health", f"{timeout:.0f}s 内 /health 未就绪")

            status, body = _get(f"http://127.0.0.1:{port}/api/update?token={token}")
            if status != 200:
                raise StepFailure("version", f"/api/update HTTP {status}")
            current = json.loads(body.decode("utf-8")).get("current")
            if isinstance(current, dict):  # 部分版本字段是 {version, tag} 结构
                current = current.get("version")
            current = current or "?"

            status, page = _get(f"http://127.0.0.1:{port}/?token={token}")
            if status != 200:
                raise StepFailure("page", f"主页 HTTP {status}")
            page_bytes = len(page)
            if page_bytes < PAGE_MIN_BYTES:
                raise StepFailure("page", f"主页 {page_bytes} B < {PAGE_MIN_BYTES} B，页面疑似缺包")

            print(
                f"PASS version={current} startup={startup:.1f}s "
                f"page_bytes={page_bytes} zip_bytes={sha_ok} total={elapsed()}"
            )
            return True, current
        finally:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                proc.kill()
            proc.wait(timeout=15)
    except StepFailure:
        raise
    except Exception as exc:  # noqa: BLE001 - 冒烟脚本兜底，失败信息必须可读
        raise StepFailure("run", repr(exc)) from exc
    finally:
        _force_rmtree(tmp_root)


def main() -> int:
    parser = argparse.ArgumentParser(description="notice-hub 冷装冒烟")
    parser.add_argument("--zip", default=str(DEFAULT_ZIP), help="安装包路径")
    parser.add_argument("--port", type=int, default=0, help="端口，0=随机空闲端口")
    parser.add_argument("--token", default="smoketoken", help="注入的 web token")
    parser.add_argument("--timeout", type=float, default=60.0, help="/health 最长等待秒数")
    args = parser.parse_args()

    port = args.port or _free_port()
    try:
        ok, _ = run_smoke(Path(args.zip), port, args.token, args.timeout)
    except StepFailure as failure:
        print(f"FAIL step={failure.step} detail={failure.detail}")
        return 1
    if not ok:
        print("FAIL step=unknown")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
