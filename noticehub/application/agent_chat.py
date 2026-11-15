"""待办页长对话。钥匙留在服务端，模型只能走待办用例。"""

from __future__ import annotations

import datetime as dt
import json
import re
import threading
import urllib.error
import urllib.request
from typing import Any, Callable

from noticehub.application.agent_brief import agent_shell_enabled
from noticehub.application.tasks import (
    apply_task_action,
    correct_task,
    create_task,
    rename_task,
    set_task_pinned,
    set_task_urgent,
)
from noticehub.domain.time import parse_iso
from noticehub.timeutil import now_local

AGENT_CHAT_PATH = "/api/agent/chat"
_LOCK = threading.Lock()
_SESSIONS: dict[str, dict[str, Any]] = {}
_HISTORY = 12
_ROUNDS = 2
_CALL_TIMEOUT = 25

_SYSTEM = (
    "你是待办助手。用中文短句回答。"
    "群里整理出来的和用户自己说的事，都在下面这一张待办表里，都能增、查、改、删。"
    "不要说只管群待办，也不要说不在范围内。"
    "不能管安装、设置、托管或更新。"
    "要改数据时，回复正文只能是一个 JSON，不要加别的字："
    '{"say":"给用户看的一句","calls":[{"name":"create","summary":"去立功堡见学长","deadline":"2026-11-15T09:00"}]}。'
    "查已有待办时 calls 用空数组，在 say 里直接回答。"
    "用户说出一件要做的事、对不上已有标题时，必须 create，不要只说记下了。"
    "name 只能是 act、create、rename、pin、urgent、reschedule、clear_finished。"
    "create 用 summary 和 deadline。deadline 用 YYYY-MM-DDTHH:MM；上午没有钟点用 09:00，下午用 15:00，晚上用 20:00，只说某天用 23:59；没有日期就不要填 deadline。"
    "rename 改标题，带 task_id 和 summary。reschedule 改截止时间，带 task_id 和 deadline。"
    "act 的 action 只能是 confirm、dismiss、done、reopen、snooze、delete。"
    "来源是群、状态还是 open 的，用户说删，用 dismiss，不要 delete。"
    "delete 和 clear_finished 第一次不要带 confirm。等用户同意后，下一句再对同一个操作带 confirm true。"
    "对不上唯一一条时不要调用，在 say 里问是哪一条。"
    "没写进表就不要说已经记下或已经改好。"
)
_RETRY = (
    "上一句没有调用。用户说的事，不管是不是群里来的，都要写进同一张待办表。"
    "请只回复 JSON。要新增就用 create，summary 写短标题，deadline 用 YYYY-MM-DDTHH:MM。"
    "上午没有钟点用 09:00，下午用 15:00，晚上用 20:00。不要拒绝，不要说记下了。"
)
_MISSED = ("只管群", "不在我的待办", "不在待办范围", "记下了", "记下来了", "已记下", "已记录", "帮你记")
_PLAN = ("要去", "要见", "记得", "提醒我", "帮我记", "帮我加", "新建", "加一条", "记一下", "记上")


def optional_get(path: str) -> tuple[str, bool] | None:
    if path == AGENT_CHAT_PATH and agent_shell_enabled():
        return ("_route_get_api_agent_chat", False)
    return None


def optional_post(path: str) -> tuple[str, bool] | None:
    if path == AGENT_CHAT_PATH and agent_shell_enabled():
        return ("_route_post_api_agent_chat", False)
    return None


def chat_ready(settings: Any) -> bool:
    """有钥匙，或明确用本机 Ollama，才把这句话交给模型。"""
    if not getattr(settings, "llm_enabled", False):
        return False
    backend = str(getattr(settings, "llm_backend", "") or "").strip().lower()
    if backend == "ollama":
        return True
    return bool(getattr(settings, "dashscope_api_key", ""))


