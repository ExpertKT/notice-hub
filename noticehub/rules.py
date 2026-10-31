# -*- coding: utf-8 -*-
"""规则引擎：解析规则、匹配消息、读写规则集。

纯函数在 noticehub.domain.rules。本模块只读写 rules.json。
"""

from __future__ import annotations

import json
import os
import re  # 测试会 patch noticehub.rules.re.compile；与 domain 共用同一个 re 模块
from pathlib import Path
from typing import Any

from noticehub.domain.rules import (
    MAX_PATTERN_CHARS,
    MAX_TEXT_CHARS,
    Rule,
    _InvalidRule,
    _parse_one,
    match,
    parse_rules,
)

__all__ = [
    "Rule", "parse_rules", "match", "MAX_TEXT_CHARS", "MAX_PATTERN_CHARS",
    "load_rules", "save_rules", "test_rules",
]

# -------------------------------------------------------------------------- 规则集存取

RULES_FILENAME = "rules.json"


def rules_path(settings: Any) -> Path:
    """规则集文件位置；拿不到 data_dir 时退回 ./data。"""
    data_dir = getattr(settings, "data_dir", None)
    return (Path(data_dir) if data_dir else Path("data")) / RULES_FILENAME


def _serialize(rule: Rule) -> dict[str, Any]:
    """把编译后的规则还原成可编辑的 dict（regex 从 re.Pattern 取回 .pattern）。"""
    match_cfg: dict[str, Any] = {}
    if rule.keywords:
        match_cfg["keyword"] = list(rule.keywords)
    if rule.regexes:
        match_cfg["regex"] = [pattern.pattern for pattern in rule.regexes]
    if rule.at_me:
        match_cfg["at_me"] = True
    if rule.senders:
        match_cfg["sender"] = list(rule.senders)
    item: dict[str, Any] = {
        "id": rule.id,
        "enabled": bool(rule.enabled),
        "scope": list(rule.scope) if isinstance(rule.scope, (list, tuple)) else (rule.scope or "*"),
        "match": match_cfg,
        "action": dict(rule.action),
        "priority": int(rule.priority),
    }
    if rule.dedupe_key:
        item["dedupe_key"] = rule.dedupe_key
    return item


def load_rules(settings: Any) -> dict[str, Any]:
    """读规则集；文件不存在、读不出、JSON 坏，一律当空规则集，不抛。

    返回体带 ``ok``，和 ``save_rules`` / ``test_rules`` 统一：门面（``lab``）只包一层
    ``{"ok": True, "result": ...}``，HTTP 层会把带 ``ok`` 的内层摊平，三种规则接口
    的响应形状因此一致。
    """
    path = rules_path(settings)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": True, "rules": [], "count": 0, "path": str(path)}
    if isinstance(raw, dict):
        items = raw.get("rules")
    else:
        items = raw
    if not isinstance(items, list):
        return {"ok": True, "rules": [], "count": 0, "path": str(path)}
    # 读出来的也要过一遍 parse_rules：坏条目跳过，不把非法规则喂给调用方。
    parsed = parse_rules(items)
    return {
        "ok": True,
        "rules": [_serialize(rule) for rule in parsed],
        "count": len(parsed),
        "path": str(path),
    }


def _validate_all(items: list) -> list[tuple[int, str]]:
    """逐条校验，返回 [(下标, 原因)]；空列表表示全部合法。

    单独走一遍而不是靠 ``parse_rules`` 的条数差：条数差只能说"有几条坏了"，
    说不出坏在哪。保存时必须告诉用户第几条、为什么。
    """
    problems: list[tuple[int, str]] = []
    for index, item in enumerate(items):
        try:
            _parse_one(item)
        except _InvalidRule as exc:
            problems.append((index, str(exc)))
        except Exception as exc:  # noqa: BLE001 - 任何意外都当这条非法，不崩保存
            problems.append((index, "无法解析：%s" % exc))
    return problems


def save_rules(settings: Any, items: Any) -> dict[str, Any]:
    """校验并原子落盘。任何一条非法就整体拒绝，一个字节都不写。"""
    if not isinstance(items, (list, tuple)):
        return {"ok": False, "error": "规则必须是一个数组"}
    problems = _validate_all(list(items))
    if problems:
        index, reason = problems[0]
        extra = "（还有 %d 条也不合法）" % (len(problems) - 1) if len(problems) > 1 else ""
        return {
            "ok": False,
            "error": "第 %d 条规则非法：%s%s。已全部拒绝，未写入任何文件。"
                     % (index + 1, reason, extra),
        }
    parsed = parse_rules(items)
    payload = {"version": 1, "rules": [_serialize(rule) for rule in parsed]}
    path = rules_path(settings)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 先写同目录临时文件再 os.replace：同分区原子替换，断电/强杀不会留下半截文件。
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as error:
        return {"ok": False, "error": "写入规则文件失败：%s" % error}
    return {"ok": True, "count": len(parsed), "path": str(path)}


def test_rules(settings: Any, message: dict) -> dict[str, Any]:
    """拿当前规则集去试跑一条消息，返回命中项；不写任何文件。

    给设置页「试一下」用：让用户看到自己写的规则到底会不会命中，
    而不是等到真群消息进来才发现写错了。
    """
    if not isinstance(message, dict):
        return {"ok": False, "error": "消息必须是对象"}
    text = str(message.get("content") or "")
    if not text.strip():
        return {"ok": False, "error": "缺少 content"}
    if len(text) > MAX_TEXT_CHARS * 20:
        return {"ok": False, "error": "content 过长"}
    loaded = load_rules(settings)
    parsed = parse_rules(loaded["rules"])
    hits = match(message, parsed)
    return {
        "ok": True,
        "hits": hits,
        "count": len(hits),
        "rule_count": len(parsed),
        "truncated": len(text) > MAX_TEXT_CHARS,
    }
