"""今日行程的 HTTP 面：读行程、导课表、存常用行程。

和 agent_chat 一样挂在 NOTICEHUB_AGENT_SHELL 后面，不进静态路由表——
默认打开，环境变量写成 0 才关。
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import math
import os
import re
from pathlib import Path
from typing import Any

from noticehub.application import campus_roads
from noticehub.application.agent_brief import agent_shell_enabled
from noticehub.application.campus import (
    DEFAULT_POIS,
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
ORDER_FILE = "campus_plan_order.json"
# 手排的趟：哪几站算同一趟。站序在 ORDER_FILE，趟的边界在这里——
# 只存一份扁平站序的话，把站拖到别的趟里就表达不出来，刷新又会按区拆回去。
TRIPS_FILE = "campus_plan_trips.json"
# 「我现在在哪」：标了就按这儿算起点的路线。只存一份，隔天自动作废——
# 昨天的位置留在今天会把人指到错的方向。
HERE_FILE = "campus_here.json"
# 手排顺序是一次性的：攒着只会越读越乱，留两周足够回头看一眼。
ORDER_KEEP_DAYS = 14

_WEEKDAYS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
_WEEKDAY_CHARS = "一二三四五六日"
_WEEKDAY_RE = re.compile(r"(?:周|星期|礼拜)([一二三四五六日天])")
_TIME_RE = re.compile(
    r"(\d{1,2})\s*[:：]\s*(\d{2})\s*(?:-|~|～|到|至)\s*(\d{1,2})\s*[:：]\s*(\d{2})"
)
# 楼名后面跟的房号：「立德楼302」「武进楼 B203」。房号得跟着楼走，不能留在课名里。
_ROOM_RE = re.compile(r"\s*([A-Za-z]?\d{1,4}[A-Za-z]?)(?![A-Za-z0-9])")


def optional_get(path: str) -> tuple[str, bool] | None:
    if not agent_shell_enabled():
        return None
    if path in (PLAN_PATH, SCHEDULE_PATH, ROUTINES_PATH, MAP_CONFIG_PATH):
        return ("_route_get_api_agent_plan", False)
    return None


def optional_post(path: str) -> tuple[str, bool] | None:
    if not agent_shell_enabled():
        return None
    if path in (PLAN_PATH, SCHEDULE_PATH, ROUTINES_PATH):
        return ("_route_post_api_agent_plan", False)
    if path == POI_PATH:
        return ("_route_post_api_agent_poi", False)
    return None


def _data_dir(settings: Any) -> Path:
    return Path(getattr(settings, "data_dir", "") or ".")


def today_plan(store: Any, settings: Any) -> dict[str, Any]:
    """今日行程。地点表、课表、常用行程都从数据目录读，读不到就用内置的。"""
    data_dir = _data_dir(settings)
    pois = load_pois(data_dir)
    lessons = load_schedule(data_dir)
    tasks = store.list_tasks(statuses=("open",), limit=800)
    now = now_local()
    order: list[str] = []
    groups: list[list[str]] = []
    order_warning = ""
    here: dict[str, Any] | None = None
    try:
        order = load_plan_order(data_dir, now.date())
        groups = load_plan_groups(data_dir, now.date())
        here = load_here(data_dir, now.date())
    except ValueError as error:
        order_warning = str(error)
    plan = plan_day(
        tasks,
        pois=pois,
        lessons=lessons,
        routines=[item for item in load_routines(data_dir)],
        order=order,
        groups=groups,
        # 存的是 id，排程要坐标：从地点表里取回完整那条，取不到就当没标。
        here=next((item for item in pois if item["id"] == here["id"]), None) if here else None,
        today=now.date(),
        now=now,
    )
    plan["here"] = here
    if order_warning:
        plan["warnings"].insert(0, order_warning + "，今天按远近排")
    plan["lesson_count"] = len(lessons)
    plan["poi_count"] = len(pois)
    plan["path"] = str(data_dir / POI_FILE)
    # 地图要画哪栋楼、画在哪：坐标跟着行程一起回给页面，省得前端再猜一遍。
    # src 说明这处坐标是怎么来的（osm / osm-footprint / estimated / unknown），
    # 地图据此把估算位置描成虚线。
    plan["pois"] = [
        {
            "id": item["id"],
            "name": item["name"],
            "x": item["x"],
            "y": item["y"],
            "zone": item.get("zone") or "",
            "kind": item.get("kind") or "",
            "floors": item.get("floors"),
            "src": item.get("src") or "unknown",
        }
        for item in pois
    ]
    _attach_road_lines(plan, pois)
    return plan


def _attach_road_lines(plan: dict[str, Any], pois: list[dict[str, Any]]) -> None:
    """给每一趟补一条绕楼、贴路的折线（平面图坐标），画图那边直接用。

    为什么不按楼与楼连直线：校区里楼是连片的，两点之间的直线常常从楼里穿过去，
    看着像「人能穿楼走」。折线由 `campus_roads` 算：楼块水面当障碍、路面打折，
    A* 走出来的线一定绕开楼；实在绕不通（或绕得离谱）就那一段退回直线。

    页面上还有「沿路 / 直线」两档：直线档不用这份数据，两条路都不丢。
    """
    by_id = {item["id"]: item for item in pois}
    for trip in plan.get("trips") or []:
        points: list[tuple[float, float]] = []
        start = by_id.get(str(trip.get("from_place") or ""))
        if start is not None:
            points.append((float(start["x"]), float(start["y"])))
        for stop in trip.get("stops") or []:
            place = by_id.get(str(stop.get("place_id") or ""))
            if place is not None:
                points.append((float(place["x"]), float(place["y"])))
        trip["line"] = campus_roads.road_line(points) if len(points) >= 2 else []


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
    reply = ask_vision(settings, SCHEDULE_PROMPT, data)
    if reply is None:
        # 没钥匙是演示包里最常见的一种：说清下一步，别把上游的英文错误摆给评委。
        raise ValueError("这张图没读成：模型没连上。先贴文字，或者放一把钥匙到 llm.key 再试一次。")
    return str(reply).strip()


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


# ── 手排的站序 ────────────────────────────────────────────────────────
# 拖拽排出来的顺序按天存一本小账：{"2026-11-17": ["去一食堂吃饭", "还书 图书馆"]}。
# 标题就按用户看见的原样存、原样比，不做别名匹配——排的是他自己拖出来的那几行。


def _order_path(data_dir: Path) -> Path:
    return data_dir / ORDER_FILE


def _read_orders(data_dir: Path) -> dict[str, list[str]]:
    """整本顺序账。文件不在就是空；文件在但读坏了要说清楚，不能当成「用户没排过」。"""
    path = _order_path(data_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError:
        raise ValueError("手排顺序文件坏了，先修好 %s" % path)
    except OSError as error:
        raise ValueError("读不到手排顺序文件：%s" % error)
    if not isinstance(raw, dict):
        raise ValueError("手排顺序文件坏了，先修好 %s" % path)
    orders: dict[str, list[str]] = {}
    for day, value in raw.items():
        if not isinstance(value, list):
            raise ValueError("手排顺序文件坏了，先修好 %s" % path)
        titles = [" ".join(item.split()) for item in value if isinstance(item, str)]
        titles = [title for title in titles if title]
        if titles:
            orders[str(day)] = titles
    return orders


def load_plan_order(data_dir: str | Path, day: dt.date) -> list[str]:
    """某一天的手排站序。没排过是空表；文件坏了抛 ValueError，让调用方说清楚。"""
    return list(_read_orders(Path(data_dir)).get(day.isoformat(), []))


def save_plan_order(settings: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """把用户拖出来的当天站序存下来。空 order 等于把这一天的手排清掉。

    `groups` 是同一份手排的趟边界（标题数组的数组，按趟序）：带上它，拖到别的趟里的站
    下次重排还留在那趟；不带就只按站序排，趟仍按区域自动分。
    """
    data_dir = _data_dir(settings)
    day = _parse_order_day(payload.get("date"))
    raw = payload.get("order")
    if not isinstance(raw, list):
        raise ValueError("order 要是一个标题数组")
    titles: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError("order 里只能是标题字符串")
        title = " ".join(item.split())
        if title and title not in titles:
            titles.append(title)
    title_groups = _parse_groups(payload.get("groups"))
    orders = _read_orders(data_dir)
    if titles:
        orders[day.isoformat()] = titles
    else:
        orders.pop(day.isoformat(), None)
    _write_orders(data_dir, orders, day)
    trips = _read_trips(data_dir)
    if title_groups:
        trips[day.isoformat()] = title_groups
    else:
        trips.pop(day.isoformat(), None)
    _write_book(
        data_dir,
        _trips_path(data_dir),
        trips,
        day,
        label="手排的趟",
        error_text="手排的趟没存上",
    )
    return {"ok": True, "date": day.isoformat(), "order": titles, "groups": title_groups}


def _parse_groups(raw: Any) -> list[list[str]]:
    """groups 可省。写成别的东西就明确拒绝，别丢一半进去。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("groups 要是一个标题数组的数组")
    groups: list[list[str]] = []
    for item in raw:
        if not isinstance(item, list):
            raise ValueError("groups 里每一趟都该是标题数组")
        names: list[str] = []
        for name in item:
            if not isinstance(name, str):
                raise ValueError("groups 里只能是标题字符串")
            text = " ".join(name.split())
            if text and text not in names:
                names.append(text)
        if names:
            groups.append(names)
    return groups


