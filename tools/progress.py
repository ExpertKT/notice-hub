"""进度文件的读写助手：长会话里「读进度」不该等于「把 200 KB 读一遍」。

约定的形状：

    # 赛事进度

    ## §NOW
    - 分支 …，HEAD …
    - 当前在做：…
    - 下一步：…

    ## §RECENT
    …最近 N 行…

    ## 更早
    - 旧内容在 .reports/agent-progress-archive-*.md

用法：

    python tools/progress.py --rotate 30        # 只留最近 30 行，其余归档
    python tools/progress.py --now "第一行|第二行|第三行"
    python tools/progress.py --append "## 20:40 做了什么\\n- 验证：…"

被打断后接着干的第一步是 `--show`：只打印 §NOW + 最后 30 行，不读整份。
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / ".reports"
PROGRESS = REPORTS / "agent-progress.md"
NOW_HEADER = "## §NOW"
RECENT_HEADER = "## §RECENT"


def _read() -> str:
    return PROGRESS.read_text(encoding="utf-8") if PROGRESS.exists() else "# 赛事进度\n"


def _split(text: str) -> tuple[str, list[str]]:
    """拆成「§NOW 那一段」与「其余正文行」。"""
    lines = text.splitlines()
    head: list[str] = []
    body: list[str] = []
    in_now = False
    for line in lines:
        if line.startswith(NOW_HEADER):
            in_now = True
        elif in_now and line.startswith("## "):
            in_now = False
        if in_now:
            head.append(line)
        elif line.startswith(NOW_HEADER):
            continue
        else:
            body.append(line)
    return "\n".join(head), body


def _strip_scaffold(lines: list[str]) -> list[str]:
    drop = (NOW_HEADER, RECENT_HEADER, "## 更早", "# 赛事进度")
    return [ln for ln in lines if ln.strip() and not ln.startswith(drop)]


def render(now_lines: list[str], recent: list[str], archive_name: str) -> str:
    out = ["# 赛事进度", "", NOW_HEADER,
           "（开工先读这三行；要细节看 §RECENT，整份别读）"]
    out += ["- %s" % ln for ln in now_lines]
    out += ["", RECENT_HEADER]
    out += recent
    out += ["", "## 更早", "- %s" % archive_name]
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="进度文件助手")
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--rotate", type=int, metavar="N")
    parser.add_argument("--now", metavar="三行|用竖线分隔")
    parser.add_argument("--append", metavar="TEXT")
    args = parser.parse_args(argv)

    text = _read()
    now, body = _split(text)
    now_lines = [ln[2:].strip() for ln in now.splitlines()
                 if ln.startswith("- ")]
    keeper = _strip_scaffold(body)

    if args.show:
        print(now or "（还没有 §NOW）")
        print("\n--- 最近 30 行 ---")
        print("\n".join(keeper[-30:]))
        return 0

    if args.append:
        keeper += [""] + args.append.split("\\n")

    archive_name = "（还没归档）"
    if args.rotate:
        if len(keeper) > args.rotate:
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
            archive = REPORTS / ("agent-progress-archive-%s.md" % stamp)
            archive.write_text("# 赛事进度（归档 %s）\n\n" % stamp + "\n".join(keeper[:-args.rotate]),
                              encoding="utf-8")
            keeper = keeper[-args.rotate:]
            archive_name = archive.name
        else:
            archive_name = "（还没归档）"

    if args.now:
        now_lines = [part.strip() for part in args.now.split("|") if part.strip()]

    if not (args.show or args.rotate or args.now or args.append):
        parser.print_help()
        return 1

    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS.write_text(render(now_lines, keeper, archive_name), encoding="utf-8")
    print("已写入 %s（§NOW %d 行，§RECENT %d 行，更早：%s）"
          % (PROGRESS, len(now_lines), len(keeper), archive_name))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))