"""SQLite 存储：消息、摘要、投递、待办、收件箱、群名、元信息。

约定只有一条：**一个公开方法 = 一条连接 = 一个事务**。不共享连接、不建池、
不在方法之间保持事务；并发由 SQLite 自己的写锁加 15 秒忙碌等待兜住，模块内不加锁。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from noticehub.timeutil import iso, now_local, parse_iso

LOGGER = logging.getLogger("noticehub.store")

SCHEMA_VERSION = 1

TASK_STATUSES = frozenset({"candidate", "open", "done", "dismissed", "expired"})

# 唯一一份「分类 → 重要度」定义：摘要侧也必须拿到这个对象本身，不能各存一份字面量。
IMPORTANCE_BY_CATEGORY = {"urgent": 5, "action": 4, "academic": 3, "info": 3}

MESSAGE_COLUMN_MIGRATIONS: tuple[str, ...] = ("source_text TEXT NOT NULL DEFAULT ''",)

TASK_COLUMN_MIGRATIONS: tuple[str, ...] = (
    "audience TEXT NOT NULL DEFAULT ''",
    "condition_text TEXT NOT NULL DEFAULT ''",
    "details TEXT NOT NULL DEFAULT '[]'",
    "source TEXT NOT NULL DEFAULT ''",
    "confirmed_at TEXT NOT NULL DEFAULT ''",
    "dismissed_at TEXT NOT NULL DEFAULT ''",
    "last_reminded_at TEXT NOT NULL DEFAULT ''",
    "snooze_until TEXT NOT NULL DEFAULT ''",
    "confidence REAL NOT NULL DEFAULT 0",
    "remind_count INTEGER NOT NULL DEFAULT 0",
    "duplicate_of INTEGER NOT NULL DEFAULT 0",
    "pinned INTEGER NOT NULL DEFAULT 0",
    "urgent_override INTEGER",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    msg_id TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'qqbot',
    event TEXT NOT NULL DEFAULT '',
    group_id TEXT NOT NULL,
    group_name TEXT NOT NULL DEFAULT '',
    sender_id TEXT NOT NULL DEFAULT '',
    sender_name TEXT NOT NULL DEFAULT '',
    ts TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    source_text TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_messages_received ON messages(received_at);
CREATE INDEX IF NOT EXISTS idx_messages_group ON messages(group_id, received_at);
CREATE INDEX IF NOT EXISTS idx_messages_group_names ON messages(group_id, ts, group_name);

CREATE TABLE IF NOT EXISTS processed (
    msg_id TEXT PRIMARY KEY REFERENCES messages(msg_id),
    digest_id INTEGER,
    processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    window_start TEXT NOT NULL DEFAULT '',
    window_end TEXT NOT NULL DEFAULT '',
    item_count INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    llm_used INTEGER NOT NULL DEFAULT 0,
    body TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    digest_id INTEGER NOT NULL,
    channel TEXT NOT NULL,
    target TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (dedupe_key, channel, target)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_status ON deliveries(status, updated_at);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_key TEXT NOT NULL UNIQUE,
    summary TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    audience TEXT NOT NULL DEFAULT '',
    condition_text TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT '[]',
    category TEXT NOT NULL DEFAULT 'info',
    importance INTEGER NOT NULL DEFAULT 3,
    deadline TEXT NOT NULL DEFAULT '',
    groups TEXT NOT NULL DEFAULT '[]',
    sender TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    digest_id INTEGER NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    done_at TEXT NOT NULL DEFAULT '',
    snooze_until TEXT NOT NULL DEFAULT '',
    confirmed_at TEXT NOT NULL DEFAULT '',
    dismissed_at TEXT NOT NULL DEFAULT '',
    last_reminded_at TEXT NOT NULL DEFAULT '',
    remind_count INTEGER NOT NULL DEFAULT 0,
    duplicate_of INTEGER NOT NULL DEFAULT 0,
    urgent_override INTEGER,
    pinned INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, deadline, importance);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);

CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    event TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_task_events_created ON task_events(created_at);

CREATE TABLE IF NOT EXISTS inbox_marks (
    msg_id TEXT PRIMARY KEY,
    verdict TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    method TEXT NOT NULL DEFAULT '',
    classified_at TEXT NOT NULL,
    task_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_inbox_verdict ON inbox_marks(verdict, classified_at);

CREATE TABLE IF NOT EXISTS group_names (
    group_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dingtalk_sync (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_BROKEN_MARKERS = ("not a database", "malformed", "corrupt", "encrypted")

_TASK_ACTIONS = frozenset({"confirm", "dismiss", "done", "snooze", "reopen", "delete"})

_CORRECTIONS = frozenset(
    {"not_notice", "not_task", "not_urgent", "duplicate", "category", "deadline", "clear_deadline"}
)

_FINISHED_STATUSES = ("done", "dismissed", "expired")

_UPSERT_TASK = """
INSERT INTO tasks (
    task_key, summary, action, audience, condition_text, details, category,
    importance, deadline, groups, sender, evidence, digest_id, status,
    confidence, source, created_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(task_key) DO UPDATE SET
    summary = excluded.summary,
    action = excluded.action,
    audience = excluded.audience,
    condition_text = excluded.condition_text,
    details = excluded.details,
    category = excluded.category,
    groups = excluded.groups,
    sender = excluded.sender,
    digest_id = excluded.digest_id,
    importance = MAX(tasks.importance, excluded.importance),
    confidence = MAX(tasks.confidence, excluded.confidence),
    deadline = CASE WHEN excluded.deadline <> '' THEN excluded.deadline ELSE tasks.deadline END,
    evidence = CASE WHEN excluded.evidence <> '' THEN excluded.evidence ELSE tasks.evidence END,
    source = CASE WHEN excluded.source <> '' THEN excluded.source ELSE tasks.source END,
    updated_at = excluded.updated_at
"""

_PUNCTUATION = set("，。、；：！？,.!?;:（）()[]【】\"'“”‘’-_—")


# ---------------------------------------------------------------- 小工具


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return "[]"


def _json_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        parsed = list(value)
    elif not value:
        return []
    else:
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _clean_sequence(values: Any, *, unique: bool = False) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    cleaned = [str(item).strip() for item in values if str(item).strip()]
    return list(dict.fromkeys(cleaned)) if unique else cleaned


def _detail_text(detail: Any) -> str:
    if detail is None or detail == "":
        return ""
    if isinstance(detail, (dict, list)):
        return _json_text(detail)
    return str(detail)


def _normalise(text: Any) -> str:
    return "".join(
        character
        for character in str(text or "")
        if not character.isspace() and character not in _PUNCTUATION
    ).lower()


def _seconds_since(stamp: Any) -> float | None:
    moment = parse_iso(stamp)
    if moment is None:
        return None
    return (now_local() - moment).total_seconds()


def _first_row(path: Path, statement: str) -> tuple[Any, str]:
    """只读地取一行。返回 (行, 错误文本)；不建库、不写库。"""
    if not path.is_file():
        return None, ""
    failure = ""
    try:
        reader: sqlite3.Connection | None = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15
        )
    except sqlite3.Error:
        reader = None
    if reader is not None:
        try:
            return reader.execute(statement).fetchone(), ""
        except sqlite3.Error as error:
            failure = str(error)
        finally:
            reader.close()
    try:
        fallback = sqlite3.connect(path, timeout=15)
    except sqlite3.Error as error:
        return None, str(error) or failure
    try:
        return fallback.execute(statement).fetchone(), ""
    except sqlite3.Error as error:
        failure = str(error)
    finally:
        fallback.close()
    return None, failure


def _task_event_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": _as_int(row["id"]),
        "task_id": _as_int(row["task_id"]),
        "event": str(row["event"] or ""),
        "detail": str(row["detail"] or ""),
        "created_at": str(row["created_at"] or ""),
    }


def _digest_payload_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return list(payload)
    if not payload:
        return []
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


# ---------------------------------------------------------------- Store


class Store:
    def __init__(self, path: Any, *, retention_days: int = 30) -> None:
        self.path = Path(path)
        self.retention_days = _as_int(retention_days, 30)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.messages_audit = self.path.parent / "messages.jsonl"
        self.digests_audit = self.path.parent / "digests.jsonl"
        self.init()

    # -------------------------------------------------------- 连接与初始化

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def init(self) -> None:
        recovered = self._quarantine_broken_database()
        version = self._stored_version()
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                "数据库结构版本 %d 高于本程序支持的 %d，请升级 notice-hub 后再启动：%s"
                % (version, SCHEMA_VERSION, self.path.resolve())
            )
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            self._add_columns(connection, "messages", MESSAGE_COLUMN_MIGRATIONS)
            self._add_columns(connection, "tasks", TASK_COLUMN_MIGRATIONS)
            connection.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
            if recovered:
                connection.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('recovered_from', ?)",
                    (recovered,),
                )

    @staticmethod
    def _add_columns(
        connection: sqlite3.Connection, table: str, definitions: Sequence[str]
    ) -> None:
        present = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(%s)" % table)
        }
        for definition in definitions:
            column = definition.split(" ", 1)[0]
            if column not in present:
                connection.execute("ALTER TABLE %s ADD COLUMN %s" % (table, definition))

    def _stored_version(self) -> int:
        row, _ = _first_row(self.path, "PRAGMA user_version")
        return _as_int(row[0]) if row is not None else 0

    def _integrity_problem(self) -> str:
        try:
            if not self.path.is_file() or self.path.stat().st_size == 0:
                return ""
        except OSError:
            return ""
        row, failure = _first_row(self.path, "PRAGMA integrity_check")
        if row is not None:
            report = str(row[0] or "")
            if report.strip().lower() == "ok":
                return ""
            failure = report
        lowered = failure.lower()
        if any(marker in lowered for marker in _BROKEN_MARKERS):
            return failure
        # locked / busy / io 这类临时故障绝不能触发隔离。
        return ""

    def _quarantine_broken_database(self) -> str:
        problem = self._integrity_problem()
        if not problem:
            return ""
        stamp = now_local().strftime("%Y%m%d-%H%M%S")
        stem = "%s.corrupt-%s" % (self.path.name, stamp)
        target = self.path.with_name(stem)
        attempt = 2
        while target.exists():
            target = self.path.with_name("%s-%d" % (stem, attempt))
            attempt += 1
        moved: list[str] = []
        failed: list[str] = []
        for suffix in ("", "-wal", "-shm"):
            source = self.path.with_name(self.path.name + suffix)
            if not source.is_file():
                continue
            try:
                source.rename(target.with_name(target.name + suffix))
                moved.append(source.name)
            except OSError as error:
                failed.append(source.name)
                LOGGER.warning("隔离损坏数据库时无法移动 %s：%s", source, error)
        if failed:
            # 隔离没做完就不能在原路径建新库：那会把没搬走的坏文件覆盖掉，数据就真没了。
            LOGGER.error(
                "数据库已损坏（%s），但隔离不完整：%s 没能移走（已移走 %s）。未创建新库。",
                problem,
                "、".join(failed),
                "、".join(moved) or "无",
            )
            raise RuntimeError(
                "数据库已损坏，且隔离不完整：%s 移动失败。请先关闭占用这些文件的程序再重试。"
                % "、".join(failed)
            )
        LOGGER.error(
            "数据库已损坏（%s），已隔离为 %s，新库已建立，历史数据需从备份恢复。",
            problem,
            target.name,
        )
        return target.name

    # -------------------------------------------------------- 旁路审计

    def _append_audit(self, path: Path, record: Mapping[str, Any]) -> None:
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(dict(record), ensure_ascii=False, default=str) + "\n")
        except (OSError, TypeError, ValueError) as error:
            LOGGER.warning("写入 JSONL 失败 %s: %s", path, error)

    # -------------------------------------------------------- 消息

    def insert_message(
        self,
        *,
        msg_id: str,
        group_id: str,
        content: str,
        source_text: str = "",
        ts: Any = None,
        received_at: Any = None,
        source: str = "qqbot",
        event: str = "",
        sender_id: str = "",
        sender_name: str = "",
        group_name: str = "",
    ) -> bool:
        key = str(msg_id or "").strip()
        if not key:
            raise ValueError("msg_id 不能为空")
        received = iso(received_at) if received_at is not None else ""
        if not received:
            received = iso(now_local())
        sent_at = iso(ts) if ts is not None else ""
        if not sent_at:
            sent_at = received
        record = {
            "msg_id": key,
            "source": str(source or "qqbot"),
            "event": str(event or ""),
            "group_id": str(group_id or ""),
            "group_name": str(group_name or ""),
            "sender_id": str(sender_id or ""),
            "sender_name": str(sender_name or ""),
            "ts": sent_at,
            "received_at": received,
            "content": str(content or ""),
            "source_text": str(source_text or ""),
        }
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO messages"
                " (msg_id, source, event, group_id, group_name, sender_id, sender_name,"
                " ts, received_at, content, source_text)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(record.values()),
            )
            inserted = cursor.rowcount > 0
        if inserted:
            self._append_audit(self.messages_audit, record)
        return inserted

    def unprocessed_messages(self, *, limit: int = 400) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT m.* FROM messages AS m"
                " LEFT JOIN processed AS p ON p.msg_id = m.msg_id"
                " WHERE p.msg_id IS NULL"
                " ORDER BY m.received_at ASC, m.msg_id ASC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_processed(self, msg_ids: Any, digest_id: Any) -> int:
        keys = _clean_sequence(msg_ids)
        if not keys:
            return 0
        stamp = iso(now_local())
        with self._connect() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO processed (msg_id, digest_id, processed_at)"
                " SELECT ?, ?, ? WHERE EXISTS (SELECT 1 FROM messages WHERE msg_id = ?)",
                [(key, digest_id, stamp, key) for key in keys],
            )
        return len(keys)

    def oldest_unprocessed(self) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT MIN(m.received_at) AS oldest FROM messages AS m"
                " LEFT JOIN processed AS p ON p.msg_id = m.msg_id"
                " WHERE p.msg_id IS NULL"
            ).fetchone()
        return str(row["oldest"] or "") if row is not None else ""

    # -------------------------------------------------------- 摘要

    def insert_digest(
        self,
        *,
        kind: str,
        window_start: Any,
        window_end: Any,
        body: str,
        payload: Any,
        message_count: Any,
        llm_used: Any,
    ) -> int:
        items = list(payload) if isinstance(payload, (list, tuple)) else []
        created = iso(now_local())
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO digests"
                " (kind, window_start, window_end, item_count, message_count, llm_used,"
                " body, payload, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(kind or ""),
                    str(window_start or ""),
                    str(window_end or ""),
                    len(items),
                    _as_int(message_count),
                    1 if llm_used else 0,
                    str(body or ""),
                    _json_text(items),
                    created,
                ),
            )
            digest_id = int(cursor.lastrowid or 0)
        self._append_audit(
            self.digests_audit,
            {
                "id": digest_id,
                "kind": str(kind or ""),
                "window_start": str(window_start or ""),
                "window_end": str(window_end or ""),
                "item_count": len(items),
                "message_count": _as_int(message_count),
                "llm_used": bool(llm_used),
                "body": str(body or ""),
                "created_at": created,
            },
        )
        return digest_id

    @staticmethod
    def _digest_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["items"] = _digest_payload_items(data.get("payload"))
        return data

    def get_digest(self, digest_id: Any) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM digests WHERE id = ?", (_as_int(digest_id),)
            ).fetchone()
        return self._digest_row(row) if row is not None else None

    def recent_digests(self, *, limit: int = 5) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM digests ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
            ).fetchall()
        return [self._digest_row(row) for row in rows]

    def recent_items(self, *, hours: float = 6, limit: int = 200) -> list[dict[str, Any]]:
        span = _as_float(hours, 6)
        if span <= 0:
            return []
        cutoff = iso(now_local() - dt.timedelta(hours=span))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, kind, payload, created_at FROM digests"
                " WHERE created_at >= ? AND item_count > 0"
                " ORDER BY id DESC LIMIT ?",
                (cutoff, max(1, int(limit))),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            entries = _digest_payload_items(row["payload"])
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                item = dict(entry)
                item["digest_id"] = _as_int(row["id"])
                item["kind"] = str(row["kind"] or "")
                item["created_at"] = str(row["created_at"] or "")
                items.append(item)
        return items

    def recent_item_texts(self, *, hours: float = 6, limit: int = 200) -> list[dict[str, str]]:
        return [
            {
                "text": str(item.get("text") or item.get("summary") or ""),
                "summary": str(item.get("summary") or ""),
                "group": str(item.get("group") or ""),
                "ts": str(item.get("created_at") or ""),
            }
            for item in self.recent_items(hours=hours, limit=limit)
        ]

    # -------------------------------------------------------- 投递

    def claim_delivery(
        self,
        *,
        digest_id: Any,
        channel: str,
        target: str,
        dedupe_key: str,
        max_attempts: Any,
        retry_seconds: Any,
    ) -> tuple[bool, int]:
        channel_text = str(channel or "")
        target_text = str(target or "")
        key_text = str(dedupe_key or "")
        limit = _as_int(max_attempts, 1)
        cooldown = _as_float(retry_seconds, 0.0)
        stamp = iso(now_local())
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, status, attempts, updated_at FROM deliveries"
                " WHERE dedupe_key = ? AND channel = ? AND target = ?",
                (key_text, channel_text, target_text),
            ).fetchone()
            if row is None:
                cursor = connection.execute(
                    "INSERT INTO deliveries"
                    " (digest_id, channel, target, dedupe_key, status, attempts,"
                    " last_error, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, 'pending', 1, '', ?, ?)",
                    (_as_int(digest_id), channel_text, target_text, key_text, stamp, stamp),
                )
                return True, int(cursor.lastrowid or 0)
            delivery_id = _as_int(row["id"])
            if str(row["status"] or "") == "sent":
                return False, delivery_id
            if _as_int(row["attempts"]) >= limit:
                return False, delivery_id
            age = _seconds_since(row["updated_at"])
            if cooldown > 0 and age is not None and age < cooldown:
                return False, delivery_id
            connection.execute(
                "UPDATE deliveries SET attempts = attempts + 1, status = 'pending',"
                " updated_at = ? WHERE id = ?",
                (stamp, delivery_id),
            )
            return True, delivery_id

    def mark_delivery(self, delivery_id: Any, *, ok: Any, error: str = "") -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE deliveries SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
                ("sent" if ok else "failed", str(error or "")[:500], iso(now_local()), _as_int(delivery_id)),
            )

    def increment_delivery_attempt(self, delivery_id: Any) -> int:
        key = _as_int(delivery_id)
        stamp = iso(now_local())
        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempts FROM deliveries WHERE id = ?", (key,)
            ).fetchone()
            if row is None:
                return 0
            attempts = _as_int(row["attempts"]) + 1
            connection.execute(
                "UPDATE deliveries SET attempts = ?, status = 'pending', updated_at = ?"
                " WHERE id = ?",
                (attempts, stamp, key),
            )
        return attempts

    def pending_deliveries(
        self, *, max_attempts: Any, retry_seconds: Any
    ) -> list[dict[str, Any]]:
        limit = _as_int(max_attempts, 1)
        cooldown = _as_float(retry_seconds, 0.0)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT d.id, d.digest_id, d.channel, d.target, d.dedupe_key, d.status,"
                " d.attempts, d.last_error, d.created_at, d.updated_at,"
                " g.kind, g.window_start, g.window_end, g.body, g.item_count"
                " FROM deliveries AS d LEFT JOIN digests AS g ON g.id = d.digest_id"
                " WHERE d.status <> 'sent' AND d.attempts < ?"
                " ORDER BY d.updated_at ASC LIMIT 50",
                (limit,),
            ).fetchall()
        pending: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            if cooldown > 0:
                age = _seconds_since(data.get("updated_at"))
                if age is not None and age < cooldown:
                    continue
            pending.append(data)
        return pending

    def get_delivery(self, delivery_id: Any) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, status, attempts, last_error, updated_at FROM deliveries WHERE id = ?",
                (_as_int(delivery_id),),
            ).fetchone()
        return dict(row) if row is not None else None

    def delivery_stats(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS amount FROM deliveries GROUP BY status"
            ).fetchall()
        counted = {str(row["status"] or ""): _as_int(row["amount"]) for row in rows}
        return {
            "sent": counted.get("sent", 0),
            "pending": counted.get("pending", 0),
            "failed": counted.get("failed", 0),
            "total": sum(counted.values()),
        }

    # -------------------------------------------------------- 同步状态

    def dingtalk_sync_state(self) -> dict[str, int]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM dingtalk_sync WHERE key = 'wal_state'"
            ).fetchone()
        state = _json_object(row["value"]) if row is not None else {}
        return {
            key: max(0, _as_int(state.get(key)))
            for key in ("salt_1", "salt_2", "frame_index", "last_created_at")
        }

    def save_dingtalk_sync_state(self, state: Mapping[str, Any] | None) -> None:
        source = state if isinstance(state, Mapping) else {}
        payload = {}
        for key in ("salt_1", "salt_2", "frame_index", "last_created_at"):
            payload[key] = max(0, _as_int(source.get(key)))
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO dingtalk_sync (key, value, updated_at)"
                " VALUES ('wal_state', ?, ?)",
                (_json_text(payload), iso(now_local())),
            )

    def chaoxing_sync_state(self) -> dict[str, list[str]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM dingtalk_sync WHERE key = 'chaoxing_state'"
            ).fetchone()
        state = _json_object(row["value"]) if row is not None else {}
        result: dict[str, list[str]] = {}
        for key in ("notice_ids", "work_ids"):
            value = state.get(key)
            result[key] = _clean_sequence(value) if isinstance(value, (list, tuple)) else []
        return result

    def save_chaoxing_sync_state(self, state: Mapping[str, Any] | None) -> None:
        source = state if isinstance(state, Mapping) else {}
        payload: dict[str, list[str]] = {}
        for key in ("notice_ids", "work_ids"):
            value = source.get(key)
            items = _clean_sequence(value) if isinstance(value, (list, tuple)) else []
            payload[key] = items[-1000:]
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO dingtalk_sync (key, value, updated_at)"
                " VALUES ('chaoxing_state', ?, ?)",
                (_json_text(payload), iso(now_local())),
            )

    # -------------------------------------------------------- 收件箱

    def save_mark(
        self,
        msg_id: str,
        verdict: str,
        reason: str = "",
        method: str = "",
        task_id: Any = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO inbox_marks"
                " (msg_id, verdict, reason, method, classified_at, task_id)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(msg_id or ""),
                    str(verdict or ""),
                    str(reason or ""),
                    str(method or ""),
                    iso(now_local()),
                    None if task_id is None else _as_int(task_id),
                ),
            )

    def unmarked_messages(
        self,
        *,
        since: Any = None,
        until: Any = None,
        groups: Any = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        clauses = ["k.msg_id IS NULL"]
        params: list[Any] = []
        if since:
            clauses.append("m.received_at >= ?")
            params.append(str(since))
        if until:
            clauses.append("m.received_at <= ?")
            params.append(str(until))
        group_ids = _clean_sequence(groups)
        if group_ids:
            clauses.append("m.group_id IN (%s)" % ", ".join("?" for _ in group_ids))
            params.extend(group_ids)
        params.append(int(limit))
        sql = (
            "SELECT m.* FROM messages AS m"
            " LEFT JOIN inbox_marks AS k ON k.msg_id = m.msg_id"
            " WHERE %s ORDER BY m.received_at ASC, m.msg_id ASC LIMIT ?" % " AND ".join(clauses)
        )
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def inbox_items(
        self,
        *,
        verdict: Any = None,
        group_id: Any = None,
        q: Any = None,
        limit: int = 50,
        offset: int = 0,
        promoted: Any = None,
    ) -> list[dict[str, Any]]:
        clauses = ["1 = 1"]
        params: list[Any] = []
        if verdict:
            clauses.append("k.verdict = ?")
            params.append(str(verdict))
        if group_id:
            clauses.append("m.group_id = ?")
            params.append(str(group_id))
        if q:
            clauses.append("m.content LIKE ?")
            params.append("%" + str(q) + "%")
        if promoted is True:
            clauses.append("k.task_id IS NOT NULL")
        elif promoted is False:
            clauses.append("k.task_id IS NULL")
        sql = (
            "SELECT m.msg_id, m.source, m.event, m.received_at, m.group_id, m.group_name,"
            " m.sender_name, m.content, k.verdict, k.reason, k.method, k.task_id"
            " FROM messages AS m JOIN inbox_marks AS k ON k.msg_id = m.msg_id"
            # 同一秒收到的一批消息要按入库顺序给出来（rowid 升序），否则「最近的几条」
            # 在同一个时间戳内会倒着排，翻页和截图都对不上。
            " WHERE %s ORDER BY m.received_at DESC, m.rowid ASC LIMIT ? OFFSET ?"
            % " AND ".join(clauses)
        )
        params.extend([int(limit), int(offset)])
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def inbox_counts(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT verdict, COUNT(*) AS amount FROM inbox_marks GROUP BY verdict"
            ).fetchall()
            promoted = connection.execute(
                "SELECT COUNT(*) AS amount FROM inbox_marks WHERE task_id IS NOT NULL"
            ).fetchone()
        counts = {"notice": 0, "suspect": 0, "noise": 0}
        for row in rows:
            counts[str(row["verdict"] or "")] = _as_int(row["amount"])
        counts["promoted"] = _as_int(promoted["amount"]) if promoted is not None else 0
        return counts

    def delete_inbox_items(self, msg_ids: Any) -> int:
        keys = _clean_sequence(msg_ids)
        if not keys:
            return 0
        marks = ", ".join("?" for _ in keys)
        with self._connect() as connection:
            connection.execute("DELETE FROM inbox_marks WHERE msg_id IN (%s)" % marks, keys)
            cursor = connection.execute(
                "DELETE FROM messages WHERE msg_id IN (%s)" % marks, keys
            )
            removed = cursor.rowcount
        return max(0, int(removed or 0))

    # -------------------------------------------------------- 待办：写入

    def upsert_task(
        self,
        *,
        task_key: str,
        summary: str,
        audience: str = "",
        condition: str = "",
        details: Sequence[str] = (),
        action: str = "",
        category: str = "info",
        importance: int = 3,
        deadline: str = "",
        groups: Sequence[str] = (),
        sender: str = "",
        evidence: str = "",
        digest_id: int = 0,
        status: str = "open",
        confidence: float = 0.0,
        source: str = "",
        classification_reason: str = "",
    ) -> int:
        key = str(task_key or "").strip()
        if not key:
            return 0
        state = str(status or "open").strip().lower()
        if state not in TASK_STATUSES:
            state = "open"
        stamp = iso(now_local())
        score = _as_float(confidence)
        with self._connect() as connection:
            connection.execute(
                _UPSERT_TASK,
                (
                    key,
                    str(summary or ""),
                    str(action or ""),
                    str(audience or ""),
                    str(condition or ""),
                    _json_text(_clean_sequence(details)),
                    str(category or "info"),
                    _as_int(importance, 3),
                    str(deadline or ""),
                    _json_text(_clean_sequence(groups, unique=True)),
                    str(sender or ""),
                    str(evidence or ""),
                    _as_int(digest_id),
                    state,
                    score,
                    str(source or ""),
                    stamp,
                    stamp,
                ),
            )
            row = connection.execute(
                "SELECT id FROM tasks WHERE task_key = ?", (key,)
            ).fetchone()
            if row is None:
                return 0
            task_id = _as_int(row["id"])
            created = connection.execute(
                "SELECT 1 FROM task_events WHERE task_id = ? AND event = 'created' LIMIT 1",
                (task_id,),
            ).fetchone()
            if created is None:
                reason = str(classification_reason or "")
                self._add_event(
                    connection,
                    task_id,
                    "created",
                    {"status": state, "confidence": score, "reason": reason},
                    stamp,
                )
                if state == "candidate":
                    self._add_event(
                        connection,
                        task_id,
                        "candidate_detected",
                        {"confidence": score, "reason": reason},
                        stamp,
                    )
        return task_id

    @staticmethod
    def _add_event(
        connection: sqlite3.Connection,
        task_id: Any,
        event: str,
        detail: Any,
        created_at: str,
    ) -> None:
        connection.execute(
            "INSERT INTO task_events (task_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
            (_as_int(task_id), str(event), _detail_text(detail), created_at),
        )

    @staticmethod
    def _write_task(connection: sqlite3.Connection, task_id: Any, **fields: Any) -> None:
        assignments = ", ".join("%s = ?" % column for column in fields)
        connection.execute(
            "UPDATE tasks SET %s WHERE id = ?" % assignments,
            list(fields.values()) + [_as_int(task_id)],
        )

    def _task_row(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        task_id = _as_int(data.get("id"))
        key = str(data.get("task_key") or "")
        data["groups"] = _json_list(data.get("groups"))
        data["details"] = _json_list(data.get("details"))
        data["condition"] = data.pop("condition_text", "")
        data["snooze_until"] = str(data.get("snooze_until") or "")
        data["duplicate_of"] = _as_int(data.get("duplicate_of"))
        data["pinned"] = bool(data.get("pinned"))
        data["done"] = str(data.get("status") or "") == "done"
        data["importance"] = _as_int(data.get("importance"), 3)
        data["digest_id"] = _as_int(data.get("digest_id"))
        data["confidence"] = _as_float(data.get("confidence"))
        data["candidate_detail"] = self._candidate_detail(connection, task_id)
        data["source_text"] = self._task_source_text(connection, key)
        data["duplicate_summary"] = self._duplicate_summary(connection, data["duplicate_of"])
        return data

    @staticmethod
    def _candidate_detail(connection: sqlite3.Connection, task_id: int) -> str:
        row = connection.execute(
            "SELECT detail FROM task_events WHERE task_id = ? AND event = 'candidate_detected'"
            " ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return str(row["detail"] or "") if row is not None else ""

    @staticmethod
    def _task_source_text(connection: sqlite3.Connection, task_key: str) -> str | None:
        if not task_key:
            return None
        row = connection.execute(
            "SELECT source_text, content FROM messages WHERE msg_id = ? LIMIT 1", (task_key,)
        ).fetchone()
        if row is None:
            return None
        return str(row["source_text"] or row["content"] or "")

    @staticmethod
    def _duplicate_summary(connection: sqlite3.Connection, duplicate_of: int) -> str:
        if not duplicate_of:
            return ""
        row = connection.execute(
            "SELECT summary FROM tasks WHERE id = ?", (duplicate_of,)
        ).fetchone()
        return str(row["summary"] or "") if row is not None else ""

    # -------------------------------------------------------- 待办：读取

    def get_task(self, task_id: Any) -> dict[str, Any] | None:
        key = _as_int(task_id)
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (key,)).fetchone()
            if row is None:
                return None
            return self._task_row(connection, row)

    def list_tasks(
        self,
        *,
        include_done: bool = True,
        statuses: Sequence[str] | None = None,
        limit: int | None = 800,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        filters = _clean_sequence(statuses)
        if filters:
            clauses.append("status IN (%s)" % ", ".join("?" for _ in filters))
            params.extend(filters)
        elif not include_done:
            clauses.append("status = 'open'")
        sql = "SELECT * FROM tasks"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += (
            " ORDER BY (deadline = '') ASC, deadline ASC, importance DESC, id DESC"
            " LIMIT ?"
        )
        params.append(-1 if limit is None else _as_int(limit))
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
            return [self._task_row(connection, row) for row in rows]

    def list_open_tasks(self, *, limit: int = 800) -> list[dict[str, Any]]:
        return self.list_tasks(statuses=("open",), limit=limit)

    def list_task_events(self, task_id: Any, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, task_id, event, detail, created_at FROM task_events"
                " WHERE task_id = ? ORDER BY id DESC LIMIT ?",
                (_as_int(task_id), max(1, int(limit))),
            ).fetchall()
        return [_task_event_row(row) for row in rows]

    def task_stats(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS amount FROM tasks GROUP BY status"
            ).fetchall()
            overdue = connection.execute(
                "SELECT COUNT(*) AS amount FROM tasks"
                " WHERE status = 'open' AND deadline <> '' AND deadline < ?",
                (iso(now_local()),),
            ).fetchone()
        counts = {str(row["status"] or ""): _as_int(row["amount"]) for row in rows}
        return {
            "total": sum(counts.values()),
            "candidate": counts.get("candidate", 0),
            "open": counts.get("open", 0),
            "done": counts.get("done", 0),
            "dismissed": counts.get("dismissed", 0),
            "expired": counts.get("expired", 0),
            "overdue": _as_int(overdue["amount"]) if overdue is not None else 0,
        }

    def task_metrics(self, *, start: Any, end: Any) -> dict[str, Any]:
        begin = iso(start)
        finish = iso(end)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event, COUNT(*) AS amount FROM task_events"
                " WHERE created_at >= ? AND created_at < ? GROUP BY event",
                (begin, finish),
            ).fetchall()
            reminded_done = connection.execute(
                "SELECT COUNT(DISTINCT done.task_id) AS amount FROM task_events AS done"
                " WHERE done.event = 'done' AND done.created_at >= ? AND done.created_at < ?"
                " AND EXISTS (SELECT 1 FROM task_events AS note"
                "             WHERE note.task_id = done.task_id AND note.event = 'reminded'"
                "             AND note.id < done.id)",
                (begin, finish),
            ).fetchone()
            overdue = connection.execute(
                "SELECT COUNT(*) AS amount FROM tasks"
                " WHERE status = 'open' AND deadline <> '' AND deadline < ?",
                (finish,),
            ).fetchone()
        counts = {str(row["event"] or ""): _as_int(row["amount"]) for row in rows}
        candidates = counts.get("created", 0)
        confirmed = counts.get("confirmed", 0)
        dismissed = counts.get("dismissed", 0)
        completions = counts.get("done", 0)
        reminders = counts.get("reminded", 0)
        overdue_total = _as_int(overdue["amount"]) if overdue is not None else 0
        return {
            "candidates": candidates,
            "confirmed": confirmed,
            "dismissed": dismissed,
            "completions": completions,
            "reminders": reminders,
            "completions_after_reminder": (
                _as_int(reminded_done["amount"]) if reminded_done is not None else 0
            ),
            "overdue": _as_int(overdue["amount"]) if overdue is not None else 0,
            "confirmation_rate": round(confirmed * 100 / candidates, 1) if candidates else 0.0,
            "dismissal_rate": round(dismissed * 100 / candidates, 1) if candidates else 0.0,
            "completion_rate": round(completions * 100 / max(1, completions + overdue_total), 1),
        }

    # -------------------------------------------------------- 待办：动作

    def set_task_urgent(self, task_id: Any, value: Any) -> dict[str, Any]:
        if value is not None and type(value) is not bool:
            raise ValueError("紧急覆盖值必须是 true、false 或 null")
        stored = None if value is None else (1 if value else 0)
        key = _as_int(task_id)
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (key,)).fetchone()
            if row is None:
                return {"ok": False, "error": "任务不存在"}
            connection.execute(
                "UPDATE tasks SET urgent_override = ?, updated_at = ? WHERE id = ?",
                (stored, iso(now_local()), key),
            )
            fresh = connection.execute("SELECT * FROM tasks WHERE id = ?", (key,)).fetchone()
            return {**self._task_row(connection, fresh), "ok": True}

    def set_task_pinned(self, task_id: Any, value: Any) -> dict[str, Any]:
        if type(value) is not bool:
            raise ValueError("置顶值必须是 true 或 false")
        key = _as_int(task_id)
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (key,)).fetchone()
            if row is None:
                return {"ok": False, "error": "任务不存在"}
            if value and str(row["status"] or "") == "done":
                return {"ok": False, "error": "已完成的任务不能置顶"}
            connection.execute(
                "UPDATE tasks SET pinned = ?, updated_at = ? WHERE id = ?",
                (1 if value else 0, iso(now_local()), key),
            )
            fresh = connection.execute("SELECT * FROM tasks WHERE id = ?", (key,)).fetchone()
            return {**self._task_row(connection, fresh), "ok": True}

    def apply_task_action(self, task_id: Any, action: str, *, detail: Any = "") -> bool:
        name = str(action or "").strip().lower()
        if name not in _TASK_ACTIONS:
            raise ValueError("不支持的任务动作: %s" % name)
        key = _as_int(task_id)
        stamp = iso(now_local())
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (key,)).fetchone()
            if row is None:
                return False
            state = str(row["status"] or "")
            if name == "delete":
                finished = state in _FINISHED_STATUSES
                manual = str(row["task_key"] or "").startswith("manual:")
                if not (finished or (state == "open" and manual)):
                    return False
                connection.execute("DELETE FROM task_events WHERE task_id = ?", (key,))
                connection.execute("DELETE FROM tasks WHERE id = ?", (key,))
                return True
            if name == "confirm":
                if state not in {"candidate", "open"}:
                    return False
                self._write_task(connection, key, status="open", confirmed_at=stamp, updated_at=stamp)
                self._add_event(connection, key, "confirmed", detail, stamp)
                return True
            if name == "done":
                if state not in {"open", "candidate"}:
                    return False
                self._write_task(connection, key, status="done", done_at=stamp, updated_at=stamp)
                self._add_event(connection, key, "done", detail, stamp)
                return True
            if name == "dismiss":
                if state not in {"candidate", "open"}:
                    return False
                self._write_task(
                    connection, key, status="dismissed", dismissed_at=stamp, updated_at=stamp
                )
                self._add_event(connection, key, "dismissed", detail, stamp)
                return True
            if name == "snooze":
                if state not in {"candidate", "open"}:
                    return False
                until = self._snooze_until(detail)
                self._write_task(
                    connection,
                    key,
                    last_reminded_at=stamp,
                    snooze_until=until,
                    updated_at=stamp,
                )
                self._add_event(connection, key, "snoozed", {"until": until}, stamp)
                return True
            if state not in _FINISHED_STATUSES:
                return False
            self._write_task(
                connection, key, status="open", done_at="", confirmed_at=stamp, updated_at=stamp
            )
            self._add_event(connection, key, "reopened", detail, stamp)
            return True

    @staticmethod
    def _snooze_until(detail: Any) -> str:
        payload = detail if isinstance(detail, dict) else {}
        until = str(payload.get("until") or "").strip()
        if until:
            return until
        return iso(now_local() + dt.timedelta(days=1))

    def set_task_status(self, task_id: Any, done: Any) -> bool:
        return bool(self.apply_task_action(task_id, "done" if done else "reopen"))

    def mark_task_reminded(
        self, task_id: Any, *, detail: Any = "", when: Any = None
    ) -> bool:
        key = _as_int(task_id)
        stamp = iso(when) if when is not None else ""
        if not stamp:
            stamp = iso(now_local())
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM tasks WHERE id = ?", (key,)
            ).fetchone()
            if row is None or str(row["status"] or "") not in {"open", "candidate"}:
                return False
            connection.execute(
                "UPDATE tasks SET last_reminded_at = ?, remind_count = remind_count + 1,"
                " updated_at = ? WHERE id = ?",
                (stamp, stamp, key),
            )
            self._add_event(connection, key, "reminded", detail, stamp)
            return True

    # -------------------------------------------------------- 待办：纠错

    def _find_duplicate(
        self, connection: sqlite3.Connection, task: Mapping[str, Any], value: Any
    ) -> int:
        current = _as_int(task.get("id"))
        text = str(value or "").strip()
        if text:
            candidate = 0
            try:
                candidate = int(text)
            except (TypeError, ValueError):
                candidate = 0
            if candidate and candidate != current:
                found = connection.execute(
                    "SELECT id FROM tasks WHERE id = ?", (candidate,)
                ).fetchone()
                if found is not None:
                    return _as_int(found["id"])
        probe = _normalise(task.get("summary"))
        if not probe:
            return 0
        rows = connection.execute(
            "SELECT id, summary FROM tasks"
            " WHERE status NOT IN ('dismissed', 'expired') AND id <> ?"
            " ORDER BY id DESC LIMIT 200",
            (current,),
        ).fetchall()
        for row in rows:
            if _normalise(row["summary"]) == probe:
                return _as_int(row["id"])
        return 0

    def apply_task_correction(
        self, task_id: Any, correction: str, *, value: Any = ""
    ) -> bool:
        name = str(correction or "").strip()
        if name not in _CORRECTIONS:
            raise ValueError("不支持的纠错类型: %s" % correction)
        key = _as_int(task_id)
        stamp = iso(now_local())
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (key,)).fetchone()
            if row is None:
                return False
            current = dict(row)
            previous = {
                "status": str(current.get("status") or ""),
                "category": str(current.get("category") or ""),
                "importance": _as_int(current.get("importance"), 3),
                "deadline": str(current.get("deadline") or ""),
            }
            changes: dict[str, Any] = {}
            merged: list[str] = []
            if name in {"not_notice", "not_task"}:
                changes["status"] = "dismissed"
                if previous["status"] != "dismissed":
                    changes["dismissed_at"] = stamp
            elif name == "duplicate":
                target = self._find_duplicate(connection, current, value)
                changes["status"] = "dismissed"
                changes["duplicate_of"] = target
                if previous["status"] != "dismissed":
                    changes["dismissed_at"] = stamp
                if target:
                    target_row = connection.execute(
                        "SELECT groups FROM tasks WHERE id = ?", (target,)
                    ).fetchone()
                    existing = _json_list(target_row["groups"] if target_row else "[]")
                    merged = [
                        item
                        for item in _json_list(current.get("groups"))
                        if item not in existing
                    ]
                    if merged:
                        connection.execute(
                            "UPDATE tasks SET groups = ?, updated_at = ? WHERE id = ?",
                            (_json_text(existing + merged), stamp, target),
                        )
            elif name == "not_urgent":
                if previous["category"] == "urgent":
                    changes["category"] = "action"
                changes["importance"] = min(previous["importance"], 3)
            elif name == "category":
                chosen = str(value or "").strip()
                if chosen not in IMPORTANCE_BY_CATEGORY:
                    raise ValueError("无效的任务分类")
                changes["category"] = chosen
                changes["importance"] = int(IMPORTANCE_BY_CATEGORY[chosen])
            elif name == "deadline":
                text = "" if value is None else str(value).strip()
                if text:
                    parsed = iso(value)
                    if not parsed:
                        raise ValueError("无效的截止时间")
                    changes["deadline"] = parsed
                else:
                    changes["deadline"] = ""
            else:
                changes["deadline"] = ""
            changes["updated_at"] = stamp
            self._write_task(connection, key, **changes)
            fresh = connection.execute("SELECT * FROM tasks WHERE id = ?", (key,)).fetchone()
            updated = dict(fresh) if fresh is not None else {}
            groups = _json_list(updated.get("groups"))
            for item in merged:
                if item not in groups:
                    groups.append(item)
            detail: dict[str, Any] = {
                "type": name,
                "previous": previous,
                "new": {
                    "status": str(updated.get("status") or ""),
                    "category": str(updated.get("category") or ""),
                    "importance": _as_int(updated.get("importance"), 3),
                    "deadline": str(updated.get("deadline") or ""),
                    "duplicate_of": _as_int(updated.get("duplicate_of")),
                },
                "value": str(value)[:500],
                "groups": groups,
            }
            if merged:
                detail["merged_groups"] = merged
            self._add_event(connection, key, "corrected", detail, stamp)
            return True

    def correction_stats(self, *, days: int = 30) -> dict[str, Any]:
        span = max(1, _as_int(days, 30))
        cutoff = iso(now_local() - dt.timedelta(days=span))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT e.detail, t.groups FROM task_events AS e"
                " LEFT JOIN tasks AS t ON t.id = e.task_id"
                " WHERE e.event = 'corrected' AND e.created_at >= ?"
                " ORDER BY e.id ASC",
                (cutoff,),
            ).fetchall()
        by_type: dict[str, int] = {}
        by_group: dict[str, dict[str, int]] = {}
        total = 0
        for row in rows:
            total += 1
            payload = _json_object(row["detail"])
            kind = str(payload.get("type") or "unknown")
            by_type[kind] = by_type.get(kind, 0) + 1
            groups = payload.get("groups")
            if not isinstance(groups, list):
                groups = _json_list(row["groups"])
            seen: list[str] = []
            for entry in groups:
                label = str(entry or "").strip()
                if label and label not in seen:
                    seen.append(label)
            for label in seen:
                bucket = by_group.setdefault(label, {})
                bucket[kind] = bucket.get(kind, 0) + 1
                bucket["total"] = bucket.get("total", 0) + 1
        return {"days": span, "total": total, "by_type": by_type, "by_group": by_group}

    def correction_insights(
        self, *, days: int = 30, min_group_hits: int = 3
    ) -> list[str]:
        stats = self.correction_stats(days=days)
        threshold = max(2, _as_int(min_group_hits, 3))
        lines: list[str] = []
        for label, bucket in sorted(
            stats["by_group"].items(), key=lambda pair: -_as_int(pair[1].get("total"))
        ):
            if _as_int(bucket.get("total")) < threshold:
                continue
            parts: list[str] = []
            urgent = _as_int(bucket.get("not_urgent"))
            if urgent:
                parts.append("误判紧急 %d 次" % urgent)
            notice = _as_int(bucket.get("not_notice")) + _as_int(bucket.get("not_task"))
            if notice:
                parts.append("误判为通知/待办 %d 次" % notice)
            duplicate = _as_int(bucket.get("duplicate"))
            if duplicate:
                parts.append("重复 %d 次" % duplicate)
            if not parts:
                continue
            lines.append(
                "%s：%s，建议收紧该群规则或加入低优先级群。"
                % (label, "、".join(parts))
            )
        if _as_int(stats["by_type"].get("duplicate")) >= 3:
            lines.append(
                "重复标记偏多，建议复查跨群去重时间窗 QQ_DIGEST_DEDUPE_HOURS。"
            )
        return lines

    # -------------------------------------------------------- 待办：清理

    def clear_tasks(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS amount FROM tasks").fetchone()
            total = _as_int(row["amount"]) if row is not None else 0
            connection.execute("DELETE FROM task_events")
            connection.execute("DELETE FROM tasks")
        return total

    def clear_finished_tasks(self) -> int:
        marks = ", ".join("?" for _ in _FINISHED_STATUSES)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM tasks WHERE status IN (%s)" % marks, list(_FINISHED_STATUSES)
            ).fetchall()
            ids = [_as_int(row["id"]) for row in rows]
            if not ids:
                return 0
            placeholders = ", ".join("?" for _ in ids)
            connection.execute(
                "DELETE FROM task_events WHERE task_id IN (%s)" % placeholders, ids
            )
            connection.execute("DELETE FROM tasks WHERE id IN (%s)" % placeholders, ids)
        return len(ids)

    # -------------------------------------------------------- 群名

    def group_names(self) -> dict[str, str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT group_id, group_name FROM messages"
                " WHERE group_name <> '' AND group_name <> group_id"
                " GROUP BY group_id HAVING ts = MAX(ts)"
            ).fetchall()
            names = {
                str(row["group_id"] or ""): str(row["group_name"] or "")
                for row in rows
                if str(row["group_id"] or "")
            }
            for row in connection.execute("SELECT group_id, name FROM group_names").fetchall():
                key = str(row["group_id"] or "")
                name = str(row["name"] or "")
                if key and name:
                    names[key] = name
        return names

    def remember_group_names(self, names: Mapping[str, Any] | None) -> int:
        cleaned: dict[str, str] = {}
        for key, value in dict(names or {}).items():
            group_id = str(key or "").strip()
            label = str(value or "").strip()
            if not group_id or not label or group_id == label:
                continue
            cleaned[group_id] = label
        if not cleaned:
            return 0
        stamp = iso(now_local())
        with self._connect() as connection:
            connection.executemany(
                "INSERT INTO group_names (group_id, name, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(group_id) DO UPDATE SET"
                " name = excluded.name, updated_at = excluded.updated_at",
                [(key, label, stamp) for key, label in cleaned.items()],
            )
        return len(cleaned)

    def group_label(
        self,
        group_id: Any,
        names: Mapping[str, str] | None,
        aliases: Mapping[str, str] | None = None,
    ) -> str:
        key = str(group_id or "").strip()
        if not key:
            return ""
        alias = str((aliases or {}).get(key) or "").strip()
        if alias:
            return alias
        label = str((names or {}).get(key) or "").strip()
        if label:
            return label
        return key

    # -------------------------------------------------------- 元信息与维护

    def meta_get(self, key: str, default: str = "") -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM meta WHERE key = ?", (str(key or ""),)
            ).fetchone()
        if row is None or row["value"] is None:
            return default
        return str(row["value"])

    def meta_set(self, key: str, value: Any) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (str(key or ""), str(value)),
            )

    def prune(self, *, retention_days: int | None = None) -> int:
        days = _as_int(retention_days) if retention_days else self.retention_days
        cutoff = iso(now_local() - dt.timedelta(days=days))
        with self._connect() as connection:
            # 先子后父：processed 的外键指向 messages。
            connection.execute(
                "DELETE FROM processed WHERE msg_id IN"
                " (SELECT msg_id FROM messages WHERE received_at < ?)",
                (cutoff,),
            )
            cursor = connection.execute(
                "DELETE FROM messages WHERE received_at < ?", (cutoff,)
            )
            removed = max(0, int(cursor.rowcount or 0))
            connection.execute(
                "DELETE FROM processed WHERE msg_id NOT IN (SELECT msg_id FROM messages)"
            )
            connection.execute(
                "DELETE FROM inbox_marks WHERE msg_id NOT IN (SELECT msg_id FROM messages)"
            )
        return removed

    def counts(self) -> dict[str, int]:
        with self._connect() as connection:
            messages = connection.execute("SELECT COUNT(*) AS amount FROM messages").fetchone()
            pending = connection.execute(
                "SELECT COUNT(*) AS amount FROM messages AS m"
                " LEFT JOIN processed AS p ON p.msg_id = m.msg_id"
                " WHERE p.msg_id IS NULL"
            ).fetchone()
            digests = connection.execute("SELECT COUNT(*) AS amount FROM digests").fetchone()
            sent = connection.execute(
                "SELECT COUNT(*) AS amount FROM deliveries WHERE status = 'sent'"
            ).fetchone()
            failed = connection.execute(
                "SELECT COUNT(*) AS amount FROM deliveries WHERE status = 'failed'"
            ).fetchone()
        return {
            "messages": _as_int(messages["amount"]) if messages is not None else 0,
            "unprocessed": _as_int(pending["amount"]) if pending is not None else 0,
            "digests": _as_int(digests["amount"]) if digests is not None else 0,
            "deliveries_sent": _as_int(sent["amount"]) if sent is not None else 0,
            "deliveries_failed": _as_int(failed["amount"]) if failed is not None else 0,
        }


def effective_urgent(task: Mapping[str, Any] | None) -> bool:
    """紧急判定：有覆盖值就听它，没有才跟随 AI 分类。"""
    data = task or {}
    override = data.get("urgent_override")
    if override is not None:
        return bool(override)
    return str(data.get("category") or "") == "urgent"
