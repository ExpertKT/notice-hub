"""今日行程的 HTTP 面：读行程、导课表、存常用行程。

和 agent_chat 一样挂在 NOTICEHUB_AGENT_SHELL 后面，不进静态路由表——
默认打开，环境变量写成 0 才关。
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from pathlib import Path
from typing import Any

from noticehub.application.agent_brief import agent_shell_enabled
from noticehub.application.campus import (
    POI_FILE,
    SCHEDULE_FILE,
    load_pois,
    load_schedule,
    match_place,
)
from noticehub.application.trips import plan_day
from noticehub.timeutil import now_local

PLAN_PATH = "/api/agent/plan"
SCHEDULE_PATH = "/api/agent/schedule"
ROUTINES_PATH = "/api/agent/routines"
ROUTINE_FILE = "campus_routines.json"

_WEEKDAYS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
_WEEKDAY_CHARS = "一二三四五六日"
_WEEKDAY_RE = re.compile(r"(?:周|星期|礼拜)([一二三四五六日天])")
_TIME_RE = re.compile(
    r"(\d{1,2})\s*[:：]\s*(\d{2})\s*(?:-|~|～|到|至)\s*(\d{1,2})\s*[:：]\s*(\d{2})"
)
# 楼名后面跟的房号：「立德楼302」「基础实验楼 B203」。房号得跟着楼走，不能留在课名里。
_ROOM_RE = re.compile(r"\s*([A-Za-z]?\d{1,4}[A-Za-z]?)(?![A-Za-z0-9])")


def optional_get(path: str) -> tuple[str, bool] | None:
    if not agent_shell_enabled():
        return None
    if path in (PLAN_PATH, SCHEDULE_PATH, ROUTINES_PATH):
        return ("_route_get_api_agent_plan", False)
    return None


def optional_post(path: str) -> tuple[str, bool] | None:
    if not agent_shell_enabled():
        return None
    if path in (SCHEDULE_PATH, ROUTINES_PATH):
        return ("_route_post_api_agent_plan", False)
    return None


def _data_dir(settings: Any) -> Path:
    return Path(getattr(settings, "data_dir", "") or ".")


def today_plan(store: Any, settings: Any) -> dict[str, Any]:
    """今日行程。地点表、课表、常用行程都从数据目录读，读不到就用内置的。"""
    data_dir = _data_dir(settings)
    pois = load_pois(data_dir)
    lessons = load_schedule(data_dir)
    tasks = store.list_tasks(statuses=("open",), limit=800)
    plan = plan_day(
        tasks,
        pois=pois,
        lessons=lessons,
        routines=[item for item in load_routines(data_dir)],
        today=now_local().date(),
        now=now_local(),
    )
    plan["lesson_count"] = len(lessons)
    plan["poi_count"] = len(pois)
    plan["path"] = str(data_dir / POI_FILE)
    # 地图要画哪栋楼、画在哪：坐标跟着行程一起回给页面，省得前端再猜一遍。
    plan["pois"] = [
        {
            "id": item["id"],
            "name": item["name"],
            "x": item["x"],
            "y": item["y"],
            "zone": item.get("zone") or "",
            "kind": item.get("kind") or "",
            "floors": item.get("floors"),
        }
        for item in pois
    ]
    return plan


def parse_schedule_text(text: str, pois: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """把「周一 08:00-09:40 高等数学 立德楼302」这种一行一节课的文本拆成课表。

    认不出的行原样返回在 rejected 里，让用户看见哪几行没进去——不静默丢。
    """
    lessons: list[dict[str, Any]] = []
    rejected: list[str] = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        day_match = _WEEKDAY_RE.search(line)
        time_match = _TIME_RE.search(line)
        if day_match is None or time_match is None:
            rejected.append(line)
            continue
        weekday = _WEEKDAYS[day_match.group(1)]
        start = "%02d:%02d" % (int(time_match.group(1)), int(time_match.group(2)))
        end = "%02d:%02d" % (int(time_match.group(3)), int(time_match.group(4)))
        if start >= end:
            rejected.append(line)
            continue
        rest = _TIME_RE.sub(" ", _WEEKDAY_RE.sub(" ", line)).strip()
        poi = match_place(rest, pois)
        place = ""
        name = rest
        if poi is not None:
            place = poi["name"]
            for alias in [poi["name"]] + list(poi.get("aliases") or []):
                if not alias or alias not in name:
                    continue
                at = name.index(alias)
                head, tail = name[:at], name[at + len(alias):]
                room = _ROOM_RE.match(tail)
                if room is not None:
                    # 「高等数学 立德楼302」里的 302 是房间号，跟着楼走，别留在课名里。
                    place = place + room.group(1)
                    tail = tail[room.end():]
                name = " ".join((head + " " + tail).split())
                break
        lessons.append(
            {"weekday": weekday, "start": start, "end": end, "name": name or "课", "place": place or rest}
        )
    return lessons, rejected


SCHEDULE_PROMPT = (
    "这是一张大学课程表截图。请把每一节课单独写成一行，格式严格照这个样例：\n"
    "周一 08:00-09:40 高等数学 立德楼302\n"
    "规则：星期写「周一」到「周日」；时间是开始-结束；课名和地点都照图上原字，"
    "楼名与房间号之间空一格。空格子、表头、「节次」那一列都不要输出。"
    "不要解释，不要编号，不要 Markdown。"
)
SCHEDULE_IMAGE_MIMES = ("image/png", "image/jpeg", "image/webp", "image/gif")
SCHEDULE_IMAGE_MAX_BYTES = 8 * 1024 * 1024


def _decode_schedule_image(data_url: str) -> tuple[str, bytes]:
    """把页面传上来的 data URL 拆成 (mime, 字节)。格式和体积在进模型前就挡住。"""
    text = str(data_url or "").strip()
    if not text.startswith("data:") or "," not in text:
        raise ValueError("图片要按 data URL 传（data:image/png;base64,…）")
    head, _, body = text.partition(",")
    if ";base64" not in head:
        raise ValueError("图片要 base64 编码")
    mime = head[5:].split(";")[0].strip().lower()
    if mime not in SCHEDULE_IMAGE_MIMES:
        raise ValueError("只认 PNG / JPEG / WebP / GIF，这张是 %s" % (mime or "未知格式"))
    try:
        raw = base64.b64decode("".join(body.split()), validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("图片内容坏了，base64 解不开：%s" % error) from error
    if not raw:
        raise ValueError("这张图是空的")
    if len(raw) > SCHEDULE_IMAGE_MAX_BYTES:
        raise ValueError(
            "图片 %.1f MB，超过 8 MB，先裁一下再传" % (len(raw) / 1048576)
        )
    return mime, raw


def read_schedule_image(settings: Any, data_url: str) -> str:
    """让模型把课表截图抄成「周一 08:00-09:40 高等数学 立德楼302」这种行。

    只负责抄字，不负责判断：认出来的行照样要过 parse_schedule_text，
    模型多写一句解释、少写一节课都在那一步露出来。
    """
    from noticehub.application.agent_chat import ask_vision

    mime, raw = _decode_schedule_image(data_url)
    data = "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii"))
    try:
        reply = ask_vision(settings, SCHEDULE_PROMPT, data)
    except RuntimeError as error:
        raise ValueError("读图没成功：%s" % error) from error
    return str(reply or "").strip()


def _schedule_text(lessons: list[dict[str, Any]]) -> str:
    """课表写回对话框的统一文本形状，和用户手贴的格式一模一样。"""
    return "\n".join(
        "周%s %s-%s %s %s"
        % (
            _WEEKDAY_CHARS[int(item["weekday"]) - 1],
            item["start"],
            item["end"],
            item["name"],
            item["place"],
        )
        for item in lessons
    )


def save_schedule(settings: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """三种写法：截图（只认出草稿，不落盘）、给整份 lessons、或者给一段文本让这里拆。"""
    data_dir = _data_dir(settings)
    pois = load_pois(data_dir)
    image = str(payload.get("image") or "").strip()
    if image:
        # 截图识别只回草稿：课表是排程的输入，认错一节课比认不出更麻烦，让用户在框里核对完再存。
        text = read_schedule_image(settings, image)
        lessons, rejected = parse_schedule_text(text, pois)
        if not lessons:
            snippet = " ".join(text.split())[:80] or "（空）"
            raise ValueError(
                "这张图里没认出一节课。模型回的是「%s」，换张清楚点的截图，或者直接贴文字。" % snippet
            )
        return {
            "ok": True,
            "saved": False,
            "count": len(lessons),
            "lessons": lessons,
            "rejected": rejected,
            "text": _schedule_text(lessons),
        }
    text = str(payload.get("text") or "")
    lessons = payload.get("lessons")
    rejected: list[str] = []
    if isinstance(lessons, list):
        clean = [item for item in lessons if isinstance(item, dict)]
    else:
        clean, rejected = parse_schedule_text(text, pois)
    if not clean:
        raise ValueError("没认出任何一节课，格式参考「周一 08:00-09:40 高等数学 立德楼302」")
    path = data_dir / SCHEDULE_FILE
    data_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"lessons": clean}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "lessons": len(clean), "rejected": rejected, "path": str(path)}


def schedule_payload(settings: Any) -> dict[str, Any]:
    data_dir = _data_dir(settings)
    return {
        "ok": True,
        "lessons": load_schedule(data_dir),
        "path": str(data_dir / SCHEDULE_FILE),
        "sample": "周一 08:00-09:40 高等数学 立德楼302",
    }


def _routines_path(data_dir: Path) -> Path:
    return data_dir / ROUTINE_FILE


def load_routines(data_dir: str | Path) -> list[dict[str, Any]]:
    path = _routines_path(Path(data_dir))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = raw.get("routines") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return []
    clean: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or not str(item.get("title") or "").strip():
            continue
        clean.append(
            {
                "id": str(item.get("id") or ""),
                "title": str(item["title"]),
                "place": str(item.get("place") or ""),
                "minutes": int(item.get("minutes") or 10),
            }
        )
    return clean


def save_routines(settings: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """action=add 加一条，action=remove 删一条。常用行程是用户自己攒的，不预置。"""
    data_dir = _data_dir(settings)
    pois = load_pois(data_dir)
    items = load_routines(data_dir)
    action = str(payload.get("action") or "add")
    if action == "remove":
        routine_id = str(payload.get("id") or "")
        if not routine_id:
            raise ValueError("要删哪一条")
        remaining = [item for item in items if item["id"] != routine_id]
        if len(remaining) == len(items):
            raise ValueError("没有这一条")
        items = remaining
    elif action == "add":
        title = " ".join(str(payload.get("title") or "").split())
        if not title:
            raise ValueError("先写一句要办的事")
        if len(title) > 60:
            raise ValueError("标题请控制在 60 字以内")
        place_text = str(payload.get("place") or "") or title
        poi = match_place(place_text, pois)
        if poi is None:
            raise ValueError("没认出地点，写个校园里的楼名再试")
        try:
            minutes = int(payload.get("minutes") or 10)
        except (TypeError, ValueError):
            minutes = 10
        minutes = max(5, min(120, minutes))
        numbers = [int(item["id"][1:]) for item in items if item["id"][:1] == "r" and item["id"][1:].isdigit()]
        items.append(
            {
                "id": "r%d" % ((max(numbers) if numbers else 0) + 1),
                "title": title,
                "place": poi["name"],
                "minutes": minutes,
            }
        )
    else:
        raise ValueError("action 只能是 add 或 remove")
    path = _routines_path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"routines": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "routines": items}


def routines_payload(settings: Any) -> dict[str, Any]:
    return {"ok": True, "routines": load_routines(_data_dir(settings))}
