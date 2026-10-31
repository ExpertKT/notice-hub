"""数据库自愈与快照：坏库隔离重建、结构版本校验、每日快照保留份数、并发 upsert。"""

from __future__ import annotations

import contextlib
import datetime as dt
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from noticehub.config import Settings  # noqa: E402
from noticehub.service import DigestService  # noqa: E402
from noticehub.store import SCHEMA_VERSION, Store  # noqa: E402


class StoreRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "digest.sqlite3"

    def test_corrupt_database_is_quarantined_and_rebuilt(self) -> None:
        self.path.write_bytes(b"\x00\xff not a sqlite database " * 200)

        with self.assertLogs("noticehub.store", level="ERROR") as logs:
            store = Store(self.path)

        self.assertTrue(self.path.is_file(), "隔离后必须建出新库")
        quarantined = list(self.dir.glob("digest.sqlite3.corrupt-*"))
        self.assertEqual(len(quarantined), 1, [str(item) for item in self.dir.iterdir()])
        self.assertRegex(quarantined[0].name, r"\.corrupt-\d{8}-\d{6}$")
        self.assertEqual(store.meta_get("recovered_from"), quarantined[0].name)
        self.assertIn("已隔离", "\n".join(logs.output))
        store.insert_message(msg_id="m1", group_id="g1", content="新库可用")
        self.assertEqual(store.counts()["messages"], 1)

    def test_user_version_is_written_to_database(self) -> None:
        Store(self.path)
        with contextlib.closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION
            )

    def test_newer_database_refuses_to_start(self) -> None:
        Store(self.path)
        with contextlib.closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA user_version=%d" % (SCHEMA_VERSION + 1))

        with self.assertRaises(RuntimeError) as caught:
            Store(self.path)
        self.assertIn("高于", str(caught.exception))

    def test_concurrent_upsert_of_same_key_keeps_one_row(self) -> None:
        store = Store(self.path)
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def worker() -> None:
            try:
                barrier.wait(timeout=10)
                for _ in range(100):
                    store.upsert_task(task_key="same-key", summary="并发任务")
            except BaseException as error:  # noqa: BLE001 —— 线程里的异常要带回主线程断言
                errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(errors, [])
        with contextlib.closing(sqlite3.connect(self.path)) as connection:
            rows = connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE task_key = 'same-key'"
            ).fetchone()[0]
            created = connection.execute(
                "SELECT COUNT(*) FROM task_events WHERE event = 'created'"
            ).fetchone()[0]
        self.assertEqual(rows, 1)
        self.assertEqual(created, 1, "同一 key 只该留一条 created 事件")

    def test_upsert_merges_without_clobbering_user_state(self) -> None:
        """upsert 护栏：以后改 SET 列之前先看这里，别把下面任何一条改坏。

        同 key 二次 upsert 必须：① 返回同一个 id；② status 这类用户处理状态不被覆盖
        （done 不会被改回 open）；③ importance / confidence 取 MAX，不随新值变小；
        ④ 空 deadline / evidence / source 不清空已有值；⑤ created 事件只 1 条。
        """
        store = Store(self.path)
        first = store.upsert_task(
            task_key="guard",
            summary="旧",
            importance=2,
            confidence=0.4,
            deadline="2026-11-01",
            evidence="截图",
            source="qqbot",
        )
        second = store.upsert_task(
            task_key="guard",
            summary="新",
            importance=1,
            confidence=0.2,
            deadline="",
            evidence="",
            source="",
        )
        self.assertEqual(first, second)
        store.set_task_status(first, True)
        store.upsert_task(task_key="guard", summary="再来", importance=5, confidence=0.1)

        task = store.get_task(first)
        assert task is not None
        self.assertEqual(task["summary"], "再来")
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["importance"], 5)
        self.assertEqual(float(task["confidence"]), 0.4)
        self.assertEqual(task["deadline"], "2026-11-01")
        self.assertEqual(task["evidence"], "截图")
        self.assertEqual(task["source"], "qqbot")
        created = [
            event for event in store.list_task_events(first) if event["event"] == "created"
        ]
        self.assertEqual(len(created), 1)


class SnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.data_dir = root / "data"
        self.settings = Settings(
            group_whitelist=("g1",),
            data_dir=self.data_dir,
            log_dir=root / "logs",
        )
        self.store = Store(self.data_dir / "digest.sqlite3")
        self.service = DigestService(self.settings, store=self.store)

    def test_daily_snapshot_is_written_once_per_day(self) -> None:
        day = dt.datetime(2026, 10, 28, 9, 0)

        self.service._maybe_snapshot(day)
        target = self.data_dir / "snapshots" / "digest-20261028.sqlite3"
        self.assertTrue(target.is_file())
        with contextlib.closing(sqlite3.connect(target)) as connection:
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )

        self.service._maybe_snapshot(day)
        snapshots = list((self.data_dir / "snapshots").glob("digest-*.sqlite3"))
        self.assertEqual(len(snapshots), 1, "同一天只做一次")

    def test_snapshots_keep_only_seven(self) -> None:
        directory = self.data_dir / "snapshots"
        directory.mkdir(parents=True)
        for index in range(1, 10):
            (directory / ("digest-202610%02d.sqlite3" % index)).write_bytes(b"x")

        self.service._maybe_snapshot(dt.datetime(2026, 10, 28, 9, 0))

        names = sorted(path.name for path in directory.glob("digest-*.sqlite3"))
        self.assertEqual(len(names), 7)
        self.assertEqual(names[-1], "digest-20261028.sqlite3")
        self.assertNotIn("digest-20261001.sqlite3", names, "最旧的要先删")


if __name__ == "__main__":
    unittest.main()
