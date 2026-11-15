"""把演示要用的待办、课表和常做的事塞进演示数据目录。

只写 NOTICEHUB_DATA_DIR 指向的那个目录；已经有待办就什么都不做，重复执行没有副作用。
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from noticehub.bootstrap.cli import build_service  # noqa: E402
from noticehub.config import Settings  # noqa: E402
from noticehub.application.agent_plan import save_routines, save_schedule  # noqa: E402
from noticehub.application.tasks import create_task  # noqa: E402

TASKS = (
    "交班会材料 博雅居302",
    "找辅导员签字 立功楼512",
    "补办学生证 行政楼204",
    "交大学物理实验报告 图文信息楼B203",
    "取快递 菜鸟驿站",
)

WEEKDAYS = "一二三四五六日"

# 课表按「今天」写，演示哪天打开都有一节课垫着。最后一节 15:40 在立功楼下课，
# 所以行程起点自然是立功楼，不用先回宿舍。
LESSONS = (
    "08:00-09:40 高等数学 立功楼302",
    "10:00-11:40 大学物理 图文信息楼B203",
    "14:00-15:40 程序设计基础 立功楼401",
)


def main() -> int:
    env_file = os.environ.get("NOTICEHUB_ENV_FILE")
    settings = Settings.from_env(env_file=Path(env_file) if env_file else None)
    service = build_service(settings, console=False)
    store = service.store

    existing = store.list_tasks(statuses=("open",), limit=1)
    if existing:
        print("演示数据已存在，跳过")
        return 0

    for summary in TASKS:
        create_task(store, summary, "")

    day = WEEKDAYS[date.today().isoweekday() - 1]
    text = "\n".join(f"周{day} {item}" for item in LESSONS)
    save_schedule(settings, {"text": text})
    save_routines(settings, {"action": "add", "title": "第一食堂 吃饭", "minutes": 30})
    print(f"演示数据写好了：{len(TASKS)} 件待办、3 节课、1 个常做的事 → {settings.data_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
