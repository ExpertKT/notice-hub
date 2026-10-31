"""「实验室」统一门面。

路由层只依赖本模块：先查开关（Settings.lab_enabled），再惰性 import
真实模块，任何失败都收成 ``{"ok": False, "error": ...}``，不抛栈。
纯函数、无 I/O、无全局状态；真实功能一律输出**候选**，不直接写库。
"""

from __future__ import annotations

import importlib
import importlib.util
from typing import Any, Mapping

__all__ = [
    "available",
    "call",
    "extract_events",
    "list_replies",
    "ack_reply",
    "search",
    "list_rules",
    "save_rules",
    "test_rules",
]

# 功能名 -> 模块全名。四个模块都已定稿落地（rules.py 自 beta.13 起接线，
# load_rules/save_rules 负责 data/rules.json 的读写）。
_LAB_MODULES: dict[str, str] = {
    "search": "noticehub.search",
    "replies": "noticehub.replies",
    "events": "noticehub.event_extract",
    "rules": "noticehub.rules",
}


def _module_ready(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False


def available(settings: Any) -> dict[str, bool]:
    """各实验功能当前是否可用：开关开 且 模块已落地。"""
    return {
        name: settings.lab_enabled(name) and _module_ready(module_name)
        for name, module_name in _LAB_MODULES.items()
    }


def _run(settings: Any, name: str, func_name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    if name not in _LAB_MODULES:
        return {"ok": False, "error": "未知实验功能"}
    if not settings.lab_enabled(name):
        return {"ok": False, "error": "实验功能未开启"}
    module_name = _LAB_MODULES[name]
    if not _module_ready(module_name):
        return {"ok": False, "error": "该实验功能尚未就绪"}
    try:
        module = importlib.import_module(module_name)
        func = getattr(module, func_name)
        return {"ok": True, "result": func(*args, **kwargs)}
    except Exception as exc:  # noqa: BLE001 - 门面兜底，任何底层异常都不外抛
        return {"ok": False, "error": f"该实验功能尚未就绪：{exc}"}


def search(settings: Any, query: str, **filters: Any) -> dict[str, Any]:
    return _run(settings, "search", "search", query, **filters)


def list_replies(settings: Any, **filters: Any) -> dict[str, Any]:
    return _run(settings, "replies", "list_replies", **filters)


def ack_reply(settings: Any, msg_id: str) -> dict[str, Any]:
    return _run(settings, "replies", "ack_reply", msg_id)


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
