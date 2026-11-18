"""校园地点表。

坐标只认 OSM：`noticehub/web_static/js/campus-geo.json` 是落盘的那一份
（快照像素平面，0.9 米/像素，原点校区西北角，x 向东、y 向南），由
`F:\\qq-notice-hub\\nh-tmp\\ev-map\\geo\\build_geo.py` 生成，署名 © OpenStreetMap
contributors, ODbL 1.0。这里只留名字、别名、分区、层数、开放时间——不再存手摆坐标，
因为手摆坐标和真实地图差着一次 8.9° 旋转，两套点放一起就是「水上乐园」。

每处地点的 `src` 说明坐标是怎么来的：
  osm            OSM 上有名字的楼面，质心直接当点位
  osm-footprint  OSM 上有面但没名字，按平面图换算点配上号
  estimated      OSM 没面，取平面图坐标经「六栋同名楼拟合」的相似变换换算（约 ±20 米）
  unknown        连换算都做不了，目前没有

内置为常州大学西太湖校区 27 处地点：11 栋宿舍（1–11 号楼）、2 个餐厅、
图书馆、图文信息中心楼、8 栋教学（马士、白云、武进、汤湖、立德、立功、立言、立行）、
立艺楼、汤湖体育馆、运动场，外加博雅居这片宿舍区的一个片区点。

平面图上的楼号也当别名留着（J1 立德楼 / X1 马士楼 这种），
有人照着平面图上的号说话，也认得出来。

「图书馆」和「图文信息中心楼」是两处：OSM 上也是两个点，按两处登记，各占一条记录。

校准坐标可放 <数据目录>/campus_poi.json，
同 id 覆盖内置那一条，新 id 直接追加——不用改代码。那里的 x/y 跟地图同一个平面。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

POI_FILE = "campus_poi.json"
# 对内置 27 条的改动（改名/挪位置/隐藏），只存被改过的字段，读出时叠上去。
# 内置原始值在 DEFAULT_POIS，不被覆盖——删掉这份文件就回到出厂。
OVERRIDE_FILE = "campus_poi_override.json"
# 换校区时自带的整份地点表（与 campus_poi.json 同格式）。有就用它当基线，
# 否则回落 DEFAULT_POIS。seed 里的 id 允许沿用内置 id，因为 override 是按 id 叠的。
SEED_FILE = "campus_poi_seed.json"
SCHEDULE_FILE = "campus_schedule.json"
GEO_FILE = Path(__file__).resolve().parent.parent / "web_static" / "js" / "campus-geo.json"

# kind 只用来挑图标与默认停留时长，不参与判断。坐标见 campus-geo.json。
DEFAULT_POIS: tuple[dict[str, Any], ...] = (
    {"id":"dorm_1","name":"1号楼","aliases":[],"zone":"生活区","kind":"dorm","open":"","floors":6},
    {"id":"dorm_2","name":"2号楼","aliases":[],"zone":"生活区","kind":"dorm","open":"","floors":6},
    {"id":"dorm_3","name":"3号楼","aliases":[],"zone":"生活区","kind":"dorm","open":"","floors":6},
    {"id":"dorm_4","name":"4号楼","aliases":[],"zone":"生活区","kind":"dorm","open":"","floors":6},
    {"id":"dorm_5","name":"5号楼","aliases":[],"zone":"生活区","kind":"dorm","open":"","floors":6},
    {"id":"dorm_6","name":"6号楼","aliases":[],"zone":"生活区","kind":"dorm","open":"","floors":6},
    {"id":"dorm_7","name":"7号楼","aliases":[],"zone":"生活区","kind":"dorm","open":"","floors":6},
    {"id":"dorm_8","name":"8号楼","aliases":[],"zone":"生活区","kind":"dorm","open":"","floors":6},
    {"id":"dorm_9","name":"9号楼","aliases":[],"zone":"生活区","kind":"dorm","open":"","floors":6},
    {"id":"dorm_10","name":"10号楼","aliases":[],"zone":"生活区","kind":"dorm","open":"","floors":6},
    {"id":"dorm_11","name":"11号楼","aliases":[],"zone":"生活区","kind":"dorm","open":"","floors":6},
    {"id":"canteen1","name":"第一餐厅","aliases":["一食堂","第一食堂"],"zone":"生活区","kind":"food","open":"","floors":2},
    {"id":"canteen2","name":"第二餐厅","aliases":["二食堂","第二食堂"],"zone":"生活区","kind":"food","open":"","floors":2},
    {"id":"library","name":"图书馆","aliases":["借书处","还书"],"zone":"教学区","kind":"library","open":"","floors":5},
    {"id":"tuwen","name":"图文信息中心楼","aliases":["图文信息楼","图文信息中心","图文楼"],"zone":"教学区","kind":"library","open":"","floors":5},
    {"id":"mashi","name":"马士楼","aliases":["马山楼","X1"],"zone":"教学区","kind":"teach","open":"","floors":5},
    {"id":"baiyun","name":"白云楼","aliases":["X2"],"zone":"教学区","kind":"teach","open":"","floors":5},
    {"id":"wujin","name":"武进楼","aliases":["X3"],"zone":"教学区","kind":"teach","open":"","floors":5},
    {"id":"tanghu","name":"汤湖楼","aliases":["滆湖楼","X4"],"zone":"教学区","kind":"teach","open":"","floors":5},
    {"id":"lide","name":"立德楼","aliases":["J1"],"zone":"教学区","kind":"teach","open":"","floors":5},
    {"id":"ligong","name":"立功楼","aliases":["J2"],"zone":"教学区","kind":"teach","open":"","floors":5},
    {"id":"liyan","name":"立言楼","aliases":["J3"],"zone":"教学区","kind":"teach","open":"","floors":5},
    {"id":"lixing","name":"立行楼","aliases":["J4"],"zone":"教学区","kind":"teach","open":"","floors":5},
    {"id":"liyi","name":"立艺楼","aliases":["J5"],"zone":"文体区","kind":"teach","open":"","floors":4},
    {"id":"gym","name":"汤湖体育馆","aliases":["润湖体育馆","体育馆"],"zone":"文体区","kind":"sport","open":"","floors":3},
    {"id":"field","name":"运动场","aliases":["体育场","操场"],"zone":"文体区","kind":"sport","open":"","floors":0},
    # 博雅居是宿舍区的叫法，不是某一栋楼的专名，所以单独当一个片区点，
    # 不挂到 1–11 号楼任何一栋上。
    {"id":"boyaju","name":"博雅居","aliases":["博雅","博雅居宿舍区"],"zone":"生活区","kind":"dorm","open":"","floors":0},
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


def _geo_table() -> dict[str, Any]:
    """OSM 落盘的坐标表。读不到就当空表：楼画不出来，但不退回手摆坐标。"""
    raw = _read_json(GEO_FILE)
    if not isinstance(raw, dict):
        return {}
    pois = raw.get("pois")
    return pois if isinstance(pois, dict) else {}


def _xy_spot(item: dict[str, Any]) -> dict[str, Any] | None:
    """seed 条目自带 x/y（campus_poi.json 格式）→ 凑成 _placed 认的 geo 行。"""
    x = _finite_number(item.get("x"), None)
    y = _finite_number(item.get("y"), None)
    if x is None or y is None:
        return None
    return {"px": [x, y], "lng": item.get("lng"), "lat": item.get("lat"), "src": item.get("src")}


def _placed(item: dict[str, Any], spot: Any) -> dict[str, Any] | None:
    """内置条目 + geo 表里那行 → 一条带坐标的地点。没有坐标就是 None。"""
    if not isinstance(spot, dict):
        return None
    px = spot.get("px")
    if not isinstance(px, list) or len(px) != 2:
        return None
    x = _finite_number(px[0], None)
    y = _finite_number(px[1], None)
    if x is None or y is None:
        return None
    row = dict(item)
    row["x"] = x
    row["y"] = y
    row["lng"] = _finite_number(spot.get("lng"), None)
    row["lat"] = _finite_number(spot.get("lat"), None)
    row["src"] = str(spot.get("src") or "unknown")
    return row


def _read_override(data_dir: Path) -> dict[str, dict[str, Any]]:
    """读内置点的改动。坏文件/不是对象就当没改过，绝不抛——地图不能被一份配置写坏拖垮。"""
    raw = _read_json(Path(data_dir) / OVERRIDE_FILE)
    if not isinstance(raw, dict):
        return {}
    overrides: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        clean: dict[str, Any] = {}
        if isinstance(value.get("name"), str) and value["name"].strip():
            clean["name"] = " ".join(value["name"].split())
        for axis in ("x", "y"):
            number = _finite_number(value.get(axis), None)
            if number is not None:
                clean[axis] = number
        if isinstance(value.get("hidden"), bool):
            clean["hidden"] = value["hidden"]
        if clean:
            overrides[str(key)] = clean
    return overrides


def _baseline(data_dir: str | Path | None) -> tuple[dict[str, Any], ...]:
    """基线地点表：自带 campus_poi_seed.json 优先，否则回落内置 DEFAULT_POIS。

    seed 与 campus_poi.json 同格式，条目自带 px 坐标（不依赖 geo 表，换校区时
    geo 表里没有新校区的点）。文件缺、写坏或不是数组一律当不存在。
    """
    seed = _read_json(Path(data_dir) / SEED_FILE) if data_dir else None
    if isinstance(seed, list):
        rows = [item for item in seed if isinstance(item, dict) and item.get("id")]
        if rows:
            return tuple(rows)
    return DEFAULT_POIS


def load_pois(data_dir: str | Path | None) -> list[dict[str, Any]]:
    """自带/内置地点表 + 用户覆盖。坏文件当不存在，不抛异常——排程不该因为一份 JSON 写错就崩。

    geo 表里没有坐标的地点不进这份表：排程和画图都得有位置，缺一个数的点混进来
    只会在别处炸成 None。真缺了，看 geo 表就知道少谁。
    """
    geo = _geo_table()
    overrides = _read_override(Path(data_dir)) if data_dir else {}
    pois = []
    for item in _baseline(data_dir):
        # geo 表优先（内置点的坐标来自落盘 OSM）；seed 条目自带 x/y，没有 geo 行就用它。
        spot = geo.get(str(item["id"])) or _xy_spot(item)
        row = _placed(item, spot)
        if row is None:
            continue
        override = overrides.get(str(item["id"]))
        if override:
            # 只叠被改过的字段，其余留在出厂值上；隐藏的不进表，排程和地图都看不见。
            if "hidden" in override:
                continue
            row.update({key: override[key] for key in ("name", "x", "y") if key in override})
        pois.append(row)
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
        if item.get("hidden") is True:
            continue  # 自己点的地方也能隐藏：不在排程和地图里出现，文件里留着。
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
            # 自己在地图上点的位置：老 id 沿用 geo 表里的经纬度，新 id 只有平面坐标。
            "lng": _finite_number(item.get("lng"), base.get("lng")),
            "lat": _finite_number(item.get("lat"), base.get("lat")),
            "src": str(item.get("src") or base.get("src") or "custom"),
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
