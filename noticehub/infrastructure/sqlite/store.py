"""SQLite 持久化：消息去重、摘要归档、投递去重与重启恢复。

行为契约见 `docs/rewrite-spec/batch-b.md` §1。公开面经 `noticehub/store.py` 再导出，
表名/列名/索引名是用户库里已经存在的契约，改动即破坏兼容。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from noticehub.domain.time import iso, parse_iso
from noticehub.infrastructure.clock import SystemClock

LOGGER = logging.getLogger("noticehub.store")


def now_local() -> dt.datetime:
    return SystemClock().now()


SCHEMA_VERSION = 1

# 分类 → 重要度：全项目唯一一份。摘要模块与「手动改分类」必须拿到同一个对象。
IMPORTANCE_BY_CATEGORY = {"urgent": 5, "action": 4, "academic": 3, "info": 3}

TASK_STATUSES = {"candidate", "open", "done", "dismissed", "expired"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    msg_id       TEXT PRIMARY KEY,
    source       TEXT NOT NULL DEFAULT 'qqbot',
    event        TEXT NOT NULL DEFAULT '',
    group_id     TEXT NOT NULL,
    group_name   TEXT NOT NULL DEFAULT '',
    sender_id    TEXT NOT NULL DEFAULT '',
    sender_name  TEXT NOT NULL DEFAULT '',
    ts           TEXT NOT NULL DEFAULT '',
    received_at  TEXT NOT NULL,
    content      TEXT NOT NULL DEFAULT '',
    source_text  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_messages_received ON messages(received_at);
CREATE INDEX IF NOT EXISTS idx_messages_group ON messages(group_id, received_at);
-- 群名回查要 (group_id, ts, group_name) 三列；缺这条索引就变成逐行回表。
CREATE INDEX IF NOT EXISTS idx_messages_group_names ON messages(group_id, ts, group_name);

CREATE TABLE IF NOT EXISTS processed (
    msg_id       TEXT PRIMARY KEY REFERENCES messages(msg_id),
    digest_id    INTEGER,
    processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS digests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,
    window_start  TEXT NOT NULL DEFAULT '',
    window_end    TEXT NOT NULL DEFAULT '',
    item_count    INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    llm_used      INTEGER NOT NULL DEFAULT 0,
    body          TEXT NOT NULL DEFAULT '',
    payload       TEXT NOT NULL DEFAULT '[]',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    digest_id   INTEGER NOT NULL,
    channel     TEXT NOT NULL,
    target      TEXT NOT NULL,
    dedupe_key  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE(dedupe_key, channel, target)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_status ON deliveries(status, updated_at);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_key    TEXT NOT NULL UNIQUE,
    summary     TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL DEFAULT 'info',
    importance  INTEGER NOT NULL DEFAULT 3,
    deadline    TEXT NOT NULL DEFAULT '',
    groups      TEXT NOT NULL DEFAULT '[]',
    sender      TEXT NOT NULL DEFAULT '',
    evidence    TEXT NOT NULL DEFAULT '',
    audience      TEXT NOT NULL DEFAULT '',
    condition_text TEXT NOT NULL DEFAULT '',
    details       TEXT NOT NULL DEFAULT '[]',
    status      TEXT NOT NULL DEFAULT 'open',
    digest_id   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    done_at     TEXT NOT NULL DEFAULT '',
    snooze_until TEXT NOT NULL DEFAULT '',
    duplicate_of INTEGER NOT NULL DEFAULT 0,
    urgent_override INTEGER,
    pinned INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, deadline, importance);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);

CREATE TABLE IF NOT EXISTS task_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL,
    event       TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
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
    group_id   TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dingtalk_sync (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

MESSAGE_COLUMN_MIGRATIONS = {"source_text": "TEXT NOT NULL DEFAULT ''"}

TASK_COLUMN_MIGRATIONS = {
    "audience": "TEXT NOT NULL DEFAULT ''",
    "condition_text": "TEXT NOT NULL DEFAULT ''",
    "details": "TEXT NOT NULL DEFAULT '[]'",
    "confidence": "REAL NOT NULL DEFAULT 0",
    "source": "TEXT NOT NULL DEFAULT ''",
    "confirmed_at": "TEXT NOT NULL DEFAULT ''",
    "dismissed_at": "TEXT NOT NULL DEFAULT ''",
    "last_reminded_at": "TEXT NOT NULL DEFAULT ''",
    "remind_count": "INTEGER NOT NULL DEFAULT 0",
    "snooze_until": "TEXT NOT NULL DEFAULT ''",
    "duplicate_of": "INTEGER NOT NULL DEFAULT 0",
    "urgent_override": "INTEGER",
    "pinned": "INTEGER NOT NULL DEFAULT 0",
}

_CORRUPT_MARKERS = ("not a database", "malformed", "corrupt", "encrypted")
_WAL_KEYS = ("salt_1", "salt_2", "frame_index", "last_created_at")
_DELIVERY_FIELDS = "id, status, attempts, last_error, updated_at"
_INBOX_FIELDS = (
    "m.msg_id, m.source, m.event, m.received_at, m.group_id, m.group_name,"
    " m.sender_name, m.content, i.verdict, i.reason, i.method, i.task_id"
)
_TASK_SELECT = """
    SELECT t.*,
           (SELECT e.detail FROM task_events e
             WHERE e.task_id = t.id AND e.event = 'candidate_detected'
             ORDER BY e.id DESC LIMIT 1) AS candidate_detail,
           (SELECT COALESCE(NULLIF(m.source_text, ''), m.content) FROM messages m
             WHERE m.msg_id = t.task_key LIMIT 1) AS source_text,
           (SELECT d.summary FROM tasks d WHERE d.id = t.duplicate_of) AS duplicate_summary
      FROM tasks t
