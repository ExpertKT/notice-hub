"""「最近推送」列表：服务端把摘要正文整理好，页面只管渲染。"""

from __future__ import annotations

import re
from typing import Any, Mapping

# 正文上限：够放几条明细，又不至于把整库搬进一个响应。
BODY_LIMIT = 4000
ELLIPSIS_NOTE = "\n…（以下省略）"

# 旧正文的群号被 40 字硬切过（79185542 / 7918554…）；只在抬头两行里恢复。
HEADER_LINES = 2
CUT_NUMBER = re.compile(r"(?<!\d)(\d{6,11})(?!\d)(…)?")
ACTION_LINE = re.compile(r"^\s*需做：(.*)$")
ITEM_TITLE = re.compile(r"^\s*\d+\.\s*(?:\[[^\]]*\]\s*)?(.*)$")
ACTION_OVERLAP_MIN = 10



def _clip_body(text: str, limit: int = BODY_LIMIT) -> str:
    """超长正文按整行截断，结尾标出省略，不留半行。"""
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    budget = max(1, limit - len(ELLIPSIS_NOTE))
    cut = text[:budget]
    position = cut.rfind("\n")
    if position > 0:
        cut = cut[:position]
    return cut.rstrip() + ELLIPSIS_NOTE


def _group_names(store: Any) -> dict[str, str]:
    """从库里读群号→群名；读不到就当没有，列表照常返回。"""
    getter = getattr(store, "group_names", None)
    if not callable(getter):
        return {}
    try:
        mapping = getter()
    except Exception:  # 群名只是锦上添花，不该让整页列表挂掉
        return {}
    if not isinstance(mapping, Mapping):
        return {}
    return {
        str(key).strip(): str(value).strip()
        for key, value in mapping.items()
        if str(key).strip() and str(value).strip()
    }


def _apply_group_names(text: str, names: Mapping[str, str]) -> str:
    """把正文里成串出现的群号换成群名；同一串更长的号码不算命中。"""
    if not text or not names:
        return text
    keys = sorted((key for key in names if key), key=len, reverse=True)
    if not keys:
        return text
    pattern = re.compile(r"(?<!\d)(?:%s)(?!\d)" % "|".join(re.escape(key) for key in keys))
    return pattern.sub(lambda match: names.get(match.group(0), match.group(0)), text)


def _restore_name_by_prefix(match: re.Match[str], names: Mapping[str, str]) -> str:
    """半截群号只有能唯一补成全号时才换名字；有歧义原样留着，绝不猜。"""
    digits = match.group(1)
    if digits in names:
        return names[digits]
    candidates = [name for key, name in names.items() if key.startswith(digits)]
    if len(candidates) == 1:
        return candidates[0]
    return match.group(0)


def _restore_cut_group_ids(text: str, names: Mapping[str, str]) -> str:
    """旧摘要按 40 字硬切过，抬头里会留下半截群号；只在前两行恢复成完整群名。"""
    if not text or not names:
        return text
    lines = text.split("\n")
    for index in range(min(HEADER_LINES, len(lines))):
        lines[index] = CUT_NUMBER.sub(lambda match: _restore_name_by_prefix(match, names), lines[index])
    return "\n".join(lines)


def _strip_tail_mark(value: str) -> str:
    return value.rstrip().rstrip("…").rstrip()


def _action_forms(value: str) -> list[str]:
    """旧格式把来源（· 群 · 人）接在「需做」后面；原文与去掉来源的正文都试一遍。"""
    forms = [_strip_tail_mark(value)]
    head = _strip_tail_mark(value.split(" · ", 1)[0])
    if head and head != forms[0]:
        forms.append(head)
    return [form for form in forms if form]


def _repeats_title(title: str, forms: list[str]) -> bool:
    """一方是另一方的前缀、且重合至少 ACTION_OVERLAP_MIN 字，就算重复。"""
    if not title:
        return False
    for form in forms:
        shorter, longer = (title, form) if len(title) <= len(form) else (form, title)
        if len(shorter) >= ACTION_OVERLAP_MIN and longer.startswith(shorter):
            return True
    return False


def _drop_inline_action(line: str, title: str) -> str:
    """旧格式把「需做」接在「截止」后面（· 需做：… · 群 · 人）；重复时只摘掉这一段。"""
    kept: list[str] = []
    dropped = False
    for segment in line.split(" · "):
        if segment.lstrip().startswith("需做："):
            payload = segment.lstrip()[len("需做："):]
            if _repeats_title(title, _action_forms(payload)):
                dropped = True
                continue
        kept.append(segment)
    return " · ".join(kept) if dropped else line


def _drop_repeated_actions(text: str) -> str:
    """旧摘要的「需做」常是条目标题重复一遍；和上方最近的标题比，重复就删掉。"""
    if not text:
        return text
    kept: list[str] = []
    title = ""
    for line in text.split("\n"):
        action = ACTION_LINE.match(line)
        if action:
            if _repeats_title(title, _action_forms(action.group(1))):
                continue
            kept.append(line)
            continue
        if "需做：" in line:
            line = _drop_inline_action(line, title)
        item = ITEM_TITLE.match(line)
        if item:
            title = _strip_tail_mark(item.group(1))
        kept.append(line)
    return "\n".join(kept)

def list_notices(store: Any) -> dict[str, Any]:
    """最近几条推送。空正文的条目不返回，页面不会出现只有标题的空卡。"""
    names = _group_names(store)
    items: list[dict[str, Any]] = []
    for digest in store.recent_digests(limit=8):
        text = _apply_group_names(str(digest.get("body") or ""), names)
        text = _restore_cut_group_ids(text, names)
        text = _drop_repeated_actions(text)
        body = _clip_body(text)
        if not body:
            continue
        items.append(
            {
                "id": digest.get("id"),
                "kind": digest.get("kind"),
                "created_at": digest.get("created_at"),
                "window_end": digest.get("window_end"),
                "summary": "%s 条重点" % (digest.get("item_count") or 0),
                "body": body,
            }
        )
    return {"items": items}
