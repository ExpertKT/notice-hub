"""待回清单（实验室）：按规则从已有消息里挑出「大概需要我回一句」的，存进 reply_pending。

三条硬边界：
1. 纯规则，不调模型；判定依据只有正文与发送者，不做语义推断。
2. 只新建 reply_pending 表，不改现有表结构、不动消息主流程。
3. 默认不扫描、不挂钩摘要/推送；实验室里手动开启后由界面显式调用。
"""

from __future__ import annotations

import re
from typing import Any

from .timeutil import iso, now_local

SCHEMA = """
CREATE TABLE IF NOT EXISTS reply_pending (
    msg_id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL DEFAULT '',
    sender TEXT NOT NULL DEFAULT '',
    snippet TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    ack_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_reply_pending_ack ON reply_pending(ack_at, created_at);
"""

RULE_AT = "提到我"
RULE_QUESTION = "疑问句"
RULE_DEADLINE = "截止时间"
RULE_CONFIRM = "要求确认"

_QUESTION_MARKS = ("?", "？")
_QUESTION_WORDS = ("求", "帮", "请问", "在吗", "有没有", "怎么", "如何")
_DEADLINE_WORDS = ("今晚", "明天", "明早", "后天", "几号前", "截止前", "截止时间", "截止日期", "尽快", "之前完成", "ddl")
_CONFIRM_WORDS = ("收到请回复", "收到请回", "请回复", "确认一下", "能不能", "麻烦确认", "是否收到", "回复一下")

_CQ_CODE = re.compile(r"\[CQ:[^\]]*\]")
_EMOJI = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")
_PUNCT = re.compile(r"[\s，。！？!?、,.~～·…—\-_]+")
_SNIPPET_CHARS = 120


def _ensure_schema(store: Any) -> None:
    with store._connect() as connection:  # noqa: SLF001 - 同包内复用私有连接，避免改 store.py
        connection.executescript(SCHEMA)


def _text(message: dict[str, Any]) -> str:
    content = str(message.get("content") or "")
    source_text = str(message.get("source_text") or "")
    if not content:
        return source_text
    if not source_text or source_text == content:
        return content
    return f"{content}\n{source_text}"


def _plain(text: str) -> str:
    """去掉 CQ 码，只留人看的正文。"""
    return _CQ_CODE.sub("", text)


def _mentions_me(text: str, me_qq: str, me_nickname: str) -> bool:
    if me_nickname and me_nickname in text:
        return True
    if me_qq:
        marker = re.escape(me_qq)
        if re.search(rf"@{marker}(?!\d)", text) or re.search(rf"qq={marker}(?!\d)", text):
            return True
    return False


def detect_reasons(text: str, *, me_qq: str = "", me_nickname: str = "") -> list[str]:
    """按四类规则判定一条消息，返回命中的规则名（可能多条）。"""
    reasons: list[str] = []
    if _mentions_me(text, me_qq, me_nickname):
        reasons.append(RULE_AT)
    if any(mark in text for mark in _QUESTION_MARKS) or any(word in text for word in _QUESTION_WORDS):
        reasons.append(RULE_QUESTION)
    if any(word in text for word in _DEADLINE_WORDS):
        reasons.append(RULE_DEADLINE)
    if any(word in text for word in _CONFIRM_WORDS):
        reasons.append(RULE_CONFIRM)
    return reasons


def _is_noise(text: str) -> bool:
    """纯表情/图片/标点：正文里剥掉 CQ 码、表情、标点后什么都不剩。"""
    plain = _plain(text)
    plain = _EMOJI.sub("", plain)
    plain = _PUNCT.sub("", plain)
    return not plain


def _is_self(message: dict[str, Any], me_qq: str, me_nickname: str) -> bool:
    sender_id = str(message.get("sender_id") or "")
    sender_name = str(message.get("sender_name") or "")
    if me_qq and sender_id == me_qq:
        return True
    if me_nickname and sender_name == me_nickname:
        return True
    return False


def _snippet(text: str) -> str:
    plain = _PUNCT.sub(" ", _plain(text)).strip()
    plain = re.sub(r"\s+", " ", plain)
    return plain[:_SNIPPET_CHARS]


