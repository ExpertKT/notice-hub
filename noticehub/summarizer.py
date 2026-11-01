"""消息摘要：把一批群消息筛成可推送的重点，并渲染成推送正文。

设计上分三层：
- 分析内核在 `qq_digest`（分词打分、中文时间解析、本地摘要、相似度、链接抽取），本模块只调用它。
- 本模块负责「哪些值得推」「跨群去重」「要不要问模型」「怎么排版」。
- 除了调用模型和读配置，其余都是纯计算：同样输入必得同样输出，不写库、不写文件。

分类与重要度的换算表只有一份，在 `store` 里；这里从那边取用同一个对象，不复制字面量。
"""

from __future__ import annotations

import datetime as dt
import html as html_module
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import qq_digest

from .domain.eligibility import is_notice_item, is_quiet_group_item, is_task_eligible
from .retry import llm_should_retry
from .store import IMPORTANCE_BY_CATEGORY
from .timeutil import iso, now_local

__all__ = [
    "Digest",
    "IMPORTANCE_BY_CATEGORY",
    "analyse_records",
    "build_digest",
    "classify_task_item",
    "evidence_sentence",
    "filter_recent_duplicates",
    "finalize_digest",
    "format_push_text",
    "html_push_text",
    "is_focus",
    "is_notice_item",
    "is_quiet_group_item",
    "is_task_eligible",
    "payload_to_item",
    "preview_text",
    "push_summary",
    "record_to_message",
    "urgent_analyses",
]

LOGGER = logging.getLogger(__name__)

CATEGORY_LABELS = {"urgent": "紧急", "action": "待办", "academic": "学业", "info": "通知"}
CATEGORY_ORDER = {"urgent": 0, "action": 1, "academic": 2, "info": 3}
VALID_CATEGORIES = frozenset(CATEGORY_LABELS)
KIND_TITLES = {
    "urgent": "紧急通知",
    "window": "群通知汇总",
    "silent": "静默窗口",
    "morning": "今日截止",
    "evening": "截止提醒",
}
LLM_BACKENDS = frozenset({"auto", "ollama", "openai", "codebuddy"})
# 没有密钥也能跑的后端：它们先走本机命令行，不需要云端凭据。
KEYLESS_BACKENDS = frozenset({"auto", "codebuddy"})
LLM_ITEM_LIMIT = 50
LLM_TEXT_LIMIT = 1200

STRONG_URGENT_WORDS = ("紧急", "务必", "尽快", "马上", "立即", "最后通知", "逾期", "过期", "截止")
DIRECTIVE_WORDS = ("请", "务必", "必须", "要求", "按时", "尽早", "截止", "前完成", "前提交")
ACTION_WORDS = (
    "提交", "填写", "报名", "缴费", "领取", "参加", "完成", "上传", "确认",
    "回复", "核对", "下载", "安装", "办理", "注册", "认证", "上交",
)
WEAK_WORDS = ("记得", "别忘了", "不要忘记", "可能要", "可能", "也许", "大概", "尽量", "有空", "考虑", "可以在")
VAGUE_TIME_WORDS = ("近期", "近日", "稍后", "左右", "前后", "之前要", "尽快")
EVIDENCE_WORDS = (
    "提交", "报名", "填写", "截止", "材料", "考试", "缴费", "领取", "上课", "会议", "讲座", "面试", "答辩", "选课",
)
URGENT_TAGS = frozenset({"action", "notice", "academic", "broadcast", "authority"})
SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])|\n+")
CLOCK_WORD = re.compile(r"\b\d{1,2}:\d{2}\b")
WHITESPACE = re.compile(r"\s+")


def _first_text(*values: Any) -> str:
    """按顺序取第一个非空值并转成字符串。"""
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clock(value: dt.datetime | None) -> str:
    """把时间点格式成 `YYYY-MM-DD HH:MM`（无时分时省掉后半段）。"""
    return qq_digest.format_deadline(value)


