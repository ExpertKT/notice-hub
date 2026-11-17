"""校园地点表。

坐标是米制平面（校区西北角为原点，x 向东、y 向南，即图片像素行号方向），
不要经纬度——排程只需要「谁离谁多远」，米制平面比经纬度快、也算得准。

内置为常州大学西太湖校区 25 栋楼（走读校园平面图，0.9 米/像素量出的楼体中心）
加 1 处图书馆点。每栋都拿高德同名点复核过，残差 16–94 米，
其中有 7 栋同时对上百度（名字优先，不按最近邻猜）。
层数取平面图标注，营业时间未知留空。

平面图上的楼号也当别名留着（J1 立德楼 / X1 马士楼 这种），
有人照着平面图上的号说话，也认得出来。

「图书馆」和「图文信息中心楼」是两处：高德两个点换算到这张平面图上
相距约 100 米，按两处登记，各占一条记录。

校准坐标可放 <数据目录>/campus_poi.json，
同 id 覆盖内置那一条，新 id 直接追加——不用改代码。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

POI_FILE = "campus_poi.json"
SCHEDULE_FILE = "campus_schedule.json"

# kind 只用来挑图标与默认停留时长，不参与判断。
DEFAULT_POIS: tuple[dict[str, Any], ...] = (
    {"id":"dorm_1","name":"1号楼","aliases":[],"zone":"生活区","x":177.3,"y":483.3,"kind":"dorm","open":"","floors":6},
    {"id":"dorm_2","name":"2号楼","aliases":[],"zone":"生活区","x":271.8,"y":483.3,"kind":"dorm","open":"","floors":6},
    {"id":"dorm_3","name":"3号楼","aliases":[],"zone":"生活区","x":177.3,"y":391.5,"kind":"dorm","open":"","floors":6},
    {"id":"dorm_4","name":"4号楼","aliases":[],"zone":"生活区","x":271.8,"y":391.5,"kind":"dorm","open":"","floors":6},
    {"id":"dorm_5","name":"5号楼","aliases":[],"zone":"生活区","x":391.5,"y":391.5,"kind":"dorm","open":"","floors":6},
    {"id":"dorm_6","name":"6号楼","aliases":[],"zone":"生活区","x":274.5,"y":292.5,"kind":"dorm","open":"","floors":6},
    {"id":"dorm_7","name":"7号楼","aliases":[],"zone":"生活区","x":388.8,"y":294.3,"kind":"dorm","open":"","floors":6},
    {"id":"dorm_8","name":"8号楼","aliases":[],"zone":"生活区","x":276.3,"y":205.2,"kind":"dorm","open":"","floors":6},
    {"id":"dorm_9","name":"9号楼","aliases":[],"zone":"生活区","x":400.5,"y":181.8,"kind":"dorm","open":"","floors":6},
    {"id":"dorm_10","name":"10号楼","aliases":[],"zone":"生活区","x":504.0,"y":184.5,"kind":"dorm","open":"","floors":6},
    {"id":"dorm_11","name":"11号楼","aliases":[],"zone":"生活区","x":501.3,"y":90.0,"kind":"dorm","open":"","floors":6},
    {"id":"canteen1","name":"第一餐厅","aliases":["一食堂","第一食堂"],"zone":"生活区","x":297.0,"y":585.0,"kind":"food","open":"","floors":2},
    {"id":"canteen2","name":"第二餐厅","aliases":["二食堂","第二食堂"],"zone":"生活区","x":397.8,"y":87.3,"kind":"food","open":"","floors":2},
    {"id":"library","name":"图书馆","aliases":["借书处","还书"],"zone":"教学区","x":563.3,"y":622.1,"kind":"library","open":"","floors":5},
    {"id":"tuwen","name":"图文信息中心楼","aliases":["图文信息楼","图文信息中心","图文楼"],"zone":"教学区","x":580.5,"y":526.5,"kind":"library","open":"","floors":5},
    {"id":"mashi","name":"马士楼","aliases":["马山楼","X1"],"zone":"教学区","x":663.3,"y":558.0,"kind":"teach","open":"","floors":5},
    {"id":"baiyun","name":"白云楼","aliases":["X2"],"zone":"教学区","x":508.5,"y":571.5,"kind":"teach","open":"","floors":5},
    {"id":"wujin","name":"武进楼","aliases":["X3"],"zone":"教学区","x":540.0,"y":639.0,"kind":"teach","open":"","floors":5},
    {"id":"tanghu","name":"汤湖楼","aliases":["滆湖楼","X4"],"zone":"教学区","x":631.8,"y":645.3,"kind":"teach","open":"","floors":5},
    {"id":"lide","name":"立德楼","aliases":["J1"],"zone":"教学区","x":430.2,"y":850.5,"kind":"teach","open":"","floors":5},
    {"id":"ligong","name":"立功楼","aliases":["J2"],"zone":"教学区","x":730.8,"y":847.8,"kind":"teach","open":"","floors":5},
    {"id":"liyan","name":"立言楼","aliases":["J3"],"zone":"教学区","x":733.5,"y":981.0,"kind":"teach","open":"","floors":5},
    {"id":"lixing","name":"立行楼","aliases":["J4"],"zone":"教学区","x":432.0,"y":1008.0,"kind":"teach","open":"","floors":5},
    {"id":"liyi","name":"立艺楼","aliases":["J5"],"zone":"文体区","x":742.5,"y":298.8,"kind":"teach","open":"","floors":4},
    {"id":"gym","name":"汤湖体育馆","aliases":["润湖体育馆","体育馆"],"zone":"文体区","x":747.0,"y":409.5,"kind":"sport","open":"","floors":3},
    {"id":"field","name":"运动场","aliases":["体育场","操场"],"zone":"文体区","x":715.5,"y":150.3,"kind":"sport","open":"","floors":0},
    # 博雅居是宿舍区的叫法，不是某一栋楼的专名，所以单独当一个片区点，
    # 不挂到 1–11 号楼任何一栋上。坐标取平面图上「博雅居 Boys Dormitory」那块牌子
    # 的落点（3/4 号楼那排宿舍的北侧）；高德的「博雅居8/9号楼」换算后落在校园东侧
    # 几百米外，与这张平面图对不上，不采用。
    {"id":"boyaju","name":"博雅居","aliases":["博雅","博雅居宿舍区"],"zone":"生活区","x":275.4,"y":349.2,"kind":"dorm","open":"","floors":0},
)

# 默认周课表：只要 <数据目录>/campus_schedule.json 存在就整份替换。
# 结构 {"lessons": [{"weekday": 1-7, "start": "08:00", "end": "09:40",
#                    "name": "高等数学", "place": "立德楼 302"}]}
DEFAULT_SCHEDULE: dict[str, Any] = {"lessons": []}


def _read_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _finite_number(value: Any, fallback: float | None) -> float | None:
    if isinstance(value, bool):
        return fallback
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return number if math.isfinite(number) else fallback


def load_pois(data_dir: str | Path | None) -> list[dict[str, Any]]:
    """内置地点表 + 用户覆盖。坏文件当不存在，不抛异常——排程不该因为一份 JSON 写错就崩。"""
    pois = [dict(item) for item in DEFAULT_POIS]
    if not data_dir:
        return pois
    custom = _read_json(Path(data_dir) / POI_FILE)
    if not isinstance(custom, list):
        return pois
    defaults = {str(item["id"]): item for item in pois}
    by_id = dict(defaults)
    for item in custom:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        poi_id = str(item["id"])
        base = defaults.get(poi_id, {})
        x = _finite_number(item.get("x"), base.get("x"))
        y = _finite_number(item.get("y"), base.get("y"))
        name = str(item.get("name") or base.get("name") or "")
        if not name or x is None or y is None:
            continue
        floors = item.get("floors")
        if type(floors) is not int or floors <= 0:
            floors = base.get("floors")  # 自定义新点未提供层数时为未知。
        # 偏移按用户校准方向：向东/向北为正；既有 x/y 和 API 仍为 x 东/y 南。
        dx = _finite_number(item.get("dx"), 0.0)
        dy = _finite_number(item.get("dy"), 0.0)
        calibrated_x = _finite_number(x + dx, x)
        calibrated_y = _finite_number(y - dy, y)
        aliases = item.get("aliases", base.get("aliases", []))
        if not isinstance(aliases, list):
            aliases = base.get("aliases", [])
        merged = {
            "id": poi_id,
            "name": name,
            "aliases": [str(a) for a in aliases],
            "zone": str(item.get("zone", base.get("zone")) or "未分区"),
            "x": calibrated_x,
            "y": calibrated_y,
            "kind": str(item.get("kind", base.get("kind")) or "other"),
            "open": str(item.get("open", base.get("open")) or ""),
            "floors": floors,
        }
        by_id[poi_id] = merged
    return list(by_id.values())


def load_schedule(data_dir: str | Path | None) -> list[dict[str, Any]]:
    """周课表。读到就用读到的，读不到就是空课表（排程会退化成「不撞课」而不是报错）。"""
    if not data_dir:
        return []
    raw = _read_json(Path(data_dir) / SCHEDULE_FILE)
    if not isinstance(raw, dict):
        return []
    lessons = raw.get("lessons")
    if not isinstance(lessons, list):
        return []
    clean: list[dict[str, Any]] = []
    for item in lessons:
        if not isinstance(item, dict):
            continue
        try:
            weekday = int(item.get("weekday"))
        except (TypeError, ValueError):
            continue
        start = str(item.get("start") or "")
        end = str(item.get("end") or "")
        if not (1 <= weekday <= 7) or not _is_clock(start) or not _is_clock(end):
            continue
        if start >= end:
            continue
        clean.append(
            {
                "weekday": weekday,
                "start": start,
                "end": end,
                "name": str(item.get("name") or ""),
                "place": str(item.get("place") or ""),
            }
        )
    return clean


def _is_clock(text: str) -> bool:
    if len(text) != 5 or text[2] != ":":
        return False
    hour, minute = text[:2], text[3:]
    return hour.isdigit() and minute.isdigit() and int(hour) < 24 and int(minute) < 60


def poi_index(pois: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """(别名, 地点) 按别名长度倒序——「11号楼」必须先于「1号楼」命中。"""
    pairs: list[tuple[str, dict[str, Any]]] = []
    for poi in pois:
        names = [str(poi.get("name") or "")] + [str(a) for a in (poi.get("aliases") or [])]
        for name in names:
            name = name.strip()
            if name:
                pairs.append((name, poi))
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    return pairs


def match_place(text: str, pois: list[dict[str, Any]]) -> dict[str, Any] | None:
    """从一句中文里认出地点。认不出就返回 None——不猜。"""
    if not text:
        return None
    haystack = str(text)
    for name, poi in poi_index(pois):
        if name in haystack:
            return poi
    return None


def route_room(text: str) -> str:
    """「武进楼 B203」→「B203」。房间号单独留着，界面上写「B203」比整句有用。"""
    parts = str(text or "").replace("，", " ").replace(",", " ").split()
    for token in parts:
        core = token.strip("()（）[]【】")
        if not core or not core.isascii():
            continue
        has_digit = any(ch.isdigit() for ch in core)
        has_alpha = any(ch.isalpha() for ch in core)
        if has_digit and (has_alpha or len(core) <= 4):
            return core
    return ""
