"""日志：控制台 + 按大小轮转的文件。

只有服务进程（`main.py`）写 `qq-live-digest.log`；启动器只读它的末尾几行做报错提示，
自己写的是 `launcher.log`。所以这里用 `RotatingFileHandler` 是安全的——同一个文件没有
第二个进程在写，轮转时改名不会被别的句柄挡住。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def setup_logging(log_dir: Path, *, level: int = logging.INFO, console: bool = True) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001
            pass

    formatter = logging.Formatter(FORMAT)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "qq-live-digest.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if console:
        if hasattr(sys.stdout, "reconfigure"):
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        root.addHandler(stream)

    # qq-botpy 自带日志很吵，保留 warning 以上
    logging.getLogger("botpy").setLevel(logging.WARNING)
    return root