def _day_label(value: dt.datetime, now: dt.datetime) -> str:
    """截止日期在人话里的说法：今天 / 明天 / MM-DD。"""
    day = value.date()
    today = now.date()
    if day == today:
        return "今天"
    if day == today + dt.timedelta(days=1):
        return "明天"
    return "%02d-%02d" % (value.month, value.day)


_EMPTY_LIST = list


def _stamp_text(value: dt.datetime | None) -> str:
    """时间点的展示写法；没有时间就给空串。"""
    if not value:
        return ""
    return value.strftime("%Y-%m-%d %H:%M")


@dataclass
class Digest:
    """一次摘要的产出。字段就是推送、界面和归档共用的那一份。"""

    kind: str = "window"
    window_start: dt.datetime | None = None
    window_end: dt.datetime | None = None
    # 下面两组容器字段各自独立，别让它们共享同一个列表对象
    items: list[dict[str, Any]] = field(default_factory=_EMPTY_LIST)
    message_ids: list[str] = field(default_factory=_EMPTY_LIST)
    body: str = field(default_factory=str)
    summary: str = ""
    html: str = ""
    llm_used: bool = False
    llm_error: str = ""
    llm_retryable: bool = False
    message_count: int = 0
    groups: list[str] = field(default_factory=_EMPTY_LIST)
    id: int = 0
    omitted_count: int = 0

    @property
    def has_focus(self) -> bool:
        return bool(self.items)

    @property
    def title(self) -> str:
        return KIND_TITLES.get(str(self.kind or ""), "群通知")

    @property
    def window_start_text(self) -> str:
        return _stamp_text(self.window_start)

    @property
    def window_end_text(self) -> str:
        return _stamp_text(self.window_end)

    def payload(self) -> list[dict[str, Any]]:
        """归档用的条目列表；键集合固定，老库读回来还能照原样渲染。"""
        result: list[dict[str, Any]] = []
        for item in self.items:
            message = item.get("message")
            text = str(getattr(message, "text", "") or "")
            category = str(item.get("category") or "info")
            if category not in VALID_CATEGORIES:
                category = "info"
            details = [
                str(value).strip()
                for value in (item.get("details") or ())
                if str(value).strip()
            ] if isinstance(item.get("details"), (list, tuple)) else []
            result.append(
                {
                    "category": category,
                    "importance": _as_int(item.get("importance"), IMPORTANCE_BY_CATEGORY.get(category, 3)),
                    "score": _as_int(item.get("score"), 0),
                    "summary": str(item.get("summary") or "").strip() or qq_digest.local_summary(text),
                    "audience": str(item.get("audience") or "").strip(),
                    "condition": str(item.get("condition") or "").strip(),
                    "details": details,
                    "action": str(item.get("action") or "").strip() or qq_digest.local_action(text),
                    "evidence": str(item.get("evidence") or "").strip(),
                    "deadline": _clock(item.get("deadline_dt")),
                    "group": qq_digest.item_group(item),
                    "group_id": str(item.get("group_id") or ""),
                    "sender": str(getattr(message, "sender", "") or ""),
                    "text": text,
                    "msg_id": str(item.get("msg_id") or ""),
                }
            )
        return result


def record_to_message(record: Mapping[str, Any]) -> qq_digest.Message:
    """库里的消息行 → 内核认的消息值对象。时间按 ts、received_at 依次回退。"""
    stamp = qq_digest.parse_timestamp(record.get("ts"))
    if stamp is None:
        stamp = qq_digest.parse_timestamp(record.get("received_at"))
    if stamp is None:
        stamp = now_local()
    return qq_digest.Message(
        timestamp=stamp,
        sender=_first_text(record.get("sender_name"), record.get("sender_id"), "群成员"),
        text=str(record.get("content") or ""),
        source=_first_text(record.get("group_name"), record.get("group_id")),
    )


