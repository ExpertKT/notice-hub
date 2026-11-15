"""校园地点表。

坐标是米制平面（校区西南角为原点，x 向东、y 向南），不要经纬度——
排程只需要「谁离谁多远」，米制平面比经纬度快、也算得准。

内置这份是占位值：楼名取自校区实际楼名，坐标按相对方位估的，只保证
「谁在东谁在西、大致几公里」不出错。真实坐标放 <数据目录>/campus_poi.json，
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
    # ── 教学区 ────────────────────────────────────────────────
    {"id": "boyaju", "name": "博雅居", "aliases": ["博雅楼", "博雅"], "zone": "教学区",
     "x": 0, "y": 0, "kind": "teach", "open": "07:00-22:00"},
    {"id": "ligonglou", "name": "立功楼", "aliases": ["立功"], "zone": "教学区",
     "x": -260, "y": 120, "kind": "teach", "open": "07:00-22:00"},
    {"id": "tuwen", "name": "图文信息楼", "aliases": ["图文信息中心", "图文"], "zone": "教学区",
     "x": 300, "y": -40, "kind": "teach", "open": "07:00-22:00"},
    {"id": "library", "name": "图书馆", "aliases": ["图书馆", "借书处", "还书"], "zone": "教学区",
     "x": 380, "y": 90, "kind": "library", "open": "08:00-22:00"},
    {"id": "admin", "name": "行政楼", "aliases": ["行政楼", "教务处"], "zone": "教学区",
     "x": 200, "y": -160, "kind": "office", "open": "08:30-17:00"},
    {"id": "lab", "name": "实验楼", "aliases": ["实验楼", "实验室"], "zone": "教学区",
     "x": 240, "y": 30, "kind": "teach", "open": "07:00-22:00"},
    # ── 生活区 ────────────────────────────────────────────────
    {"id": "canteen1", "name": "第一食堂", "aliases": ["一食堂", "第一食堂", "食堂"], "zone": "生活区",
     "x": 60, "y": 320, "kind": "food", "open": "06:30-20:30"},
    {"id": "canteen2", "name": "第二食堂", "aliases": ["二食堂", "第二食堂"], "zone": "生活区",
     "x": 420, "y": 360, "kind": "food", "open": "06:30-20:30"},
    {"id": "market", "name": "超市", "aliases": ["超市", "教育超市", "买东西"], "zone": "生活区",
     "x": 170, "y": 366, "kind": "shop", "open": "08:00-22:30"},
    {"id": "cainiao", "name": "菜鸟驿站", "aliases": ["菜鸟", "驿站", "快递", "取件"], "zone": "生活区",
     "x": 300, "y": 400, "kind": "parcel", "open": "08:00-20:00"},
    {"id": "dorm1", "name": "一号宿舍楼", "aliases": ["1号楼", "一号楼", "宿舍"], "zone": "生活区",
     "x": -120, "y": 700, "kind": "dorm", "open": ""},
    {"id": "dorm2", "name": "二号宿舍楼", "aliases": ["2号楼", "二号楼"], "zone": "生活区",
     "x": 60, "y": 720, "kind": "dorm", "open": ""},
    {"id": "dorm3", "name": "三号宿舍楼", "aliases": ["3号楼", "三号楼"], "zone": "生活区",
     "x": 200, "y": 700, "kind": "dorm", "open": ""},
    {"id": "hospital", "name": "校医院", "aliases": ["校医院", "医务室"], "zone": "生活区",
     "x": 520, "y": -220, "kind": "health", "open": "08:00-17:00"},
    # ── 文体区 ────────────────────────────────────────────────
    {"id": "gym", "name": "体育馆", "aliases": ["体育馆", "球馆"], "zone": "文体区",
     "x": 620, "y": 200, "kind": "sport", "open": "08:00-21:30"},
    {"id": "stadium", "name": "田径场", "aliases": ["操场", "田径场", "跑道"], "zone": "文体区",
     "x": 700, "y": 420, "kind": "sport", "open": "06:00-22:00"},
    {"id": "activity", "name": "大学生活动中心", "aliases": ["活动中心", "大活"], "zone": "文体区",
     "x": 250, "y": 240, "kind": "hall", "open": "08:00-21:00"},
    {"id": "studio", "name": "琴房", "aliases": ["琴房"], "zone": "文体区",
     "x": 150, "y": 190, "kind": "hall", "open": "08:00-21:00"},
    # ── 门 ────────────────────────────────────────────────────
    {"id": "gate_south", "name": "南门", "aliases": ["南门", "正门"], "zone": "校门",
     "x": 260, "y": 520, "kind": "gate", "open": ""},
    {"id": "gate_north", "name": "北门", "aliases": ["北门"], "zone": "校门",
     "x": -80, "y": -240, "kind": "gate", "open": ""},
)

# 默认周课表：只要 <数据目录>/campus_schedule.json 存在就整份替换。
# 结构 {"lessons": [{"weekday": 1-7, "start": "08:00", "end": "09:40",
#                    "name": "高等数学", "place": "立功楼 302"}]}
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
