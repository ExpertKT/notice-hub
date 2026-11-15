"""实验室功能的统一门面。

路由层只依赖本模块：惰性 import 真实模块，任何失败都收成
``{"ok": False, "error": ...}``，不抛栈。
纯函数、无 I/O、无全局状态；真实功能一律输出**候选**，不直接写库。
1.1.0 起检索与抽日程常开，只剩自动整理规则一个开关（Settings.lab_rules）。
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
from typing import Any

LOGGER = logging.getLogger(__name__)

__all__ = [
    "available",
    "call",
    "extract_events",
    "search",
    "list_rules",
    "save_rules",
    "test_rules",
]

# 功能名 -> 模块全名。三个模块都已定稿落地（rules.py 自 beta.13 起接线，
# load_rules/save_rules 负责 data/rules.json 的读写）。
_LAB_MODULES: dict[str, str] = {
    "search": "noticehub.web.search",
    "events": "noticehub.ingest.event_extract",
    "rules": "noticehub.ingest.rules",
}


def _module_ready(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False


def available(settings: Any) -> dict[str, bool]:
    """各功能当前是否可用：只看模块有没有落地。"""
    return {name: _module_ready(module_name) for name, module_name in _LAB_MODULES.items()}


def _run(settings: Any, name: str, func_name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    if name not in _LAB_MODULES:
        return {"ok": False, "error": "未知实验功能"}
    # 只有自动整理规则还留着开关；其余三项常开。
    if name == "rules" and not settings.lab_rules:
        return {"ok": False, "error": "自动整理规则已关闭"}
    module_name = _LAB_MODULES[name]
    if not _module_ready(module_name):
        return {"ok": False, "error": "该实验功能尚未就绪"}
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - 模块没落地就是「尚未就绪」
        return {"ok": False, "error": f"该实验功能尚未就绪：{type(exc).__name__}: {exc}"}
    func = getattr(module, func_name, None)
    if not callable(func):
        return {"ok": False, "error": f"该实验功能尚未就绪：模块里没有 {func_name}"}
    try:
        return {"ok": True, "result": func(*args, **kwargs)}
    except Exception as exc:  # noqa: BLE001 - 门面兜底，任何底层异常都不外抛
        # 模块在、函数在，失败就是这次调用真出错：原样带上异常类型，别把真实原因
        # 抹成「尚未就绪」，否则用户和日志都查不出到底哪一步坏了。
        LOGGER.exception("实验功能 %s 执行失败", name)
        return {"ok": False, "error": f"实验功能 {name} 执行失败：{type(exc).__name__}: {exc}"}


def search(settings: Any, db_path: Any, query: str, **filters: Any) -> dict[str, Any]:
    """全文检索跑在消息库上：库路径由调用方给，门面不碰存储。"""
    return _run(settings, "search", "search", db_path, query, **filters)


def extract_events(settings: Any, text: str, **kwargs: Any) -> dict[str, Any]:
    return _run(settings, "events", "extract_events", text, **kwargs)


def list_rules(settings: Any) -> dict[str, Any]:
    return _run(settings, "rules", "load_rules", settings)


def save_rules(settings: Any, rules: Any) -> dict[str, Any]:
    return _run(settings, "rules", "save_rules", settings, rules)


def test_rules(settings: Any, message: Any) -> dict[str, Any]:
    return _run(settings, "rules", "test_rules", settings, message)


def call(settings: Any, name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """通用派发入口；未知功能名直接报错，不猜。"""
    return _run(settings, name, name, *args, **kwargs)
