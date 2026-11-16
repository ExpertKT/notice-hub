"""把演示要用的待办、课表和常做的事塞进演示数据目录。

只写 NOTICEHUB_DATA_DIR 指向的那个目录；已经有待办就什么都不做，重复执行没有副作用。
"""
from __future__ import annotations

import json
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
    "交班会材料 立行楼302",
    "找同学拿材料 立德楼512",
    "还书 图书馆",
    "交大学物理实验报告 基础实验楼B203",
    "取生活用品 学生公寓A",
)

WEEKDAYS = "一二三四五六日"

# 演示地点表，写进 <数据目录>/campus_poi.json（内置 17 栋按 id 合并，见 campus.load_pois）。
# 内置表里没有用户场景点名的这三个楼：博雅居是宿舍区的叫法，挂到内置学生公寓A 上当别名，
# 原地复用那一栋，不另画一栋压在上面；立功楼、图文信息楼查不到权威坐标，
# 摆在南、东两处教学区空白位置（最近楼 154／165 米），位置是示意值，不是测绘结果。
DEMO_POIS: tuple[dict, ...] = (
    {"id": "dorm_a", "aliases": ["博雅居", "博雅"]},
    {"id": "gongli", "name": "立功楼", "aliases": ["立功"], "zone": "教学区",
     "x": 100.0, "y": -250.0, "kind": "teach", "floors": 6},
    {"id": "tuwen", "name": "图文信息楼", "aliases": ["图文楼", "信息楼", "图文"], "zone": "教学区",
     "x": 650.0, "y": -150.0, "kind": "library", "floors": 5},
)
POI_FILE = "campus_poi.json"

# 课表按「今天」写，演示哪天打开都有课。最后一节 15:40 在立德楼下课。
# 以下任务和房号是演示样本，不表示这些楼里的真实办事窗口。
LESSONS = (
    "08:00-09:40 高等数学 立行楼302",
    "10:00-11:40 大学物理 基础实验楼B203",
    "14:00-15:40 程序设计基础 立德楼401",
)


def write_pois(data_dir: str | Path) -> Path:
    """写演示地点表。整份覆盖：重复执行结果一样，不会越跑越多。"""
    path = Path(data_dir) / POI_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(list(DEMO_POIS), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> int:
    env_file = os.environ.get("NOTICEHUB_ENV_FILE")
    settings = Settings.from_env(env_file=Path(env_file) if env_file else None)
    service = build_service(settings, console=False)
    store = service.store
    write_pois(settings.data_dir)

    existing = store.list_tasks(statuses=("open",), limit=1)
    if existing:
        print("演示数据已存在，跳过")
        return 0

    for summary in TASKS:
        create_task(store, summary, "")

    day = WEEKDAYS[date.today().isoweekday() - 1]
    text = "\n".join(f"周{day} {item}" for item in LESSONS)
    save_schedule(settings, {"text": text})
    save_routines(settings, {"action": "add", "title": "一食堂 吃饭", "minutes": 30})
    print(f"演示数据写好了：{len(TASKS)} 件待办、3 节课、1 个常做的事 → {settings.data_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
