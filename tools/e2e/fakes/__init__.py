"""W3 平台替身统一入口。

剧本（后端 harness）只认这一个接口：

    from tools.e2e.fakes import start
    onebot = start("onebot", token="e2e", target_url="http://127.0.0.1:8765/onebot/event",
                   target_token="e2e")
    onebot.port            # HTTP 替身是随机端口；目录替身是 None
    onebot.env             # 要合并进被测实例 .env 的键值
    onebot.info            # 断言用的元数据（状态、组、消息条数…）
    onebot.stop()          # 优雅停：关端口 / 关独占句柄 / 删临时目录

清单
----
* `onebot`   —— OneBot v11 HTTP 服务 + `/__inject` 事件注入（`fakes/onebot.py`）
* `wechat`   —— 假微信 4.x 库（真 SQLCipher v4 页格式，`fakes/wechat_store.py`）
* `dingtalk` —— 假钉钉本地库（AES-128-ECB 整页加密，`fakes/dingtalk_store.py`）
* `push`     —— 推送收包端，落盘 `requests.json`（`fakes/push_sink.py`）
* `chaoxing` —— 学习通接口三态（`fakes/chaoxing.py`，**接线受限**，见该文件头）

⚠️ 运行产物绝不进仓库
--------------------
替身生成的目录/日志/收包记录一律落在 `%TEMP%\\nh-e2e-<run-id>\\<kind>`
（`fakes/_rundir.py`；绝不回落仓库根）。沙箱里 python 写不了 %TEMP% 时，
自测请用显式 `root=` 落到仓库内临时目录，并在**同一轮**删掉；
每轮收尾确认 `git status --porcelain --untracked-files=all` 没有
`nh-e2e-*` / `nh-fake-*` / `tmp*` 残留。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 单独以 akes 包导入时（harness 就是这么导的），仓库根不在 sys.path 上，
# 这里补一次，供 	ools.wechat_probe 这类仓库内模块使用。
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dataclasses import dataclass, field
from typing import Any

__all__ = ["Handle", "start", "available"]


@dataclass
class Handle:
    name: str
    kind: str
    port: int | None
    base_url: str | None
    env: dict[str, str] = field(default_factory=dict)
    info: dict[str, Any] = field(default_factory=dict)
    impl: Any = None
    _stop: Any = None

    def stop(self) -> None:
        if self._stop is not None:
            self._stop()


def available() -> dict[str, bool]:
    """给剧本做能力探测。"""
    return {
        "onebot": True,
        "wechat": True,
        "dingtalk": True,
        "push": True,
        "chaoxing": True,
    }


def start(name: str, **opts: Any) -> Handle:
    """按名字起一个替身。未知名字直接报错，别静默降级成「什么都不做」。"""
    key = str(name or "").strip().lower()
    if key == "onebot":
        from . import onebot

        raw = onebot.start(**opts)
    elif key == "wechat":
        from . import wechat_store

        raw = wechat_store.start(**opts)
    elif key == "dingtalk":
        from . import dingtalk_store

        raw = dingtalk_store.start(**opts)
    elif key == "push":
        from . import push_sink

        raw = push_sink.start(**opts)
    elif key == "chaoxing":
        from . import chaoxing

        raw = chaoxing.start(**opts)
    else:
        raise KeyError("未知替身 %r；可用：%s" % (name, ", ".join(k for k, v in available().items() if v)))
    return Handle(
        name=raw["name"],
        kind=raw["kind"],
        port=raw.get("port"),
        base_url=raw.get("base_url"),
        env=dict(raw.get("env") or {}),
        info=dict(raw.get("info") or {}),
        impl=raw.get("impl"),
        _stop=raw.get("stop"),
    )
