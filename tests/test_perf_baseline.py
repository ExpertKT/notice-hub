"""性能基线（回归护栏，不是性能指标）。

目的：某条关键路径突然慢 10 倍时让单测红掉，不做精确计时。
阈值 = 本机实测 × 5 再取整，留足 CI 抖动空间；跑挂了先重跑一次再怀疑代码。
"""
# ponytail: 阈值是护栏值，非 SLA；如 CI 抖动导致误报，改为同函数两次调用的相对比值
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

from noticehub.config import Settings  # noqa: E402
from noticehub.store import Store  # noqa: E402
from noticehub import webapp as webapp_module  # noqa: E402
from noticehub.webapp import TaskWebServer, _lan_hosts  # noqa: E402

SEED_COUNT = 1000
TOKEN = "perf"


def _best_of_two(func):
    """同一函数连测两次取较小值，压掉冷启动/抖动单点毛刺。"""
    times = []
    for _ in range(2):
        start = time.perf_counter()
        func()
        times.append(time.perf_counter() - start)
    return min(times)


class PerfBaselineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = Store(Path(cls.tmp.name) / "perf.sqlite3")
        for index in range(SEED_COUNT):
            cls.store.upsert_task(
                task_key=f"perf-{index:05d}",
                summary=f"性能基线任务 {index}",
                category="info",
                groups=["perf群"],
                details=("明细一", "明细二"),
            )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_list_tasks_1000_items_default_limit(self) -> None:
        tasks = self.store.list_tasks()
        self.assertEqual(len(tasks), 800)  # 默认截断 800 条
        elapsed = _best_of_two(lambda: self.store.list_tasks())
        # 本机实测 1000 行插入后默认查询 ~0.02s
        self.assertLess(elapsed, 1.0, f"list_tasks 默认查询 {elapsed:.3f}s，超过基线")

    def test_list_tasks_1000_items_limit_none(self) -> None:
        tasks = self.store.list_tasks(limit=None)
        self.assertEqual(len(tasks), SEED_COUNT)
        elapsed = _best_of_two(lambda: self.store.list_tasks(limit=None))
        # 本机实测全量查询 ~0.03s
        self.assertLess(elapsed, 1.5, f"list_tasks 全量 {elapsed:.3f}s，超过基线")

    def test_lan_hosts_first_call_not_stuck(self) -> None:
        # 清空适配器缓存，模拟 /api/pair/info 首次冷启动
        webapp_module._adapter_cache = None
        webapp_module._adapter_thread = None
        webapp_module._adapter_waited = False
        start = time.perf_counter()
        hosts = _lan_hosts()
        cold = time.perf_counter() - start
        self.assertIsInstance(hosts, list)
        # 首调上限：实现里首调最多等 ADAPTER_FIRST_WAIT(0.4s)+缓存读，留 10s 余量
        self.assertLess(cold, 10.0, f"_lan_hosts 首调 {cold:.2f}s，疑似被网卡枚举卡住")
        warm = _best_of_two(_lan_hosts)
        self.assertLess(warm, 1.0, f"_lan_hosts 热调 {warm:.2f}s，缓存没生效")

    def test_api_tasks_with_1000_tasks(self) -> None:
        settings = Settings(
            web_host="127.0.0.1",
            web_port=0,
            web_token=TOKEN,
            data_dir=Path(self.tmp.name) / "data",
        )
        server = TaskWebServer(settings, self.store)
        self.assertTrue(server.start())
        self.addCleanup(server.stop)
        base = f"http://127.0.0.1:{server.server.server_address[1]}"
        request = urllib.request.Request(base + "/api/tasks", headers={"X-Token": TOKEN})

        def fetch() -> int:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read())
            # /api/tasks 返回分组负载：today/week/later/done/candidates
            return sum(len(payload.get(key) or []) for key in ("today", "week", "later", "done", "candidates"))

        count = fetch()
        self.assertGreater(count, 0, "API 返回任务数为 0")
        elapsed = _best_of_two(fetch)
        # 本机实测 800 条经 HTTP 序列化 ~0.05s
        self.assertLess(elapsed, 3.0, f"/api/tasks 一次请求 {elapsed:.2f}s，超过基线")


if __name__ == "__main__":
    unittest.main()
