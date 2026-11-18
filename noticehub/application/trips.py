"""行程：把散着的事按地点聚成「一次出门」。

这一层刻意不碰模型。地点识别有规则兜底，聚类、排序、步行时间、截止校验
全是本地计算，所以模型连不上时行程依旧排得出来——界面上唯一受影响的是
对话那一栏，不是行程本身。

输入都是普通 dict（待办来自 Store，地点来自 campus.py），输出也是 dict，
方便直接丢给 HTTP 层。
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any, Iterable, Sequence

from noticehub.application.campus import match_place, route_room

WALK_M_PER_MIN = 70.0      # 含进楼、找教室、等电梯，比纯步行速度略保守
DWELL_MIN = 10             # 每件事在楼里停留的默认分钟数
DAY_START = "08:00"
DAY_END = "19:00"
MIN_WINDOW_MIN = 25        # 短于这个数的空隙不值得专门跑一趟

_FINISHED = frozenset({"done", "dismissed", "expired"})


def _clock_minutes(text: str) -> int:
    return int(text[:2]) * 60 + int(text[3:])


def _minutes_clock(minutes: int) -> str:
    minutes = max(0, min(24 * 60 - 1, int(minutes)))
    return "%02d:%02d" % (minutes // 60, minutes % 60)


def _parse_deadline(value: Any) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _distance(a: dict[str, Any], b: dict[str, Any]) -> float:
    return math.hypot(float(a["x"]) - float(b["x"]), float(a["y"]) - float(b["y"]))


def _walk_minutes(metres: float, speed: float = WALK_M_PER_MIN) -> int:
    if metres <= 0:
        return 0
    return max(1, int(math.ceil(metres / max(1.0, speed))))


def free_windows(lessons: Sequence[dict[str, Any]], day_end: str = DAY_END) -> list[dict[str, Any]]:
    """今天的可用时间窗：把课表挖掉之后剩下的整段空隙。

    没有课表就等于整天可用——排程会退化成「不撞课」而不是报错。
    返回 [{"start": "09:40", "end": "12:00", "minutes": 140}, ...]
    """
    cursor = _clock_minutes(DAY_START)
    limit = _clock_minutes(day_end)
    windows: list[dict[str, Any]] = []
    for lesson in sorted(lessons, key=lambda item: item["start"]):
        begin = _clock_minutes(lesson["start"])
        finish = _clock_minutes(lesson["end"])
        if begin - cursor >= MIN_WINDOW_MIN:
            windows.append(_window(cursor, begin))
        cursor = max(cursor, finish)
    if limit - cursor >= MIN_WINDOW_MIN:
        windows.append(_window(cursor, limit))
    return windows


def _window(start: int, end: int) -> dict[str, Any]:
    return {"start": _minutes_clock(start), "end": _minutes_clock(end), "minutes": end - start}


def last_lesson_place(lessons: Sequence[dict[str, Any]], pois: list[dict[str, Any]]) -> dict[str, Any] | None:
    """起点 = 今天最后一节课的楼。下课直接走，比回宿舍再出发省一趟。"""
    for lesson in sorted(lessons, key=lambda item: item["start"], reverse=True):
        place = match_place(str(lesson.get("place") or ""), pois)
        if place:
            return place
    return None


def _poi_open(poi: dict[str, Any]) -> tuple[int, int] | None:
    text = str(poi.get("open") or "")
    if len(text) != 11 or text[5] != "-":
        return None
    try:
        return _clock_minutes(text[:5]), _clock_minutes(text[6:])
    except ValueError:
        return None


def _order_stops(stops: list[dict[str, Any]], origin: dict[str, Any] | None) -> list[dict[str, Any]]:
    """最近邻排序：从起点出发，每次去剩下的最近一个。人少点也少，够用。"""
    remaining = list(stops)
    ordered: list[dict[str, Any]] = []
    cursor = origin
    while remaining:
        if cursor is None:
            nxt = remaining.pop(0)
        else:
            nxt = min(remaining, key=lambda item: _distance(cursor, item["place"]))
            remaining.remove(nxt)
        ordered.append(nxt)
        cursor = nxt["place"]
    return ordered


def _apply_manual_order(
    stops: list[dict[str, Any]], origin: dict[str, Any] | None, order: Sequence[str]
) -> list[dict[str, Any]]:
    """用户手排的站序优先。

    标题按原样比（不猜别名）；名单里已经不存在的标题自然匹配不上，直接跳过不报错；
    没被排到的（新加的、别的日期排过的）按原来的最近邻补在后面，起点接着最后一站。
    """
    rank: dict[str, int] = {}
    for index, title in enumerate(order or ()):
        rank.setdefault(str(title), index)
    if not rank:
        return _order_stops(stops, origin)
    pinned = [stop for stop in stops if str(stop["title"]) in rank]
    rest = [stop for stop in stops if str(stop["title"]) not in rank]
    pinned.sort(key=lambda stop: rank[str(stop["title"])])
    tail = _order_stops(rest, pinned[-1]["place"] if pinned else origin)
    return pinned + tail


def _collect_candidates(
    tasks: Iterable[dict[str, Any]], pois: list[dict[str, Any]], now: dt.datetime, today: dt.date
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    usable: list[dict[str, Any]] = []
    unscheduled: list[dict[str, Any]] = []
    for task in tasks:
        if str(task.get("status") or "") in _FINISHED or task.get("done"):
            continue
        summary = str(task.get("summary") or "").strip()
        if not summary:
            continue
        deadline = _parse_deadline(task.get("deadline"))
        if deadline and deadline.date() > today:
            unscheduled.append({
                "task_id": task.get("id"),
                "title": summary,
                "reason": "安排在 %s，不排入今天的行程" % deadline.date().isoformat(),
                "deadline": str(task.get("deadline") or ""),
            })
            continue
        place = match_place(summary, pois)
        if place is None:
            unscheduled.append(
                {
                    "task_id": task.get("id"),
                    "title": summary,
                    "reason": "没认出地点",
                    "deadline": str(task.get("deadline") or ""),
                }
            )
            continue
        usable.append(
            {
                "task_id": task.get("id"),
                "title": summary,
                "room": route_room(summary),
                "place": place,
                "deadline": deadline,
                "deadline_text": str(task.get("deadline") or ""),
                "overdue": bool(deadline and deadline < now),
                "kind": "task",
            }
        )
    usable.sort(
        key=lambda item: (
            item["deadline"] is None,
            item["deadline"] or dt.datetime.max,
            str(item["title"]),
        )
    )
    return usable, unscheduled


def _group_by_zone(stops: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    zones: dict[str, list[dict[str, Any]]] = {}
    for stop in stops:
        zones.setdefault(str(stop["place"].get("zone") or "未分区"), []).append(stop)
    return [zones[key] for key in sorted(zones)]


def _build_trip(
    stops: list[dict[str, Any]],
    *,
    origin: dict[str, Any] | None,
    start_minutes: int,
    limit_minutes: int,
    day: dt.date,
    speed: float,
    order: Sequence[str] = (),
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """排一趟。装不下的部分单独返回，不悄悄丢掉。"""
    ordered = _apply_manual_order(stops, origin, order)
    cursor_place = origin
    # 起点单独记下来：cursor_place 往下会被改成最后一站，回头就不知道这趟从哪出发了。
    from_place = origin
    cursor = start_minutes
    walk_total = 0.0
    placed: list[dict[str, Any]] = []
    overflow: list[dict[str, Any]] = []
    warnings: list[str] = []
    limit = limit_minutes

    for stop in ordered:
        if cursor_place is not None:
            metres = _distance(cursor_place, stop["place"])
            walk_total += metres
            cursor += _walk_minutes(metres, speed)
        dwell = int(stop.get("minutes") or DWELL_MIN)
        open_window = _poi_open(stop["place"])
        arrives_minutes = cursor
        leave = cursor + dwell
        if open_window and leave > open_window[1]:
            warnings.append("%s 的办事窗口到 %s 就关，这趟可能会白跑" % (
                stop["place"]["name"], _minutes_clock(open_window[1])))
        deadline_minutes = None
        if stop["deadline"] and stop["deadline"].date() == day:
            deadline_minutes = stop["deadline"].hour * 60 + stop["deadline"].minute
        if leave > limit or (deadline_minutes is not None and leave > deadline_minutes):
            overflow.append(stop)
            continue
        placed.append(
            {
                "kind": stop.get("kind", "task"),
                "task_id": stop.get("task_id"),
                "title": stop["title"],
                "room": stop.get("room", ""),
                "place_id": stop["place"]["id"],
                "place": stop["place"]["name"],
                "zone": stop["place"].get("zone") or "",
                "arrive": _minutes_clock(arrives_minutes),
                "leave": _minutes_clock(leave),
                "minutes": dwell,
                "why": _why(stop, cursor_place, origin, first=not placed),
            }
        )
        cursor = leave
        cursor_place = stop["place"]

    trip = {
        "zone": placed[0]["zone"] if placed else "",
        "start_at": placed[0]["arrive"] if placed else _minutes_clock(start_minutes),
        "end_at": placed[-1]["leave"] if placed else _minutes_clock(start_minutes),
        # 这一趟从哪栋楼出发：地图画折线要知道起点，不然只画得出楼与楼之间的直线段。
        "from_place": from_place["id"] if from_place is not None else None,
        "stops": placed,
        "distance_m": int(round(walk_total)),
        "walk_minutes": _walk_minutes(walk_total, speed),
        "ok": bool(placed),
        "last_place": cursor_place,
    }
    return trip, overflow, warnings


def _why(
    stop: dict[str, Any],
    previous: dict[str, Any] | None,
    origin: dict[str, Any] | None,
    *,
    first: bool = False,
) -> str:
    """界面上的「为什么先来这里」。只用算出来的事实，不写形容词。"""
    if previous is None:
        if origin is not None:
            return "从这里出发：你今天最后一节课在这栋楼"
        return "第一站：没有课表，从默认起点算起"
    metres = int(round(_distance(previous, stop["place"])))
    if metres == 0:
        if first and origin is not None:
            return "就在下课的这栋楼里，顺路先办"
        return "同一栋楼里还有一件，办完再一起走"
    return "接着走 %d 米，比再跑一趟省一次往返" % metres


def plan_day(
    tasks: Iterable[dict[str, Any]],
    *,
    pois: list[dict[str, Any]],
    lessons: Sequence[dict[str, Any]] = (),
    routines: Sequence[dict[str, Any]] = (),
    order: Sequence[str] = (),
    today: dt.date | None = None,
    now: dt.datetime | None = None,
    day_end: str = DAY_END,
    speed: float = WALK_M_PER_MIN,
) -> dict[str, Any]:
    """排今天的行程。纯函数：同样的输入永远同样的输出。"""
    today = today or (now or dt.datetime.now()).date()
    now = now or dt.datetime.now()
    weekday = today.isoweekday()
    today_lessons = [item for item in lessons if int(item.get("weekday") or 0) == weekday]
    windows = free_windows(today_lessons, day_end=day_end)
    origin = last_lesson_place(today_lessons, pois)

    usable, unscheduled = _collect_candidates(tasks, pois, now, today)

    for item in routines:
        place = match_place(str(item.get("place") or ""), pois)
        if place is None:
            continue
        usable.append(
            {
                "task_id": None,
                "title": str(item.get("title") or "顺路办"),
                "room": "",
                "place": place,
                "deadline": None,
                "deadline_text": "",
                "overdue": False,
                "kind": "routine",
                "minutes": int(item.get("minutes") or DWELL_MIN),
            }
        )

    groups = _group_by_zone(usable)
    trips: list[dict[str, Any]] = []
    overflow: list[dict[str, Any]] = []
    warnings: list[str] = []

    # 只在「最后一节课之后」的窗口里排出门：课间跑出去办事的风险是迟到，
    # 产品不该主动建议这种事。没有课表时整段白天都算可用。
    anchor = None
    if today_lessons:
        anchor = max(_clock_minutes(item["end"]) for item in today_lessons)
    segments = [
        item for item in windows if anchor is None or _clock_minutes(item["start"]) >= anchor
    ] or windows[-1:]

    seg_index = 0
    cursor_place = origin
    cursor = _clock_minutes(segments[0]["start"]) if segments else 0

    for group in groups:
        while seg_index < len(segments):
            limit = _clock_minutes(segments[seg_index]["end"])
            trip, spill, notes = _build_trip(
                group,
                origin=cursor_place,
                start_minutes=cursor,
                limit_minutes=limit,
                day=today,
                speed=speed,
                order=order,
            )
            if trip["ok"]:
                warnings.extend(notes)
                cursor_place = trip.pop("last_place")
                trips.append(trip)
                cursor = _clock_minutes(trip["end_at"]) + 5
                for stop in spill:
                    overflow.append(
                        {
                            "task_id": stop.get("task_id"),
                            "title": stop["title"],
                            "place": stop["place"]["name"],
                            "reason": "这一趟塞不下，建议明天上午或提前出发",
                        }
                    )
                break
            trip.pop("last_place", None)
            seg_index += 1
            if seg_index < len(segments):
                cursor = _clock_minutes(segments[seg_index]["start"])
                cursor_place = origin
        else:
            reason = "今天没有够长的时间窗" if not segments else "这一趟塞不下，建议明天上午或提前出发"
            for stop in group:
                overflow.append(
                    {
                        "task_id": stop.get("task_id"),
                        "title": stop["title"],
                        "place": stop["place"]["name"],
                        "reason": reason,
                    }
                )

    checks = _checks(trips, today_lessons, origin, windows, today)
    return {
        "date": today.isoformat(),
        "weekday": weekday,
        "origin": None if origin is None else {
            "id": origin["id"], "name": origin["name"], "zone": origin.get("zone") or ""
        },
        "windows": windows,
        "trips": trips,
        "unscheduled": unscheduled,
        "overflow": overflow,
        "warnings": warnings,
        "checks": checks,
        "counts": {
            "trips": len(trips),
            "stops": sum(len(trip["stops"]) for trip in trips),
            "distance_m": sum(trip["distance_m"] for trip in trips),
        },
    }


def _checks(
    trips: list[dict[str, Any]],
    lessons: Sequence[dict[str, Any]],
    origin: dict[str, Any] | None,
    windows: list[dict[str, Any]],
    day: dt.date,
) -> list[str]:
    lines: list[str] = []
    if lessons:
        lines.append("今天的课：%d 节，最晚一节 %s 下课" % (
            len(lessons), sorted(lessons, key=lambda item: item["end"])[-1]["end"]))
    else:
        lines.append("今天没课，整天可用")
    if origin is not None:
        lines.append("起点用最后一节课的 %s，不用先回宿舍" % origin["name"])
    else:
        lines.append("没有可用的课表地点，起点按第一个地点算")
    if not windows:
        lines.append("今天没有够长的时间窗，可能排不出行程")
    for trip in trips:
        lines.append("这一趟 %d 件、走路 %d 分钟，%s 出发" % (
            len(trip["stops"]), trip["walk_minutes"], trip["start_at"]))
    return lines