def chat_turn(
    store: Any,
    settings: Any,
    session_id: str,
    text: str,
    *,
    poster: Callable[[list[dict[str, str]]], str] | None = None,
) -> dict[str, Any]:
    raw = " ".join(str(text or "").split())
    if not raw:
        raise ValueError("请先写一句话")
    if len(raw) > 500:
        raise ValueError("一句话请控制在 500 字以内")
    sid = _session_id(session_id)
    if not chat_ready(settings):
        return {"ok": False, "fallback": True, "say": "", "changed": False, "session_id": sid}
    with _LOCK:
        session = _SESSIONS.setdefault(sid, {"history": [], "pending": ""})
        history = list(session["history"])
        pending = str(session.get("pending") or "")
    messages = _messages(store, history, raw, pending)
    changed = False
    say = ""
    content = ""
    had_calls = False
    last_outcomes: list[dict[str, Any]] = []
    local = {"pending": pending, "kept": False}
    send = poster or (lambda batch: _post(settings, batch))
    for _ in range(_ROUNDS):
        content = send(messages)
        parsed = _parse(content)
        say = parsed["say"]
        if not parsed["calls"]:
            break
        had_calls = True
        last_outcomes = [
            _run_call(store, settings, local, call, allow_confirm=bool(pending))
            for call in parsed["calls"][:3]
        ]
        changed = changed or any(item.get("changed") for item in last_outcomes)
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": "工具结果：" + json.dumps(last_outcomes, ensure_ascii=False)})
        say = say or _say_from(last_outcomes)
    if _should_retry(raw, say, changed, had_calls):
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": _RETRY})
        content = send(messages)
        parsed = _parse(content)
        say = parsed["say"]
        if parsed["calls"]:
            last_outcomes = [
                _run_call(store, settings, local, call, allow_confirm=bool(pending))
                for call in parsed["calls"][:3]
            ]
            changed = changed or any(item.get("changed") for item in last_outcomes)
            say = say or _say_from(last_outcomes)
    if not say:
        say = "没说上，再说一次。"
    if not changed and _missed_write(say):
        say = _say_from(last_outcomes) or "还没写进待办。直接说要记的标题和时间。"
    if pending and not local.get("kept") and not changed:
        local["pending"] = ""
    with _LOCK:
        kept = _SESSIONS.setdefault(sid, local)
        kept["history"] = (history + [{"role": "user", "content": raw}, {"role": "assistant", "content": say}])[-_HISTORY * 2 :]
        kept["pending"] = local.get("pending") or ""
    return {"ok": True, "fallback": False, "say": say, "changed": changed, "session_id": sid}


def _session_id(value: str) -> str:
    text = "".join(ch for ch in str(value or "") if ch.isalnum())
    if 8 <= len(text) <= 40:
        return text
    import secrets

    return secrets.token_hex(16)


def _should_retry(text: str, say: str, changed: bool, had_calls: bool) -> bool:
    if changed or had_calls:
        return False
    if _missed_write(say):
        return True
    return any(piece in text for piece in _PLAN)


def _missed_write(say: str) -> bool:
    return any(piece in str(say or "") for piece in _MISSED)


def _messages(store: Any, history: list[dict[str, str]], text: str, pending: str) -> list[dict[str, str]]:
    today = now_local()
    note = "今天是 %s 周%s。\n当前待办：\n%s" % (
        today.strftime("%Y-%m-%d"),
        "一二三四五六日"[today.weekday()],
        _snapshot(store),
    )
    if pending:
        note += "\n有待确认、还没执行的操作：" + pending
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "system", "content": note},
        *history,
        {"role": "user", "content": text},
    ]


def _snapshot(store: Any) -> str:
    rows = []
    for task in store.list_tasks():
        status = str(task.get("status") or "")
        if status in {"dismissed", "expired"}:
            continue
        rows.append(task)
    rows.sort(key=lambda task: (0 if str(task.get("status") or "") in {"open", "candidate"} else 1, int(task.get("id") or 0)))
    lines = []
    for task in rows[:40]:
        status = str(task.get("status") or "")
        origin = "手动" if str(task.get("source") or "") == "manual" or str(task.get("task_key") or "").startswith("manual:") else "群"
        lines.append(
            "{id} {status} {origin} {deadline} {summary}".format(
                id=task.get("id"),
                status=status,
                origin=origin,
                deadline=str(task.get("deadline") or "无截止"),
                summary=str(task.get("summary") or "")[:80],
            )
        )
    return "\n".join(lines) if lines else "没有待办"