def payload_to_item(entry: Mapping[str, Any]) -> dict[str, Any]:
    """归档条目 → 可再次渲染的条目（历史摘要回看时用）。"""
    item = dict(entry)
    text = str(entry.get("text") or "")
    stamp = qq_digest.parse_timestamp(entry.get("created_at"))
    item["message"] = qq_digest.Message(
        timestamp=stamp,
        sender=str(entry.get("sender") or ""),
        text=text,
        source=_first_text(entry.get("group"), entry.get("group_id")),
    )
    category = str(entry.get("category") or "info")
    if category not in VALID_CATEGORIES:
        category = "info"
    item["category"] = category
    item["importance"] = _as_int(entry.get("importance"), IMPORTANCE_BY_CATEGORY.get(category, 3))
    item["score"] = _as_int(entry.get("score"), 0)
    item["deadline_dt"] = qq_digest.parse_timestamp(entry.get("deadline"))
    item["group_name"] = _first_text(entry.get("group"))
    item["group_id"] = str(entry.get("group_id") or "")
    item["msg_id"] = str(entry.get("msg_id") or "")
    details = entry.get("details")
    item["details"] = [str(value) for value in details if str(value).strip()] if isinstance(details, list) else []
    if not str(item.get("evidence") or "").strip():
        item["evidence"] = evidence_sentence(text, item)
    return item


