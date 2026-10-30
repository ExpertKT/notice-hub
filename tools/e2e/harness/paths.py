"""运行产物放哪儿：一律 `%TEMP%`，而且绝不允许落进仓库。

这台机器上 `tempfile.gettempdir()` 会解析到 `F:\\qq-notice-hub`（工作区把 TEMP 指过去了），
所以不能直接用它——否则 Edge 的 profile、冷装目录、报告、截图全都会写进仓库根。
这里逐个候选目录验证：必须是真实存在、可写、且不在仓库内的目录。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

_OVERRIDE: Path | None = None


def set_work_root(path: str | os.PathLike | None) -> None:
    """显式指定产物根目录（`--work-root` / `NH_E2E_WORK_ROOT`）；仍禁止落进仓库。"""
    global _OVERRIDE
    _OVERRIDE = Path(path) if path else None


def repo_root() -> Path:
    return REPO_ROOT


def _inside_repo(path: Path) -> bool:
    return path == REPO_ROOT or REPO_ROOT in path.parents


def _writable(path: Path) -> bool:
    try:
        handle, probe = tempfile.mkstemp(prefix=".nh-e2e-probe-", dir=str(path))
    except (OSError, PermissionError):
        return False
    try:
        os.close(handle)
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass
    return True


def temp_root() -> Path:
    """返回一个可用且不在仓库内的临时根目录。"""
    if _OVERRIDE is not None:
        resolved = ensure_outside_repo(_OVERRIDE)
        resolved.mkdir(parents=True, exist_ok=True)
        if not _writable(resolved):
            raise RuntimeError(f"--work-root 目录不可写：{resolved}")
        return resolved
    candidates: list[Path] = []
    for key in ("TEMP", "TMP", "TMPDIR"):
        value = os.environ.get(key)
        if value:
            candidates.append(Path(value))
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.append(Path(local_appdata) / "Temp")
    candidates.append(Path.home() / "AppData" / "Local" / "Temp")
    candidates.append(Path("C:/Windows/Temp"))

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if _inside_repo(resolved) or not resolved.is_dir():
            continue
        if _writable(resolved):
            return resolved
    raise RuntimeError(
        "找不到仓库之外可写的临时目录（候选：%s）。\n"
        "这台机器上 .venv 的解释器被限制为只能写仓库内，所以用它跑 E2E 必然失败；"
        "请改用系统 Python（`python tools/e2e/run.py ...`）或用 --work-root <仓库外目录> 指定可写目录。\n"
        "拒绝把 E2E 产物写进 %s。" % (", ".join(str(item) for item in candidates), REPO_ROOT)
    )


def ensure_outside_repo(path: Path) -> Path:
    resolved = path.resolve()
    if _inside_repo(resolved):
        raise RuntimeError(f"E2E 运行产物不允许写进仓库：{resolved}")
    return resolved


def run_dir(run_id: str) -> Path:
    """本次运行的根目录：`NH_E2E_RUN_DIR` 优先，否则 `%TEMP%\\nh-e2e-<run-id>`。

    建不出来就抛错退出，**绝不回落成当前目录**——上次 1555 个文件进仓库就是这么来的。
    """
    override = os.environ.get("NH_E2E_RUN_DIR", "").strip()
    if override:
        path = ensure_outside_repo(Path(override))
    else:
        path = ensure_outside_repo(temp_root() / f"nh-e2e-{run_id}")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise RuntimeError(
            f"运行目录建不出来：{path}（{error}）；可用 NH_E2E_RUN_DIR 指向一个可写目录，"
            f"或用系统 Python 运行（.venv 的解释器被限制只能写仓库内）"
        ) from error
    if not _writable(path):
        raise RuntimeError(f"运行目录不可写：{path}；换成 NH_E2E_RUN_DIR 或改用一个能写盘的 Python")
    return path