"""


def effective_urgent(task: dict[str, Any]) -> bool:
    """用户改过就以用户为准，没改过才跟随 AI 分类。"""
    override = task.get("urgent_override")
    return bool(override) if override is not None else str(task.get("category") or "") == "urgent"


def _blank(value: Any) -> str:
    return str(value or "")


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _json_list(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except (json.JSONDecodeError, TypeError):
            value = []
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value if str(item or "").strip()]


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class Store:
    def __init__(self, path: Path | str, *, retention_days: int = 30) -> None:
        self.path = Path(path)
        self.retention_days = retention_days
        self.logger = LOGGER
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.path.parent / "messages.jsonl"
        self.digest_jsonl_path = self.path.parent / "digests.jsonl"
        self.init()

    # ------------------------------------------------------------ 基础设施
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
            connection.commit()
        finally:
            connection.close()

    def init(self) -> None:
        fault = self._integrity_fault()
        quarantined = self._quarantine() if fault else ""
        version = self._user_version()
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                "数据库结构版本 %d 高于本程序支持的 %d，请升级 notice-hub 后再启动：%s"
                % (version, SCHEMA_VERSION, self.path)
            )
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            self._add_columns(connection, "messages", MESSAGE_COLUMN_MIGRATIONS)
            self._add_columns(connection, "tasks", TASK_COLUMN_MIGRATIONS)
            if version != SCHEMA_VERSION:
                connection.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
        if quarantined:
            self.meta_set("recovered_from", quarantined)
            self.logger.error(
                "数据库已损坏（%s），已隔离为 %s，新库已建立，历史数据需从备份恢复。",
                fault,
                quarantined,
            )

    def _user_version(self) -> int:
        try:
            connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        except sqlite3.Error:
            return 0
        try:
            return _as_int(connection.execute("PRAGMA user_version").fetchone()[0])
        except sqlite3.Error:
            return 0
        finally:
            connection.close()

    def _integrity_fault(self) -> str:
        """只有「库确实坏了」才返回问题描述；临时锁/忙/IO 故障绝不能触发隔离。"""
        try:
            if not self.path.exists() or self.path.stat().st_size == 0:
                return ""
        except OSError:
            return ""
        try:
            connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        except sqlite3.Error as error:
            return self._corrupt_reason(str(error))
        try:
            row = connection.execute("PRAGMA integrity_check").fetchone()
            verdict = str(row[0]) if row else "（无结果）"
        except sqlite3.Error as error:
            return self._corrupt_reason(str(error))
        finally:
            connection.close()
        return "" if verdict == "ok" else verdict

    @staticmethod
    def _corrupt_reason(message: str) -> str:
        lowered = message.lower()
        return message if any(marker in lowered for marker in _CORRUPT_MARKERS) else ""

    def _quarantine(self) -> str:
        """坏库连同 -wal/-shm 一起改名留证；同名已存在就退让，绝不覆盖上一份样本。"""
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        target = Path("%s.corrupt-%s" % (self.path, stamp))
        suffix_index = 2
        while target.exists():
            target = Path("%s.corrupt-%s-%d" % (self.path, stamp, suffix_index))
            suffix_index += 1
        for suffix in ("", "-wal", "-shm"):
            source = Path("%s%s" % (self.path, suffix))
            if source.exists():
                source.replace(Path("%s%s" % (target, suffix)))
        return target.name

    @staticmethod
    def _add_columns(connection: sqlite3.Connection, table: str, columns: Mapping[str, str]) -> None:
        existing = {str(row["name"]) for row in connection.execute("PRAGMA table_info(%s)" % table)}
        for name, definition in columns.items():
            if name not in existing:
                connection.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, definition))

    def _audit(self, path: Path, payload: dict[str, Any]) -> None:
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(_dumps(payload) + "\n")
        except OSError as error:  # 审计失败不影响主流程
            LOGGER.warning("写入 JSONL 失败 %s: %s", path, error)

    # ---------------------------------------------------------------- 消息
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
        received = iso(received_at or now_local())
        stamped = iso(ts) or received
        row = {
            "msg_id": key,
            "source": _blank(source),
            "event": _blank(event),
            "group_id": _blank(group_id),
            "group_name": _blank(group_name),
            "sender_id": _blank(sender_id),
            "sender_name": _blank(sender_name),
            "ts": stamped,
            "received_at": received,
            "content": _blank(content),
            "source_text": _blank(source_text),
        }
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO messages"
                " (msg_id, source, event, group_id, group_name, sender_id, sender_name,"
                "  ts, received_at, content, source_text)"
                " VALUES (:msg_id, :source, :event, :group_id, :group_name, :sender_id,"
                " :sender_name, :ts, :received_at, :content, :source_text)",
                row,
            )
            fresh = cursor.rowcount > 0
        if fresh:
            self._audit(self.jsonl_path, row)
        return fresh

    def unprocessed_messages(self, *, limit: int = 400) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT m.* FROM messages m"
                " LEFT JOIN processed p ON p.msg_id = m.msg_id"
                " WHERE p.msg_id IS NULL"
                " ORDER BY m.received_at ASC, m.msg_id ASC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_processed(self, msg_ids: Iterable[str], digest_id: int | None) -> int:
        keys = [str(item) for item in msg_ids if str(item or "").strip()]
        if not keys:
            return 0
        stamp = iso(now_local())
        with self._connect() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO processed (msg_id, digest_id, processed_at) VALUES (?, ?, ?)",
                [(key, digest_id, stamp) for key in keys],
            )
        return len(keys)

    def oldest_unprocessed(self) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT MIN(m.received_at) AS oldest FROM messages m"
                " LEFT JOIN processed p ON p.msg_id = m.msg_id"
                " WHERE p.msg_id IS NULL"
            ).fetchone()
        return _blank(row["oldest"]) if row else ""

    # ---------------------------------------------------------------- 摘要
    def insert_digest(
        self,
        *,
        kind: str,
        window_start: str,
        window_end: str,
        body: str,
        payload: list[dict[str, Any]],
        message_count: int,
        llm_used: bool,
    ) -> int:
        created_at = iso(now_local())
        items = list(payload or [])
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO digests"
                " (kind, window_start, window_end, item_count, message_count,"
                "  llm_used, body, payload, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _blank(kind),
                    _blank(window_start),
                    _blank(window_end),
                    len(items),
                    int(message_count),
                    int(bool(llm_used)),
                    _blank(body),
                    _dumps(items),
                    created_at,
                ),
            )
            digest_id = int(cursor.lastrowid or 0)
        self._audit(
            self.digest_jsonl_path,
            {
                "id": digest_id,
                "kind": _blank(kind),
                "window_start": _blank(window_start),
                "window_end": _blank(window_end),
                "item_count": len(items),
                "message_count": int(message_count),
                "llm_used": bool(llm_used),
                "body": _blank(body),
                "created_at": created_at,
            },
        )
        return digest_id

    def get_digest(self, digest_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM digests WHERE id = ?", (int(digest_id),)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["items"] = self._payload_of(data)
        return data

    @staticmethod
    def _payload_of(data: dict[str, Any]) -> list[Any]:
        try:
            parsed = json.loads(data.get("payload") or "[]")
        except (json.JSONDecodeError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []

    def recent_digests(self, *, limit: int = 5) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, kind, window_start, window_end, item_count, message_count,"
                " body, payload, created_at FROM digests ORDER BY id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            data["items"] = self._payload_of(data)
            result.append(data)
        return result

    def recent_items(self, *, hours: int = 6, limit: int = 200) -> list[dict[str, Any]]:
        if hours <= 0:
            return []
        cutoff = iso(now_local() - dt.timedelta(hours=hours))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, kind, payload, created_at FROM digests"
                " WHERE created_at >= ? AND item_count > 0"
                " ORDER BY id DESC LIMIT ?",
                (cutoff, max(1, int(limit))),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            for entry in self._payload_of(dict(row)):
                if not isinstance(entry, dict):
                    continue
                item = dict(entry)
                item["digest_id"] = int(row["id"] or 0)
                item["kind"] = _blank(row["kind"])
                item["created_at"] = _blank(row["created_at"])
                items.append(item)
        return items

    def recent_item_texts(self, *, hours: int = 6, limit: int = 200) -> list[dict[str, Any]]:
        return [
            {
                "text": _blank(item.get("text") or item.get("summary")),
                "summary": _blank(item.get("summary")),
                "group": _blank(item.get("group")),
                "ts": _blank(item.get("created_at")),
            }
            for item in self.recent_items(hours=hours, limit=limit)
        ]

    # ---------------------------------------------------------------- 投递
    def claim_delivery(
        self,
        *,
        digest_id: int,
        channel: str,
        target: str,
        dedupe_key: str,
        max_attempts: int,
        retry_seconds: int,
    ) -> tuple[bool, int | None]:
        stamp = now_local()
        stamp_text = iso(stamp)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, status, attempts, updated_at FROM deliveries"
                " WHERE dedupe_key = ? AND channel = ? AND target = ?",
                (dedupe_key, channel, target),
            ).fetchone()
            if row is None:
                cursor = connection.execute(
                    "INSERT INTO deliveries"
                    " (digest_id, channel, target, dedupe_key, status, attempts, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, 'pending', 1, ?, ?)",
                    (int(digest_id), channel, target, dedupe_key, stamp_text, stamp_text),
                )
                return True, int(cursor.lastrowid or 0)
            delivery_id = int(row["id"])
            if str(row["status"]) == "sent":
                return False, delivery_id
            if int(row["attempts"]) >= int(max_attempts):
                return False, delivery_id
            previous = parse_iso(row["updated_at"])
            if previous and (stamp - previous).total_seconds() < int(retry_seconds):
                return False, delivery_id
            connection.execute(
                "UPDATE deliveries SET attempts = attempts + 1, status = 'pending', updated_at = ?"
                " WHERE id = ?",
                (stamp_text, delivery_id),
            )
            return True, delivery_id

    def get_delivery(self, delivery_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT %s FROM deliveries WHERE id = ?" % _DELIVERY_FIELDS, (int(delivery_id),)
            ).fetchone()
        return dict(row) if row else None

    def mark_delivery(self, delivery_id: int, *, ok: bool, error: str = "") -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE deliveries SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
                ("sent" if ok else "failed", _blank(error)[:500], iso(now_local()), int(delivery_id)),
            )

    def increment_delivery_attempt(self, delivery_id: int) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempts FROM deliveries WHERE id = ?", (int(delivery_id),)
            ).fetchone()
            if row is None:
                return 0
            attempts = int(row["attempts"]) + 1
            connection.execute(
                "UPDATE deliveries SET attempts = ?, status = 'pending', updated_at = ? WHERE id = ?",
                (attempts, iso(now_local()), int(delivery_id)),
            )
        return attempts

    def pending_deliveries(self, *, max_attempts: int, retry_seconds: int) -> list[dict[str, Any]]:
        cutoff = now_local().timestamp() - int(retry_seconds)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT d.*, g.kind, g.window_start, g.window_end, g.body, g.item_count"
                " FROM deliveries d JOIN digests g ON g.id = d.digest_id"
                " WHERE d.status != 'sent' AND d.attempts < ?"
                " ORDER BY d.updated_at ASC LIMIT 50",
                (int(max_attempts),),
            ).fetchall()
        pending: list[dict[str, Any]] = []
        for row in rows:
            updated = parse_iso(row["updated_at"])
            if updated and updated.timestamp() > cutoff:
                continue
            pending.append(dict(row))
        return pending

    def delivery_stats(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS n FROM deliveries GROUP BY status"
            ).fetchall()
        counts = {_blank(row["status"]): int(row["n"] or 0) for row in rows}
        return {
            "sent": counts.get("sent", 0),
            "pending": counts.get("pending", 0),
            "failed": counts.get("failed", 0),
            "total": sum(counts.values()),
        }

    # ------------------------------------------------------- 两张同步游标
    def dingtalk_sync_state(self) -> dict[str, Any]:
        value = self._sync_value("wal_state")
        if not isinstance(value, dict):
            return {key: 0 for key in _WAL_KEYS}
        return {key: _as_int(value.get(key)) for key in _WAL_KEYS}

    def save_dingtalk_sync_state(self, state: dict[str, Any]) -> None:
        payload = {
            "salt_1": _as_int(state.get("salt_1")),
            "salt_2": _as_int(state.get("salt_2")),
            "frame_index": max(0, _as_int(state.get("frame_index"))),
            "last_created_at": max(0, _as_int(state.get("last_created_at"))),
        }
        self._write_sync_value("wal_state", payload)

    def chaoxing_sync_state(self) -> dict[str, Any]:
        value = self._sync_value("chaoxing_state")
        if not isinstance(value, dict):
            return {"notice_ids": [], "work_ids": []}
        return {
            "notice_ids": _json_list(value.get("notice_ids")),
            "work_ids": _json_list(value.get("work_ids")),
        }

    def save_chaoxing_sync_state(self, state: dict[str, Any]) -> None:
        payload = {
            "notice_ids": _json_list(state.get("notice_ids"))[-1000:],
            "work_ids": _json_list(state.get("work_ids"))[-1000:],
        }
        self._write_sync_value("chaoxing_state", payload)

    def _sync_value(self, key: str) -> Any:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM dingtalk_sync WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(_blank(row["value"]) or "null")
        except (json.JSONDecodeError, TypeError):
            return None

    def _write_sync_value(self, key: str, payload: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO dingtalk_sync (key, value, updated_at) VALUES (?, ?, ?)",
                (key, _dumps(payload), iso(now_local())),
            )

    # -------------------------------------------------------------- 收件箱
    def save_mark(
        self,
        msg_id: str,
        verdict: str,
        reason: str = "",
        method: str = "",
        task_id: int | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO inbox_marks"
                " (msg_id, verdict, reason, method, classified_at, task_id)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (str(msg_id), str(verdict), _blank(reason), _blank(method), iso(now_local()), task_id),
            )

    def unmarked_messages(
        self,
        *,
        since: Any = None,
        until: Any = None,
        groups: Any = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if since:
            conditions.append("m.received_at >= ?")
            params.append(iso(since))
        if until:
            conditions.append("m.received_at <= ?")
            params.append(iso(until))
        if groups:
            names = [str(item) for item in groups]
            conditions.append("m.group_id IN (%s)" % ",".join("?" * len(names)))
            params.extend(names)
        sql = "SELECT m.* FROM messages m LEFT JOIN inbox_marks i ON i.msg_id = m.msg_id"
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += (" AND " if conditions else " WHERE ") + "i.msg_id IS NULL"
        sql += " ORDER BY m.received_at, m.msg_id LIMIT ?"
        params.append(int(limit))
        with self._connect() as connection:
            rows = connection.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def inbox_items(
        self,
        *,
        verdict: Any = None,
        group_id: Any = None,
        q: Any = None,
        limit: int = 50,
        offset: int = 0,
        promoted: bool | None = None,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if verdict:
            conditions.append("i.verdict = ?")
            params.append(verdict)
        if group_id:
            conditions.append("m.group_id = ?")
            params.append(group_id)
        if q:
            conditions.append("m.content LIKE ?")
            params.append("%" + str(q) + "%")
        if promoted is True:
            conditions.append("i.task_id IS NOT NULL")
        elif promoted is False:
            conditions.append("i.task_id IS NULL")
        sql = "SELECT %s FROM messages m JOIN inbox_marks i ON i.msg_id = m.msg_id" % _INBOX_FIELDS
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY m.received_at DESC LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
        with self._connect() as connection:
            rows = connection.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def inbox_counts(self) -> dict[str, int]:
        counts = {"notice": 0, "suspect": 0, "noise": 0}
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT verdict, COUNT(*) AS n FROM inbox_marks GROUP BY verdict"
            ).fetchall()
            promoted = connection.execute(
                "SELECT COUNT(*) FROM inbox_marks WHERE task_id IS NOT NULL"
            ).fetchone()
        counts.update({_blank(row["verdict"]): int(row["n"] or 0) for row in rows})
        counts["promoted"] = int(promoted[0] or 0) if promoted else 0
        return counts

    def delete_inbox_items(self, msg_ids: Iterable[str]) -> int:
        keys = [str(item) for item in msg_ids if str(item or "").strip()]
        if not keys:
            return 0
        marks = ",".join("?" * len(keys))
        with self._connect() as connection:
            connection.execute("DELETE FROM inbox_marks WHERE msg_id IN (%s)" % marks, keys)
            removed = connection.execute(
                "DELETE FROM messages WHERE msg_id IN (%s)" % marks, keys
            ).rowcount
        return max(0, int(removed or 0))

    # ---------------------------------------------------------------- 待办
    def upsert_task(
        self,
        *,
        task_key: str,
        summary: str,
        audience: str = "",
        condition: str = "",
        details: Iterable[str] = (),
        action: str = "",
        category: str = "info",
        importance: int = 3,
        deadline: str = "",
        groups: Iterable[str] = (),
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
        state = str(status or "open")
        if state not in TASK_STATUSES:
            state = "open"
        stamp = iso(now_local())
        group_list = list(dict.fromkeys(_json_list(list(groups or ()))))
        detail_list = [str(item) for item in (details or ()) if str(item or "").strip()]
        with self._connect() as connection:
            row = connection.execute(
                "INSERT INTO tasks"
                " (task_key, summary, action, audience, condition_text, details, category,"
                "  importance, deadline, groups, sender, evidence, status, confidence, source,"
                "  digest_id, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(task_key) DO UPDATE SET"
                "  summary = excluded.summary,"
                "  action = excluded.action,"
                "  audience = excluded.audience,"
                "  condition_text = excluded.condition_text,"
                "  details = excluded.details,"
                "  category = excluded.category,"
                "  importance = MAX(tasks.importance, excluded.importance),"
                "  deadline = CASE WHEN excluded.deadline <> '' THEN excluded.deadline ELSE tasks.deadline END,"
                "  groups = excluded.groups,"
                "  sender = excluded.sender,"
                "  evidence = CASE WHEN excluded.evidence <> '' THEN excluded.evidence ELSE tasks.evidence END,"
                "  confidence = MAX(tasks.confidence, excluded.confidence),"
                "  source = CASE WHEN excluded.source <> '' THEN excluded.source ELSE tasks.source END,"
                "  digest_id = excluded.digest_id,"
                "  updated_at = excluded.updated_at"
                " RETURNING id",
                (
                    key,
                    _blank(summary),
                    _blank(action),
                    _blank(audience),
                    _blank(condition),
                    _dumps(detail_list),
                    _blank(category) or "info",
                    _as_int(importance, 3),
                    _blank(deadline),
                    _dumps(group_list),
                    _blank(sender),
                    _blank(evidence),
                    state,
                    float(confidence or 0.0),
                    _blank(source),
                    _as_int(digest_id),
                    stamp,
                    stamp,
                ),
            ).fetchone()
            task_id = int(row["id"])
            first_time = connection.execute(
                "SELECT 1 FROM task_events WHERE task_id = ? AND event = 'created' LIMIT 1",
                (task_id,),
            ).fetchone() is None
            if first_time:
                confidence_value = float(confidence or 0.0)
                self._record_event(
                    connection,
                    task_id,
                    "created",
                    {"status": state, "confidence": confidence_value, "reason": classification_reason},
                    stamp,
                )
                if state == "candidate":
                    self._record_event(
                        connection,
                        task_id,
                        "candidate_detected",
                        {"confidence": confidence_value, "reason": classification_reason},
                        stamp,
                    )
            return task_id

    @staticmethod
    def _record_event(
        connection: sqlite3.Connection,
        task_id: int,
        event: str,
        detail: Any = "",
        created_at: Any = None,
    ) -> None:
        if isinstance(detail, (dict, list)):
            detail = _dumps(detail)
        connection.execute(
            "INSERT INTO task_events (task_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
            (int(task_id), str(event), _blank(detail), iso(created_at or now_local())),
        )

    def list_tasks(
        self,
        *,
        include_done: bool = True,
        statuses: Iterable[str] | None = None,
        limit: int | None = 800,
    ) -> list[dict[str, Any]]:
        wanted = [str(item) for item in statuses] if statuses is not None else None
        conditions: list[str] = []
        params: list[Any] = []
        if wanted:
            conditions.append("t.status IN (%s)" % ",".join("?" * len(wanted)))
            params.extend(wanted)
        elif not include_done:
            conditions.append("t.status = 'open'")
        sql = _TASK_SELECT
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += (
            " ORDER BY CASE WHEN t.deadline = '' THEN 1 ELSE 0 END, t.deadline ASC,"
            " t.importance DESC, t.id DESC LIMIT ?"
        )
        # SQLite 里负数 LIMIT 等于不限制，limit=None 走这一支。
        params.append(max(1, int(limit)) if limit is not None else -1)
        with self._connect() as connection:
            rows = connection.execute(sql, tuple(params)).fetchall()
        return [self._task_dict(row) for row in rows]

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                _TASK_SELECT + " WHERE t.id = ?", (int(task_id),)
            ).fetchone()
        return self._task_dict(row) if row else None

    @staticmethod
    def _task_dict(row: sqlite3.Row) -> dict[str, Any]:
        task = dict(row)
        task["groups"] = _json_list(task.get("groups"))
        task["details"] = _json_list(task.get("details"))
        task["condition"] = _blank(task.pop("condition_text", ""))
        task["done"] = _blank(task.get("status")) == "done"
        task["snooze_until"] = _blank(task.get("snooze_until"))
        task["duplicate_of"] = _as_int(task.get("duplicate_of"))
        task["pinned"] = bool(task.get("pinned"))
        override = task.get("urgent_override")
        task["urgent_override"] = None if override is None else _as_int(override)
        return task

    def set_task_urgent(self, task_id: int, value: bool | None) -> dict[str, Any]:
        if value is not None and not isinstance(value, bool):
            raise ValueError("紧急覆盖值必须是 true、false 或 null")
        with self._connect() as connection:
            exists = connection.execute("SELECT 1 FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
            if exists is None:
                return {"ok": False, "error": "任务不存在"}
            connection.execute(
                "UPDATE tasks SET urgent_override = ?, updated_at = ? WHERE id = ?",
                (None if value is None else int(value), iso(now_local()), int(task_id)),
            )
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
        result = self._task_dict(row)
        result["ok"] = True
        return result

    def set_task_pinned(self, task_id: int, value: bool) -> dict[str, Any]:
        if not isinstance(value, bool):
            raise ValueError("置顶值必须是 true 或 false")
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
            if row is None:
                return {"ok": False, "error": "任务不存在"}
            if value and _blank(row["status"]) == "done":
                return {"ok": False, "error": "已完成的任务不能置顶"}
            connection.execute(
                "UPDATE tasks SET pinned = ?, updated_at = ? WHERE id = ?",
                (int(value), iso(now_local()), int(task_id)),
            )
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
        result = self._task_dict(row)
        result["ok"] = True
        return result

    def list_open_tasks(self, *, limit: int = 800) -> list[dict[str, Any]]:
        return self.list_tasks(statuses=("open",), limit=limit)

    def list_task_events(self, task_id: int, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, task_id, event, detail, created_at FROM task_events"
                " WHERE task_id = ? ORDER BY id DESC LIMIT ?",
                (int(task_id), max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def apply_task_action(self, task_id: int, action: str, *, detail: Any = "") -> bool:
        name = str(action or "").strip().lower()
        stamp = iso(now_local())
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status, task_key FROM tasks WHERE id = ?", (int(task_id),)
            ).fetchone()
            if row is None:
                return False
            current = _blank(row["status"]) or "open"
            if name in {"confirm", "dismiss", "done", "snooze"}:
                if current not in {"candidate", "open"}:
                    return False
            if name == "confirm":
                connection.execute(
                    "UPDATE tasks SET status = 'open', confirmed_at = ?, updated_at = ? WHERE id = ?",
                    (stamp, stamp, int(task_id)),
                )
                self._record_event(connection, task_id, "confirmed", detail, stamp)
                return True
            if name == "dismiss":
                connection.execute(
                    "UPDATE tasks SET status = 'dismissed', dismissed_at = ?, updated_at = ? WHERE id = ?",
                    (stamp, stamp, int(task_id)),
                )
                self._record_event(connection, task_id, "dismissed", detail, stamp)
                return True
            if name == "done":
                connection.execute(
                    "UPDATE tasks SET status = 'done', done_at = ?, updated_at = ? WHERE id = ?",
                    (stamp, stamp, int(task_id)),
                )
                self._record_event(connection, task_id, "done", detail, stamp)
                return True
            if name == "snooze":
                payload = dict(detail) if isinstance(detail, dict) else {}
                until = parse_iso(payload.get("until")) if payload.get("until") else None
                if not isinstance(until, dt.datetime):
                    until = now_local() + dt.timedelta(days=1)
                payload["until"] = iso(until)
                connection.execute(
                    "UPDATE tasks SET last_reminded_at = ?, snooze_until = ?, updated_at = ? WHERE id = ?",
                    (stamp, iso(until), stamp, int(task_id)),
                )
                self._record_event(connection, task_id, "snoozed", payload, stamp)
                return True
            if name == "reopen":
                if current not in {"done", "dismissed", "expired"}:
                    return False
                connection.execute(
                    "UPDATE tasks SET status = 'open', done_at = '', confirmed_at = ?, updated_at = ?"
                    " WHERE id = ?",
                    (stamp, stamp, int(task_id)),
                )
                self._record_event(connection, task_id, "reopened", detail, stamp)
                return True
            if name == "delete":
                manual_open = current == "open" and _blank(row["task_key"]).startswith("manual:")
                if current not in {"done", "dismissed", "expired"} and not manual_open:
                    return False
                connection.execute("DELETE FROM task_events WHERE task_id = ?", (int(task_id),))
                connection.execute("DELETE FROM tasks WHERE id = ?", (int(task_id),))
                return True
        raise ValueError("不支持的任务动作: %s" % name)

    def set_task_status(self, task_id: int, done: bool) -> bool:
        return self.apply_task_action(task_id, "done" if done else "reopen")

    def task_stats(self) -> dict[str, int]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total,"
                " COALESCE(SUM(status = 'candidate'), 0) AS candidate,"
                " COALESCE(SUM(status = 'open'), 0) AS open,"
                " COALESCE(SUM(status = 'done'), 0) AS done,"
                " COALESCE(SUM(status = 'dismissed'), 0) AS dismissed,"
                " COALESCE(SUM(status = 'expired'), 0) AS expired,"
                " COALESCE(SUM(status = 'open' AND deadline <> '' AND deadline < ?), 0) AS overdue"
                " FROM tasks",
                (iso(now_local()),),
            ).fetchone()
        return {
            key: int(row[key] or 0)
            for key in ("total", "candidate", "open", "done", "dismissed", "expired", "overdue")
        }

    def mark_task_reminded(self, task_id: int, *, detail: Any = "", when: Any = None) -> bool:
        stamp = iso(when or now_local())
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET last_reminded_at = ?, remind_count = remind_count + 1,"
                " updated_at = ? WHERE id = ? AND status IN ('open', 'candidate')",
                (stamp, stamp, int(task_id)),
            )
            if not cursor.rowcount:
                return False
            self._record_event(connection, task_id, "reminded", detail, stamp)
        return True

    def task_metrics(self, *, start: str, end: str) -> dict[str, int | float]:
        with self._connect() as connection:
            def count(sql: str, params: tuple[Any, ...] = ()) -> int:
                row = connection.execute(sql, params).fetchone()
                return int(row[0] or 0)

            def between(event: str) -> int:
                return count(
                    "SELECT COUNT(*) FROM task_events"
                    " WHERE event = ? AND created_at >= ? AND created_at < ?",
                    (event, start, end),
                )

            candidates = between("candidate_detected")
            confirmed = between("confirmed")
            dismissed = between("dismissed")
            completions = between("done")
            reminders = between("reminded")
            completed_after_reminder = count(
                "SELECT COUNT(DISTINCT d.task_id) FROM task_events d"
                " WHERE d.event = 'done' AND d.created_at >= ? AND d.created_at < ?"
                "   AND EXISTS (SELECT 1 FROM task_events r WHERE r.task_id = d.task_id"
                "               AND r.event = 'reminded' AND r.created_at <= d.created_at)",
                (start, end),
            )
            overdue = count(
                "SELECT COUNT(*) FROM tasks WHERE status = 'open' AND deadline <> '' AND deadline < ?",
                (end,),
            )
        return {
            "candidates": candidates,
            "confirmed": confirmed,
            "dismissed": dismissed,
            "completions": completions,
            "reminders": reminders,
            "completions_after_reminder": completed_after_reminder,
            "overdue": overdue,
            "confirmation_rate": round(confirmed * 100 / candidates, 1) if candidates else 0.0,
            "dismissal_rate": round(dismissed * 100 / candidates, 1) if candidates else 0.0,
            "completion_rate": round(completions * 100 / max(1, completions + overdue), 1),
        }

    def apply_task_correction(self, task_id: int, correction: str, *, value: Any = "") -> bool:
        name = str(correction or "").strip().lower()
        if name not in {
            "not_notice", "not_task", "not_urgent", "duplicate", "category", "deadline", "clear_deadline",
        }:
            raise ValueError("不支持的纠错类型: %s" % name)
        stamp = iso(now_local())
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status, category, importance, deadline, summary, groups FROM tasks WHERE id = ?",
                (int(task_id),),
            ).fetchone()
            if row is None:
                return False
            previous = {
                "status": _blank(row["status"]) or "open",
                "category": _blank(row["category"]) or "info",
                "importance": _as_int(row["importance"], 3),
                "deadline": _blank(row["deadline"]),
            }
            new = dict(previous)
            duplicate_of = 0
            if name in {"not_notice", "not_task"}:
                new["status"] = "dismissed"
            elif name == "duplicate":
                new["status"] = "dismissed"
                duplicate_of = self._duplicate_target(
                    connection,
                    task_id=int(task_id),
                    summary=_blank(row["summary"]),
                    requested=value,
                )
            elif name == "not_urgent":
                new["category"] = "action" if previous["category"] == "urgent" else previous["category"]
                new["importance"] = min(previous["importance"], 3)
            elif name == "category":
                category = str(value or "").strip().lower()
                if category not in {"urgent", "action", "academic", "info"}:
                    raise ValueError("无效的任务分类")
                new["category"] = category
                new["importance"] = IMPORTANCE_BY_CATEGORY[category]
            elif name == "deadline":
                parsed = iso(value)
                if str(value or "").strip() and not parsed:
                    raise ValueError("无效的截止时间")
                new["deadline"] = parsed
            else:
                new["deadline"] = ""

            dismissed_at = stamp if new["status"] == "dismissed" and previous["status"] != "dismissed" else ""
            connection.execute(
                "UPDATE tasks SET status = ?, category = ?, importance = ?, deadline = ?,"
                " duplicate_of = ?, dismissed_at = CASE WHEN ? <> '' THEN ? ELSE dismissed_at END,"
                " updated_at = ? WHERE id = ?",
                (
                    new["status"],
                    new["category"],
                    new["importance"],
                    new["deadline"],
                    int(duplicate_of),
                    dismissed_at,
                    dismissed_at,
                    stamp,
                    int(task_id),
                ),
            )
            detail = {
                "type": name,
                "previous": previous,
                "new": {
                    "status": new["status"],
                    "category": new["category"],
                    "importance": new["importance"],
                    "deadline": new["deadline"],
                    "duplicate_of": duplicate_of,
                },
                "value": str(value or "")[:500],
            }
            if duplicate_of:
                merged = self._merge_groups(connection, duplicate_of, _blank(row["groups"]))
                if merged:
                    detail["merged_groups"] = merged
            self._record_event(connection, task_id, "corrected", detail, stamp)
        return True

    @staticmethod
    def _duplicate_target(
        connection: sqlite3.Connection,
        *,
        task_id: int,
        summary: str,
        requested: Any,
    ) -> int:
        try:
            candidate = int(requested or 0)
        except (TypeError, ValueError):
            candidate = 0
        if candidate and candidate != int(task_id):
            row = connection.execute(
                "SELECT id FROM tasks WHERE id = ? AND id <> ?", (candidate, int(task_id))
            ).fetchone()
            if row is not None:
                return int(row["id"])
        target = Store._normalized(summary)
        if not target:
            return 0
        rows = connection.execute(
            "SELECT id, summary FROM tasks"
            " WHERE id <> ? AND status NOT IN ('dismissed', 'expired')"
            " ORDER BY id DESC LIMIT 200",
            (int(task_id),),
        ).fetchall()
        for item in rows:
            if Store._normalized(item["summary"]) == target:
                return int(item["id"])
        return 0

    @staticmethod
    def _normalized(text: str) -> str:
        return re.sub(r"[\s，。、；：！？,.!?;:（）()\[\]【】\"'“”‘’\-_—]+", "", str(text or "")).lower()

    @staticmethod
    def _merge_groups(connection: sqlite3.Connection, original_id: int, groups: Any) -> list[str]:
        incoming = _json_list(groups)
        if not incoming:
            return []
        row = connection.execute("SELECT groups FROM tasks WHERE id = ?", (int(original_id),)).fetchone()
        if row is None:
            return []
        merged = _json_list(row["groups"])
        for name in incoming:
            if name not in merged:
                merged.append(name)
        if merged != _json_list(row["groups"]):
            connection.execute(
                "UPDATE tasks SET groups = ?, updated_at = ? WHERE id = ?",
                (_dumps(merged), iso(now_local()), int(original_id)),
            )
        return merged

    def correction_stats(self, *, days: int = 30) -> dict[str, Any]:
        since = iso(now_local() - dt.timedelta(days=max(1, int(days))))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT e.detail AS detail, t.groups AS groups FROM task_events e"
                " JOIN tasks t ON t.id = e.task_id"
                " WHERE e.event = 'corrected' AND e.created_at >= ? ORDER BY e.id DESC",
                (since,),
            ).fetchall()
        total = 0
        by_type: dict[str, int] = {}
        by_group: dict[str, dict[str, int]] = {}
        for row in rows:
            try:
                detail = json.loads(_blank(row["detail"]) or "{}")
            except (json.JSONDecodeError, TypeError):
                detail = {}
            if not isinstance(detail, dict):
                detail = {}
            kind = _blank(detail.get("type")) or "unknown"
            total += 1
            by_type[kind] = by_type.get(kind, 0) + 1
            for name in _json_list(row["groups"]):
                entry = by_group.setdefault(name, {})
                entry[kind] = entry.get(kind, 0) + 1
                entry["total"] = entry.get("total", 0) + 1
        return {"days": int(days), "total": total, "by_type": by_type, "by_group": by_group}

    def correction_insights(self, *, days: int = 30, min_group_hits: int = 3) -> list[str]:
        stats = self.correction_stats(days=days)
        threshold = max(2, int(min_group_hits))
        insights: list[str] = []
        groups = stats.get("by_group") or {}
        for name, entry in sorted(groups.items(), key=lambda pair: -int(pair[1].get("total") or 0)):
            if int(entry.get("total") or 0) < threshold:
                continue
            reasons: list[str] = []
            missed = int(entry.get("not_urgent") or 0)
            noise = int(entry.get("not_notice") or 0) + int(entry.get("not_task") or 0)
            duplicated = int(entry.get("duplicate") or 0)
            if missed:
                reasons.append("误判紧急 %d 次" % missed)
            if noise:
                reasons.append("误判为通知/待办 %d 次" % noise)
            if duplicated:
                reasons.append("重复 %d 次" % duplicated)
            if reasons:
                insights.append("%s：%s，建议收紧该群规则或加入低优先级群。" % (name, "、".join(reasons)))
        if int((stats.get("by_type") or {}).get("duplicate") or 0) >= 3:
            insights.append("重复标记偏多，建议复查跨群去重时间窗 QQ_DIGEST_DEDUPE_HOURS。")
        return insights

    def clear_tasks(self) -> int:
        with self._connect() as connection:
            connection.execute("DELETE FROM task_events")
            removed = connection.execute("DELETE FROM tasks").rowcount
        return int(removed or 0)

    def clear_finished_tasks(self) -> int:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM tasks WHERE status IN ('done', 'dismissed', 'expired')"
            ).fetchall()
            keys = [int(row["id"]) for row in rows]
            if not keys:
                return 0
            marks = ",".join("?" * len(keys))
            connection.execute("DELETE FROM task_events WHERE task_id IN (%s)" % marks, keys)
            connection.execute("DELETE FROM tasks WHERE id IN (%s)" % marks, keys)
        return len(keys)

    # -------------------------------------------------------------- 群名
    def group_names(self) -> dict[str, str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT m.group_id AS group_id, m.group_name AS group_name FROM messages m"
                " JOIN (SELECT group_id AS group_id, MAX(ts) AS ts FROM messages"
                "        WHERE group_name <> '' AND group_name <> group_id"
                "        GROUP BY group_id) latest"
                "   ON latest.group_id = m.group_id AND latest.ts = m.ts"
                " WHERE m.group_name <> '' AND m.group_name <> m.group_id"
                " GROUP BY m.group_id"
            ).fetchall()
            cached = connection.execute("SELECT group_id, name FROM group_names").fetchall()
        names = {_blank(row["group_id"]): _blank(row["group_name"]) for row in rows}
        names.update({_blank(row["group_id"]): _blank(row["name"]) for row in cached})
        return names

    def remember_group_names(self, names: Mapping[str, str]) -> int:
        cleaned = {
            str(key).strip(): str(value).strip()
            for key, value in (names or {}).items()
            if str(key).strip() and str(value).strip() and str(value).strip() != str(key).strip()
        }
        if not cleaned:
            return 0
        stamp = iso(now_local())
        with self._connect() as connection:
            connection.executemany(
                "INSERT INTO group_names (group_id, name, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(group_id) DO UPDATE SET name = excluded.name, updated_at = excluded.updated_at",
                [(key, value, stamp) for key, value in cleaned.items()],
            )
        return len(cleaned)

    @staticmethod
    def group_label(group_id: Any, names: dict[str, str], aliases: Any = None) -> str:
        key = str(group_id or "")
        if not key:
            return ""
        alias = str((aliases or {}).get(key) or "").strip()
        return alias or str(names.get(key) or "").strip() or key

    # ------------------------------------------------------------ 元信息
    def meta_get(self, key: str, default: str = "") -> str:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return _blank(row["value"]) if row else default

    def meta_set(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    def prune(self, *, retention_days: int | None = None) -> int:
        days = int(retention_days or self.retention_days)
        cutoff = iso(now_local() - dt.timedelta(days=days))
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM processed WHERE msg_id IN"
                " (SELECT msg_id FROM messages WHERE received_at < ?)",
                (cutoff,),
            )
            removed = connection.execute(
                "DELETE FROM messages WHERE received_at < ?", (cutoff,)
            ).rowcount
            connection.execute("DELETE FROM processed WHERE msg_id NOT IN (SELECT msg_id FROM messages)")
            connection.execute("DELETE FROM inbox_marks WHERE msg_id NOT IN (SELECT msg_id FROM messages)")
        return int(removed or 0)

    def counts(self) -> dict[str, int]:
        with self._connect() as connection:
            def scalar(sql: str) -> int:
                row = connection.execute(sql).fetchone()
                return int(row[0] or 0)

            return {
                "messages": scalar("SELECT COUNT(*) FROM messages"),
                "unprocessed": scalar(
                    "SELECT COUNT(*) FROM messages m"
                    " LEFT JOIN processed p ON p.msg_id = m.msg_id WHERE p.msg_id IS NULL"
                ),
                "digests": scalar("SELECT COUNT(*) FROM digests"),
                "deliveries_sent": scalar("SELECT COUNT(*) FROM deliveries WHERE status = 'sent'"),
                "deliveries_failed": scalar("SELECT COUNT(*) FROM deliveries WHERE status = 'failed'"),
            }
