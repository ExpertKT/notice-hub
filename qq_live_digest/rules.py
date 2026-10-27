# -*- coding: utf-8 -*-
"""规则引擎：解析规则、匹配消息、读写规则集。

纯函数部分（``parse_rules`` / ``match``）只做「判定」，绝不执行：不写库、不推送。
``load_rules`` / ``save_rules`` 只碰 ``<data_dir>/rules.json`` 一个文件，不读消息库。

保存是 fail-closed：先整体 ``parse_rules``，任何一条非法就一个字节都不写。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

__all__ = [
    "Rule", "parse_rules", "match", "MAX_TEXT_CHARS", "load_rules", "save_rules", "test_rules",
]

logger = logging.getLogger(__name__)

# Python 的 re 没有可靠超时。这里用「解析期静态拒绝高危模式 + 输入截断」降低风险，
# 但不保证完全消除灾难性回溯；规则作者对自己写的正则负责。
# ponytail: 需要真超时再换 regex 库或子进程。
MAX_TEXT_CHARS = 500
# 单个正则模式的长度上限（字符数），超长模式直接拒绝。
MAX_PATTERN_CHARS = 500

_TEMPLATE_VARS = ("msg_id", "group_id", "sender")


class _SafeMap(dict):
    def __missing__(self, key: str) -> str:
        return ""


class Rule:
    """一条已解析、预编译的规则。字段与 data/rules.json 一一对应。"""

    __slots__ = ("id", "enabled", "scope", "keywords", "regexes", "at_me",
                 "senders", "action", "priority", "dedupe_key")

    def __init__(self, *, rule_id: str, enabled: bool, scope: Any,
                 keywords: list[str], regexes: list[re.Pattern], at_me: bool,
                 senders: list[str], action: dict, priority: int,
                 dedupe_key: str | None):
        self.id = rule_id
        self.enabled = enabled
        # None / "*" / 空列表 = 全部群；否则是指定群 id 列表。
        self.scope = scope
        self.keywords = keywords
        self.regexes = regexes
        self.at_me = at_me
        self.senders = senders
        self.action = action
        self.priority = priority
        self.dedupe_key = dedupe_key


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def parse_rules(items: Any) -> list[Rule]:
    """从 dict/JSON 列表解析规则；非法条目 warning 后跳过。"""
    rules: list[Rule] = []
    if not isinstance(items, (list, tuple)):
        logger.warning("rules: 规则集合必须是列表，忽略 %r", type(items).__name__)
        return rules
    for index, item in enumerate(items):
        try:
            rule = _parse_one(item)
        except _InvalidRule as exc:
            logger.warning("rules: 第 %d 条规则非法已跳过：%s", index, exc)
            continue
        rules.append(rule)
    return rules


class _InvalidRule(Exception):
    pass


# 合法 match 键名（拼错的键名会让规则退化成「匹配全部」，必须 fail-closed）。
_MATCH_KEYS = frozenset({"keyword", "regex", "at_me", "sender"})

# 量词：+ * 或 {n} / {n,} / {n,m}
_QUANTIFIER = r"(?:[+*]|\{\s*\d+\s*(?:,\s*\d*\s*)?\})"
# 嵌套量词：一个已量词化的分组后面又跟量词，如 (a+)+、(a*)*、(.*a){20}。
_NESTED_QUANTIFIER_RE = re.compile(
    r"\([^()]*" + _QUANTIFIER + r"[^()]*\)\s*" + _QUANTIFIER
)
# 无界点号量词：.* / .+ / .{0,}；两个以上连用是典型炸弹。
_UNBOUNDED_DOT_RE = re.compile(r"\.(?:[+*]|\{\s*0\s*,\s*\})")


def _check_pattern(pattern: str) -> None:
    """静态拒绝高危正则；命中即抛 _InvalidRule（fail-closed，不编译）。"""
    if len(pattern) > MAX_PATTERN_CHARS:
        raise _InvalidRule(
            f"regex 过长（{len(pattern)} > {MAX_PATTERN_CHARS} 字符）：{pattern[:40]!r}…"
        )
    if _NESTED_QUANTIFIER_RE.search(pattern):
        raise _InvalidRule(f"regex 含嵌套量词（灾难性回溯风险）：{pattern!r}")
    if len(_UNBOUNDED_DOT_RE.findall(pattern)) >= 2:
        raise _InvalidRule(f"regex 含多个无界 .*/.+（灾难性回溯风险）：{pattern!r}")


def _parse_one(item: Any) -> Rule:
    if not isinstance(item, dict):
        raise _InvalidRule("不是 dict")
    rule_id = str(item.get("id") or "").strip()
    if not rule_id:
        raise _InvalidRule("缺少 id")
    match_cfg = item.get("match")
    if match_cfg is None:
        match_cfg = {}
    if not isinstance(match_cfg, dict):
        raise _InvalidRule("match 必须是 dict")
    # match:{} 或未知键名（如把 regex 写成 regexes）都会让 matcher 为空而匹配全部消息，
    # 配置笔误可能变成全量通知，因此一律 fail-closed 拒绝。
    if not match_cfg:
        raise _InvalidRule("match 不能为空")
    unknown_keys = sorted(set(match_cfg) - _MATCH_KEYS)
    if unknown_keys:
        raise _InvalidRule(
            f"match 含未知键名 {unknown_keys}，只支持 {sorted(_MATCH_KEYS)}"
        )

    scope = item.get("scope")
    if scope is not None and not isinstance(scope, (list, tuple)) and scope != "*":
        raise _InvalidRule("scope 必须是 '*' 或群 id 列表")

    keywords = [str(word) for word in _as_list(match_cfg.get("keyword")) if str(word)]

    regexes: list[re.Pattern] = []
    for pattern in _as_list(match_cfg.get("regex")):
        _check_pattern(pattern)
        try:
            regexes.append(re.compile(pattern))
        except re.error as exc:
            raise _InvalidRule(f"regex 编译失败 {pattern!r}: {exc}")

    senders = [str(one) for one in _as_list(match_cfg.get("sender")) if str(one)]

    action = item.get("action")
    if isinstance(action, str):
        action = {"action": action, "payload": {}}
    if not isinstance(action, dict) or not action.get("action"):
        raise _InvalidRule("action 必须是 {action, payload} 或非空动作名")
    if "payload" not in action or not isinstance(action["payload"], dict):
        action = {**action, "payload": dict(action.get("payload") or {})}

    try:
        priority = int(item.get("priority", 0))
    except (TypeError, ValueError):
        raise _InvalidRule("priority 必须是整数")

    dedupe_key = item.get("dedupe_key")
    if dedupe_key is not None and not isinstance(dedupe_key, str):
        raise _InvalidRule("dedupe_key 必须是模板字符串")

    return Rule(
        rule_id=rule_id,
        enabled=bool(item.get("enabled", True)),
        scope=scope if scope is not None else "*",
        keywords=keywords,
        regexes=regexes,
        at_me=bool(match_cfg.get("at_me", False)),
        senders=senders,
        action=action,
        priority=priority,
        dedupe_key=dedupe_key,
    )


def _in_scope(rule: Rule, group_id: str) -> bool:
    if rule.scope == "*" or not rule.scope:
        return True
    return group_id in rule.scope


def _matchers_hit(rule: Rule, message: dict, text: str) -> bool:
    if rule.keywords and not any(word in text for word in rule.keywords):
        return False
    for pattern in rule.regexes:
        if pattern.search(text) is None:
            return False
    if rule.at_me and not bool(message.get("at_me")):
        return False
    if rule.senders and str(message.get("sender") or "") not in rule.senders:
        return False
    return True


def _render_dedupe_key(rule: Rule, message: dict) -> str | None:
    if not rule.dedupe_key:
        return None
    variables = {name: str(message.get(name) or "") for name in _TEMPLATE_VARS}
    try:
        return rule.dedupe_key.format_map(_SafeMap(variables))
    except (ValueError, IndexError):
        logger.warning("rules: 规则 %s 的 dedupe_key 模板非法已忽略", rule.id)
        return None


def match(message: dict, rules: list[Rule], *, settings: Any = None) -> list[dict]:
    """对一条消息评估规则，返回命中动作意图（按 priority 降序）。

    纯函数：不读库、不写库、不推送；settings 参数保留给未来按配置过滤，
    当前不参与判定。同一消息命中同一渲染后的 dedupe_key 只返回第一次。
    """
    del settings  # 预留接口，不参与判定
    if not isinstance(message, dict):
        return []
    text = str(message.get("content") or "")[:MAX_TEXT_CHARS]
    group_id = str(message.get("group_id") or "")

    hits = [
        rule for rule in rules
        if rule.enabled and _in_scope(rule, group_id) and _matchers_hit(rule, message, text)
    ]
    hits.sort(key=lambda rule: rule.priority, reverse=True)

    results: list[dict] = []
    seen_keys: set[str] = set()
    for rule in hits:
        key = _render_dedupe_key(rule, message)
        if key is not None:
            if key in seen_keys:
                continue
            seen_keys.add(key)
        results.append({
            "rule_id": rule.id,
            "action": dict(rule.action),
            "dedupe_key": key,
        })
    return results


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
