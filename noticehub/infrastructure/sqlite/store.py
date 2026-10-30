"""SQLite 持久化：消息去重、摘要归档、投递去重与重启恢复。"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import datetime as dt
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from noticehub.domain.time import iso, parse_iso
from noticehub.infrastructure.clock import SystemClock

LOGGER = logging.getLogger("qq_live_digest.store")


def now_local() -> dt.datetime:
    return SystemClock().now()

# 库结构版本：用 PRAGMA user_version 记在库里。旧程序打开新库会读不懂新列，所以
# 库里版本 > 代码版本时直接拒绝启动，别让旧程序在半懂的状态下写坏数据。
SCHEMA_VERSION = 1

# 分类 → 重要度：**唯一一份定义**。LLM 分类落库（summarizer）与用户手动改分类
# （apply_task_correction）都 import 这一个常量，别再各写一份——以前 action 一处 4、
# 一处 3，info 一处 3、一处 1，同一条任务点一下「改成待办」重要度就跟着跳。
IMPORTANCE_BY_CATEGORY = {"urgent": 5, "action": 4, "academic": 3, "info": 3}

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
    done_at     TEXT NOT NULL DEFAULT ''
    ,snooze_until TEXT NOT NULL DEFAULT ''
    ,duplicate_of INTEGER NOT NULL DEFAULT 0
    ,urgent_override INTEGER
    ,pinned INTEGER NOT NULL DEFAULT 0
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


TASK_STATUSES = {"candidate", "open", "done", "dismissed", "expired"}


def effective_urgent(task: dict[str, Any]) -> bool:
    """Return user override when set, otherwise follow the AI category."""
    override = task.get("urgent_override")
    return bool(override) if override is not None else str(task.get("category") or "") == "urgent"


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


class Store:
    def __init__(self, path: Path | str, *, retention_days: int = 30) -> None:
        self.path = Path(path)
        self.retention_days = retention_days
        self.logger = LOGGER
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.path.parent / "messages.jsonl"
        self.digest_jsonl_path = self.path.parent / "digests.jsonl"
        self.init()

    # ---------------------------------------------------------------- 基础设施
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
        problem = self._integrity_problem()
        quarantined = self._quarantine_corrupt() if problem else ""
        version = self._read_user_version()
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                "数据库结构版本 %d 高于本程序支持的 %d，请升级 notice-hub 后再启动：%s"
                % (version, SCHEMA_VERSION, self.path)
            )
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate_messages(connection)
            self._migrate_tasks(connection)
            if version != SCHEMA_VERSION:
                connection.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
        if quarantined:
            self.meta_set("recovered_from", quarantined)
            self.logger.error(
                "数据库已损坏（%s），已隔离为 %s，新库已建立，历史数据需从备份恢复。",
                problem,
                quarantined,
            )

    def _read_user_version(self) -> int:
        """只读读库结构版本；库还不存在/打不开时按 0 算。"""
        try:
            connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        except sqlite3.Error:
            return 0
        try:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])
        except sqlite3.Error:
            return 0
        finally:
            connection.close()

    def _integrity_problem(self) -> str:
        """只读做一次 integrity_check（同 doctor.py 的做法）；正常返回空串，否则返回问题描述。

        只有「库确实坏了」才返回非空：locked / busy / io 这类临时故障绝不能拿去隔离库，
        否则双开实例或一次抖动就能把好数据搬走。
        """
        try:
            if not self.path.exists() or self.path.stat().st_size == 0:
                return ""
        except OSError:
            return ""
        try:
            connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        except sqlite3.Error as error:
            return self._corruption_hint(str(error))
        try:
            row = connection.execute("PRAGMA integrity_check").fetchone()
            result = str(row[0]) if row else "（无结果）"
        except sqlite3.Error as error:
            return self._corruption_hint(str(error))
        finally:
            connection.close()
        return "" if result == "ok" else result

    @staticmethod
    def _corruption_hint(message: str) -> str:
        lowered = message.lower()
        hints = ("not a database", "malformed", "corrupt", "encrypted")
        return message if any(hint in lowered for hint in hints) else ""

    def _quarantine_corrupt(self) -> str:
        """把坏库连同 -wal/-shm 改名隔离，返回隔离文件名；随后 init 会建新库。

        隔离物是证据：同名已存在时加序号退让，绝不覆盖上一次的坏库样本。
        """
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        target = Path("%s.corrupt-%s" % (self.path, stamp))
        index = 2
        while target.exists():
            target = Path("%s.corrupt-%s-%d" % (self.path, stamp, index))
            index += 1
        for suffix in ("", "-wal", "-shm"):
            source = Path("%s%s" % (self.path, suffix))
            if source.exists():
                source.replace(Path("%s%s" % (target, suffix)))
        return target.name

    @staticmethod
    def _migrate_messages(connection: sqlite3.Connection) -> None:
        existing = {str(row["name"]) for row in connection.execute("PRAGMA table_info(messages)").fetchall()}
        for name, definition in MESSAGE_COLUMN_MIGRATIONS.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE messages ADD COLUMN {name} {definition}")

    @staticmethod
    def _migrate_tasks(connection: sqlite3.Connection) -> None:
        existing = {str(row["name"]) for row in connection.execute("PRAGMA table_info(tasks)").fetchall()}
        for name, definition in TASK_COLUMN_MIGRATIONS.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")

    @staticmethod
    def _task_event(
        connection: sqlite3.Connection,
        task_id: int,
        event: str,
        detail: Any = "",
        *,
        created_at: Any = None,
    ) -> None:
        if isinstance(detail, (dict, list)):
            detail = json.dumps(detail, ensure_ascii=False, default=str)
        connection.execute(
            "INSERT INTO task_events (task_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
            (int(task_id), str(event), str(detail or ""), iso(created_at or now_local())),
        )

    def _append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        except OSError as error:  # 审计日志失败不应影响主流程
            LOGGER.warning("写入 JSONL 失败 %s: %s", path, error)

    # ------------------------------------------------------------------- 消息
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
        """写入消息，msg_id 重复时返回 False。"""
        msg_id = str(msg_id or "").strip()
        if not msg_id:
            raise ValueError("msg_id 不能为空")
        received = iso(received_at or now_local())
        stamp = iso(ts) or received
        payload = {
            "msg_id": msg_id,
            "source": source,
            "event": event,
            "group_id": str(group_id or ""),
            "group_name": group_name,
            "sender_id": str(sender_id or ""),
            "sender_name": sender_name,
            "ts": stamp,
            "received_at": received,
            "content": content or "",
            "source_text": source_text or "",
        }
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO messages
                    (msg_id, source, event, group_id, group_name, sender_id, sender_name, ts, received_at, content, source_text)
                VALUES (:msg_id, :source, :event, :group_id, :group_name, :sender_id, :sender_name, :ts, :received_at, :content, :source_text)
                """,
                payload,
            )
            inserted = cursor.rowcount > 0
        if inserted:
            self._append_jsonl(self.jsonl_path, payload)
        return inserted

    def unprocessed_messages(self, *, limit: int = 400) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT m.* FROM messages m
                LEFT JOIN processed p ON p.msg_id = m.msg_id
                WHERE p.msg_id IS NULL
                ORDER BY m.received_at ASC, m.msg_id ASC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_processed(self, msg_ids: Iterable[str], digest_id: int | None) -> int:
        ids = [str(item) for item in msg_ids if str(item or "").strip()]
        if not ids:
            return 0
        stamp = iso(now_local())
        with self._connect() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO processed (msg_id, digest_id, processed_at) VALUES (?, ?, ?)",
                [(msg_id, digest_id, stamp) for msg_id in ids],
            )
        return len(ids)

    def oldest_unprocessed(self) -> str:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT MIN(m.received_at) AS oldest FROM messages m
                LEFT JOIN processed p ON p.msg_id = m.msg_id
                WHERE p.msg_id IS NULL
                """
            ).fetchone()
        return str(row["oldest"] or "") if row else ""

    # ------------------------------------------------------------------- 摘要
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
        record = {
            "kind": kind,
            "window_start": window_start,
            "window_end": window_end,
            "message_count": int(message_count),
            "item_count": len(payload),
            "llm_used": bool(llm_used),
            "body": body,
            "created_at": iso(now_local()),
        }
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO digests
                    (kind, window_start, window_end, item_count, message_count, llm_used, body, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["kind"],
                    record["window_start"],
                    record["window_end"],
                    record["item_count"],
                    record["message_count"],
                    int(record["llm_used"]),
                    record["body"],
                    json.dumps(payload, ensure_ascii=False, default=str),
                    record["created_at"],
                ),
            )
            digest_id = int(cursor.lastrowid or 0)
        record["id"] = digest_id
        self._append_jsonl(self.digest_jsonl_path, record)
        return digest_id

    def get_digest(self, digest_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM digests WHERE id = ?", (int(digest_id),)).fetchone()
        if not row:
            return None
        data = dict(row)
        try:
            data["items"] = json.loads(data.get("payload") or "[]")
        except json.JSONDecodeError:
            data["items"] = []
        return data

    # ------------------------------------------------------------------- 投递
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
        """返回 (是否应立即发送, delivery_id)。

        已成功发送的写入 sent，永不重发；失败的在冷却期后允许重试，直到达到上限。
        """
        stamp = now_local()
        stamp_text = iso(stamp)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, status, attempts, updated_at FROM deliveries WHERE dedupe_key=? AND channel=? AND target=?",
                (dedupe_key, channel, target),
            ).fetchone()
            if row is None:
                cursor = connection.execute(
                    """
                    INSERT INTO deliveries
                        (digest_id, channel, target, dedupe_key, status, attempts, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'pending', 1, ?, ?)
                    """,
                    (int(digest_id), channel, target, dedupe_key, stamp_text, stamp_text),
                )
                return True, int(cursor.lastrowid or 0)

            delivery_id = int(row["id"])
            if row["status"] == "sent":
                return False, delivery_id
            if int(row["attempts"]) >= int(max_attempts):
                return False, delivery_id
            updated = parse_iso(row["updated_at"])
            if updated and (stamp - updated).total_seconds() < int(retry_seconds):
                return False, delivery_id
            connection.execute(
                """
                UPDATE deliveries SET attempts = attempts + 1, status='pending', updated_at=?
                WHERE id=?
                """,
                (stamp_text, delivery_id),
            )
            return True, delivery_id

    def get_delivery(self, delivery_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, status, attempts, last_error, updated_at FROM deliveries WHERE id=?",
                (int(delivery_id),),
            ).fetchone()
        return dict(row) if row else None

    def mark_delivery(self, delivery_id: int, *, ok: bool, error: str = "") -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE deliveries SET status=?, last_error=?, updated_at=? WHERE id=?",
                ("sent" if ok else "failed", (error or "")[:500], iso(now_local()), int(delivery_id)),
            )

    def increment_delivery_attempt(self, delivery_id: int) -> int:
        """重试已在 pending_deliveries 中筛过，这里只累加尝试次数。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempts FROM deliveries WHERE id=?", (int(delivery_id),)
            ).fetchone()
            attempts = int(row["attempts"]) + 1 if row else 0
            if row:
                connection.execute(
                    "UPDATE deliveries SET attempts=?, status='pending', updated_at=? WHERE id=?",
                    (attempts, iso(now_local()), int(delivery_id)),
                )
        return attempts

    def pending_deliveries(self, *, max_attempts: int, retry_seconds: int) -> list[dict[str, Any]]:
        threshold = now_local().timestamp() - int(retry_seconds)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT d.*, g.kind, g.window_start, g.window_end, g.body, g.item_count
                FROM deliveries d JOIN digests g ON g.id = d.digest_id
                WHERE d.status != 'sent' AND d.attempts < ?
                ORDER BY d.updated_at ASC
                LIMIT 50
                """,
                (int(max_attempts),),
            ).fetchall()
        pending: list[dict[str, Any]] = []
        for row in rows:
            updated = parse_iso(row["updated_at"])
            if updated and updated.timestamp() > threshold:
                continue
            pending.append(dict(row))
        return pending

    # ---------------------------------------------------------- 钉钉 WAL 同步状态
    def dingtalk_sync_state(self) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM dingtalk_sync WHERE key=?", ("wal_state",)
            ).fetchone()
        if row is None:
            return {"salt_1": 0, "salt_2": 0, "frame_index": 0, "last_created_at": 0}
        try:
            value = json.loads(str(row["value"]))
        except (json.JSONDecodeError, TypeError):
            return {"salt_1": 0, "salt_2": 0, "frame_index": 0, "last_created_at": 0}
        if not isinstance(value, dict):
            return {"salt_1": 0, "salt_2": 0, "frame_index": 0, "last_created_at": 0}
        return value

    def chaoxing_sync_state(self) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM dingtalk_sync WHERE key=?", ("chaoxing_state",)).fetchone()
        if row is None:
            return {"notice_ids": [], "work_ids": []}
        try:
            value = json.loads(str(row["value"]))
        except (json.JSONDecodeError, TypeError):
            return {"notice_ids": [], "work_ids": []}
        return value if isinstance(value, dict) else {"notice_ids": [], "work_ids": []}

    def save_chaoxing_sync_state(self, state: dict[str, Any]) -> None:
        safe = {"notice_ids": [str(x) for x in state.get("notice_ids", [])][-1000:], "work_ids": [str(x) for x in state.get("work_ids", [])][-1000:]}
        with self._connect() as connection:
            connection.execute("INSERT OR REPLACE INTO dingtalk_sync (key, value, updated_at) VALUES (?, ?, ?)", ("chaoxing_state", json.dumps(safe, ensure_ascii=False), iso(now_local())))

    def save_dingtalk_sync_state(self, state: dict[str, Any]) -> None:
        safe_state = {
            "salt_1": int(state.get("salt_1") or 0),
            "salt_2": int(state.get("salt_2") or 0),
            "frame_index": max(0, int(state.get("frame_index") or 0)),
            "last_created_at": max(0, int(state.get("last_created_at") or 0)),
        }
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO dingtalk_sync (key, value, updated_at) VALUES (?, ?, ?)",
                ("wal_state", json.dumps(safe_state), iso(now_local())),
            )

    # ------------------------------------------------------------------- 元信息
    # ------------------------------------------------------------------- 去重
    def recent_items(self, *, hours: int = 6, limit: int = 200) -> list[dict[str, Any]]:
        """最近推送过的条目，用于跨群、跨窗口的重复通知抑制。"""
        if hours <= 0:
            return []
        cutoff = iso(now_local() - dt.timedelta(hours=hours))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, kind, payload, created_at FROM digests
                WHERE created_at >= ? AND item_count > 0
                ORDER BY id DESC LIMIT ?
                """,
                (cutoff, max(1, int(limit))),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(payload, list):
                continue
            for entry in payload:
                if not isinstance(entry, dict):
                    continue
                item = dict(entry)
                item["digest_id"] = int(row["id"] or 0)
                item["kind"] = str(row["kind"] or "")
                item["created_at"] = str(row["created_at"] or "")
                items.append(item)
        return items

    def recent_item_texts(self, *, hours: int = 6, limit: int = 200) -> list[dict[str, Any]]:
        """最近推送过的条目文本，用于跨群、跨窗口的重复通知抑制。"""
        return [
            {
                "text": str(item.get("text") or item.get("summary") or ""),
                "summary": str(item.get("summary") or ""),
                "group": str(item.get("group") or ""),
                "ts": str(item.get("created_at") or ""),
            }
            for item in self.recent_items(hours=hours, limit=limit)
        ]

    def recent_digests(self, *, limit: int = 5) -> list[dict[str, Any]]:
        """最近几条摘要（含条目 payload），供 show 命令回溯判断依据。"""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, kind, window_start, window_end, item_count, message_count, body, payload, created_at
                FROM digests ORDER BY id DESC LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            try:
                data["items"] = json.loads(data.get("payload") or "[]")
            except (json.JSONDecodeError, TypeError):
                data["items"] = []
            result.append(data)
        return result
    # ---------------------------------------------------------------- 收件箱
    def save_mark(self, msg_id: str, verdict: str, reason: str = "", method: str = "", task_id: int | None = None) -> None:
        with self._connect() as connection:
            connection.execute("INSERT OR REPLACE INTO inbox_marks(msg_id,verdict,reason,method,classified_at,task_id) VALUES(?,?,?,?,?,?)", (str(msg_id), str(verdict), str(reason), str(method), iso(now_local()), task_id))

    def unmarked_messages(self, *, since=None, until=None, groups=None, limit=1000) -> list[dict[str, Any]]:
        clauses, args = [], []
        if since: clauses.append("m.received_at >= ?"); args.append(iso(since))
        if until: clauses.append("m.received_at <= ?"); args.append(iso(until))
        if groups: clauses.append("m.group_id IN (%s)" % ",".join("?" * len(groups))); args.extend(str(x) for x in groups)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as c:
            rows = c.execute(f"SELECT m.* FROM messages m LEFT JOIN inbox_marks i ON i.msg_id=m.msg_id {where} {'AND' if where else 'WHERE'} i.msg_id IS NULL ORDER BY m.received_at,m.msg_id LIMIT ?", (*args, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def inbox_items(self, *, verdict=None, group_id=None, q=None, limit=50, offset=0, promoted: bool | None = None) -> list[dict[str, Any]]:
        clauses, args = [], []
        if verdict: clauses.append("i.verdict=?"); args.append(verdict)
        if group_id: clauses.append("m.group_id=?"); args.append(group_id)
        if q: clauses.append("m.content LIKE ?"); args.append("%" + q + "%")
        if promoted is True: clauses.append("i.task_id IS NOT NULL")
        elif promoted is False: clauses.append("i.task_id IS NULL")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as c:
            rows=c.execute(f"SELECT m.msg_id,m.source,m.event,m.received_at,m.group_id,m.group_name,m.sender_name,m.content,i.verdict,i.reason,i.method,i.task_id FROM messages m JOIN inbox_marks i ON i.msg_id=m.msg_id {where} ORDER BY m.received_at DESC LIMIT ? OFFSET ?", (*args,int(limit),int(offset))).fetchall()
        return [dict(r) for r in rows]

    def inbox_counts(self) -> dict[str, int]:
        with self._connect() as c:
            rows=c.execute("SELECT verdict,COUNT(*) n FROM inbox_marks GROUP BY verdict").fetchall()
        out={"notice":0,"suspect":0,"noise":0}; out.update({str(r["verdict"]):int(r["n"]) for r in rows})
        with self._connect() as c: out["promoted"] = int(c.execute("SELECT COUNT(*) FROM inbox_marks WHERE task_id IS NOT NULL").fetchone()[0])
        return out

    def delete_inbox_items(self, msg_ids: Iterable[str]) -> int:
        """真删收件箱条目：同一事务里删掉 ``inbox_marks`` 与 ``messages`` 对应行。

        已转成的待办存在 tasks 表里、自带 evidence，不跟着删。
        条数以 ``messages`` 实际删除数为准（marks 可能本来就不存在，不虚报）。
        空输入返回 0，未知 id 计 0，都不抛异常。
        """
        ids = [str(msg_id) for msg_id in msg_ids if str(msg_id or "").strip()]
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        with self._connect() as connection:
            connection.execute(f"DELETE FROM inbox_marks WHERE msg_id IN ({placeholders})", ids)
            deleted = connection.execute(f"DELETE FROM messages WHERE msg_id IN ({placeholders})", ids).rowcount
        return max(0, int(deleted or 0))

    # ------------------------------------------------------------------- 任务
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
        """写入或更新任务；重复出现时保留用户处理过的状态。"""
        key = str(task_key or "").strip()
        if not key:
            return 0
        status = str(status or "open")
        if status not in TASK_STATUSES:
            status = "open"
        stamp = iso(now_local())
        group_list = [str(name) for name in groups if str(name or "").strip()]
        groups_json = json.dumps(list(dict.fromkeys(group_list)), ensure_ascii=False)
        detail_list = [str(value) for value in details if str(value or "").strip()]
        details_json = json.dumps(detail_list, ensure_ascii=False)
        with self._connect() as connection:
            # 一条语句完成「无则插入、有则更新」：并发同 key 不会再撞 UNIQUE 抛 IntegrityError。
            # 不动 status/done_at/snooze_until/pinned 等用户处理状态，保住「已处理不被覆盖」。
            row = connection.execute(
                """
                INSERT INTO tasks
                    (task_key, summary, action, audience, condition_text, details,
                     category, importance, deadline, groups, sender, evidence,
                     status, confidence, source, digest_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_key) DO UPDATE SET
                    summary=excluded.summary,
                    action=excluded.action,
                    audience=excluded.audience,
                    condition_text=excluded.condition_text,
                    details=excluded.details,
                    category=excluded.category,
                    importance=MAX(tasks.importance, excluded.importance),
                    deadline=CASE WHEN excluded.deadline <> '' THEN excluded.deadline ELSE tasks.deadline END,
                    groups=excluded.groups,
                    sender=excluded.sender,
                    evidence=CASE WHEN excluded.evidence <> '' THEN excluded.evidence ELSE tasks.evidence END,
                    confidence=MAX(tasks.confidence, excluded.confidence),
                    source=CASE WHEN excluded.source <> '' THEN excluded.source ELSE tasks.source END,
                    digest_id=excluded.digest_id,
                    updated_at=excluded.updated_at
                RETURNING id
                """,
                (
                    key,
                    str(summary or ""),
                    str(action or ""),
                    str(audience or ""),
                    str(condition or ""),
                    details_json,
                    str(category or "info"),
                    int(importance or 3),
                    str(deadline or ""),
                    groups_json,
                    str(sender or ""),
                    str(evidence or ""),
                    status,
                    float(confidence or 0.0),
                    str(source or ""),
                    int(digest_id or 0),
                    stamp,
                    stamp,
                ),
            ).fetchone()
            task_id = int(row["id"])
            # 新建还是更新：靠这条任务有没有 created 事件判断，不依赖秒级时间戳。
            created = connection.execute(
                "SELECT 1 FROM task_events WHERE task_id = ? AND event = 'created' LIMIT 1",
                (task_id,),
            ).fetchone() is None
            if created:
                self._task_event(
                    connection,
                    task_id,
                    "created",
                    {"status": status, "confidence": float(confidence or 0.0), "reason": classification_reason},
                    created_at=stamp,
                )
                if status == "candidate":
                    self._task_event(
                        connection,
                        task_id,
                        "candidate_detected",
                        {"confidence": float(confidence or 0.0), "reason": classification_reason},
                        created_at=stamp,
                    )
            return task_id

    def list_tasks(
        self,
        *,
        include_done: bool = True,
        statuses: Iterable[str] | None = None,
        limit: int | None = 800,
    ) -> list[dict[str, Any]]:
        """默认只取前 800 条；统计/导出这类必须全量的调用点显式传 limit=None。"""
        where: list[str] = []
        params: list[Any] = []
        status_list = [str(item) for item in statuses] if statuses is not None else None
        if status_list:
            where.append("t.status IN (" + ",".join("?" for _ in status_list) + ")")
            params.extend(status_list)
        elif not include_done:
            where.append("t.status = 'open'")
        sql = """
            SELECT t.*,
                   (SELECT detail FROM task_events e
                    WHERE e.task_id = t.id AND e.event = 'candidate_detected'
                    ORDER BY e.id DESC LIMIT 1) AS candidate_detail
                   ,(SELECT COALESCE(NULLIF(m.source_text, ''), m.content) FROM messages m
                     WHERE m.msg_id = t.task_key LIMIT 1) AS source_text
                   ,(SELECT d.summary FROM tasks d WHERE d.id = t.duplicate_of) AS duplicate_summary
            FROM tasks t
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += (
            " ORDER BY CASE WHEN t.deadline = '' THEN 1 ELSE 0 END, t.deadline ASC,"
            " t.importance DESC, t.id DESC LIMIT ?"
        )
        # SQLite 里负数 LIMIT 等价于不限制，None 走这一支。
        params.append(max(1, int(limit)) if limit is not None else -1)
        with self._connect() as connection:
            rows = connection.execute(sql, tuple(params)).fetchall()
        return [self._task_row(row) for row in rows]

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT t.*,
                       (SELECT detail FROM task_events e
                        WHERE e.task_id = t.id AND e.event = 'candidate_detected'
                        ORDER BY e.id DESC LIMIT 1) AS candidate_detail
                       ,(SELECT COALESCE(NULLIF(m.source_text, ''), m.content) FROM messages m
                         WHERE m.msg_id = t.task_key LIMIT 1) AS source_text
                       ,(SELECT d.summary FROM tasks d WHERE d.id = t.duplicate_of) AS duplicate_summary
                FROM tasks t WHERE t.id = ?
                """,
                (int(task_id),),
            ).fetchone()
        return self._task_row(row) if row else None

    def set_task_urgent(self, task_id: int, value: bool | None) -> dict[str, Any]:
        """Set or clear the user's urgent override and return the updated task."""
        if value is not None and not isinstance(value, bool):
            raise ValueError("紧急覆盖值必须是 true、false 或 null")
        with self._connect() as connection:
            exists = connection.execute("SELECT 1 FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
            if exists is None:
                return {"ok": False, "error": "任务不存在"}
            connection.execute(
                "UPDATE tasks SET urgent_override=?, updated_at=? WHERE id=?",
                (None if value is None else int(value), iso(now_local()), int(task_id)),
            )
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
        result = self._task_row(row)
        result["ok"] = True
        return result

    def set_task_pinned(self, task_id: int, value: bool) -> dict[str, Any]:
        """置顶/取消置顶一条待办；已完成的任务不能置顶。"""
        if not isinstance(value, bool):
            raise ValueError("置顶值必须是 true 或 false")
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
            if row is None:
                return {"ok": False, "error": "任务不存在"}
            if value and str(row["status"]) == "done":
                return {"ok": False, "error": "已完成的任务不能置顶"}
            connection.execute(
                "UPDATE tasks SET pinned=?, updated_at=? WHERE id=?",
                (int(value), iso(now_local()), int(task_id)),
            )
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
        result = self._task_row(row)
        result["ok"] = True
        return result

    @staticmethod
    def _task_row(row: sqlite3.Row) -> dict[str, Any]:
        task = dict(row)
        try:
            groups = json.loads(task.get("groups") or "[]")
        except (json.JSONDecodeError, TypeError):
            groups = []
        task["groups"] = [str(name) for name in groups] if isinstance(groups, list) else []
        try:
            details = json.loads(task.get("details") or "[]")
        except (json.JSONDecodeError, TypeError):
            details = []
        task["details"] = [str(value) for value in details] if isinstance(details, list) else []
        task["condition"] = str(task.pop("condition_text", "") or "")
        task["done"] = str(task.get("status") or "") == "done"
        task["snooze_until"] = str(task.get("snooze_until") or "")
        task["duplicate_of"] = int(task.get("duplicate_of") or 0)
        task["pinned"] = bool(task.get("pinned"))
        return task

    def group_names(self) -> dict[str, str]:
        """群号 → 群名。消息记录里群名常常就等于群号（入库那一刻没取到名字），
        所以只挑每个群里真正带名字的那条最新记录；NapCat 群列表里报回来的名字存在
        group_names 表里，它更权威、也留得住（NapCat 不在线时界面照样显示群名）。"""
        with self._connect() as connection:
            # 先按群聚合出「最新那条带名字的消息」的时间，再 JOIN 回 messages。
            # 早先写成逐行相关子查询，消息量上去后单次要十几秒（3 万条约 15 秒），
            # /api/tasks 每次都调这里，前端 30 秒就掐断了。
            rows = connection.execute(
                """
                SELECT m.group_id AS group_id, m.group_name AS group_name
                FROM messages m
                JOIN (
                    SELECT group_id AS group_id, MAX(ts) AS ts
                    FROM messages
                    WHERE group_name <> '' AND group_name <> group_id
                    GROUP BY group_id
                ) latest
                  ON latest.group_id = m.group_id AND latest.ts = m.ts
                WHERE m.group_name <> '' AND m.group_name <> m.group_id
                GROUP BY m.group_id
                """
            ).fetchall()
            cached = connection.execute("SELECT group_id, name FROM group_names").fetchall()
        names = {str(row["group_id"]): str(row["group_name"]) for row in rows}
        names.update({str(row["group_id"]): str(row["name"]) for row in cached})
        return names

    def remember_group_names(self, names: Mapping[str, str]) -> int:
        """记下 NapCat 群列表报回来的群名（群号 → 群名），下次不用再问 NapCat。"""
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
                """
                INSERT INTO group_names (group_id, name, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET name = excluded.name, updated_at = excluded.updated_at
                """,
                [(key, value, stamp) for key, value in cleaned.items()],
            )
        return len(cleaned)

    @staticmethod
    def group_label(group_id: Any, names: dict[str, str], aliases: Any = None) -> str:
        """界面上要显示群名而不是群号：优先用户保存过的别名，其次消息里带回来的群名，最后才退回群号。"""
        key = str(group_id or "")
        if not key:
            return ""
        alias = str((aliases or {}).get(key) or "").strip()
        return alias or str(names.get(key) or "").strip() or key

    def list_open_tasks(self, *, limit: int = 800) -> list[dict[str, Any]]:
        return self.list_tasks(statuses=("open",), limit=limit)

    def list_task_events(self, task_id: int, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, task_id, event, detail, created_at
                FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT ?
                """,
                (int(task_id), max(1, int(limit)))
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _normalize_summary(text: str) -> str:
        return re.sub(r"[\s，。、；：！？,.!?;:（）()\[\]【】\"'“”‘’\-_—]+", "", str(text or "")).lower()

    @classmethod
    def _resolve_duplicate_target(
        cls,
        connection: sqlite3.Connection,
        *,
        task_id: int,
        summary: str,
        requested: Any,
    ) -> int:
        """找到“重复”指向的原始任务：优先用显式任务号，否则按摘要文本匹配。"""
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
        target = cls._normalize_summary(summary)
        if not target:
            return 0
        rows = connection.execute(
            """
            SELECT id, summary FROM tasks
            WHERE id <> ? AND status NOT IN ('dismissed', 'expired')
            ORDER BY id DESC LIMIT 200
            """,
            (int(task_id),),
        ).fetchall()
        for item in rows:
            if cls._normalize_summary(item["summary"]) == target:
                return int(item["id"])
        return 0

    @staticmethod
    def _merge_task_groups(connection: sqlite3.Connection, original_id: int, groups: Any) -> list[str]:
        """把重复任务的来源群并入原始任务，返回合并后的群列表。"""
        if isinstance(groups, str):
            try:
                groups = json.loads(groups or "[]")
            except (json.JSONDecodeError, TypeError):
                groups = []
        incoming = [str(name) for name in (groups or []) if str(name).strip()]
        if not incoming:
            return []
        row = connection.execute(
            "SELECT groups FROM tasks WHERE id = ?", (int(original_id),)
        ).fetchone()
        if row is None:
            return []
        try:
            existing = json.loads(row["groups"] or "[]")
        except (json.JSONDecodeError, TypeError):
            existing = []
        previous = [str(name) for name in existing if str(name).strip()] if isinstance(existing, list) else []
        merged = list(previous)
        for name in incoming:
            if name not in merged:
                merged.append(name)
        if merged != previous:
            connection.execute(
                "UPDATE tasks SET groups=?, updated_at=? WHERE id=?",
                (json.dumps(merged, ensure_ascii=False), iso(now_local()), int(original_id)),
            )
        return merged

    def apply_task_action(self, task_id: int, action: str, *, detail: Any = "") -> bool:
        """执行确认/忽略/完成/重开/稍后提醒，并追加事件记录。"""
        action = str(action or "").strip().lower()
        stamp = iso(now_local())
        with self._connect() as connection:
            row = connection.execute("SELECT status FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
            if row is None:
                return False
            current = str(row["status"] or "open")
            if action == "confirm":
                if current not in {"candidate", "open"}:
                    return False
                connection.execute(
                    "UPDATE tasks SET status='open', confirmed_at=?, updated_at=? WHERE id=?",
                    (stamp, stamp, int(task_id)),
                )
                self._task_event(connection, task_id, "confirmed", detail, created_at=stamp)
                return True
            if action == "dismiss":
                if current not in {"candidate", "open"}:
                    return False
                connection.execute(
                    "UPDATE tasks SET status='dismissed', dismissed_at=?, updated_at=? WHERE id=?",
                    (stamp, stamp, int(task_id)),
                )
                self._task_event(connection, task_id, "dismissed", detail, created_at=stamp)
                return True
            if action == "done":
                if current not in {"open", "candidate"}:
                    return False
                connection.execute(
                    "UPDATE tasks SET status='done', done_at=?, updated_at=? WHERE id=?",
                    (stamp, stamp, int(task_id)),
                )
                self._task_event(connection, task_id, "done", detail, created_at=stamp)
                return True
            if action == "snooze":
                if current not in {"candidate", "open"}:
                    return False
                payload = dict(detail) if isinstance(detail, dict) else {}
                until = parse_iso(payload.get("until")) if payload.get("until") else None
                if not isinstance(until, dt.datetime):
                    until = now_local() + dt.timedelta(days=1)
                payload["until"] = iso(until)
                connection.execute(
                    "UPDATE tasks SET last_reminded_at=?, snooze_until=?, updated_at=? WHERE id=?",
                    (stamp, iso(until), stamp, int(task_id)),
                )
                self._task_event(connection, task_id, "snoozed", payload, created_at=stamp)
                return True
            if action == "reopen":
                if current not in {"done", "dismissed", "expired"}:
                    return False
                connection.execute(
                    "UPDATE tasks SET status='open', done_at='', confirmed_at=?, updated_at=? WHERE id=?",
                    (stamp, stamp, int(task_id)),
                )
                self._task_event(connection, task_id, "reopened", detail, created_at=stamp)
                return True
            if action == "delete":
                # 只删「已结束」的任务：open/candidate 没有「删掉但还欠着」这条路，让用户走忽略。
                # 真删、不写事件——删掉的东西不该变成一条 dismissed，把忽略率推高。
                if current not in {"done", "dismissed", "expired"}:
                    return False
                connection.execute("DELETE FROM task_events WHERE task_id = ?", (int(task_id),))
                connection.execute("DELETE FROM tasks WHERE id = ?", (int(task_id),))
                return True
            raise ValueError(f"不支持的任务动作: {action}")

    def set_task_status(self, task_id: int, done: bool) -> bool:
        """兼容旧调用：true=完成，false=重新打开。"""
        return self.apply_task_action(task_id, "done" if done else "reopen")

    def task_stats(self) -> dict[str, int]:
        now = iso(now_local())
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(status = 'candidate'), 0) AS candidate,
                       COALESCE(SUM(status = 'open'), 0) AS open,
                       COALESCE(SUM(status = 'done'), 0) AS done,
                       COALESCE(SUM(status = 'dismissed'), 0) AS dismissed,
                       COALESCE(SUM(status = 'expired'), 0) AS expired,
                       COALESCE(SUM(status = 'open' AND deadline <> '' AND deadline < ?), 0) AS overdue
                FROM tasks
                """,
                (now,),
            ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "candidate": int(row["candidate"] or 0),
            "open": int(row["open"] or 0),
            "done": int(row["done"] or 0),
            "dismissed": int(row["dismissed"] or 0),
            "expired": int(row["expired"] or 0),
            "overdue": int(row["overdue"] or 0),
        }

    def mark_task_reminded(self, task_id: int, *, detail: Any = "", when: Any = None) -> bool:
        stamp = iso(when or now_local())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks SET last_reminded_at=?, remind_count=remind_count+1, updated_at=?
                WHERE id=? AND status IN ('open', 'candidate')
                """,
                (stamp, stamp, int(task_id)),
            )
            changed = bool(cursor.rowcount)
            if changed:
                self._task_event(connection, task_id, "reminded", detail, created_at=stamp)
        return changed

    def task_metrics(self, *, start: str, end: str) -> dict[str, int | float]:
        with self._connect() as connection:
            def scalar(sql: str, params: tuple[Any, ...] = ()) -> int:
                row = connection.execute(sql, params).fetchone()
                return int(row[0] or 0)

            candidates = scalar(
                "SELECT COUNT(*) FROM task_events WHERE event='candidate_detected' AND created_at>=? AND created_at<?",
                (start, end),
            )
            confirmed = scalar(
                "SELECT COUNT(*) FROM task_events WHERE event='confirmed' AND created_at>=? AND created_at<?",
                (start, end),
            )
            dismissed = scalar(
                "SELECT COUNT(*) FROM task_events WHERE event='dismissed' AND created_at>=? AND created_at<?",
                (start, end),
            )
            completions = scalar(
                "SELECT COUNT(*) FROM task_events WHERE event='done' AND created_at>=? AND created_at<?",
                (start, end),
            )
            reminders = scalar(
                "SELECT COUNT(*) FROM task_events WHERE event='reminded' AND created_at>=? AND created_at<?",
                (start, end),
            )
            completions_after_reminder = scalar(
                """
                SELECT COUNT(DISTINCT d.task_id)
                FROM task_events d
                WHERE d.event='done' AND d.created_at>=? AND d.created_at<?
                  AND EXISTS (
                      SELECT 1 FROM task_events r
                      WHERE r.task_id=d.task_id AND r.event='reminded' AND r.created_at<=d.created_at
                  )
                """,
                (start, end),
            )
            overdue = scalar(
                "SELECT COUNT(*) FROM tasks WHERE status='open' AND deadline<>'' AND deadline<?",
                (end,),
            )
        return {
            "candidates": candidates,
            "confirmed": confirmed,
            "dismissed": dismissed,
            "completions": completions,
            "reminders": reminders,
            "completions_after_reminder": completions_after_reminder,
            "overdue": overdue,
            "confirmation_rate": round(confirmed * 100 / candidates, 1) if candidates else 0.0,
            "dismissal_rate": round(dismissed * 100 / candidates, 1) if candidates else 0.0,
            "completion_rate": round(completions * 100 / max(1, completions + overdue), 1),
        }

    def delivery_stats(self) -> dict[str, int]:
        """投递队列概况，用于 /health 观测推送是否堆积。"""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS n FROM deliveries GROUP BY status"
            ).fetchall()
        data = {str(row["status"]): int(row["n"] or 0) for row in rows}
        return {
            "sent": data.get("sent", 0),
            "pending": data.get("pending", 0),
            "failed": data.get("failed", 0),
            "total": sum(data.values()),
        }

    def correction_stats(self, *, days: int = 30) -> dict[str, Any]:
        """按群和纠错类型汇总人工纠错，供规则反馈使用。"""
        since = iso(now_local() - dt.timedelta(days=max(1, int(days))))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT e.detail AS detail, t.groups AS groups
                FROM task_events e JOIN tasks t ON t.id = e.task_id
                WHERE e.event = 'corrected' AND e.created_at >= ?
                ORDER BY e.id DESC
                """,
                (since,),
            ).fetchall()
        total = 0
        by_type: dict[str, int] = {}
        by_group: dict[str, dict[str, int]] = {}
        for row in rows:
            try:
                detail = json.loads(row["detail"] or "{}")
            except (json.JSONDecodeError, TypeError):
                detail = {}
            kind = str(detail.get("type") or "unknown")
            total += 1
            by_type[kind] = by_type.get(kind, 0) + 1
            try:
                groups = json.loads(row["groups"] or "[]")
            except (json.JSONDecodeError, TypeError):
                groups = []
            for name in groups if isinstance(groups, list) else []:
                entry = by_group.setdefault(str(name), {})
                entry[kind] = entry.get(kind, 0) + 1
                entry["total"] = entry.get("total", 0) + 1
        return {"days": int(days), "total": total, "by_type": by_type, "by_group": by_group}

    def correction_insights(self, *, days: int = 30, min_group_hits: int = 3) -> list[str]:
        """把纠错样本转成可读的规则建议；只提建议，不自动改配置。"""
        stats = self.correction_stats(days=days)
        threshold = max(2, int(min_group_hits))
        insights: list[str] = []
        ranked = sorted(
            (stats.get("by_group") or {}).items(),
            key=lambda item: -int(item[1].get("total") or 0),
        )
        for name, entry in ranked:
            total = int(entry.get("total") or 0)
            if total < threshold:
                continue
            urgent_miss = int(entry.get("not_urgent") or 0)
            noise = int(entry.get("not_notice") or 0) + int(entry.get("not_task") or 0)
            duplicate = int(entry.get("duplicate") or 0)
            reasons: list[str] = []
            if urgent_miss:
                reasons.append(f"误判紧急 {urgent_miss} 次")
            if noise:
                reasons.append(f"误判为通知/待办 {noise} 次")
            if duplicate:
                reasons.append(f"重复 {duplicate} 次")
            if reasons:
                insights.append(
                    f"{name}：{'、'.join(reasons)}，建议收紧该群规则或加入低优先级群。"
                )
        if int((stats.get("by_type") or {}).get("duplicate") or 0) >= 3:
            insights.append("重复标记偏多，建议复查跨群去重时间窗 QQ_DIGEST_DEDUPE_HOURS。")
        return insights

    def clear_tasks(self) -> int:
        with self._connect() as connection:
            connection.execute("DELETE FROM task_events")
            cursor = connection.execute("DELETE FROM tasks")
            removed = cursor.rowcount
        return int(removed)

    def clear_finished_tasks(self) -> int:
        """只清「已完成/已忽略/已过期」：连它们的 task_events 一起删，open/candidate 一律不动。

        与 ``clear_tasks()``（清库）区分开：这是界面上「清空已完成」的落点，返回删掉的任务条数。
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM tasks WHERE status IN ('done', 'dismissed', 'expired')"
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if not ids:
                return 0
            placeholders = ",".join("?" * len(ids))
            connection.execute(f"DELETE FROM task_events WHERE task_id IN ({placeholders})", ids)
            connection.execute(f"DELETE FROM tasks WHERE id IN ({placeholders})", ids)
        return len(ids)

    def meta_get(self, key: str, default: str = "") -> str:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def meta_set(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    # ------------------------------------------------------------------- 维护
    def prune(self, *, retention_days: int | None = None) -> int:
        days = int(retention_days or self.retention_days)
        cutoff = iso(now_local() - dt.timedelta(days=days))
        with self._connect() as connection:
            # 先删子表（processed 外键引用 messages），再删父表
            connection.execute(
                "DELETE FROM processed WHERE msg_id IN (SELECT msg_id FROM messages WHERE received_at < ?)",
                (cutoff,),
            )
            cursor = connection.execute("DELETE FROM messages WHERE received_at < ?", (cutoff,))
            removed = cursor.rowcount or 0
            connection.execute("DELETE FROM processed WHERE msg_id NOT IN (SELECT msg_id FROM messages)")
        return removed

    def counts(self) -> dict[str, int]:
        with self._connect() as connection:
            def scalar(sql: str) -> int:
                row = connection.execute(sql).fetchone()
                return int(row[0] or 0)

            return {
                "messages": scalar("SELECT COUNT(*) FROM messages"),
                "unprocessed": scalar(
                    "SELECT COUNT(*) FROM messages m LEFT JOIN processed p ON p.msg_id=m.msg_id WHERE p.msg_id IS NULL"
                ),
                "digests": scalar("SELECT COUNT(*) FROM digests"),
                "deliveries_sent": scalar("SELECT COUNT(*) FROM deliveries WHERE status='sent'"),
                "deliveries_failed": scalar("SELECT COUNT(*) FROM deliveries WHERE status='failed'"),
            }

    def apply_task_correction(self, task_id: int, correction: str, *, value: Any = "") -> bool:
        """记录用户纠错，并按纠错类型修正任务状态或字段。"""
        correction = str(correction or "").strip().lower()
        allowed = {"not_notice", "not_task", "not_urgent", "duplicate", "category", "deadline", "clear_deadline"}
        if correction not in allowed:
            raise ValueError(f"不支持的纠错类型: {correction}")
        stamp = iso(now_local())
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status, category, importance, deadline, summary, groups FROM tasks WHERE id = ?",
                (int(task_id),),
            ).fetchone()
            if row is None:
                return False
            previous_status = str(row["status"] or "open")
            previous_category = str(row["category"] or "info")
            previous_importance = int(row["importance"] or 3)
            previous_deadline = str(row["deadline"] or "")
            new_status = previous_status
            new_category = previous_category
            new_importance = previous_importance
            new_deadline = previous_deadline
            duplicate_of = 0
            if correction in {"not_notice", "not_task"}:
                new_status = "dismissed"
            elif correction == "duplicate":
                new_status = "dismissed"
                duplicate_of = self._resolve_duplicate_target(
                    connection,
                    task_id=int(task_id),
                    summary=str(row["summary"] or ""),
                    requested=value,
                )
            elif correction == "not_urgent":
                new_category = "action" if previous_category == "urgent" else previous_category
                new_importance = min(previous_importance, 3)
            elif correction == "category":
                new_category = str(value or "").strip().lower()
                if new_category not in {"urgent", "action", "academic", "info"}:
                    raise ValueError("无效的任务分类")
                new_importance = IMPORTANCE_BY_CATEGORY[new_category]
            elif correction == "deadline":
                parsed_deadline = iso(value)
                if str(value or "").strip() and not parsed_deadline:
                    raise ValueError("无效的截止时间")
                new_deadline = parsed_deadline
            elif correction == "clear_deadline":
                new_deadline = ""
            detail = {
                "type": correction,
                "previous": {
                    "status": previous_status,
                    "category": previous_category,
                    "importance": previous_importance,
                    "deadline": previous_deadline,
                },
                "new": {
                    "status": new_status,
                    "category": new_category,
                    "importance": new_importance,
                    "deadline": new_deadline,
                    "duplicate_of": duplicate_of,
                },
                "value": str(value or "")[:500],
            }
            dismissed_at = stamp if new_status == "dismissed" and previous_status != "dismissed" else ""
            connection.execute(
                """
                UPDATE tasks SET
                    status=?, category=?, importance=?, deadline=?, duplicate_of=?,
                    dismissed_at=CASE WHEN ? <> '' THEN ? ELSE dismissed_at END,
                    updated_at=?
                WHERE id=?
                """,
                (
                    new_status,
                    new_category,
                    new_importance,
                    new_deadline,
                    int(duplicate_of),
                    dismissed_at,
                    dismissed_at,
                    stamp,
                    int(task_id),
                ),
            )
            if duplicate_of:
                merged_groups = self._merge_task_groups(connection, duplicate_of, row["groups"])
                if merged_groups:
                    detail["merged_groups"] = merged_groups
            self._task_event(connection, task_id, "corrected", detail, created_at=stamp)
            return True
