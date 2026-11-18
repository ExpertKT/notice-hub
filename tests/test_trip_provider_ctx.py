"""锁 trip.js providerCtx(id) 的取值契约。

为什么要有：providerCtx 曾写死 ``key: ''``，导致「配了 key 也判得出 ready、真去连
却拿不到 key」，高德/百度永远弹「还没配 key，先看自绘或 OSM」。这里不复制逻辑，
直接把真的 trip.js 喂给 node，用探针供应商走真的 boot → loadModeConf → choose →
openOnline → makeMap → providerCtx → init 这条链，把 init 收到的 ctx 抓出来比对。

两个场景（各一个独立 node 子进程，互不串味）：
- with-key    配了 key  ⇒ init 被调，ctx.key === conf.amap.key，ctx.secret 同理
- without-key 没配 key  ⇒ init 不被调（choose 的 ready 闸拦下），不拿空 key 去连

node 不在 PATH 时 skip（跟 tests/test_web_static_assets.py 同一取舍），免得没装
Node 的机器全量门禁变红。探针脚本 tests/js_provider_ctx_probe.js 自己 exit 1 on FAIL。
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
TRIP_JS = ROOT_DIR / "noticehub" / "web_static" / "js" / "trip.js"
PROBE_JS = Path(__file__).resolve().parent / "js_provider_ctx_probe.js"


def _run_probe(scenario: str) -> subprocess.CompletedProcess:
    node = shutil.which("node")
    assert node, "调用方应先确认 node 在 PATH"
    return subprocess.run(
        [node, str(PROBE_JS), str(TRIP_JS), scenario],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )


class ProviderCtxProbeTests(unittest.TestCase):
    def test_configured_key_reaches_provider_init(self) -> None:
        """配了 key：ctx.key 必须等于 conf[id].key（secret 同理）。"""
        if not shutil.which("node"):
            self.skipTest("node 不在 PATH，跳过 JS providerCtx 探针")
        result = _run_probe("with-key")
        self.assertEqual(
            result.returncode, 0,
            f"探针没过：stdout={result.stdout!r} stderr={result.stderr!r}",
        )
        self.assertIn("PASS test_provider_ctx_passes_configured_key", result.stdout)

    def test_missing_key_never_inits_provider(self) -> None:
        """没配 key：ready 闸拦下，init 不被调——不会拿空 key 去连。"""
        if not shutil.which("node"):
            self.skipTest("node 不在 PATH，跳过 JS providerCtx 探针")
        result = _run_probe("without-key")
        self.assertEqual(
            result.returncode, 0,
            f"探针没过：stdout={result.stdout!r} stderr={result.stderr!r}",
        )
        self.assertIn("PASS test_provider_ctx_without_key_never_inits", result.stdout)


if __name__ == "__main__":
    unittest.main()
