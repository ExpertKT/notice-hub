"""群消息全文检索（实验性，默认关闭）。

只提供纯函数与一个类，不注册路由、不挂钩任何既有流程：没人显式调用它时，它
对现有功能零影响。要用请自己在设置页「实验室」打开后接到接口上。

索引怎么建
    `messages_fts` 是 FTS5 的 external content 表（`content='messages'`），只存
    token，不复制正文；按 rowid 增量追平，追平进度记在 `search_state` 里，不碰
    `meta` 和 `messages` 本身。撤销/回填整表用 `ensure_index(..., rebuild=True)`。

中文为什么用 trigram
    `unicode61` 不分词中文，`trigram` 能按 3 字符子串匹配（`显卡价格` 能命中）。
    代价：**2 个字符以下查询 trigram 命中不了**，所以短查询自动退回 LIKE 全表
    扫描（结果正确、速度慢）。
    ponytail: 短查询不分词只 LIKE，几万条消息够用；真慢了再上分词器。

已知边界
    消息被 `Store.prune()` 清掉后，FTS 里会留悬空 rowid；查询走 JOIN，悬空行
    自然被过滤掉，不会返回幽灵结果，但索引文件不会自己缩（要 `rebuild=True`）。
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from .timeutil import iso

LOGGER = logging.getLogger(__name__)

TOKENIZER = "trigram"
# trigram 至少要 3 个字符才能命中；更短的查询退回 LIKE。
TRIGRAM_MIN_CHARS = 3

_INDEX_TABLE = "messages_fts"
_STATE_TABLE = "search_state"
_CATCHUP_KEY = "messages_max_rowid"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS search_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content='messages',
    content_rowid='rowid',
    tokenize='trigram'
);
"""


class SearchError(RuntimeError):
    """索引不可用时抛出（数据库只读、被锁住、不是 notice-hub 的库等）。"""


def _connect(db_path: Path | str) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path), timeout=15)
    connection.row_factory = sqlite3.Row
    return connection


