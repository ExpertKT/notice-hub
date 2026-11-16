"""校园地点表。

坐标是米制平面（校区西南角为原点，x 向东、y 向南），不要经纬度——
排程只需要「谁离谁多远」，米制平面比经纬度快、也算得准。

内置为常州大学西太湖校区 17 栋楼，来自赛事 campus-osm18.json 的楼体采样中心。
源坐标 x 向东、y 向北，保留西南角原点并取 y = -源 y，维持本模块 y 向南。
这是地图采样估算，不是实地测绘；层数取来源 f，营业时间未知留空。
坐标来源 © OpenStreetMap contributors，ODbL：https://www.openstreetmap.org/copyright
校准坐标可放 <数据目录>/campus_poi.json，
同 id 覆盖内置那一条，新 id 直接追加——不用改代码。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

POI_FILE = "campus_poi.json"
SCHEDULE_FILE = "campus_schedule.json"

# kind 只用来挑图标与默认停留时长，不参与判断。
DEFAULT_POIS: tuple[dict[str, Any], ...] = (
    {"id":"library","name":"图书馆","aliases":["借书处","还书"],"zone":"教学区","x":391.1,"y":-368.6,"kind":"library","open":"","floors":5},
    {"id":"liyan","name":"立言楼","aliases":[],"zone":"教学区","x":899.5,"y":-724,"kind":"teach","open":"","floors":5},
    {"id":"gym","name":"润湖体育馆","aliases":["体育馆"],"zone":"文体区","x":571.9,"y":-565,"kind":"sport","open":"","floors":3},
    {"id":"lide","name":"立德楼","aliases":[],"zone":"教学区","x":221.5,"y":-154.9,"kind":"teach","open":"","floors":5},
    {"id":"lab","name":"基础实验楼","aliases":["实验楼"],"zone":"教学区","x":488.8,"y":-113.2,"kind":"teach","open":"","floors":5},
    {"id":"dorm_a","name":"学生公寓A","aliases":[],"zone":"生活区","x":257.7,"y":-613.4,"kind":"dorm","open":"","floors":6},
    {"id":"dorm_b","name":"学生公寓B","aliases":[],"zone":"生活区","x":155.9,"y":-628.2,"kind":"dorm","open":"","floors":6},
    {"id":"canteen1","name":"一食堂","aliases":["第一食堂"],"zone":"生活区","x":132.4,"y":-409.8,"kind":"food","open":"","floors":3},
    {"id":"dorm_c","name":"学生公寓C","aliases":[],"zone":"生活区","x":288.6,"y":-743.2,"kind":"dorm","open":"","floors":6},
    {"id":"dorm_d","name":"学生公寓D","aliases":[],"zone":"生活区","x":378.2,"y":-734.8,"kind":"dorm","open":"","floors":6},
    {"id":"lixing","name":"立行楼","aliases":[],"zone":"教学区","x":214,"y":-46.7,"kind":"teach","open":"","floors":5},
    {"id":"dorm_e","name":"学生公寓E","aliases":[],"zone":"生活区","x":179.8,"y":-751.9,"kind":"dorm","open":"","floors":6},
    {"id":"lixin","name":"立信楼","aliases":[],"zone":"教学区","x":462.3,"y":-12.2,"kind":"teach","open":"","floors":5},
    {"id":"dorm_f","name":"学生公寓F","aliases":[],"zone":"生活区","x":133.6,"y":-503.1,"kind":"dorm","open":"","floors":6},
    {"id":"dorm_g","name":"学生公寓G","aliases":[],"zone":"生活区","x":39.9,"y":-516.1,"kind":"dorm","open":"","floors":6},
    {"id":"dorm_h","name":"学生公寓H","aliases":[],"zone":"生活区","x":51.3,"y":-600.1,"kind":"dorm","open":"","floors":6},
    {"id":"dorm_i","name":"学生公寓I","aliases":[],"zone":"生活区","x":185.9,"y":-5.3,"kind":"dorm","open":"","floors":6},
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


def load_pois(data_dir: str | Path | None) -> list[dict[str, Any]]:
    """内置地点表 + 用户覆盖。坏文件当不存在，不抛异常——排程不该因为一份 JSON 写错就崩。"""
    pois = [dict(item) for item in DEFAULT_POIS]
    if not data_dir:
        return pois
    custom = _read_json(Path(data_dir) / POI_FILE)
    if not isinstance(custom, list):
        return pois
    by_id = {str(item.get("id")): item for item in pois}
    for item in custom:
        if not isinstance(item, dict) or not item.get("id") or not item.get("name"):
            continue
        try:
            x = float(item.get("x"))
            y = float(item.get("y"))
        except (TypeError, ValueError):
            continue
        merged = {
            "id": str(item["id"]),
            "name": str(item["name"]),
            "aliases": [str(a) for a in (item.get("aliases") or [])],
            "zone": str(item.get("zone") or "未分区"),
            "x": x,
            "y": y,
            "kind": str(item.get("kind") or "other"),
            "open": str(item.get("open") or ""),
        }
        by_id[merged["id"]] = merged
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
    """(别名, 地点) 按别名长度倒序——「图文信息楼」必须先于「图文」命中。"""
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
    """「图文信息楼 B203」→「B203」。房间号单独留着，界面上写「B203」比整句有用。"""
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
