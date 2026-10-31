"""把消息记录筛成重点条目、按需精炼，并渲染三种推送文本。

行为契约见 `docs/rewrite-spec/batch-b.md` §2。消息分析（分类、打分、截止时间解析）由下层
能力提供，本模块只负责「选什么、怎么排、怎么写」。

同时再导出三个资格判定，历史调用方（收件箱、日程）从这里拿。
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from html import escape as html_escape
from typing import Any, Iterable

import qq_digest  # noqa: F401  精炼调用点被测试按名字打桩，必须保留模块属性
from noticehub.domain.eligibility import (  # noqa: F401  再导出
    is_notice_item,
    is_quiet_group_item,
    is_task_eligible,
)
from qq_digest import Message

from .config import Settings
from .retry import llm_should_retry
from .store import IMPORTANCE_BY_CATEGORY
from .timeutil import now_local, parse_iso

LOGGER = logging.getLogger(__name__)

CATEGORY_ORDER = {"urgent": 0, "action": 1, "academic": 2, "info": 3}
CATEGORY_LABEL = {"urgent": "紧急", "action": "待办", "academic": "学业", "info": "通知"}
KIND_TITLE = {
    "urgent": "紧急通知",
    "window": "群通知汇总",
    "silent": "静默窗口",
    "morning": "今日截止",
    "evening": "截止提醒",
}
SUBSTANTIVE_TAGS = {"action", "notice", "academic", "broadcast", "authority"}
STRONG_URGENT_WORDS = ("紧急", "务必", "尽快", "马上", "立即", "最后通知", "逾期", "过期", "截止")
WEAK_ACTION_WORDS = ("记得", "别忘了", "不要忘记", "可能要", "可能", "也许", "大概", "尽量", "有空", "考虑", "可以在")
STRONG_DIRECTIVE_WORDS = ("请", "务必", "必须", "要求", "按时", "尽早", "截止", "前完成", "前提交")
AMBIGUOUS_TIME_WORDS = ("近期", "近日", "稍后", "左右", "前后", "之前要", "尽快")
DIRECT_ACTION_WORDS = (
    "提交", "填写", "报名", "缴费", "领取", "参加", "完成", "上传", "确认",
    "回复", "核对", "下载", "安装", "办理", "注册", "认证", "上交",
)
EVIDENCE_WORDS = (
    "提交", "报名", "填写", "截止", "材料", "考试", "缴费", "领取",
    "上课", "会议", "讲座", "面试", "答辩", "选课",
)
LLM_ITEM_LIMIT = 50
NO_KEY_MESSAGE = "未配置 DASHSCOPE_API_KEY"


@dataclass
class Digest:
    kind: str
    window_start: dt.datetime | None
    window_end: dt.datetime | None
    items: list[dict[str, Any]] = field(default_factory=list)
    message_ids: list[str] = field(default_factory=list)
    body: str = ""
    summary: str = ""
    html: str = ""
    llm_used: bool = False
    llm_error: str = ""
    llm_retryable: bool = False
    message_count: int = 0
    groups: list[str] = field(default_factory=list)
    id: int = 0
    omitted_count: int = 0

    @property
    def has_focus(self) -> bool:
        return bool(self.items)

    @property
    def title(self) -> str:
        return KIND_TITLE.get(self.kind, "群通知")

    @property
    def window_start_text(self) -> str:
        return self.window_start.strftime("%Y-%m-%d %H:%M") if self.window_start else ""

    @property
    def window_end_text(self) -> str:
        return self.window_end.strftime("%Y-%m-%d %H:%M") if self.window_end else ""

    def payload(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for item in self.items:
            message: Message = item["message"]
            entries.append(
                {
                    "category": str(item.get("category") or "info"),
                    "importance": int(item.get("importance") or 0),
                    "score": int(item.get("score") or 0),
                    "summary": str(item.get("summary") or qq_digest.local_summary(message.text) or ""),
                    "audience": str(item.get("audience") or ""),
                    "condition": str(item.get("condition") or ""),
                    "details": [str(value) for value in (item.get("details") or []) if str(value).strip()],
                    "action": str(item.get("action") or qq_digest.local_action(message.text) or ""),
                    "evidence": str(item.get("evidence") or ""),
                    "deadline": qq_digest.format_deadline(item.get("deadline_dt") or item.get("deadline")),
                    "group": str(item.get("group_name") or ""),
                    "group_id": str(item.get("group_id") or ""),
                    "sender": str(message.sender or ""),
                    "text": str(message.text or ""),
                    "msg_id": str(item.get("msg_id") or ""),
                }
            )
        return entries


def record_to_message(record: dict[str, Any]) -> Message:
    stamp = parse_iso(record.get("ts")) or parse_iso(record.get("received_at")) or now_local()
    sender = str(record.get("sender_name") or record.get("sender_id") or "群成员")
    return Message(
        timestamp=stamp,
        sender=sender,
        text=str(record.get("content") or ""),
        source=str(record.get("group_name") or record.get("group_id") or ""),
    )


def payload_to_item(entry: dict[str, Any]) -> dict[str, Any]:
    text = str(entry.get("text") or "")
    group = str(entry.get("group") or "")
    category = str(entry.get("category") or "info")
    item: dict[str, Any] = {
        "message": Message(
            timestamp=parse_iso(entry.get("created_at")) or now_local(),
            sender=str(entry.get("sender") or "群成员"),
            text=text,
            source=group,
        ),
        "category": category,
        "importance": int(entry.get("importance") or IMPORTANCE_BY_CATEGORY.get(category, 3)),
        "score": int(entry.get("score") or 0),
        "summary": str(entry.get("summary") or qq_digest.local_summary(text)),
        "audience": str(entry.get("audience") or ""),
        "condition": str(entry.get("condition") or ""),
        "details": [str(value) for value in (entry.get("details") or []) if str(value).strip()],
        "action": str(entry.get("action") or qq_digest.local_action(text)),
        "evidence": str(entry.get("evidence") or ""),
        "deadline_dt": qq_digest.parse_timestamp(entry.get("deadline")),
        "group_name": group,
        "group_id": str(entry.get("group_id") or ""),
        "msg_id": str(entry.get("msg_id") or ""),
    }
    if not item["evidence"]:
        item["evidence"] = evidence_sentence(text, item)
    return item


def analyse_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    analyses: list[dict[str, Any]] = []
    for record in records:
        analysis = qq_digest.analyze_message(record_to_message(record))
        analysis["group_name"] = str(record.get("group_name") or record.get("group_id") or "")
        analysis["group_id"] = str(record.get("group_id") or "")
        analysis["msg_id"] = str(record.get("msg_id") or "")
        analysis["record"] = record
        analyses.append(analysis)
    return analyses


def _first_hit(text: str, words: Iterable[str]) -> str:
    for word in words:
        if word and word in text:
            return word
    return ""


def classify_task_item(item: dict[str, Any], settings: Settings) -> dict[str, Any]:
    message: Message | None = item.get("message")
    text = str(getattr(message, "text", "") or "")
    category = str(item.get("category") or "info")
    action_words = [str(word) for word in (item.get("action_words") or []) if str(word)]
    deadline = item.get("deadline_dt") or item.get("deadline")
    has_deadline = isinstance(deadline, dt.datetime)
    evidence = str(item.get("evidence") or evidence_sentence(text, item))

    weak = _first_hit(text, WEAK_ACTION_WORDS)
    vague = _first_hit(text, AMBIGUOUS_TIME_WORDS)
    directive = _first_hit(text, STRONG_DIRECTIVE_WORDS)
    direct = bool(action_words) or bool(_first_hit(text, DIRECT_ACTION_WORDS))
    urgent = category == "urgent"
    tags = set(item.get("tags") or ())
    score = int(item.get("score") or 0)
    quiet = is_quiet_group_item(item, settings)

    confidence = 0.28
    if has_deadline:
        confidence += 0.25
    if directive:
        confidence += 0.20
    if direct:
        confidence += 0.14
    if urgent:
        confidence += 0.10
    if category == "action":
        confidence += 0.08
    if evidence:
        confidence += 0.05
    if score >= 4:
        confidence += 0.05
    if weak:
        confidence -= 0.24
    if vague:
        confidence -= 0.14
    if quiet:
        confidence -= 0.08
    if not direct:
        confidence -= 0.08
    confidence = round(max(0.05, min(0.98, confidence)), 2)

    status = "candidate"
    reason = "只有行动线索，证据还不够直接，先放到待确认。"
    if weak:
        reason = "原话有“%s”等不确定措辞，先确认是否真的要办。" % weak
    elif has_deadline and (direct or directive):
        status = "open"
        reason = "有明确行动要求和截止时间，直接进入待办。"
    elif urgent and (direct or directive or "urgent" in tags):
        status = "open"
        reason = "规则判断为紧急事项，直接进入待办。"
    elif directive and direct:
        status = "open"
        reason = "有明确的行动要求，没有写截止时间。"
    elif has_deadline and not direct and not directive:
        reason = "提到了日期，但更像活动日期而不是截止时间。"
    elif vague:
        reason = "“%s”的时间语义不明确，先确认。" % vague
    elif not has_deadline and direct:
        reason = "识别到行动，但没有明确截止时间，先确认是否真的要办。"

    return {
        "status": status,
        "confidence": confidence,
        "reason": reason,
        "source": str(item.get("source") or "qq_message"),
    }


def is_focus(analysis: dict[str, Any], settings: Settings) -> bool:
    if bool(analysis.get("colloquial_question")):
        return False
    if is_quiet_group_item(analysis, settings) and not is_notice_item(analysis, settings):
        return False
    if analysis.get("category") == "urgent":
        return int(analysis.get("score") or 0) >= 1
    return int(analysis.get("score") or 0) >= settings.min_score


def filter_recent_duplicates(
    analyses: list[dict[str, Any]],
    history: Iterable[dict[str, Any]] | None,
    settings: Settings,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    hours = int(settings.dedupe_hours or 0)
    if hours <= 0 or not history:
        return analyses, []
    seen: list[tuple[str, dict[str, Any]]] = []
    for entry in history:
        text = str(entry.get("text") or "").strip()
        if text:
            seen.append((qq_digest.normalize_for_match(text), entry))
    if not seen:
        return analyses, []
    kept: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    for item in analyses:
        text = qq_digest.normalize_for_match(item["message"].text)
        hit = next((entry for normalized, entry in seen if qq_digest.similar_text(text, normalized)), None)
        if hit is None:
            kept.append(item)
            continue
        item["suppressed_duplicate"] = {
            "group": str(hit.get("group") or ""),
            "summary": str(hit.get("summary") or ""),
            "ts": str(hit.get("ts") or ""),
        }
        suppressed.append(item)
    return kept, suppressed


def urgent_analyses(
    analyses: Iterable[dict[str, Any]],
    settings: Settings,
    *,
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    if not settings.urgent_immediate:
        return []
    stamp = now or now_local()
    chosen: list[dict[str, Any]] = []
    for item in analyses:
        if is_quiet_group_item(item, settings):
            continue
        if item.get("category") != "urgent":
            continue
        score = int(item.get("score") or 0)
        tags = set(item.get("tags") or ())
        if score < 4 or not (tags & SUBSTANTIVE_TAGS):
            continue
        message: Message = item["message"]
        strong = bool(_first_hit(message.text, STRONG_URGENT_WORDS))
        deadline = item.get("deadline")
        soon = False
        if isinstance(deadline, dt.datetime):
            delta = deadline - stamp
            soon = dt.timedelta(0) <= delta <= dt.timedelta(hours=48)
        if strong or item.get("action_words") or soon:
            chosen.append(item)
    return chosen


def evidence_sentence(text: str, item: dict[str, Any] | None = None, limit: int = 300) -> str:
    value = " ".join(str(text or "").split())
    if not value:
        return ""
    sentences = [part.strip() for part in re.split(r"(?<=[。！？!?；;\n])", value) if part.strip()]
    if not sentences:
        return value[:limit]
    keys: list[str] = []
    deadline = (item or {}).get("deadline_dt") or (item or {}).get("deadline")
    if isinstance(deadline, dt.datetime):
        keys.extend(
            [
                deadline.strftime("%m-%d"),
                deadline.strftime("%m月%d日"),
                "%d月%d日" % (deadline.month, deadline.day),
                "%d日" % deadline.day,
                deadline.strftime("%H:%M"),
            ]
        )
    keys.extend(STRONG_URGENT_WORDS)
    keys.extend(EVIDENCE_WORDS)
    for sentence in sentences:
        if any(key and key in sentence for key in keys):
            return sentence[:limit]
    return sentences[0][:limit]


def _fill_item(item: dict[str, Any]) -> dict[str, Any]:
    """补齐渲染与归档都需要的字段，保证两条出口看到同一份内容。"""
    message: Message = item["message"]
    if not item.get("summary"):
        item["summary"] = qq_digest.local_summary(message.text)
    if not item.get("action"):
        item["action"] = qq_digest.local_action(message.text)
    item.setdefault("audience", "")
    item.setdefault("condition", "")
    if not isinstance(item.get("details"), list):
        item["details"] = []
    if not item.get("deadline_dt"):
        item["deadline_dt"] = item.get("deadline")
    if not item.get("evidence"):
        item["evidence"] = evidence_sentence(message.text, item)
    if not item.get("importance"):
        item["importance"] = IMPORTANCE_BY_CATEGORY.get(str(item.get("category")), 3)
    return item


def _gather(
    analyses: list[dict[str, Any]],
    settings: Settings,
    history: Iterable[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], int]:
    candidates = [item for item in analyses if is_focus(item, settings)]
    if history:
        candidates, suppressed = filter_recent_duplicates(candidates, history, settings)
        if suppressed:
            names = sorted({str(item.get("group_name") or "") for item in suppressed if item.get("group_name")})
            LOGGER.info(
                "跨群去重：%d 条与最近 %d 小时内已推内容重复，不再重复推送（%s）。",
                len(suppressed),
                settings.dedupe_hours,
                "、".join(names) or "来源未知",
            )
    candidates = qq_digest.dedupe_items(candidates)
    candidates.sort(key=lambda item: int(item.get("score") or 0), reverse=True)
    omitted = max(0, len(candidates) - settings.max_items)
    if omitted:
        LOGGER.warning("本次 %d 条重点超出单次上限 %d，未展开。", omitted, settings.max_items)
    return candidates[: settings.max_items], omitted


def _refine(
    items: list[dict[str, Any]],
    settings: Settings,
) -> tuple[list[dict[str, Any]], bool, str, bool]:
    """LLM 精炼；失败只降级，不让摘要整体失败。"""
    if not (settings.llm_enabled and items):
        return items, False, "", False
    if not settings.dashscope_api_key and settings.llm_backend not in {"auto", "codebuddy"}:
        return items, False, NO_KEY_MESSAGE, False
    try:
        refined = qq_digest.refine_with_dashscope(
            items,
            settings.dashscope_api_key,
            settings.dashscope_model,
            settings.dashscope_endpoint,
            settings.llm_timeout,
            retries=settings.llm_max_retries,
            backoff=settings.llm_retry_backoff,
            backend=settings.llm_backend,
        )
    except RuntimeError as error:
        LOGGER.warning("LLM 精炼失败，回退本地规则：%s", error)
        return items, False, str(error), llm_should_retry(error)
    return refined, True, "", False


def build_digest(
    settings: Settings,
    records: list[dict[str, Any]],
    *,
    kind: str = "window",
    now: dt.datetime | None = None,
    history: Iterable[dict[str, Any]] | None = None,
) -> Digest:
    stamp = now or now_local()
    analyses = analyse_records(records)
    pool = urgent_analyses(analyses, settings) if kind == "urgent" else analyses

    selected, omitted = _gather(pool, settings, history)
    selected, llm_used, llm_error, llm_retryable = _refine(selected, settings)
    items = [_fill_item(item) for item in qq_digest.sort_items(selected)]

    groups: list[str] = []
    for record in records:
        name = str(record.get("group_name") or record.get("group_id") or "")
        if name and name not in groups:
            groups.append(name)

    digest = Digest(
        kind=kind,
        window_start=parse_iso(records[0].get("received_at")) if records else stamp,
        window_end=stamp,
        items=items,
        message_ids=[str(record.get("msg_id") or "") for record in records],
        llm_used=llm_used,
        llm_error=llm_error,
        llm_retryable=llm_retryable,
        message_count=len(records),
        groups=groups,
        omitted_count=omitted,
    )
    return finalize_digest(digest, settings)


def _squeeze(text: str, limit: int) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def format_push_text(digest: Digest, settings: Settings) -> str:
    stamp = digest.window_end_text or now_local().strftime("%Y-%m-%d %H:%M")
    scope = "、".join(digest.groups) if digest.groups else "未知群"
    counts = "%d 条消息 · %d 条重点" % (digest.message_count, len(digest.items))
    if digest.omitted_count:
        counts += "（另有 %d 条超出单次上限未展开）" % digest.omitted_count
    lines = ["【QQ%s】%s" % (digest.title, stamp), "%s · %s" % (_squeeze(scope, 40), counts), ""]

    for index, item in enumerate(digest.items, start=1):
        message: Message = item["message"]
        label = CATEGORY_LABEL.get(str(item.get("category")), "通知")
        summary = item.get("summary") or qq_digest.local_summary(message.text)
        action = item.get("action") or qq_digest.local_action(message.text)
        deadline = qq_digest.format_deadline(item.get("deadline_dt") or item.get("deadline"))
        lines.append("%d. [%s] %s" % (index, label, _squeeze(summary, 60)))

        parts: list[str] = []
        if deadline:
            parts.append("截止：%s" % deadline)
        if action and action != summary:
            parts.append("需做：%s" % _squeeze(action, 30))
        groups = [str(name) for name in (item.get("duplicate_groups") or []) if str(name).strip()]
        if not groups and item.get("group_name"):
            groups = [str(item["group_name"])]
        source_label = "、".join(dict.fromkeys(groups))
        if len(set(groups)) > 1:
            source_label = "%s（跨群重复）" % source_label
        source = " · ".join(part for part in (source_label, message.sender) if part)
        if source:
            parts.append(_squeeze(source, 45))
        if parts:
            lines.append("   " + " · ".join(parts))

        links = qq_digest.extract_links(message.text)
        if links:
            lines.append("   链接：%s" % links[0])
        if settings.include_raw:
            original = _squeeze(message.text, 80)
            if original and original not in summary:
                lines.append("   原文：%s" % original)
        lines.append("")

    if digest.llm_error and digest.items:
        lines.append("（AI 摘要暂不可用，已用本地规则筛选）")
    return "\n".join(lines).strip()


def _short_deadline(value: Any, now: dt.datetime, with_time: bool = True) -> str:
    if not isinstance(value, dt.datetime):
        return ""
    clock = value.strftime("%H:%M") if with_time else ""
    if value.date() == now.date():
        return "今天%s" % clock
    if value.date() == now.date() + dt.timedelta(days=1):
        return "明天%s" % clock
    return value.strftime("%m-%d") + (" %s" % clock if clock else "")


def push_summary(digest: Digest, limit: int = 26) -> str:
    if not digest.items:
        return digest.title
    item = digest.items[0]
    message: Message = item["message"]
    summary = str(item.get("summary") or qq_digest.local_summary(message.text))
    deadline_text = qq_digest.format_deadline(item.get("deadline_dt") or item.get("deadline"))
    head = _short_deadline(
        item.get("deadline_dt"),
        digest.window_end or now_local(),
        ":" in str(deadline_text or ""),
    )
    body = _squeeze(summary, 14)
    text = ("%s %s" % (head, body)).strip() if head else body
    extra = len(digest.items) - 1
    if extra > 0:
        text = "%s +%d条" % (text, extra)
    return _squeeze(text, limit)


def html_push_text(digest: Digest, settings: Settings) -> str:
    stamp = digest.window_end_text or now_local().strftime("%Y-%m-%d %H:%M")
    scope = "、".join(digest.groups) if digest.groups else "未知群"
    omitted = " · 另有 %d 条超出单次上限未展开" % digest.omitted_count if digest.omitted_count else ""
    parts: list[str] = [
        '<div style="font-size:12px;color:#9aa0a6">'
        + "%s · %s · %d 条消息%s" % (html_escape(stamp), html_escape(_squeeze(scope, 30)), digest.message_count, omitted)
        + "</div>"
    ]

    actions = [item for item in digest.items if str(item.get("category")) in {"urgent", "action"}]
    others = [item for item in digest.items if str(item.get("category")) not in {"urgent", "action"}]

    for item in actions[:5]:
        message: Message = item["message"]
        urgent = str(item.get("category")) == "urgent"
        color = "#d93025" if urgent else "#f29900"
        label = "紧急" if urgent else "待办"
        deadline_text = qq_digest.format_deadline(item.get("deadline_dt") or item.get("deadline"))
        summary = str(item.get("summary") or qq_digest.local_summary(message.text))
        action = str(item.get("action") or qq_digest.local_action(message.text))
        evidence = str(item.get("evidence") or "")
        head = " · ".join(
            part for part in (("%s 截止" % deadline_text if deadline_text else ""), summary) if part
        )
        groups_text = "、".join(str(name) for name in (item.get("duplicate_groups") or []))
        meta = " · ".join(
            part for part in (groups_text or str(item.get("group_name") or ""), message.sender) if part
        )
        block = [
            '<div style="margin:10px 0;padding:10px 12px;background:#fff8ec;'
            + 'border-left:3px solid %s;border-radius:3px">' % color,
            '<div style="font-size:12px;color:%s;font-weight:600">%s</div>' % (color, label),
            '<div style="font-size:15px;font-weight:600;color:#1a1a1a;margin-top:2px">%s</div>'
            % html_escape(_squeeze(head, 46)),
        ]
        if action and action != summary:
            block.append(
                '<div style="font-size:13px;color:#333;margin-top:4px">需做：%s</div>'
                % html_escape(_squeeze(action, 30))
            )
        if meta:
            block.append(
                '<div style="font-size:12px;color:#9aa0a6;margin-top:4px">%s</div>'
                % html_escape(_squeeze(meta, 40))
            )
        audience = str(item.get("audience") or "")
        condition = str(item.get("condition") or "")
        if audience or condition:
            context_text = " · ".join(
                part
                for part in (
                    "适用：%s" % audience if audience else "",
                    "条件：%s" % condition if condition else "",
                )
                if part
            )
            block.append(
                '<div style="font-size:12px;color:#5f6368;margin-top:3px">%s</div>'
                % html_escape(_squeeze(context_text, 90))
            )
        if evidence and evidence not in summary:
            block.append(
                '<div style="font-size:12px;color:#b0b0b0;margin-top:2px">原文：%s</div>'
                % html_escape(_squeeze(evidence, 40))
            )
        task_url = str(item.get("task_url") or "")
        if task_url:
            block.append(
                '<div style="font-size:12px;margin-top:5px"><a href="%s" '
                'style="color:#1a73e8">查看完整原文</a></div>' % html_escape(task_url)
            )
        links = qq_digest.extract_links(message.text)
        if links:
            block.append(
                '<div style="font-size:12px;margin-top:4px"><a href="%s" '
                'style="color:#1a73e8">相关链接</a></div>' % html_escape(links[0])
            )
        block.append("</div>")
        parts.append("".join(block))

    if others:
        names = " · ".join(
            html_escape(_squeeze(str(item.get("summary") or qq_digest.local_summary(item["message"].text)), 22))
            for item in others[:5]
        )
        more = " 等 %d 条" % len(others) if len(others) > 5 else ""
        parts.append(
            '<div style="font-size:13px;color:#5f6368;margin-top:12px">其余 %d 条：%s%s</div>'
            % (len(others), names, more)
        )
    if digest.llm_error and digest.items:
        parts.append(
            '<div style="font-size:12px;color:#9aa0a6;margin-top:8px">'
            "（AI 摘要暂不可用，已用本地规则筛选）</div>"
        )
    return "".join(parts)


def finalize_digest(digest: Digest, settings: Settings) -> Digest:
    if not digest.items:
        return digest
    digest.body = format_push_text(digest, settings)
    digest.summary = push_summary(digest)
    digest.html = html_push_text(digest, settings) if settings.html_push else ""
    return digest


def preview_text(settings: Settings, records: list[dict[str, Any]]) -> str:
    if not records:
        return "没有待处理消息。"
    digest = build_digest(settings, records, kind="window")
    if not digest.items:
        return "待处理 %d 条消息，未筛出重点内容（不会推送）。" % len(records)
    return "[仅预览] 待处理 %d 条，命中 %d 条\n\n%s" % (len(records), len(digest.items), digest.body)
