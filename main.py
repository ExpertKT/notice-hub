#!/usr/bin/env python3
"""notice-hub 的命令行入口：跑服务、跑调度、预览、自检、看统计、解析群文件。

子命令（`python main.py <子命令>`）：
    run          常驻监听、汇总、推送（服务本体）
    tick         只跑一次调度，适合交给任务计划程序
    preview      看一眼待处理消息会推什么，但不真发
    send-test    发一条测试推送，用来验证通道配好没
    doctor       自检配置、数据库、机器人凭证
    stats        消息 / 摘要 / 投递的计数
    attach-test  给一个群文件或图片，只打印解析摘要
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from noticehub.bootstrap import cli as cli_commands  # noqa: E402

# 子命令全表：(名字, 一行帮助, 处理函数, ((参数名, 关键字参数), ...))。
# 位置参数与选项在这里没有区别，谁带前导「-」谁是选项。加命令只往下面添一项，
# 拼 parser 的逻辑只有一份，别再复制粘贴第 11 遍。
_COMMANDS = (
    (
        "run", "常驻运行", cli_commands.command_run,
        (
            ("--poll", {"type": int, "default": 0, "help": "轮询秒数，默认读配置"}),
        ),
    ),
    (
        "tick", "执行一次调度", cli_commands.command_tick,
        (
            ("--no-bot", {"action": "store_true", "help": "不启动机器人，只处理库里已有消息"}),
        ),
    ),
    (
        "preview", "预览待处理消息的推送内容", cli_commands.command_preview,
        (
            ("--limit", {"type": int, "default": 0}),
        ),
    ),
    ("stats", "查看统计", cli_commands.command_stats, ()),
    (
        "show", "回看最近几条摘要及判断依据", cli_commands.command_show,
        (
            ("--limit", {"type": int, "default": 5}),
        ),
    ),
    (
        "tasks", "查看待办清单和手机访问地址", cli_commands.command_tasks,
        (
            ("--all", {"action": "store_true", "help": "包含已完成"}),
            ("--limit", {"type": int, "default": 30}),
            ("--import-existing", {
                "action": "store_true", "dest": "import_existing", "help": "从历史摘要回填待办",
            }),
            ("--days", {"type": int, "default": 7, "help": "回填最近多少天"}),
            ("--reset", {"action": "store_true", "help": "先清空待办库再回填"}),
        ),
    ),
    ("catchup", "从 NapCat 补采最近的历史群消息", cli_commands.command_catchup, ()),
    (
        "doctor", "自检", cli_commands.command_doctor,
        (
            ("--online", {"action": "store_true", "help": "在线校验机器人凭证"}),
            ("--bundle", {
                "nargs": "?", "const": "", "default": None, "metavar": "路径",
                "help": "生成诊断包 zip（可给目录或 .zip 路径；默认放桌面）",
            }),
        ),
    ),
    (
        "send-test", "发送测试推送", cli_commands.command_send_test,
        (
            ("--wait", {"type": float, "default": 25, "help": "等待机器人就绪的秒数"}),
        ),
    ),
    (
        "attach-test", "解析群文件 / 图片，只打印摘要不推送", cli_commands.command_attach_test,
        (
            ("path", {"nargs": "?", "default": "", "help": "本地文件路径（可选）"}),
            ("--group", {"default": "", "help": "群号"}),
            ("--file-id", {"default": "", "help": "群文件 file_id"}),
            ("--busid", {"default": "", "help": "群文件 busid"}),
            ("--name", {"default": "", "help": "文件名（判断类型用）"}),
        ),
    ),
)


def acquire_lock(path: Path):
    """单实例锁：避免任务计划程序与手动启动同时跑两份服务。抢不到返回 None。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    if not _try_lock(handle):
        handle.close()
        return None
    _stamp_pid(handle)
    return handle


def _try_lock(handle) -> bool:
    """试拿锁：拿到返回 True，被别人占着返回 False。"""
    try:
        _take_exclusive(handle)
    except OSError:
        return False
    return True


def _stamp_pid(handle) -> None:
    """把进程号写进锁文件，出了「到底谁占着」的问题能直接看一眼。"""
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()


def _take_exclusive(handle) -> None:
    """拿非阻塞独占锁，被别人占着就抛 OSError。POSIX 与 Windows 各用各的原语。"""
    if os.name != "nt":
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    import msvcrt

    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)


def build_parser() -> argparse.ArgumentParser:
    """按 _COMMANDS 拼出完整命令行。"""
    parser = argparse.ArgumentParser(description="notice-hub：把群通知变成待办与日历事件")
    parser.add_argument("--env", default="", help=".env 文件路径，默认项目根目录 .env")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text, handler, argument_specs in _COMMANDS:
        command = sub.add_parser(name, help=help_text)
        for argument_name, options in argument_specs:
            command.add_argument(argument_name, **options)
        command.set_defaults(func=handler)
    return parser


def _dispatch(args) -> int:
    """把解析结果交给子命令的处理函数；它返回什么就是进程退出码。"""
    if not hasattr(args, "poll"):
        args.poll = 0
    return int(args.func(args))


def main(argv: list[str] | None = None) -> int:
    cli_commands.configure_console()
    args = build_parser().parse_args(argv)
    try:
        return _dispatch(args)
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