def _sender_label(message: dict[str, Any]) -> str:
    name = str(message.get("sender_name") or "").strip()
    if name:
        return name
    return str(message.get("sender_id") or "").strip()


def scan(
    store: Any,
    *,
    me_qq: str = "",
    me_nickname: str = "",
    limit: int = 500,
    now: Any = None,
) -> dict[str, int]:
    """扫最近 limit 条消息，把命中的写进 reply_pending（幂等：已存在的不重复插）。"""
    _ensure_schema(store)
    me_qq = str(me_qq or "").strip()
    me_nickname = str(me_nickname or "").strip()
    created_at = iso(now or now_local())
    scanned = matched = inserted = 0
    with store._connect() as connection:  # noqa: SLF001 - 同包内复用私有连接，避免改 store.py
        rows = connection.execute(
            "SELECT * FROM messages ORDER BY received_at DESC, msg_id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        for row in rows:
            message = dict(row)
            scanned += 1
            if _is_self(message, me_qq, me_nickname):
                continue
            text = _text(message)
            if _is_noise(text):
                continue
            reasons = detect_reasons(text, me_qq=me_qq, me_nickname=me_nickname)
            if not reasons:
                continue
            matched += 1
            cursor = connection.execute(
                "INSERT OR IGNORE INTO reply_pending "
                "(msg_id, group_id, sender, snippet, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(message.get("msg_id") or ""),
                    str(message.get("group_id") or ""),
                    _sender_label(message),
                    _snippet(text),
                    "、".join(reasons),
                    created_at,
                ),
            )
            if cursor.rowcount > 0:
                inserted += 1
    return {"scanned": scanned, "matched": matched, "inserted": inserted}


def list_pending(
    store: Any,
    *,
    limit: int = 100,
    group_id: str | None = None,
) -> list[dict[str, Any]]:
    """未回的待回条目，新的在前；group_id 给了就只看那个群。"""
    _ensure_schema(store)
    sql = "SELECT * FROM reply_pending WHERE ack_at IS NULL"
    params: list[Any] = []
    if group_id:
        sql += " AND group_id = ?"
        params.append(str(group_id))
    sql += " ORDER BY created_at DESC, msg_id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    with store._connect() as connection:  # noqa: SLF001
        rows = connection.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def ack(store: Any, msg_id: str) -> bool:
    """标记已回（软删，不物理删除）。已经回过或不存在返回 False。"""
    _ensure_schema(store)
    with store._connect() as connection:  # noqa: SLF001
        cursor = connection.execute(
            "UPDATE reply_pending SET ack_at = ? WHERE msg_id = ? AND ack_at IS NULL",
            (iso(now_local()), str(msg_id or "")),
        )
    return cursor.rowcount > 0


# ---- 实验室门面（lab.list_replies / lab.ack_reply）用的薄包装：门面不传 settings/store ----


def _default_store() -> Any:
    from .config import Settings
    from .store import Store

    settings = Settings.from_env()
    return Store(settings.data_dir / "digest.sqlite3", retention_days=settings.message_retention_days)


def _identity(settings: Any) -> tuple[str, str]:
    return str(getattr(settings, "self_qq", "") or ""), str(getattr(settings, "self_nickname", "") or "")


def list_replies(**filters: Any) -> list[dict[str, Any]]:
    from .config import Settings

    settings = Settings.from_env()
    me_qq, me_nickname = _identity(settings)
    store = _default_store()
    # 门面打开即视为一次显式扫描：扫完再列，保证新消息能出现；重复扫不会重复插。
    scan(store, me_qq=me_qq, me_nickname=me_nickname)
    return list_pending(
        store,
        limit=int(filters.get("limit", 100)),
        group_id=filters.get("group") or filters.get("group_id"),
    )


def ack_reply(msg_id: str) -> bool:
    return ack(_default_store(), msg_id)


__all__ = [
    "scan",
    "list_pending",
    "ack",
    "detect_reasons",
    "list_replies",
    "ack_reply",
    "SCHEMA",
]