"""`/api/tasks` 延迟回归：群名解析不能把一次读请求拖到前端超时。

现场（用户看到「待办读取失败：请求超时」）：
- 前端两个请求封装默认 30 秒超时（`web_static/page.html` 里 `setupApi` / `api`），
  超时后抛「请求超时，请重试」，页面拼成「待办读取失败：请求超时」。
- 后端 `/api/tasks` 每次都调 `Store.group_names()`。它原来用逐行相关子查询取
  「每个群最新那条带名字的消息」，消息量一大就爆：
  3 万条消息单次约 15 秒、`/api/tasks` 中位 12 秒；6 万条时旧 SQL 单次 47 秒，
  已经超过前端 30 秒阈值。改成「先按群聚合再 JOIN」后，6 万条约 0.19 秒。

这里用一个临时库（不碰真实 data/）造 6 万条消息，走真实 HTTP 打一次
`/api/tasks`，要求它在远小于 30 秒的预算内返回，且群名仍然解析正确。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest.config import Settings  # noqa: E402
from qq_live_digest.store import Store  # noqa: E402
from qq_live_digest.timeutil import iso, now_local  # noqa: E402
from qq_live_digest.webapp import TaskWebServer  # noqa: E402

MESSAGES = 60000
GROUPS = 60
# 前端超时是 30 秒；这里留 10 倍余量，修好后实测 0.2~0.3 秒，旧实现是 47 秒。
BUDGET_MS = 3000


class TasksLatencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "latency.sqlite3")
        self.settings = Settings(
            group_whitelist=("g0",),
            web_host="127.0.0.1",
            web_port=0,
            web_token="secret",
            data_dir=root / "data",
        )
        self.server = TaskWebServer(self.settings, self.store)
        self.assertTrue(self.server.start())
        self.addCleanup(self.server.stop)
        self.base = f"http://127.0.0.1:{self.server.server.server_address[1]}"

    def _seed_messages(self) -> None:
        """每个群上千条消息，一半只有群号、一半带真名；最后一条按设计只有群号，
        所以「最新那条带名字的消息」和「最后一条消息」不是同一条。"""
        stamp = iso(now_local())
        rows = []
        for index in range(MESSAGES):
            group_id = f"g{index % GROUPS}"
            # 同一群内按轮次推进：偶数轮带名字、奇数轮只有群号，ts 随 index 递增。
            named = (index // GROUPS) % 2 == 0
            rows.append(
                (
                    f"m{index}",
                    "qqbot",
                    group_id,
                    f"群{group_id}" if named else group_id,
                    f"2026-10-01 00:00:00.{index:09d}",
                    stamp,
                    f"通知内容 {index}",
                )
            )
        with self.store._connect() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO messages"
                " (msg_id, source, group_id, group_name, ts, received_at, content)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def _seed_task(self) -> int:
        stamp = iso(now_local())
        with self.store._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO tasks (task_key, summary, deadline, groups, status,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, 'open', ?, ?)",
                ("m0", "交实验报告", "2026-12-31 23:59:00", json.dumps(["g0"]), stamp, stamp),
            )
            return int(cursor.lastrowid)

    def _get_tasks(self) -> tuple[dict, float]:
        request = urllib.request.Request(
            self.base + "/api/tasks", headers={"X-Token": self.server.token}
        )
        start = time.perf_counter()
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload, (time.perf_counter() - start) * 1000

    def test_api_tasks_returns_within_budget_on_large_message_history(self) -> None:
        self._seed_messages()
        self._seed_task()

        payload, elapsed = self._get_tasks()

        self.assertLess(
            elapsed,
            BUDGET_MS,
            f"/api/tasks 花了 {elapsed:.0f} ms，超过 {BUDGET_MS} ms 预算"
            "（旧实现逐行相关子查询约 47 秒，前端 30 秒就掐断）",
        )
        # 群名仍然按「最新那条带名字的消息」解析，不能退化成群号。
        tasks = payload.get("today", []) + payload.get("week", []) + payload.get("later", [])
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["groups"], ["群g0"])

    def test_group_names_query_is_not_per_row(self) -> None:
        """直接把慢查询的面积钉在 store 层：6 万条消息一次不超过 2 秒。"""
        self._seed_messages()
        start = time.perf_counter()
        names = self.store.group_names()
        elapsed = (time.perf_counter() - start) * 1000
        self.assertLess(elapsed, 2000, f"group_names 花了 {elapsed:.0f} ms")
        self.assertEqual(names.get("g0"), "群g0")
        self.assertEqual(len(names), GROUPS)


if __name__ == "__main__":
    unittest.main()