def analyse_records(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """逐条分析，并把「这条消息从哪来」补到分析结果上。"""
    analyses: list[dict[str, Any]] = []
    for record in records:
        message = record_to_message(record)
        analysis = qq_digest.analyze_message(message)
        analysis["msg_id"] = str(record.get("msg_id") or "")
        analysis["group_id"] = str(record.get("group_id") or "")
        analysis["group_name"] = _first_text(record.get("group_name"), record.get("group_id"))
        analysis["record"] = dict(record)
        analysis["deadline_dt"] = analysis.get("deadline")
        analyses.append(analysis)
    return analyses


def is_focus(analysis: dict[str, Any], settings: Any) -> bool:
    """这条值不值得进摘要：口语提问、低优先级群的闲聊都挡在这里。"""
    if bool(analysis.get("colloquial_question")):
        return False
    if is_quiet_group_item(analysis, settings) and not is_notice_item(analysis, settings):
        return False
    score = _as_int(analysis.get("score"), 0)
    if str(analysis.get("category") or "") == "urgent":
        return score >= 1
    return score >= _as_int(getattr(settings, "min_score", 3), 3)


def _confidence_of(analysis: Mapping[str, Any], settings: Any) -> float:
    """把「像不像一条真待办」量化成 0.05–0.98 的置信度。"""
    message = analysis.get("message")
    text = str(getattr(message, "text", "") or "")
    tags = set(analysis.get("tags") or ())
    action_words = list(analysis.get("action_words") or ())
    category = str(analysis.get("category") or "info")
    deadline = analysis.get("deadline_dt") or analysis.get("deadline")
    confidence = 0.28
    if deadline:
        confidence += 0.25
    if any(word in text for word in DIRECTIVE_WORDS):
        confidence += 0.20
    if action_words or any(word in text for word in ACTION_WORDS):
        confidence += 0.14
    if category == "urgent":
        confidence += 0.10
    elif category == "action":
        confidence += 0.08
    if str(analysis.get("evidence") or "").strip():
        confidence += 0.05
    if _as_int(analysis.get("score"), 0) >= 4:
        confidence += 0.05
    if any(word in text for word in WEAK_WORDS):
        confidence -= 0.24
    if any(word in text for word in VAGUE_TIME_WORDS):
        confidence -= 0.14
    if is_quiet_group_item(analysis, settings):
        confidence -= 0.08
    if not action_words:
        confidence -= 0.08
    return round(min(0.98, max(0.05, confidence)), 2)


def _task_reason(analysis: Mapping[str, Any], settings: Any) -> tuple[str, str]:
    """给定一条分析，返回（状态，理由）。顺序即优先级，和规格表一致。"""
    message = analysis.get("message")
    text = str(getattr(message, "text", "") or "")
    tags = set(analysis.get("tags") or ())
    action_words = list(analysis.get("action_words") or ())
    deadline = analysis.get("deadline_dt") or analysis.get("deadline")
    category = str(analysis.get("category") or "info")
    has_direct = bool(action_words) or any(word in text for word in ACTION_WORDS)
    has_directive = any(word in text for word in DIRECTIVE_WORDS)

    weak = next((word for word in WEAK_WORDS if word in text), "")
    if weak:
        return "candidate", "语气不确定（「%s」），先放到待确认。" % weak
    if deadline and (has_direct or has_directive):
        return "open", "有明确行动要求和截止时间，直接进入待办。"
    if category == "urgent" and (has_direct or has_directive or "urgent" in tags):
        return "open", "规则判断为紧急事项，直接进入待办。"
    if has_directive and has_direct:
        return "open", "有明确的行动要求，没有写截止时间。"
    vague = next((word for word in VAGUE_TIME_WORDS if word in text), "")
    if deadline and not has_direct and not has_directive:
        return "candidate", "提到了日期，但更像活动日期而不是截止时间。"
    if vague:
        return "candidate", "时间说法模糊（「%s」），先确认是否真的要办。" % vague
    if not deadline and has_direct:
        return "candidate", "识别到行动，但没有明确截止时间，先确认是否真的要办。"
    return "candidate", "只有行动线索，证据还不够直接，先放到待确认。"


def classify_task_item(item: Mapping[str, Any], settings: Any) -> dict[str, Any]:
    """一条消息值不值得变成待办：直接建（open）还是等用户确认（candidate）。"""
    analysis = dict(item)
    status, reason = _task_reason(analysis, settings)
    return {
        "status": status,
        "confidence": _confidence_of(analysis, settings),
        "reason": reason,
        "source": _first_text(analysis.get("source"), "qq_message"),
    }


def filter_recent_duplicates(
    analyses: Sequence[dict[str, Any]],
    history: Sequence[Mapping[str, Any]],
    settings: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """和最近已推过的内容比一遍，把重复的挑出来单独返回。"""
    hours = _as_int(getattr(settings, "dedupe_hours", 6), 6)
    if hours <= 0 or not history:
        return list(analyses), []

    remembered: list[tuple[str, Mapping[str, Any]]] = []
    for entry in history:
        if not isinstance(entry, Mapping):
            continue
        candidate_text = _first_text(entry.get("text"), entry.get("summary"))
        if candidate_text:
            remembered.append((qq_digest.normalize_for_match(candidate_text), entry))
    if not remembered:
        return list(analyses), []

    kept: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    for analysis in analyses:
        message = analysis.get("message")
        text = qq_digest.normalize_for_match(str(getattr(message, "text", "") or ""))
        match = next(
            (entry for known, entry in remembered if qq_digest.similar_text(text, known)),
            None,
        )
        if match is None:
            kept.append(analysis)
            continue
        analysis["suppressed_duplicate"] = {
            "group": _first_text(match.get("group")),
            "summary": _first_text(match.get("summary")),
            "ts": _first_text(match.get("ts")),
        }
        suppressed.append(analysis)

    if suppressed:
        names = sorted({str(item["suppressed_duplicate"].get("group") or "") for item in suppressed})
        label = "、".join(name for name in names if name) or "来源未知"
        LOGGER.info(
            "跨群去重：%d 条与最近 %d 小时内已推内容重复，不再重复推送（%s）。",
            len(suppressed),
            hours,
            label,
        )
    return kept, suppressed


def urgent_analyses(
    analyses: Sequence[dict[str, Any]],
    settings: Any,
    *,
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """够格「立刻推送」的那一小撮：紧急、有实质内容、四十八小时内要办。"""
    if not bool(getattr(settings, "urgent_immediate", True)):
        return []
    moment = now or now_local()
    horizon = moment + dt.timedelta(hours=48)
    result: list[dict[str, Any]] = []
    for analysis in analyses:
        if is_quiet_group_item(analysis, settings):
            continue
        if str(analysis.get("category") or "") != "urgent":
            continue
        if _as_int(analysis.get("score"), 0) < 4:
            continue
        if not (set(analysis.get("tags") or ()) & URGENT_TAGS):
            continue
        message = analysis.get("message")
        text = str(getattr(message, "text", "") or "")
        deadline = analysis.get("deadline_dt") or analysis.get("deadline")
        if any(word in text for word in STRONG_URGENT_WORDS):
            result.append(analysis)
            continue
        if list(analysis.get("action_words") or ()):
            result.append(analysis)
            continue
        if deadline and moment <= deadline <= horizon:
            result.append(analysis)
    return result


def evidence_sentence(text: str, item: Mapping[str, Any] | None = None, limit: int = 300) -> str:
    """从原文里挑出最能当证据的那一句，给推送和界面引用。"""
    flat = WHITESPACE.sub(" ", str(text or "")).strip()
    if not flat:
        return ""
    sentences = [part.strip() for part in SENTENCE_SPLIT.split(flat) if part.strip()]
    if not sentences:
        return flat[:limit]

    keywords: list[str] = []
    deadline = (item or {}).get("deadline_dt") if isinstance(item, Mapping) else None
    if isinstance(deadline, dt.datetime):
        keywords.extend(
            [
                deadline.strftime("%m-%d"),
                "%d月%d日" % (deadline.month, deadline.day),
                "%d月%d号" % (deadline.month, deadline.day),
                "%d日" % deadline.day,
            ]
        )
        if deadline.hour or deadline.minute:
            keywords.append(deadline.strftime("%H:%M"))
    keywords.extend(STRONG_URGENT_WORDS)
    keywords.extend(EVIDENCE_WORDS)

    for sentence in sentences:
        if any(word and word in sentence for word in keywords):
            return sentence[:limit]
    return sentences[0][:limit]


def _default_deadline(now: dt.datetime) -> dt.datetime:
    return now + dt.timedelta(days=1)


def _complete_item(item: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    """把条目缺的展示字段补齐：摘要、动作、适用对象、证据、重要度。"""
    message = item.get("message")
    text = str(getattr(message, "text", "") or "")
    category = str(item.get("category") or "info")
    if category not in VALID_CATEGORIES:
        category = "info"
    item["category"] = category
    if not str(item.get("summary") or "").strip():
        item["summary"] = qq_digest.local_summary(text)
    if not str(item.get("action") or "").strip():
        item["action"] = qq_digest.local_action(text)
    item.setdefault("audience", "")
    item.setdefault("condition", "")
    details = item.get("details")
    item["details"] = [str(value) for value in details if str(value).strip()] if isinstance(details, (list, tuple)) else []
    item.setdefault("evidence", evidence_sentence(text, item))
    if not item.get("evidence"):
        item["evidence"] = evidence_sentence(text, item)
    item["importance"] = _as_int(item.get("importance"), IMPORTANCE_BY_CATEGORY.get(category, 3))
    item["score"] = _as_int(item.get("score"), 0)
    item["deadline_dt"] = item.get("deadline_dt") or item.get("deadline")
    item["group_name"] = _first_text(item.get("group_name"), getattr(message, "source", ""))
    item.setdefault("msg_id", str(item.get("msg_id") or ""))
    return item


def _order_key(item: Mapping[str, Any]) -> tuple:
    message = item.get("message")
    deadline = item.get("deadline_dt")
    stamp = getattr(message, "timestamp", None)
    return (
        CATEGORY_ORDER.get(str(item.get("category") or "info"), 4),
        -_as_int(item.get("importance"), 0),
        -_as_int(item.get("score"), 0),
        deadline or dt.datetime.max,
        stamp or dt.datetime.max,
    )


def _refine_with_model(
    items: list[dict[str, Any]],
    settings: Any,
) -> tuple[list[dict[str, Any]], bool, str, bool]:
    """问一次模型；任何失败都退回本地规则，绝不让摘要流程挂掉。"""
    if not bool(getattr(settings, "llm_enabled", True)) or not items:
        return items, False, "", False

    backend = str(getattr(settings, "llm_backend", "auto") or "auto").strip().lower() or "auto"
    if backend not in LLM_BACKENDS:
        raise RuntimeError("不支持的 LLM backend: %s" % backend)

    api_key = str(getattr(settings, "dashscope_api_key", "") or "").strip()
    if not api_key and backend not in KEYLESS_BACKENDS:
        # 配置缺失不是可重试故障：重试也变不出密钥，只会把消息一直推迟。
        return items, False, "未配置 DASHSCOPE_API_KEY", False

    try:
        refined = qq_digest.refine_with_dashscope(
            items,
            api_key,
            str(getattr(settings, "dashscope_model", "qwen-plus") or "qwen-plus"),
            str(getattr(settings, "dashscope_endpoint", "") or ""),
            _as_int(getattr(settings, "llm_timeout", 60), 60),
            retries=_as_int(getattr(settings, "llm_max_retries", 2), 2),
            backoff=_as_float(getattr(settings, "llm_retry_backoff", 1.5), 1.5),
            backend=backend,
        )
    except RuntimeError as error:
        LOGGER.warning("LLM 精炼失败，回退本地规则：%s", error)
        return items, False, str(error), bool(llm_should_retry(error))
    return list(refined), True, "", False


def build_digest(
    settings: Any,
    records: Iterable[Mapping[str, Any]],
    *,
    kind: str = "window",
    now: dt.datetime | None = None,
    history: Sequence[Mapping[str, Any]] | None = None,
) -> Digest:
    """把一批消息做成一份摘要：筛、去重、可选精炼、排序、补齐字段。"""
    moment = now or now_local()
    rows = [dict(record) for record in records]
    analyses = analyse_records(rows)
    if str(kind) == "urgent":
        analyses = urgent_analyses(analyses, settings, now=moment)

    candidates = [analysis for analysis in analyses if is_focus(analysis, settings)]
    if history:
        candidates, _suppressed = filter_recent_duplicates(candidates, history, settings)
    candidates = qq_digest.dedupe_items(candidates)
    candidates.sort(key=lambda value: _as_int(value.get("score"), 0), reverse=True)

    max_items = max(0, _as_int(getattr(settings, "max_items", 30), 30))
    omitted = max(0, len(candidates) - max_items)
    if omitted:
        LOGGER.warning("本次 %d 条重点超出单次上限 %d，未展开。", omitted, max_items)
    selected = [_complete_item(item, moment) for item in candidates[:max_items]] if max_items else []

    llm_used = False
    llm_error = ""
    llm_retryable = False
    try:
        selected, llm_used, llm_error, llm_retryable = _refine_with_model(selected, settings)
    except RuntimeError as error:
        LOGGER.warning("LLM 精炼失败，回退本地规则：%s", error)
        llm_error = str(error)
        llm_retryable = bool(llm_should_retry(error))

    selected.sort(key=_order_key)

    message_ids = [str(record.get("msg_id") or "") for record in rows]
    groups: list[str] = []
    for record in rows:
        name = _first_text(record.get("group_name"), record.get("group_id"))
        if name and name not in groups:
            groups.append(name)
    first_stamp = qq_digest.parse_timestamp(rows[0].get("received_at")) if rows else None
    digest = Digest(
        kind=str(kind),
        window_start=first_stamp or moment,
        window_end=moment,
        items=selected,
        message_ids=message_ids,
        message_count=len(rows),
        groups=groups,
        llm_used=llm_used,
        llm_error=llm_error,
        llm_retryable=llm_retryable,
        omitted_count=omitted,
    )
    return finalize_digest(digest, settings)


def finalize_digest(digest: Digest, settings: Any) -> Digest:
    """生成推送正文、通知栏一行和 HTML 卡片；没有条目就原样返回。"""
    if not digest.items:
        return digest
    digest.body = format_push_text(digest, settings)
    digest.summary = push_summary(digest)
    digest.html = html_push_text(digest, settings) if bool(getattr(settings, "html_push", True)) else ""
    return digest


def _group_scope(digest: Digest, limit: int) -> str:
    scope = "、".join(name for name in digest.groups if name) or "未知群"
    return scope[:limit]


def format_push_text(digest: Digest, settings: Any) -> str:
    """纯文本推送正文。默认不附原文，避免把群里长段落搬进通知。"""
    moment = digest.window_end or now_local()
    head_time = digest.window_end_text or moment.strftime("%Y-%m-%d %H:%M")
    lines: list[str] = ["【QQ%s】%s" % (digest.title, head_time)]
    counts = "%s · %d 条消息 · %d 条重点" % (
        _group_scope(digest, 40),
        digest.message_count,
        len(digest.items),
    )
    if digest.omitted_count:
        counts += "（另有 %d 条超出单次上限未展开）" % digest.omitted_count
    lines.append(counts)
    lines.append("")

    include_raw = bool(getattr(settings, "include_raw", False))
    for index, item in enumerate(digest.items, 1):
        summary = qq_digest.short_summary(str(item.get("summary") or ""), 60)
        label = CATEGORY_LABELS.get(str(item.get("category") or "info"), "通知")
        lines.append("%d. [%s] %s" % (index, label, summary))

        parts: list[str] = []
        deadline_text = _clock(item.get("deadline_dt"))
        if deadline_text:
            parts.append("截止：%s" % deadline_text)
        action = qq_digest.short_summary(str(item.get("action") or ""), 30)
        if action and action not in summary:
            parts.append("需做：%s" % action)
        senders = list(item.get("duplicate_senders") or [])
        others = list(item.get("duplicate_groups") or [])
        group = qq_digest.item_group(item)
        if len(others) > 1:
            group_text = "、".join(others)
        else:
            group_text = group
        source_bits = [group_text, ", ".join(senders) or str(getattr(item.get("message"), "sender", "") or "")]
        source_line = " · ".join(bit for bit in source_bits if bit)[:45]
        if source_line:
            parts.append(source_line)
        if parts:
            lines.append("   " + " · ".join(parts))

        text = str(getattr(item.get("message"), "text", "") or "")
        links = qq_digest.extract_links(text)
        if links:
            lines.append("   链接：%s" % links[0])
        if include_raw and text:
            raw = qq_digest.short_summary(text, 80)
            if raw and raw not in summary:
                lines.append("   原文：%s" % raw)
        lines.append("")

    if digest.llm_error:
        lines.append("（AI 摘要暂不可用，已用本地规则筛选）")
    return "\n".join(lines).rstrip()


def push_summary(digest: Digest, limit: int = 26) -> str:
    """通知栏那一行：最近一条的截止时间 + 短摘要，还有别的内容就报个数。"""
    if not digest.items:
        return digest.title
    first = digest.items[0]
    moment = digest.window_end or now_local()
    deadline = first.get("deadline_dt")
    label = ""
    if isinstance(deadline, dt.datetime):
        label = _day_label(deadline, moment)
        if deadline.hour or deadline.minute:
            label += deadline.strftime("%H:%M")
    summary = qq_digest.short_summary(str(first.get("summary") or ""), 14)
    line = " ".join(part for part in (label, summary) if part)
    if len(digest.items) > 1:
        line += " +%d条" % (len(digest.items) - 1)
    return line[:limit]


def _html_item(item: Mapping[str, Any]) -> str:
    """一条「要做的事」的色块。所有插值都转义，避免群里发来的标签被执行。"""
    status = str(item.get("category") or "info")
    accent = "#d93025" if status == "urgent" else "#f29900"
    name = "紧急" if status == "urgent" else "待办"
    summary = qq_digest.short_summary(str(item.get("summary") or ""), 46)
    deadline_text = _clock(item.get("deadline_dt"))
    title = "%s 截止 · %s" % (deadline_text, summary) if deadline_text else summary
    blocks = [
        '<div style="background:#fff8ec;border-left:3px solid %s;padding:10px 12px;margin:0 0 8px;border-radius:4px">'
        % accent,
        '<div style="font-size:12px;color:%s;font-weight:600">%s</div>' % (accent, html_module.escape(name)),
        '<div style="font-size:14px;line-height:1.5;color:#1f1f1f">%s</div>' % html_module.escape(title),
    ]
    action = qq_digest.short_summary(str(item.get("action") or ""), 30)
    if action:
        blocks.append('<div style="font-size:13px;color:#444">需做：%s</div>' % html_module.escape(action))
    meta: list[str] = []
    group = qq_digest.item_group(item)
    if group:
        meta.append(group)
    sender = str(getattr(item.get("message"), "sender", "") or "")
    if sender:
        meta.append(sender)
    if meta:
        blocks.append(
            '<div style="font-size:12px;color:#777">%s</div>' % html_module.escape(" · ".join(meta)[:40])
        )
    audience = str(item.get("audience") or "").strip()
    condition = str(item.get("condition") or "").strip()
    if audience or condition:
        bits = []
        if audience:
            bits.append("适用：%s" % audience)
        if condition:
            bits.append("条件：%s" % condition)
        blocks.append(
            '<div style="font-size:12px;color:#777">%s</div>' % html_module.escape(" · ".join(bits)[:90])
        )
    evidence = str(item.get("evidence") or "").strip()
    summary_plain = str(item.get("summary") or "")
    if evidence and evidence not in summary_plain:
        blocks.append(
            '<div style="font-size:12px;color:#999">原文：%s</div>' % html_module.escape(evidence[:40])
        )
    text = str(getattr(item.get("message"), "text", "") or "")
    links = qq_digest.extract_links(text)
    if links:
        blocks.append(
            '<div style="font-size:12px"><a href="%s">相关链接</a></div>' % html_module.escape(links[0])
        )
    blocks.append("</div>")
    return "".join(blocks)


def html_push_text(digest: Digest, settings: Any) -> str:
    """HTML 卡片：值得动手的最多五块，其余合成一行。"""
    moment = digest.window_end or now_local()
    head_time = digest.window_end_text or moment.strftime("%Y-%m-%d %H:%M")
    head = "%s · %s · %d 条消息" % (head_time, _group_scope(digest, 30), digest.message_count)
    if digest.omitted_count:
        head += " · 另有 %d 条超出单次上限未展开" % digest.omitted_count
    blocks = ['<div style="font-size:12px;color:#888">%s</div>' % html_module.escape(head)]

    prominent = [
        item for item in digest.items if str(item.get("category") or "") in {"urgent", "action"}
    ][:5]
    for item in prominent:
        blocks.append(_html_item(item))

    rest = [item for item in digest.items if item not in prominent]
    if rest:
        heads = [qq_digest.short_summary(str(item.get("summary") or ""), 22) for item in rest[:5]]
        line = "其余 %d 条：%s" % (len(rest), " · ".join(heads))
        if len(rest) > 5:
            line += " 等 %d 条" % len(rest)
        blocks.append('<div style="font-size:12px;color:#555">%s</div>' % html_module.escape(line))

    if digest.llm_error:
        blocks.append(
            '<div style="font-size:12px;color:#888">（AI 摘要暂不可用，已用本地规则筛选）</div>'
        )
    return "".join(blocks)


def preview_text(settings: Any, records: Iterable[Mapping[str, Any]]) -> str:
    """命令行预览：不发推送，只说明这次会推什么。"""
    rows = [dict(record) for record in records]
    if not rows:
        return "没有待处理消息。"
    analyses = analyse_records(rows)
    candidates = [analysis for analysis in analyses if is_focus(analysis, settings)]
    if not candidates:
        return "待处理 %d 条消息，未筛出重点内容（不会推送）。" % len(rows)
    digest = build_digest(settings, rows, kind="window")
    return "[仅预览] 待处理 %d 条，命中 %d 条\n\n%s" % (len(rows), len(digest.items), digest.body)