def _parse_order_day(value: Any) -> dt.date:
    text = str(value or "").strip()
    if not text:
        raise ValueError("要带上哪天（date=2026-11-17）")
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise ValueError("日期要写成 2026-11-17 这种格式")


def _write_orders(data_dir: Path, orders: dict[str, list[str]], day: dt.date) -> None:
    """只留最近 ORDER_KEEP_DAYS 天（窗口按今天算），再老的丢掉，别让文件无限长。"""
    _write_book(
        data_dir, _order_path(data_dir), orders, day, label="手排", error_text="手排顺序没存上"
    )


def _trips_path(data_dir: Path) -> Path:
    return data_dir / TRIPS_FILE


def _read_trips(data_dir: Path) -> dict[str, list[list[str]]]:
    """整本趟账：某一天分了哪几趟、每趟是哪几站。坏文件一样要说清楚。"""
    path = _trips_path(data_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError:
        raise ValueError("手排的趟文件坏了，先修好 %s" % path)
    except OSError as error:
        raise ValueError("读不到手排的趟文件：%s" % error)
    if not isinstance(raw, dict):
        raise ValueError("手排的趟文件坏了，先修好 %s" % path)
    trips: dict[str, list[list[str]]] = {}
    for day, value in raw.items():
        if not isinstance(value, list):
            raise ValueError("手排的趟文件坏了，先修好 %s" % path)
        groups: list[list[str]] = []
        for item in value:
            if not isinstance(item, list):
                raise ValueError("手排的趟文件坏了，先修好 %s" % path)
            names = [" ".join(name.split()) for name in item if isinstance(name, str)]
            names = [name for name in names if name]
            if names:
                groups.append(names)
        if groups:
            trips[str(day)] = groups
    return trips


def load_plan_groups(data_dir: str | Path, day: dt.date) -> list[list[str]]:
    """某一天手排的趟。没排过是空表；文件坏了抛 ValueError，让调用方说清楚。"""
    return [list(group) for group in _read_trips(Path(data_dir)).get(day.isoformat(), [])]


def _here_path(data_dir: Path) -> Path:
    return data_dir / HERE_FILE


def load_here(data_dir: str | Path, day: dt.date) -> dict[str, Any] | None:
    """今天标的「我在哪」。没标过、或标的是别的日子，都算没标。"""
    path = _here_path(Path(data_dir))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise ValueError("你的位置文件坏了，先修好 %s" % path) from None
    if not isinstance(raw, dict):
        raise ValueError("你的位置文件坏了，先修好 %s" % path)
    place_id = str(raw.get("place_id") or "")
    at = str(raw.get("at") or "")
    if not place_id or not at.startswith(day.isoformat()):
        return None
    return {"id": place_id, "name": str(raw.get("name") or ""), "at": at}


def save_here(settings: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """把「我现在在哪」记下来。写进 <数据目录>/campus_here.json，隔天自动作废。"""
    from noticehub.application.route_tools import _place  # 沿用同一套「楼名或 POI 编号」的认法

    data_dir = _data_dir(settings)
    text = " ".join(str(payload.get("place") or "").split())
    if not text:
        raise ValueError("先说你在哪栋楼，比如「我在图书馆」")
    pois = load_pois(data_dir)
    place = match_place(text, pois)
    if place is None:
        try:
            place = _place(text, pois)
        except ValueError:
            raise ValueError("没认出「%s」是校园里的哪栋楼，换个楼名或地点编号再试" % text) from None
    record = {
        "place_id": place["id"],
        "name": place["name"],
        "at": now_local().isoformat(timespec="minutes"),
    }
    path = _here_path(data_dir)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        raise ValueError("位置没存上，看看数据目录还能不能写：%s" % path) from None
    return {"ok": True, "here": record}


def _write_book(
    data_dir: Path,
    path: Path,
    book: dict[str, Any],
    day: dt.date,
    *,
    label: str,
    error_text: str,
) -> None:
    """只留最近 ORDER_KEEP_DAYS 天，再老的丢掉，别让文件无限长。

    窗口锚在今天而不是「按日期倒序取前 14 个」：后者会被别的日期挤掉刚写进去的那天，
    连用户刚拖完的那份都能丢。清空某一天的时候这天本来就没有条目，也不算「太久了」。
    """
    oldest = now_local().date() - dt.timedelta(days=ORDER_KEEP_DAYS - 1)
    if day < oldest:
        raise ValueError("%s只能留最近 %d 天，%s 太久了" % (label, ORDER_KEEP_DAYS, day.isoformat()))
    kept: dict[str, Any] = {}
    for key, value in book.items():
        try:
            stamp = dt.date.fromisoformat(key)
        except ValueError:
            continue
        if stamp >= oldest:
            kept[key] = value
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(
            json.dumps(kept, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
    except OSError as error:
        raise ValueError("%s：%s" % (error_text, error))


# ── 地图底图的 key 与自定义地点 ────────────────────────────────────────
# key 只从环境变量读，进不了仓库：页面拿到的是运行时注进来的值，
# 没有 key 就回 enabled:false，前端据此把那一档做成「可见但禁用」。
MAP_CONFIG_PATH = "/api/agent/map/config"
POI_PATH = "/api/agent/campus/poi"
AMAP_KEY_ENV = "NOTICEHUB_AMAP_JS_KEY"
AMAP_SECRET_ENV = "NOTICEHUB_AMAP_JS_SECURITY_CODE"
BAIDU_KEY_ENV = "NOTICEHUB_BAIDU_JS_AK"

POI_NAME_LIMIT = 20
# 校区范围（快照像素平面，campus-geo.json 那套坐标）；落在外面的一定是点错了。
# 数值取自校园多边形的包围盒（campus-osm.js 的 campus 环，0.9 米/像素）。
CAMPUS_BOX = (0.0, 0.0, 950.0, 1450.0)


def map_config() -> dict[str, Any]:
    def pair(key_env: str, secret_env: str = "") -> dict[str, Any]:
        key = (os.environ.get(key_env) or "").strip()
        secret = (os.environ.get(secret_env) or "").strip() if secret_env else ""
        return {"enabled": bool(key), "key": key, "secret": secret}

    return {
        "ok": True,
        "amap": pair(AMAP_KEY_ENV, AMAP_SECRET_ENV),
        "baidu": pair(BAIDU_KEY_ENV),
        "osm": {
            "enabled": True,
            "attribution": "© OpenStreetMap contributors",
            "license": "ODbL",
        },
    }


def _poi_file(data_dir: Path) -> Path:
    return data_dir / POI_FILE


def _raw_pois(data_dir: Path) -> list[dict[str, Any]]:
    """只为了「追加/删除」读写这份用户文件：内置表在代码里，不经过这里。"""
    try:
        raw = json.loads(_poi_file(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def save_poi(settings: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """在地图上自己加一处地点：写进 <数据目录>/campus_poi.json，跟内置 27 条一起参与排程。

    只写这一份文件，内置表不动——所以升级换代码时自己加的点还在。
    """
    data_dir = _data_dir(settings)
    name = " ".join(str(payload.get("name") or "").split())
    action = str(payload.get("action") or "add")
    items = _raw_pois(data_dir)
    if action == "remove":
        poi_id = str(payload.get("id") or "")
        remaining = [item for item in items if str(item.get("id") or "") != poi_id]
        if len(remaining) == len(items):
            raise ValueError("没有这一处地点")
        _write_pois(data_dir, remaining)
        return {"ok": True, "removed": poi_id, "count": len(remaining)}
    if action != "add":
        raise ValueError("action 只能是 add 或 remove")
    if not name:
        raise ValueError("先给这处地点起个名字")
    if len(name) > POI_NAME_LIMIT:
        raise ValueError("名字请控制在 %d 字以内" % POI_NAME_LIMIT)
    try:
        x = float(payload.get("x"))
        y = float(payload.get("y"))
    except (TypeError, ValueError):
        raise ValueError("要先在地图上点一下取位置") from None
    if not (math.isfinite(x) and math.isfinite(y)):
        raise ValueError("取到的位置不是有效数字，再点一次")
    left, top, right, bottom = CAMPUS_BOX
    if not (left <= x <= right and top <= y <= bottom):
        raise ValueError("这个点落在校区外面了，往校园里点")
    pois = load_pois(data_dir)
    for poi in pois:
        if poi["name"] == name:
            raise ValueError("已经有叫「%s」的地点了" % name)
    numbers = [
        int(str(item.get("id"))[7:])
        for item in items
        if str(item.get("id") or "").startswith("custom_")
        and str(item.get("id"))[7:].isdigit()
    ]
    poi_id = "custom_%d" % ((max(numbers) if numbers else 0) + 1)
    record = {
        "id": poi_id,
        "name": name,
        "aliases": [str(a) for a in (payload.get("aliases") or []) if str(a).strip()],
        "zone": "自定义",
        "x": round(x, 1),
        "y": round(y, 1),
        "kind": str(payload.get("kind") or "other"),
        "open": "",
        "floors": 0,
    }
    # 在线底图上点出来的点自带经纬度：留着备查，排程仍用米制平面（campus.py 的契约）。
    for key in ("lat", "lon", "crs"):
        if payload.get(key) not in (None, ""):
            record[key] = payload[key]
    items.append(record)
    _write_pois(data_dir, items)
    return {"ok": True, "poi": record, "count": len(items) + len(DEFAULT_POIS)}


def _write_pois(data_dir: Path, items: list[dict[str, Any]]) -> None:
    path = _poi_file(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
