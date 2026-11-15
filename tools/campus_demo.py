"""生成演示用的行程数据与演示页。

演示页里的每个数字都来自 `plan_day` 的真实输出，页面不自己编数。
跑 `python tools/campus_demo.py --html` 会写出 docs/demo/行程演示.html。

场景取自比赛要求：手上已经有两件事（博雅居、立功楼），再说第三件在图文信息楼，
智能体要能自己认出地点、算进顺路的那一趟，并给出出发时间。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from noticehub.application.campus import DEFAULT_POIS  # noqa: E402
from noticehub.application.trips import plan_day  # noqa: E402

TODAY = date(2026, 11, 16)
NOW = datetime(2026, 11, 16, 15, 45)

# 今天周一：最后一节课 15:40 在立功楼下课，起点就该是立功楼。
LESSONS = [
    {"weekday": 1, "start": "08:00", "end": "09:40", "title": "高等数学", "place": "立功楼302"},
    {"weekday": 1, "start": "10:00", "end": "11:40", "title": "大学物理", "place": "图文信息楼B203"},
    {"weekday": 1, "start": "14:00", "end": "15:40", "title": "程序设计基础", "place": "立功楼401"},
]

ROUTINES = [{"id": "r1", "title": "第一食堂 吃饭", "place_id": "canteen1", "minutes": 30}]


def _task(task_id: str, summary: str) -> dict:
    return {"id": task_id, "summary": summary, "status": "open", "deadline": None}


BEFORE = [
    _task("t1", "交班会材料，博雅居 302"),
    _task("t2", "找辅导员签字，立功楼 512"),
    _task("t3", "补办学生证，行政楼 204"),
]

# 用户这句话里提到的两件事：图文信息楼有房间号，驿站只有楼名。
AFTER = BEFORE + [
    _task("t4", "交大学物理实验报告，图文信息楼 B203"),
    _task("t5", "取快递，菜鸟驿站"),
]


def _plan(tasks: list[dict]) -> dict:
    return plan_day(
        tasks,
        pois=DEFAULT_POIS,
        lessons=LESSONS,
        routines=ROUTINES,
        today=TODAY,
        now=NOW,
    )


def build() -> dict:
    return {
        "pois": DEFAULT_POIS,
        "lessons": LESSONS,
        "routines": ROUTINES,
        "before": _plan(BEFORE),
        "after": _plan(AFTER),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--html", action="store_true", help="写出演示页")
    parser.add_argument("--out", help="覆盖输出路径（默认 docs/demo/行程演示.html）")
    args = parser.parse_args()

    data = build()
    if not args.html:
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return

    template = (ROOT / "tools" / "demo_template.html").read_text(encoding="utf-8")
    html = template.replace("__PLAN_DATA__", json.dumps(data, ensure_ascii=False))
    out = Path(args.out) if args.out else ROOT / "docs" / "demo" / "行程演示.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
