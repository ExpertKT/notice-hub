"""进程日志：控制台 + 按大小轮转的文件。

部署形态是「服务进程写、启动器只读同一个文件」，所以这里不做跨进程协调，
也没有第二个写者；轮转由 handler 自己完成。

文件名是运行期契约（启动器与 doctor 都按这个名字找文件），别随手改。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

# 运维按这个格式 grep 排障，改动要单独评审。
FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

LOG_FILE_NAME = "noticehub.log"
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 5


def _silence(handler: logging.Handler) -> None:
    """摘掉旧 handler 时顺手 close；已经坏掉的 handler 不值得让启动失败。"""
    try:
        handler.close()
    except Exception:  # noqa: BLE001 - 旧句柄可能处于任意状态
        pass


def _reset_root(root: logging.Logger, level: int) -> None:
    for handler in list(root.handlers):
        root.removeHandler(handler)
        _silence(handler)
    root.setLevel(level)


def _console_handler() -> logging.Handler:
    """中文 Windows 下标准输出默认不是 UTF-8，先掰过来，掰不动也不影响写日志。"""
    stream = sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(FORMAT))
    return handler


def _file_handler(log_dir: Path) -> logging.Handler:
    handler = logging.handlers.RotatingFileHandler(
        log_dir / LOG_FILE_NAME,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(FORMAT))
    return handler


def setup_logging(log_dir: Path, *, level: int = logging.INFO, console: bool = True) -> logging.Logger:
    """把根 logger 配好并返回它；重复调用会先关掉旧 handler（进程启动早期调一次就够）。"""
    target = Path(log_dir)
    target.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    _reset_root(root, level)

    root.addHandler(_file_handler(target))
    if console:
        root.addHandler(_console_handler())

    # 第三方机器人库话太多，压到 WARNING；这条跨模块副作用必须每次都生效。
    logging.getLogger("botpy").setLevel(logging.WARNING)
    return root