def _parse(content: str) -> dict[str, Any]:
    text = str(content or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and "say" in payload:
            calls = payload.get("calls")
            return {
                "say": str(payload.get("say") or "").strip(),
                "calls": [item for item in calls if isinstance(item, dict)] if isinstance(calls, list) else [],
            }
    return {"say": text, "calls": []}


def _run_call(
    store: Any,
    settings: Any,
    session: dict[str, Any],
    call: dict[str, Any],
    *,
    allow_confirm: bool,
) -> dict[str, Any]:
    name = str(call.get("name") or "")
    try:
        if name == "create":
            session["pending"] = ""
            result = create_task(store, str(call.get("summary") or ""), _coerce_deadline(call.get("deadline")))
            return {"ok": True, "changed": True, "id": result.get("task_id"), "detail": "已新建「%s」" % result.get("summary")}
        if name == "rename":
            task_id, error = _resolve(store, call.get("task_id"), reopen=False)
            if error:
                return {"ok": False, "changed": False, "detail": error}
            session["pending"] = ""
            result = rename_task(store, int(task_id), str(call.get("summary") or ""))
            if not result.get("ok"):
                return {"ok": False, "changed": False, "detail": "没改成"}
            return {"ok": True, "changed": True, "detail": "标题已改为「%s」" % result.get("summary")}
        if name == "clear_finished":
            return _confirm(session, "clear_finished", "", bool(call.get("confirm") is True and allow_confirm), lambda: _clear(store))
        if name in {"act", "pin", "urgent", "reschedule"}:
            task_id, error = _resolve(store, call.get("task_id"), reopen=str(call.get("action") or "") == "reopen")
            if error:
                return {"ok": False, "changed": False, "detail": error}
            if name == "pin":
                session["pending"] = ""
                set_task_pinned(store, str(task_id), bool(call.get("pinned")))
                return {"ok": True, "changed": True, "detail": "置顶已改"}
            if name == "urgent":
                session["pending"] = ""
                urgent = call.get("urgent")
                set_task_urgent(store, str(task_id), urgent if isinstance(urgent, bool) else None)
                return {"ok": True, "changed": True, "detail": "紧急已改"}
            if name == "reschedule":
                session["pending"] = ""
                correct_task(store, int(task_id), "deadline", _coerce_deadline(call.get("deadline")))
                return {"ok": True, "changed": True, "detail": "已改期"}
            action = str(call.get("action") or "")
            if action == "delete":
                task = store.get_task(int(task_id)) or {}
                manual = str(task.get("task_key") or "").startswith("manual:")
                if str(task.get("status") or "") == "open" and not manual:
                    return {
                        "ok": False,
                        "changed": False,
                        "detail": "这条还开着，而且不是手动记的，只能忽略，不能删除。",
                    }
                return _confirm(
                    session,
                    "delete",
                    str(task_id),
                    bool(call.get("confirm") is True and allow_confirm),
                    lambda: _act(store, settings, int(task_id), "delete"),
                )
            session["pending"] = ""
            return _act(store, settings, int(task_id), action)
    except ValueError as error:
        return {"ok": False, "changed": False, "detail": str(error)}
    return {"ok": False, "changed": False, "detail": "不能做这个"}


def _confirm(session: dict[str, Any], name: str, task_id: str, accepted: bool, run: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    key = name + ":" + task_id
    if accepted and session.get("pending") == key:
        session["pending"] = ""
        session["kept"] = False
        return run()
    session["pending"] = key
    session["kept"] = True
    return {"ok": False, "changed": False, "detail": "还没执行。请用户确认后，再对同一操作带 confirm true。"}


def _clear(store: Any) -> dict[str, Any]:
    removed = store.clear_finished_tasks()
    return {"ok": True, "changed": True, "detail": "已清空 %s 条" % removed}


def _act(store: Any, settings: Any, task_id: int, action: str) -> dict[str, Any]:
    result = apply_task_action(store, task_id, action, settings=settings)
    if not result.get("ok"):
        return {"ok": False, "changed": False, "detail": "没改成"}
    return {"ok": True, "changed": True, "detail": "已%s %s" % (action, task_id)}


def _resolve(store: Any, raw: Any, *, reopen: bool) -> tuple[int | None, str]:
    text = str(raw or "").strip()
    if text.isdigit():
        return int(text), ""
    hits = []
    for task in store.list_tasks():
        status = str(task.get("status") or "")
        if status in {"dismissed", "expired"}:
            continue
        if status == "done" and not reopen:
            continue
        if text and text in str(task.get("summary") or ""):
            hits.append(task)
    if len(hits) == 1:
        return int(hits[0]["id"]), ""
    if not hits:
        return None, "没有标题里含「%s」的待办" % text
    titles = "、".join(str(task.get("summary") or "") for task in hits[:6])
    return None, "有 %d 条：%s。再说全一点。" % (len(hits), titles)


def _coerce_deadline(raw: Any) -> str:
    """模型有时只写「明天上午」。能看懂的换成时间，看不懂的原样交给用例报错。"""
    text = " ".join(str(raw or "").split())
    if not text or parse_iso(text) is not None:
        return text
    now = now_local()
    if "后天" in text:
        day = now + dt.timedelta(days=2)
    elif "明天" in text:
        day = now + dt.timedelta(days=1)
    elif "今天" in text:
        day = now
    else:
        return text
    hour, minute = 23, 59
    if "上午" in text:
        hour, minute = 9, 0
    elif "中午" in text:
        hour, minute = 12, 0
    elif "下午" in text:
        hour, minute = 15, 0
    elif "晚上" in text or "傍晚" in text:
        hour, minute = 20, 0
    matched = re.search(r"(\d{1,2})\s*(?:[:：点时])\s*(\d{1,2})?", text)
    if matched:
        hour = int(matched.group(1))
        minute = int(matched.group(2) or 0)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return text
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")


def _say_from(outcomes: list[dict[str, Any]]) -> str:
    details = [str(item.get("detail") or "") for item in outcomes if item.get("detail")]
    return "。".join(details)


def _post(settings: Any, messages: list[dict[str, str]]) -> str:
    endpoint = str(getattr(settings, "dashscope_endpoint", "") or "")
    if str(getattr(settings, "llm_backend", "") or "").strip().lower() == "ollama" and not endpoint:
        endpoint = "http://127.0.0.1:11434/v1/chat/completions"
    payload = {
        "model": str(getattr(settings, "dashscope_model", "") or "qwen-plus"),
        "temperature": 0.2,
        "messages": messages,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    key = str(getattr(settings, "dashscope_api_key", "") or "")
    if key:
        headers["Authorization"] = "Bearer " + key
    request = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")
    timeout = min(_CALL_TIMEOUT, int(getattr(settings, "llm_timeout", _CALL_TIMEOUT) or _CALL_TIMEOUT))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            parsed = json.loads(response.read().decode("utf-8", "replace"))
        return str(parsed["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("模型接口返回结构不认识") from error
    except urllib.error.HTTPError as error:
        raise RuntimeError("模型请求失败：HTTP %s" % error.code) from error
    except urllib.error.URLError as error:
        raise RuntimeError("模型请求失败：%s" % error) from error


def chat_history(session_id: str) -> dict[str, Any]:
    """读回这一段对话。不认识的会话号不新建，避免刷新页面造出空会话。"""
    text = "".join(ch for ch in str(session_id or "") if ch.isalnum())
    if not (8 <= len(text) <= 40):
        return {"ok": True, "session_id": "", "messages": []}
    with _LOCK:
        session = _SESSIONS.get(text)
        history = list(session["history"]) if session else []
    return {"ok": True, "session_id": text, "messages": history}


def reset_sessions() -> None:
    with _LOCK:
        _SESSIONS.clear()
