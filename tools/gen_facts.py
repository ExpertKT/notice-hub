"""把赛事文档里会漂的数字从仓库真值算出来，写进 ``docs/contest/facts.json``。

文档里的行号、条数、字节数以前全是手抄的，改一次代码就得回头再抄一遍。这里每个值都由
脚本重算，`tools/render_docs.py` 再把文档里的 ``{{facts.xxx}}`` 换成它们。

只读仓库文件：不联网、不读用户数据目录、不改产品代码。算不出来的写 ``value: null`` 加
``why``，不猜。每条事实都带 ``source``，写清是哪份文件的哪一行、或者哪条命令算出来的。

    .venv\\Scripts\\python.exe -X utf8 tools/gen_facts.py            # 重写 facts.json
    .venv\\Scripts\\python.exe -X utf8 tools/gen_facts.py --pack X.zip  # 顺带量离线包字节数
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
OUT = ROOT / "docs" / "contest" / "facts.json"

# 演示数据里那一天。作品说明、答辩口径卡里的里程与时间都是按这天算的；固定住才能复现，
# 免得「今天有没有课」「现在几点」把数字改掉。
DEMO_DAY = dt.date(2026, 11, 18)
DEMO_CLOCK = dt.time(15, 0)

DEMO_TASKS = (
    "交班会材料 立行楼302",
    "找同学拿材料 立德楼512",
    "还书 图书馆",
    "交大学物理实验报告 武进楼B203",
    "取生活用品 1号楼",
)
# 天数偏移 + 点钟，跟 demo/seed.py 的 DEADLINES 一致。
DEMO_DEADLINES = {
    "交班会材料 立行楼302": (0, "20:00"),
    "交大学物理实验报告 武进楼B203": (1, "18:00"),
}
DEMO_LESSONS = (
    ("08:00", "09:40", "高等数学", "立行楼302"),
    ("10:00", "11:40", "大学物理", "武进楼B203"),
    ("14:00", "15:40", "程序设计基础", "立德楼401"),
)
DEMO_ROUTINES = ({"title": "第一餐厅 吃饭", "place": "第一餐厅", "minutes": 30},)

WEB_TEXT_SUFFIXES = {".html", ".js", ".json", ".css", ".webmanifest", ".svg"}
# 三方库压缩成一行，算进去只会让「代码行数」变成噪声。
WEB_SKIP = {"three.module.min.js"}


def _src(path: str, line: int | None, note: str) -> str:
    """`路径:行号 · 说明`。行号拿不到就只写路径。"""
    return "%s%s · %s" % (path, "" if line is None else ":%d" % line, note)


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _line_of(rel: str, pattern: str) -> int:
    """第一处匹配所在行号。找不到直接抛，别让 source 变成假坐标。"""
    for index, line in enumerate(_read(rel).splitlines(), start=1):
        if re.search(pattern, line):
            return index
    raise ValueError("在 %s 里找不到 %s" % (rel, pattern))


def _count_lines(paths: list[Path]) -> int:
    total = 0
    for path in paths:
        total += len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    return total


def _version() -> dict[str, dict]:
    from noticehub.version import APP_CHANNEL, APP_PATCH, APP_VERSION

    note = "APP_VERSION"
    return {
        "version": {
            "value": APP_VERSION,
            "source": _src("noticehub/version.py", _line_of("noticehub/version.py", r"^APP_VERSION\s*="), note),
        },
        "channel": {
            "value": APP_CHANNEL,
            "source": _src("noticehub/version.py", _line_of("noticehub/version.py", r"^APP_CHANNEL\s*="), "APP_CHANNEL"),
        },
        "patch": {
            "value": APP_PATCH,
            "source": _src("noticehub/version.py", _line_of("noticehub/version.py", r"^APP_PATCH\s*="), "APP_PATCH"),
        },
    }


def _tests() -> dict[str, dict]:
    files = sorted((ROOT / "tests").glob("test_*.py"))
    total = 0
    for path in files:
        total += len(re.findall(r"^\s*def test_", path.read_text(encoding="utf-8"), re.M))
    return {
        "test_defs": {
            "value": total,
            "method": "static",
            "files": len(files),
            "source": "tests/test_*.py 里 ^def test_ 的行数（静态计数，不执行用例）",
        }
    }


def _code_lines() -> dict[str, dict]:
    python = sorted((ROOT / "noticehub").rglob("*.py"))
    web = sorted(
        path
        for path in (ROOT / "noticehub" / "web_static").rglob("*")
        if path.is_file() and path.suffix in WEB_TEXT_SUFFIXES and path.name not in WEB_SKIP
    )
    return {
        "code_lines": {
            "python": {
                "value": _count_lines(python),
                "files": len(python),
                "includes_blank": True,
                "source": "noticehub/**/*.py 行数（含空行）",
            },
            "web_static": {
                "value": _count_lines(web),
                "files": len(web),
                "includes_blank": True,
                "source": "noticehub/web_static/** 的文本文件行数（含空行；不含图标 PNG 与三方库 three.module.min.js）",
            },
        }
    }


def _pois() -> dict[str, dict]:
    from noticehub.application.campus import DEFAULT_POIS, load_pois

    geo = json.loads(_read("noticehub/web_static/js/campus-geo.json"))
    placed = [item for item in geo["pois"].values() if item.get("lat") and item.get("lng")]
    return {
        "poi_count": {
            "value": len(placed),
            "source": "noticehub/web_static/js/campus-geo.json · pois 里同时有 lat/lng 的条目数",
        },
        "poi_count_builtin": {
            "value": len(DEFAULT_POIS),
            "source": _src("noticehub/application/campus.py", _line_of("noticehub/application/campus.py", r"^DEFAULT_POIS"), "内置地点表条数"),
        },
        "poi_count_loaded": {
            "value": len(load_pois(None)),
            "source": "noticehub/application/campus.py load_pois(None)·地理表合过坐标后可用地点数",
        },
    }


def _routes() -> dict[str, dict]:
    from noticehub import routes as table

    get_paths = sorted({path for path in table.GET_ROUTES if path.startswith("/api/")})
    post_paths = sorted({path for path in table.POST_ROUTES if path.startswith("/api/")})
    every = sorted(set(get_paths) | set(post_paths))
    line = _line_of("noticehub/routes.py", r"^GET_ROUTES")
    return {
        "routes": {
            "value": len(every),
            "methods": {"GET": len(get_paths), "POST": len(post_paths)},
            "list": every,
            "source": _src("noticehub/routes.py", line, "GET_ROUTES + POST_ROUTES 里 /api/ 开头的路径，去重"),
        }
    }


def _demo_tasks(today: dt.date, *, book_tomorrow: bool) -> list[dict]:
    tasks = []
    for index, summary in enumerate(DEMO_TASKS, start=1):
        offset, clock = DEMO_DEADLINES.get(summary, (None, ""))
        if book_tomorrow and summary.startswith("还书"):
            offset, clock = 1, "12:00"
        deadline = "" if offset is None else f"{(today + dt.timedelta(days=offset)).isoformat()} {clock}"
        tasks.append({"id": index, "summary": summary, "deadline": deadline, "status": "open"})
    return tasks


def _demo_lessons(today: dt.date) -> list[dict]:
    return [
        {"weekday": today.isoweekday(), "start": start, "end": end, "name": name, "place": place}
        for start, end, name, place in DEMO_LESSONS
    ]


def _demo_plan(today: dt.date, *, book_tomorrow: bool = False) -> dict:
    from noticehub.application.campus import load_pois
    from noticehub.application.trips import plan_day

    return plan_day(
        _demo_tasks(today, book_tomorrow=book_tomorrow),
        pois=load_pois(None),
        lessons=_demo_lessons(today),
        routines=list(DEMO_ROUTINES),
        today=today,
        now=dt.datetime.combine(today, DEMO_CLOCK),
    )


def _trips() -> dict[str, dict]:
    plan = _demo_plan(DEMO_DAY)
    trips = plan["trips"]
    if len(trips) != 2:
        raise ValueError("演示数据应排出 2 趟，实际 %d 趟" % len(trips))
    source = _src(
        "noticehub/application/trips.py",
        _line_of("noticehub/application/trips.py", r"^def plan_day"),
        "plan_day + demo/seed.py 的样本数据（%s %s 起排）" % (DEMO_DAY.isoformat(), DEMO_CLOCK.strftime("%H:%M")),
    )
    moved = _demo_plan(DEMO_DAY, book_tomorrow=True)
    facts = {
        "trip_count": {"value": plan["counts"]["trips"], "source": source},
        "trip_stops": {"value": plan["counts"]["stops"], "source": source},
        "trip_meters": {"value": plan["counts"]["distance_m"], "source": source},
        "trip_minutes": {"value": sum(trip["walk_minutes"] for trip in trips), "source": source},
        "trip_moved_stops": {"value": moved["counts"]["stops"], "source": source + "；还书挪到明天之后"},
        "trip_moved_meters": {"value": moved["counts"]["distance_m"], "source": source + "；还书挪到明天之后"},
        "trip_moved_minutes": {
            "value": sum(trip["walk_minutes"] for trip in moved["trips"]),
            "source": source + "；还书挪到明天之后",
        },
    }
    for index, trip in enumerate(trips, start=1):
        facts["trip%d_meters" % index] = {"value": trip["distance_m"], "source": source + "；第 %d 趟" % index}
        facts["trip%d_minutes" % index] = {"value": trip["walk_minutes"], "source": source + "；第 %d 趟" % index}
        facts["trip%d_stops" % index] = {"value": len(trip["stops"]), "source": source + "；第 %d 趟" % index}
    return facts


def _route_tools() -> dict[str, dict]:
    from noticehub.application.campus import load_pois
    from noticehub.application.route_tools import plan_route, poi_near

    pois = load_pois(None)
    tasks = _demo_tasks(DEMO_DAY, book_tomorrow=False)
    near = poi_near("立德楼", 500, pois=pois, tasks=tasks)
    route = plan_route(["图书馆", "立行楼", "武进楼"], start="立德楼", pois=pois)
    near_src = "noticehub/application/route_tools.py poi_near('立德楼', 500) + 演示待办"
    route_src = "noticehub/application/route_tools.py plan_route(['图书馆','立行楼','武进楼'], start='立德楼')"
    return {
        "poi_near_places": {"value": len(near["places"]), "source": near_src},
        "poi_near_tasks": {"value": len(near["tasks"]), "source": near_src},
        "route_meters": {"value": route["distance_m"], "source": route_src},
        "route_minutes": {"value": route["walk_minutes"], "source": route_src},
        "route_input_meters": {"value": route["input_distance_m"], "source": route_src},
        "route_saved_meters": {"value": route["saved_m"], "source": route_src},
        "route_algorithm": {"value": route["algorithm"], "source": route_src},
    }


def _android() -> dict[str, dict]:
    files = sorted(path for path in (ROOT / "android").rglob("*") if path.is_file())
    return {
        "android_files": {
            "value": len(files),
            "source": "android/**/* 文件数（工程只有骨架，没打过包）",
        }
    }


def _pack(path: Path | None) -> dict[str, dict]:
    if path is None:
        return {
            "demo_pack_bytes": {
                "value": None,
                "why": "离线包是构建产物，不进仓库；要量它得拿 --pack 指向那份 zip 重跑本脚本",
                "source": "packaging/build.ps1 产出的 群务台-演示包.zip",
            }
        }
    if not path.is_file():
        raise SystemExit("找不到离线包：%s" % path)
    return {
        "demo_pack_bytes": {
            "value": path.stat().st_size,
            "source": "实测 %s 的字节数" % path.name,
        }
    }


def build_facts(pack: Path | None = None) -> dict[str, dict]:
    """把全部事实算出来。同样的仓库永远同样的输出——测试就是靠这一点比对的。"""
    facts: dict[str, dict] = {}
    facts.update(_version())
    facts.update(_tests())
    facts.update(_code_lines())
    facts.update(_pois())
    facts.update(_routes())
    facts.update(_trips())
    facts.update(_route_tools())
    facts.update(_android())
    facts.update(_pack(pack))
    return facts


def dump(facts: dict[str, dict]) -> str:
    return json.dumps(facts, ensure_ascii=False, indent=1, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重算 docs/contest/facts.json")
    parser.add_argument("--pack", type=Path, default=None, help="离线包 zip 的路径，给了就量它的字节数")
    parser.add_argument("--out", type=Path, default=OUT, help="写到哪儿，默认 docs/contest/facts.json")
    args = parser.parse_args(argv)

    text = dump(build_facts(args.pack))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8", newline="\n")
    print("写好 %s（%d 条）" % (args.out.name, len(json.loads(text))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
