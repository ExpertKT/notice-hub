#!/usr/bin/env python3
"""qq-live-digest 命令行入口。

常用：
    python main.py run            # 常驻监听 + 汇总 + 推送
    python main.py tick           # 只跑一次调度（适合任务计划程序定时调用）
    python main.py preview        # 预览待处理消息会推什么，不发送
    python main.py send-test      # 发送一条测试推送，验证通道
    python main.py doctor         # 自检配置、数据库、机器人凭证
    python main.py stats          # 查看消息/摘要/投递统计
    python main.py attach-test X  # 解析一个群文件/图片，只打印摘要不推送
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from noticehub.bootstrap.cli import (  # noqa: E402
    command_attach_test,
    command_catchup,
    command_doctor,
    command_preview,
    command_run,
    command_send_test,
    command_show,
    command_stats,
    command_tasks,
    command_tick,
    configure_console,
)


def acquire_lock(path: Path):
    """单实例锁：避免任务计划程序与手动启动同时跑两份服务。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="notice-hub：把群通知变成待办与日历事件")
    parser.add_argument("--env", default="", help=".env 文件路径，默认项目根目录 .env")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="常驻运行")
    run.add_argument("--poll", type=int, default=0, help="轮询秒数，默认读配置")
    run.set_defaults(func=command_run)

    tick = sub.add_parser("tick", help="执行一次调度")
    tick.add_argument("--no-bot", action="store_true", help="不启动机器人，只处理库里已有消息")
    tick.set_defaults(func=command_tick)

    preview = sub.add_parser("preview", help="预览待处理消息的推送内容")
    preview.add_argument("--limit", type=int, default=0)
    preview.set_defaults(func=command_preview)

    stats = sub.add_parser("stats", help="查看统计")
    stats.set_defaults(func=command_stats)

    show = sub.add_parser("show", help="回看最近几条摘要及判断依据")
    show.add_argument("--limit", type=int, default=5)
    show.set_defaults(func=command_show)

    tasks = sub.add_parser("tasks", help="查看待办清单和手机访问地址")
    tasks.add_argument("--all", action="store_true", help="包含已完成")
    tasks.add_argument("--limit", type=int, default=30)
    tasks.add_argument(
        "--import-existing", action="store_true", dest="import_existing", help="从历史摘要回填待办"
    )
    tasks.add_argument("--days", type=int, default=7, help="回填最近多少天")
    tasks.add_argument("--reset", action="store_true", help="先清空待办库再回填")
    tasks.set_defaults(func=command_tasks)

    catchup = sub.add_parser("catchup", help="从 NapCat 补采最近的历史群消息")
    catchup.set_defaults(func=command_catchup)

    doctor = sub.add_parser("doctor", help="自检")
    doctor.add_argument("--online", action="store_true", help="在线校验机器人凭证")
    doctor.add_argument(
        "--bundle",
        nargs="?",
        const="",
        default=None,
        metavar="路径",
        help="生成诊断包 zip（可给目录或 .zip 路径；默认放桌面）",
    )
    doctor.set_defaults(func=command_doctor)

    send_test = sub.add_parser("send-test", help="发送测试推送")
    send_test.add_argument("--wait", type=float, default=25, help="等待机器人就绪的秒数")
    send_test.set_defaults(func=command_send_test)

    attach = sub.add_parser("attach-test", help="解析群文件 / 图片，只打印摘要不推送")
    attach.add_argument("path", nargs="?", default="", help="本地文件路径（可选）")
    attach.add_argument("--group", default="", help="群号")
    attach.add_argument("--file-id", default="", help="群文件 file_id")
    attach.add_argument("--busid", default="", help="群文件 busid")
    attach.add_argument("--name", default="", help="文件名（判断类型用）")
    attach.set_defaults(func=command_attach_test)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "poll"):
        args.poll = 0
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
