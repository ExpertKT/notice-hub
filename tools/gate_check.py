"""推送闸：没有一次「当前提交的全量门禁绿」就不许 push。

pre-push 钩子调这个脚本（tools/install_hooks.py 装）。分档门禁的记录在
.reports/gate/，这个脚本只认 last-full.json 里的 sha —— 改了代码不重跑全量，
记录就对不上，直接拦下来。

急事要推：把 NH_GATE_BYPASS 设成 1，脚本会打印一行警告再放行（日志里留痕）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def record_path() -> Path:
    """和 tools/gate.py 用同一套解析规则：NH_GATE_DIR 优先。"""
    override = os.environ.get("NH_GATE_DIR")
    base = Path(override) if override else ROOT / ".reports" / "gate"
    return base / "last-full.json"


RECORD = record_path()


def head_sha() -> str:
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT), capture_output=True,
                         text=True)
    return (out.stdout or "").strip()


def product_tree() -> str:
    out = subprocess.run(["git", "rev-parse", "HEAD:noticehub"], cwd=str(ROOT),
                         capture_output=True, text=True)
    return (out.stdout or "").strip()


def main() -> int:
    sha = head_sha()
    reason = ""
    note = ""
    if not RECORD.exists():
        reason = "还没有全量门禁记录"
    else:
        try:
            data = json.loads(RECORD.read_text(encoding="utf-8"))
        except ValueError as exc:
            reason = "全量门禁记录坏了：%s" % exc
        else:
            if data.get("sha") != sha:
                # 只提交了文档/用例/tools 时产品树没变，那次的绿仍然作数。
                if data.get("product") and data.get("product") == product_tree():
                    note = ("最近一次全量门禁跑的是 %s，之后只改了产品以外的文件（noticehub/ 树未变）"
                            % str(data.get("sha"))[:8])
                else:
                    reason = ("最近一次全量门禁跑的是 %s，当前 HEAD 是 %s"
                              % (str(data.get("sha"))[:8], sha[:8]))
            if not reason and data.get("exit") != 0:
                reds = data.get("reds") or {}
                reason = "最近一次全量门禁没绿（%d 条红项）" % len(reds)
    if not reason:
        if note:
            print("[gate] 放行：%s" % note)
        return 0

    if os.environ.get("NH_GATE_BYPASS") == "1":
        print("[gate] 跳过检查并推送：%s（NH_GATE_BYPASS=1，请确认你确实要这样）" % reason)
        return 0

    print("[gate] 拒绝推送：%s" % reason)
    print("[gate] 先跑一次全量：& .venv\\Scripts\\python.exe -X utf8 tools\\gate.py --tier full")
    print("[gate] 确实要绕过：$env:NH_GATE_BYPASS='1' 之后再 push（会在推送日志里留痕）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