def _max_message_rowid(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT COALESCE(MAX(rowid), 0) FROM messages").fetchone()
    return int(row[0] or 0)


def _indexed_rowid(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT value FROM search_state WHERE key = ?", (_CATCHUP_KEY,)
    ).fetchone()
    try:
        return int(row[0]) if row else 0
    except (TypeError, ValueError):
        return 0


def ensure_index(
    db_path: Path | str,
    *,
    rebuild: bool = False,
) -> int:
    """建索引并按 rowid 增量追平，返回追平后的最大 rowid。

    表已存在时只补新增消息；`rebuild=True` 清空重来（消息被批量删掉后想缩索引
    时用）。库只读时抛 `SearchError`，信息里带原因。
    """
    connection: sqlite3.Connection | None = None
    try:
        try:
            connection = _connect(db_path)
            connection.executescript(_SCHEMA)
            if rebuild:
                connection.execute("INSERT INTO messages_fts(messages_fts) VALUES('delete-all')")
                connection.execute("DELETE FROM search_state WHERE key = ?", (_CATCHUP_KEY,))
            low = _indexed_rowid(connection)
            high = _max_message_rowid(connection)
            if high > low:
                connection.execute(
                    """
                    INSERT INTO messages_fts(rowid, content)
                    SELECT rowid, content FROM messages WHERE rowid > ? ORDER BY rowid
                    """,
                    (low,),
                )
                connection.execute(
                    "INSERT OR REPLACE INTO search_state(key, value) VALUES (?, ?)",
                    (_CATCHUP_KEY, str(high)),
                )
                LOGGER.info("消息索引追平 %d → %d", low, high)
            connection.commit()
            return high
        except sqlite3.OperationalError as exc:
            if connection is not None:
                connection.rollback()
            raise SearchError("消息索引不可用：%s" % (exc,)) from exc
    finally:
        if connection is not None:
            connection.close()


def _tokens(query: str) -> list[str]:
    return [token for token in str(query or "").split() if token]


def _quote(token: str) -> str:
    """包成 FTS5 短语，顺带把用户输入的语法字符（" * OR NEAR(）当字面量。"""
    return '"' + token.replace('"', '""') + '"'


def _iso_or_empty(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value.strip()
    return iso(value)


def search(
    db_path: Path | str,
    query: str,
    *,
    group_id: str | None = None,
    since: Any = None,
    until: Any = None,
    limit: int = 50,
) -> dict[str, Any]:
    """全文检索消息，返回 `{"ok", "hits", "total", "mode", "error"}`。

    `hits` 每项含 `msg_id`/`group_id`/`group_name`/`sender_name`/`ts`/`snippet`。
    `mode` 是 `"fts"` 或 `"like"`（短查询走兜底），空查询为 `"none"`。
    查询串里的 FTS5 语法一律当字面量，不抛异常、不注入。
    """
    tokens = _tokens(query)
    if not tokens:
        return {"ok": True, "hits": [], "total": 0, "mode": "none", "error": None}

    filters: list[str] = []
    arguments: list[Any] = []
    if group_id:
        filters.append("m.group_id = ?")
        arguments.append(str(group_id))
    start = _iso_or_empty(since)
    if start:
        filters.append("m.ts >= ?")
        arguments.append(start)
    end = _iso_or_empty(until)
    if end:
        filters.append("m.ts <= ?")
        arguments.append(end)
    where = "".join(" AND " + item for item in filters)
    size = max(1, min(int(limit), 500))

    # 任一词短于 3 字符就走 LIKE：trigram 索引命不中短词，硬用会假阴性。
    use_fts = all(len(token) >= TRIGRAM_MIN_CHARS for token in tokens)
    if use_fts:
        match = " AND ".join(_quote(token) for token in tokens)
        sql = """
            SELECT m.msg_id, m.group_id, m.group_name, m.sender_name, m.ts,
                   snippet(messages_fts, 0, '[', ']', '…', 12) AS snippet,
                   bm25(messages_fts) AS score
            FROM messages_fts f
            JOIN messages m ON m.rowid = f.rowid
            WHERE messages_fts MATCH ?{where}
            ORDER BY score, m.ts DESC
            LIMIT ?""".format(where=where)
        count_sql = """
            SELECT COUNT(*)
            FROM messages_fts f
            JOIN messages m ON m.rowid = f.rowid
            WHERE messages_fts MATCH ?{where}""".format(where=where)
        parameters: list[Any] = [match] + arguments
        mode = "fts"
    else:
        like_clause = "".join(" AND m.content LIKE ?" for _ in tokens)
        sql = """
            SELECT m.msg_id, m.group_id, m.group_name, m.sender_name, m.ts,
                   substr(m.content, 1, 120) AS snippet,
                   0.0 AS score
            FROM messages m
            WHERE 1 = 1{like}{where}
            ORDER BY m.ts DESC
            LIMIT ?""".format(like=like_clause, where=where)
        count_sql = "SELECT COUNT(*) FROM messages m WHERE 1 = 1{like}{where}".format(
            like=like_clause, where=where
        )
        parameters = [("%" + token + "%") for token in tokens] + arguments
        mode = "like"

    try:
        ensure_index(db_path)
        connection = _connect(db_path)
    except SearchError as exc:
        return {"ok": False, "hits": [], "total": 0, "mode": mode, "error": str(exc)}
    except sqlite3.Error as exc:
        return {"ok": False, "hits": [], "total": 0, "mode": mode, "error": str(exc)}

    try:
        rows = connection.execute(sql, parameters + [size]).fetchall()
        total = int(connection.execute(count_sql, parameters).fetchone()[0] or 0)
    except sqlite3.Error as exc:  # 索引坏了/库被锁：报错不抛，界面还能用
        LOGGER.warning("消息检索失败：%s", exc)
        return {"ok": False, "hits": [], "total": 0, "mode": mode, "error": str(exc)}
    finally:
        connection.close()

    hits = [
        {
            "msg_id": row["msg_id"],
            "group_id": row["group_id"],
            "group_name": row["group_name"],
            "sender_name": row["sender_name"],
            "ts": row["ts"],
            "snippet": row["snippet"] or "",
        }
        for row in rows
    ]
    return {"ok": True, "hits": hits, "total": total, "mode": mode, "error": None}


class MessageSearch:
    """把 `search()`/`ensure_index()` 绑到一个库上，省得每次传路径。"""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)

    def ensure_index(self, *, rebuild: bool = False) -> int:
        return ensure_index(self.db_path, rebuild=rebuild)

    def search(
        self,
        query: str,
        *,
        group_id: str | None = None,
        since: Any = None,
        until: Any = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        return search(
            self.db_path,
            query,
            group_id=group_id,
            since=since,
            until=until,
            limit=limit,
        )


__all__ = ["MessageSearch", "SearchError", "ensure_index", "search"]