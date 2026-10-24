"""事件结构化提取（实验性，默认关闭）。

输入一段中文文本，输出**候选事件**：
``{"when_start", "when_end", "where", "what", "confidence"}``。

三条边界，别越：
* 纯规则，不调模型；
* 只读——不写库、不写日历、不发推送（写入要用户确认后走现有 API）；
* 不挂钩摘要流程，没有默认调用方，由设置页「实验室」开关决定要不要用。

时间解析是这里唯一有点分量的东西：覆盖 今天/明天/后天、周X/下周X、
几月几号、上午/下午/晚上/中午、3点/三点半/15:00、``X点到Y点结束``；
文本里没写年份时按「没过期就今年，过期就明年」定年。
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .timeutil import now_local

CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}

_WEEKDAYS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}

# 相对日：词 -> 天数偏移
_RELATIVE_DAYS = {
    "大后天": 3, "后天": 2, "明天": 1, "明日": 1, "明早": 1, "明晨": 1, "明晚": 1,
    "今天": 0, "今日": 0, "今早": 0, "今晚": 0,
}

# 已经发生过的说法：整句不再当未来事件（宁可漏，不可错）。
_PAST_MARKERS = (
    "上次", "上回", "上上周", "上周", "昨天", "昨日", "前天", "上个月", "去年",
    "已经", "早就", "已结束", "结束了", "开完了", "开完了",
)

_DEADLINE_WORDS = ("截止", "提交", "交作业", "交表", "上报", "报名", "填表", "交材料", "前交", "之前交")

_VERBS = (
    "开会", "集合", "上课", "面试", "报到", "考试", "聚餐", "答辩", "体检", "培训",
    "讲座", "打球", "报名", "交作业", "提交", "截止", "见面", "见", "参加", "补课",
    "值班", "升旗", "演练", "比赛", "汇报", "签到", "活动",
)

_PERIODS = (
    "凌晨", "早晨", "早上", "上午", "中午", "下午", "傍晚", "晚上", "夜里", "晚",
)

_PERIOD_DEFAULT_HOUR = {
    "凌晨": 6, "早晨": 7, "早上": 8, "上午": 9, "中午": 12,
    "下午": 14, "傍晚": 18, "晚上": 19, "夜里": 20, "晚": 19,
}

_CLOCK_RE = re.compile(
    r"(?P<hour>\d{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*"
    r"(?:点|时|[:：])\s*"
    r"(?P<minute>\d{1,2}|[零〇一二两三四五六七八九十]{1,3}|半|一刻|三刻|整)?"
)
_RELATIVE_RE = re.compile("|".join(sorted(_RELATIVE_DAYS, key=len, reverse=True)))
_WEEK_RE = re.compile(r"(?P<prefix>下周|本周|这周|下下|下个|这个)?\s*(?:周|星期|礼拜)(?P<day>[一二三四五六日天])")
_MONTH_DAY_RE = re.compile(
    r"(?:(?P<year>20\d{2})\s*年\s*)?"
    r"(?P<month>\d{1,2}|[一二三四五六七八九十]{1,2})\s*[月/-]\s*"
    r"(?P<day>\d{1,2}|[一二三四五六七八九十]{1,3})\s*[日号]?"
)
_PLACE_WITH_ACTION_RE = re.compile(
    r"(?:在|于|到)\s*(?P<place>[^，。；;、,！!？?\s]{1,16}?)(?=(?:"
    + "|".join(sorted(_VERBS, key=len, reverse=True))
    + r"))"
)
_PLACE_TAIL_RE = re.compile(
    r"(?P<place>[A-Za-z0-9\u4e00-\u9fa5]{1,8}?"
    r"(?:楼|栋|教室|会议室|报告厅|礼堂|体育馆|操场|实验室|食堂|门口|大厅|广场|中心)"
    r"[A-Za-z0-9\-]{0,6})"
)
_RANGE_JOINER_RE = re.compile(r"^\s*(?:到|至|-|—|–|~|～|～)\s*$")


def _cn_int(text: str) -> int | None:
    """中文/阿拉伯数字 -> int（只处理 0–99，够用了）。"""
    value = str(text or "").strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    if "十" in value:
        left, _, right = value.partition("十")
        tens = _cn_int(left) if left else 1
        ones = _cn_int(right) if right else 0
        if tens is None or ones is None:
            return None
        return tens * 10 + ones
    number = 0
    for char in value:
        if char not in _CN_DIGITS:
            return None
        number = number * 10 + _CN_DIGITS[char]
    return number


def _apply_period(hour: int, period: str) -> int:
    if period in {"下午", "傍晚", "晚上", "夜里", "晚"} and hour < 12:
        return hour + 12
    if period == "中午" and hour < 12:
        return hour + 12
    return hour


def _period_before(text: str, position: int) -> str:
    window = text[max(0, position - 6):position]
    for word in sorted(_PERIODS, key=len, reverse=True):
        if window.rfind(word) >= 0:
            return word
    return ""


def _clocks(text: str) -> list[tuple[int, int, int, int]]:
    """所有 (hour, minute, start, end)；小时已按前置时段换算成 24 小时制。"""
    found: list[tuple[int, int, int, int]] = []
    for match in _CLOCK_RE.finditer(text):
        hour = _cn_int(match.group("hour"))
        if hour is None:
            continue
        minute_text = match.group("minute") or ""
        if minute_text in {"半"}:
            minute = 30
        elif minute_text in {"一刻"}:
            minute = 15
        elif minute_text in {"三刻"}:
            minute = 45
        elif minute_text in {"整", ""}:
            minute = 0
        else:
            minute = _cn_int(minute_text)
        if minute is None:
            continue
        hour = _apply_period(hour, _period_before(text, match.start()))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            found.append((hour, minute, match.start(), match.end()))
    return found


def _find_date(text: str, now: dt.datetime) -> tuple[dt.date | None, int, int, str]:
    """返回 (日期, 起点, 终点, 该写法暗示的时段)。"""
    candidates: list[tuple[int, dt.date | None, int, int, str]] = []

    relative = _RELATIVE_RE.search(text)
    if relative:
        word = relative.group(0)
        offset = _RELATIVE_DAYS[word]
        period = ""
        if word in {"今早", "明早", "明晨"}:
            period = "早上"
        elif word in {"今晚", "明晚"}:
            period = "晚上"
        candidates.append(
            (relative.start(), now.date() + dt.timedelta(days=offset), relative.start(), relative.end(), period)
        )

    week = _WEEK_RE.search(text)
    if week:
        day = _WEEKDAYS[week.group("day")]
        monday = now.date() - dt.timedelta(days=now.isoweekday() - 1)
        prefix = week.group("prefix") or ""
        if prefix == "下周":
            target = monday + dt.timedelta(days=7 + day - 1)
        elif prefix in {"下下", "下个"}:
            target = monday + dt.timedelta(days=14 + day - 1)
        elif prefix in {"本周", "这周", "这个"}:
            target = monday + dt.timedelta(days=day - 1)
        else:  # 光写「周五」：取最近的一次（含今天）
            target = monday + dt.timedelta(days=day - 1)
            if target < now.date():
                target += dt.timedelta(days=7)
        candidates.append((week.start(), target, week.start(), week.end(), ""))

    month_day = _MONTH_DAY_RE.search(text)
    if month_day:
        month = _cn_int(month_day.group("month"))
        day = _cn_int(month_day.group("day"))
        year = _cn_int(month_day.group("year")) if month_day.group("year") else None
        target = None
        if month and day:
            if year:
                try:
                    target = dt.date(year, month, day)
                except ValueError:
                    target = None
            else:
                # 没写年份：今年还没过期就今年，过期了就明年。
                for candidate_year in (now.year, now.year + 1):
                    try:
                        candidate = dt.date(candidate_year, month, day)
                    except ValueError:
                        continue
                    if candidate >= now.date():
                        target = candidate
                        break
                if target is None:
                    try:
                        target = dt.date(now.year + 1, month, day)
                    except ValueError:
                        target = None
        if target is not None:
            candidates.append((month_day.start(), target, month_day.start(), month_day.end(), ""))

    if not candidates:
        return None, 0, 0, ""
    candidates.sort(key=lambda item: item[0])
    _position, target, start, end, period = candidates[0]
    return target, start, end, period


def _find_place(text: str) -> str | None:
    match = _PLACE_WITH_ACTION_RE.search(text)
    if match:
        place = match.group("place").strip(" 　在于到的")
        if place:
            return place
    match = _PLACE_TAIL_RE.search(text)
    if match:
        place = match.group("place").strip(" 　在于到的")
        if place:
            return place
    return None


def _punch_out_times(
    clause: str,
    date_span: tuple[int, int],
    clocks: list[tuple[int, int, int, int]],
) -> str:
    """把日期与钟点片段挖成「，」，地点正则就不会把时间词当地点。

    「，」不在地点字符类里，被挖掉的位置天然匹配失败。
    """
    chars = list(clause)
    spans = [date_span, *[(c[2], c[3]) for c in clocks]]
    for start, end in spans:
        if end > start:
            for i in range(start, min(end, len(chars))):
                chars[i] = "，"
    return "".join(chars)


def _find_verb(text: str) -> str | None:
    best: tuple[int, str] | None = None
    for word in _VERBS:
        position = text.find(word)
        if position >= 0 and (best is None or position < best[0]):
            best = (position, word)
    return best[1] if best else None


def _what(text: str, verb: str | None) -> str:
    if verb and (position := text.find(verb)) >= 0:
        text = text[position:]
    tail = re.split(r"[，。；;、,！!？?\n]", text, maxsplit=1)[0].strip()
    return tail[:80]


def _clamp(confidence: str, min_level: str) -> bool:
    floor = CONFIDENCE_RANK.get(str(min_level).lower(), CONFIDENCE_RANK["low"])
    return CONFIDENCE_RANK[confidence] >= floor


def extract_events(
    text: str,
    *,
    now: dt.datetime | None = None,
    min_level: str = "low",
) -> list[dict[str, Any]]:
    """从一段文本里抽候选事件；只读，不写任何地方。

    ``min_level`` 是置信度下限（low/medium/high）。低于它的事件不产出。
    """
    reference = now or now_local()
    events: list[dict[str, Any]] = []
    for clause in re.split(r"[。；;！!\n]+", str(text or "")):
        clause = clause.strip()
        if not clause:
            continue
        if any(marker in clause for marker in _PAST_MARKERS):
            continue
        if clause.endswith(("？", "?")) or any(
            word in clause for word in ("几点", "什么时间", "什么时候", "哪天")
        ):
            continue

        date_value, _start, _end, implied_period = _find_date(clause, reference)
        clocks = _clocks(clause)
        verb = _find_verb(clause)
        place = _find_place(_punch_out_times(clause, (_start, _end), clocks))

        # what：句首时间短语整段去掉，保留「在A楼302开会」这类地点+动作；
        # 时间不在句首时退回按动词截。
        body = clause
        if _start == 0 and _end:
            leading_end = _end
            if clocks and clocks[0][2] <= leading_end + 2:  # 允许隔着「下午」等时段词
                leading_end = max(leading_end, clocks[0][3])
            body = clause[leading_end:].strip() or clause
        what = _what(body, verb) if body == clause else _what(body, None)

        period = implied_period or (_period_before(clause, clocks[0][2]) if clocks else "")
        deadline = any(word in clause for word in _DEADLINE_WORDS)

        when_start: dt.datetime | None = None
        when_end: dt.datetime | None = None
        end_clock: tuple[int, int, int, int] | None = None
        if date_value is not None or clocks:
            if clocks:
                # 「到X点（结束）」句式：这个钟点是结束时间，开始时间用默认值。
                end_only = clause[max(0, clocks[0][2] - 1):clocks[0][2]] == "到"
                if end_only:
                    hour, minute = (23, 59) if deadline else (
                        _PERIOD_DEFAULT_HOUR.get(period, 9), 0
                    )
                    end_clock = clocks[0]
                else:
                    hour, minute = clocks[0][0], clocks[0][1]
                    # 第二个钟点前面跟着「到/至/—/~」才当结束时间。
                    if len(clocks) > 1 and _RANGE_JOINER_RE.match(
                        clause[clocks[0][3]:clocks[1][2]]
                    ):
                        end_clock = clocks[1]
                if end_clock is not None:
                    end_hour, end_minute = end_clock[0], end_clock[1]
                    if not _period_before(clause, end_clock[2]) and period:
                        end_hour = _apply_period(end_hour, period)
                    when_end = dt.datetime.combine(
                        date_value or reference.date(), dt.time(end_hour, end_minute)
                    )
            else:
                hour, minute = 23, 59
                if not deadline:
                    hour, minute = _PERIOD_DEFAULT_HOUR.get(period, 9), 0

            if date_value is not None:
                when_start = dt.datetime.combine(date_value, dt.time(hour, minute))
            else:
                # 只有钟点没写日期：今天还没过就是今天，过了就明天。
                when_start = dt.datetime.combine(reference.date(), dt.time(hour, minute))
                if when_start < reference:
                    when_start += dt.timedelta(days=1)
            if when_end is not None and when_end <= when_start:
                # 口语里「9点到5点」「到5点结束」的 5 点指下班/散场：
                # 结束钟点没有显式时段且小于 12 时按下午处理，而不是丢弃。
                if (
                    end_clock is not None
                    and not _period_before(clause, end_clock[2])
                    and end_clock[0] < 12
                ):
                    when_end += dt.timedelta(hours=12)
                if when_end <= when_start:
                    when_end = None

        has_time = when_start is not None
        has_where = bool(place)
        has_verb = bool(verb)
        if has_time and has_where and has_verb:
            confidence = "high"
        elif has_time:
            confidence = "medium"
        elif has_where:
            confidence = "low"
        else:
            continue
        if not _clamp(confidence, min_level):
            continue

        events.append(
            {
                "when_start": when_start.isoformat(timespec="seconds") if when_start else None,
                "when_end": when_end.isoformat(timespec="seconds") if when_end else None,
                "where": place,
                "what": what,
                "confidence": confidence,
            }
        )

    events.sort(key=lambda item: (item["when_start"] is None, item["when_start"] or ""))
    return events


__all__ = ["CONFIDENCE_RANK", "extract_events"]
