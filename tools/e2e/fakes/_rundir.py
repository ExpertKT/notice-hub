"""替身运行目录的统一落点：`%TEMP%\\nh-e2e-<run-id>\\<kind>`。

为什么单独一个文件：三个替身（wechat / dingtalk / push）都要落盘，规则必须只有一份。

**硬规则：运行产物绝不进仓库。** `tempfile.gettempdir()` 在受限环境（比如本项目的
沙箱会话）里会回落到当前目录＝仓库根，所以这里**不用** tempfile，只认：
`NH_E2E_RUN_DIR` → `%TEMP%`/`%TMP%` → 都没有或建不出来就报错，绝不静默回落到 repo。

给后来人：跑 E2E/自测时，每轮结束请确认
`git status --porcelain --untracked-files=all` 里没有 `nh-e2e-*` / `nh-fake-*` / `tmp*` 残留；
沙箱里 python 写不了 %TEMP% 时，用显式 `root=` 落到仓库内临时目录并在同一轮删掉。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path


def default_run_dir(kind: str, run_id: str | None = None) -> Path:
    """返回并创建 `<run>/<kind>` 目录；拿不到可用位置就抛错（不回落仓库）。"""
    base_text = os.environ.get("NH_E2E_RUN_DIR", "").strip()
    if not base_text:
        temp_text = (os.environ.get("TEMP") or os.environ.get("TMP") or "").strip()
        if not temp_text:
            raise RuntimeError(
                "没有可用的 TEMP/TMP 环境变量：替身目录必须落在仓库之外，请设 NH_E2E_RUN_DIR"
            )
        rid = run_id or os.environ.get("NH_E2E_RUN_ID") or uuid.uuid4().hex[:8]
        base_text = str(Path(temp_text) / ("nh-e2e-" + rid))
    path = Path(base_text) / kind
    ensure_outside_repo(path, Path(__file__).resolve().parents[3])
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise RuntimeError(
            "替身目录只能放 TEMP 下的 nh-e2e-<run-id>，这里建不了（%s）；"
            "请设 NH_E2E_RUN_DIR 指到可写目录" % error
        ) from error
    return path


def ensure_outside_repo(path: Path, repo_root: Path) -> None:
    """显式 root 必须在仓库之外：落在仓库里直接报错（和 harness/paths.py 同一口径）。

    以前这里是空实现，`--rundir .` 之类会把产物写进仓库；现在不靠调用方自觉。
    """
    resolved = Path(path).resolve()
    root = Path(repo_root).resolve()
    if resolved == root or root in resolved.parents:
        raise RuntimeError(
            "替身/运行产物不允许写进仓库：%s（仓库根 %s）。请用 NH_E2E_RUN_DIR 指到 %%TEMP%% 下。"
            % (resolved, root)
        )
