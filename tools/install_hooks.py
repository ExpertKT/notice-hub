"""把仓库的 git 钩子指向 tools/hooks，并说明它拦什么。

    python tools/install_hooks.py            # 安装
    python tools/install_hooks.py --uninstall

钩子是本地配置（core.hooksPath），不入版本库，所以每个 clone / worktree 各装一次；
worktree 共用同一份仓库配置，装一次就够。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / "tools" / "hooks"


def main(argv: list[str]) -> int:
    if "--uninstall" in argv:
        subprocess.run(["git", "config", "--unset", "core.hooksPath"], cwd=str(ROOT))
        print("已卸载：core.hooksPath 不再指向 tools/hooks")
        return 0
    if not (HOOKS / "pre-push").exists():
        print("找不到 tools/hooks/pre-push")
        return 1
    subprocess.run(["git", "config", "core.hooksPath", "tools/hooks"], cwd=str(ROOT),
                   check=True)
    print("已安装：core.hooksPath=tools/hooks")
    print("拦住的东西：没有当前 HEAD 的全量门禁绿记录时，push 会被拒。")
    print("先跑一次：& .venv\\Scripts\\python.exe -X utf8 tools\\gate.py --tier full")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
