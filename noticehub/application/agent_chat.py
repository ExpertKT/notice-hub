"""待办页长对话。钥匙留在服务端，模型只能走待办用例。"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from noticehub.application.agent_brief import agent_brief, agent_shell_enabled
from noticehub.application.agent_plan import save_here, today_plan
from noticehub.application.campus import load_pois
from noticehub.application.route_tools import plan_route, poi_near
from noticehub.application.tasks import (
    apply_task_action,
    correct_task,
    create_task,
    rename_task,
    set_task_place_hint,
    set_task_pinned,
    set_task_urgent,
)
from noticehub.application.trips import WALK_M_PER_MIN
from noticehub.domain.time import parse_iso
from noticehub.doctor import sanitize
from noticehub.timeutil import now_local

AGENT_CHAT_PATH = "/api/agent/chat"
LOGGER = logging.getLogger(__name__)
# 模型通道失败时对用户说的话：厂商原文（HTTP 401、Incorrect API key、帮助链接）只进日志。
_DOWN_WRITE = "模型这边连不上，我先按看到的记："
_DOWN_READ = "模型这边连不上，我先按本地的数说："
_DOWN_DONE = "模型这边连不上，这一轮已经做完了："
_LOCAL_STEP = "模型没连上，改用本地兜底"
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
    "要调用工具时，回复正文只能是一个 JSON，不要加别的字："
    '{"say":"给用户看的一句","calls":[{"name":"day_plan"}]}。'
    "查已有待办时 calls 用空数组，在 say 里直接回答。"
    "用户说出一件要做的事、对不上已有标题时，必须 create，不要只说记下了。"
    "name 只能是 act、create、rename、pin、urgent、reschedule、clear_finished、plan_route、poi_near、day_plan、here、set_place。"
    "用户说自己在哪栋楼时（「我在图书馆」「现在在立德楼」）调用 here，place 写楼名；起点从此按这栋楼算，不用问第二遍。"
    "用户说某件事在哪栋楼时（「第 3 件在三食堂」「那个取快递的放菜鸟驿站」）调用 set_place，带 place（楼名）和 task_id 或 title；地点不进 summary，能不能排进行程靠的是 place。"
    "用户问路线、却没说自己在哪时，先问一句「你现在在哪栋楼」，别拿课表的地点当他在的地方。"
    "问路线、距离、顺路时也必须回复上述 JSON，先调用工具，不能自行编造里程或节省距离。"
    "问今天怎么安排、先办哪件、顺不顺路、要不要跑两趟时，先调用 day_plan，再回答。"
    "day_plan 无必填参数，读取今天的课表、已有待办和常做的事；返回的站序、时刻、米数就是能说的数，不许另算。"
    "用户只是问行程、没让新建待办时，不要调用 create。"
    "这一轮改动过任何待办（新建、改期、删除、置顶等），不要在回复里报行程数字；要报就先全部改完，再调一次 day_plan，只用它的新返回值。没改任何待办时，可以直接用 day_plan 的结果。"
    "plan_route 用 points（楼名或 POI 编号数组），可选 start；未填 start 时固定首点起步，不返回起点。"
    "poi_near 用 place（楼名或 POI 编号）和 radius_m（非负米数），返回附近地点及已有未完待办。"
    "要加上附近第三件事时，可先 poi_near 找待办，再 plan_route 比较顺序。"
    f"两者都只算坐标直线估算，{WALK_M_PER_MIN:g} 米/分钟；附近不代表不绕路，不保证营业、截止时间或可办理。"
    "工具失败时如实说明并让用户核对参数；这些工具不增改待办。"
    "create 必须带 summary，空 summary 是错误，不许发送；summary 和 deadline 一起给。deadline 用 YYYY-MM-DDTHH:MM；上午没有钟点用 09:00，下午用 15:00，晚上用 20:00，只说某天用 23:59；没有日期就不要填 deadline。"
    "rename 改标题，带 task_id 和 summary。reschedule 改截止时间，带 task_id 和 deadline。"
    "summary 保留用户原话里的地点和钟点，不要自己缩写——能不能排进行程，靠的就是这几个字。"
    "act 的 action 只能是 confirm、dismiss、done、reopen、snooze、delete。"
    "来源是群、状态还是 open 的，用户说删，用 dismiss，不要 delete。"
    "delete 和 clear_finished 第一次不要带 confirm。等用户同意后，下一句再对同一个操作带 confirm true。"
    "对不上唯一一条时不要调用，在 say 里问是哪一条。"
    "没写进表就不要说已经记下、已经记、已经改好——工具没返回成功就一个字都不能说成功。"
)
_RETRY = (
    "上一句没有调用。用户说的事，不管是不是群里来的，都要写进同一张待办表。"
    "请只回复 JSON。要新增就用 create，summary 必须写、不能为空，deadline 用 YYYY-MM-DDTHH:MM；缺字段就是错。"
    "上午没有钟点用 09:00，下午用 15:00，晚上用 20:00。不要拒绝，不要说记下了。"
)
_MISSED = ("只管群", "不在我的待办", "不在待办范围", "记下了", "记下来了", "已记下", "已记录", "帮你记")
_PLAN = ("要去", "要见", "记得", "提醒我", "帮我记", "帮我加", "新建", "加一条", "记一下", "记上")
# 句尾已经收过尾的字符；再接一句时不再补句号，否则就是「？。」。
_TERMINATORS = "。！？!?…"


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
        with _LOCK:
            session = _SESSIONS.setdefault(sid, {"history": [], "pending": ""})
            history = list(session["history"])
        return _no_model_turn(store, settings, sid, raw, history)
    with _LOCK:
        session = _SESSIONS.setdefault(sid, {"history": [], "pending": ""})
        history = list(session["history"])
        pending = str(session.get("pending") or "")
    messages = _messages(store, history, raw, pending)
    pois = load_pois(getattr(settings, "data_dir", None))
    messages[1]["content"] += "\n可用校园地点（编号、楼名、别名）：\n" + json.dumps(
        [{"id": p["id"], "name": p["name"], "aliases": p.get("aliases", [])} for p in pois], ensure_ascii=False,
    )
    rows = _snapshot_rows(store)
    trace: list[dict[str, Any]] = [
        {
            "kind": "read",
            "text": "读了 %d 件待办%s%s" % (
                len(rows),
                _lesson_note(settings, now_local()),
                "，和刚才 %d 轮对话" % (len(history) // 2) if history else "",
            ),
        }
    ]
    changed = False
    say = ""
    content = ""
    had_calls = False
    post_tool_say = ""
    plan_step: dict[str, Any] | None = None
    last_outcomes: list[dict[str, Any]] = []
    local = {"pending": pending, "kept": False}
    before_ids = {str(task.get("id")) for task in store.list_tasks()}
    send = poster or (lambda batch: _post(settings, batch))
    done: set[str] = set()
    degraded = False

    def _ask(batch: list[dict[str, str]]) -> str | None:
        try:
            return send(batch)
        except Exception as error:  # noqa: BLE001 - 模型通道任何失败都不许端给用户
            LOGGER.warning("对话模型不可用，改用本地兜底：%s", error)
            return None

    for _ in range(_ROUNDS):
        content = _ask(messages)
        if content is None:
            degraded = True
            break
        parsed = _parse(content)
        say = parsed["say"]
        if last_outcomes and say:
            post_tool_say = say
        if not parsed["calls"]:
            break
        had_calls = True
        calls = [call for call in parsed["calls"][:3] if _call_key(call) not in done]
        if not calls:
            # 模型把上一跳那件事又说了一遍：再执行一次就是重复建单、重复改期
            break
        done.update(_call_key(call) for call in calls)
        if say:
            plan_step = {"kind": "plan", "text": say}
            trace.append(plan_step)
        last_outcomes = [
            _run_call(store, settings, local, call, allow_confirm=bool(pending), user_text=raw)
            for call in calls
        ]
        trace.extend(_call_trace(call, outcome) for call, outcome in zip(calls, last_outcomes))
        changed = changed or any(item.get("changed") for item in last_outcomes)
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": "工具结果：" + json.dumps(last_outcomes, ensure_ascii=False)})
        say = say or _say_from(last_outcomes)
    else:
        # 保留读过工具结果后的回复；首跳执行前的计划句不作为结果。
        say = post_tool_say or _say_from(last_outcomes)
    if _should_retry(raw, say, changed, had_calls):
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": _RETRY})
        content = _ask(messages)
        if content is None:
            degraded = True
        else:
            parsed = _parse(content)
            say = parsed["say"]
            if parsed["calls"]:
                calls = [call for call in parsed["calls"][:3] if _call_key(call) not in done]
                if calls:
                    done.update(_call_key(call) for call in calls)
                    last_outcomes = [
                        _run_call(store, settings, local, call, allow_confirm=bool(pending), user_text=raw)
                        for call in calls
                    ]
                    trace.extend(
                        _call_trace(call, outcome) for call, outcome in zip(calls, last_outcomes)
                    )
                    changed = changed or any(item.get("changed") for item in last_outcomes)
                    say = say or _say_from(last_outcomes)
    if degraded:
        # 模型通道不通：走本地规则，真提的事真落库，查的事报本地数；厂商原文只进日志。
        outcome = _local_fallback(store, settings, raw, already_changed=changed, outcomes=last_outcomes)
        trace.extend(outcome["trace"])
        say, changed = outcome["say"], outcome["changed"]
    else:
        if not say:
            say = post_tool_say or _say_from(last_outcomes) or "没说上，再说一次。"
        if not changed and (last_outcomes or _missed_write(say)):
            # 事实规则：这一轮没改到任何待办，模型的句子就不算数，一律用工具结果或失败说明。
            # 关键词（_MISSED）只补「一句话都没调工具却说记下了」这条，不当唯一防线。
            say = _say_from(last_outcomes) or "还没写进待办。直接说要记的标题和时间。"
            # 过程那条也要跟着改：气泡说没记下来、过程里却写着「已记」，用户没法判断信哪句。
            if plan_step is not None:
                plan_step["text"] = say
    if changed:
        created = [str(task.get("id")) for task in store.list_tasks() if str(task.get("id")) not in before_ids]
        if created:
            try:
                advice, step = _route_advice(store, settings, created)
            except Exception as error:  # noqa: BLE001 - 排程出错不该把这一轮对话弄挂
                LOGGER.warning("排程建议算不出来：%s", error)
            else:
                trace.append(step)
                say = _with_advice(say, advice)
    if pending and not local.get("kept") and not changed:
        local["pending"] = ""
    with _LOCK:
        kept = _SESSIONS.setdefault(sid, local)
        kept["history"] = (history + [{"role": "user", "content": raw}, {"role": "assistant", "content": say}])[-_HISTORY * 2 :]
        kept["pending"] = local.get("pending") or ""
    return {
        "ok": True,
        "fallback": degraded,
        "say": say,
        "changed": changed,
        "session_id": sid,
        "trace": trace,
    }


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


def _messages(
    store: Any,
    history: list[dict[str, str]],
    text: str,
    pending: str,
    snapshot: str | None = None,
) -> list[dict[str, str]]:
    today = now_local()
    note = "今天是 %s 周%s。\n当前待办：\n%s" % (
        today.strftime("%Y-%m-%d"),
        "一二三四五六日"[today.weekday()],
        _snapshot(store) if snapshot is None else snapshot,
    )
    if pending:
        note += "\n有待确认、还没执行的操作：" + pending
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "system", "content": note},
        *history,
        {"role": "user", "content": text},
    ]


def _snapshot_rows(store: Any) -> list[dict[str, Any]]:
    rows = []
    for task in store.list_tasks():
        status = str(task.get("status") or "")
        if status in {"dismissed", "expired"}:
            continue
        rows.append(task)
    rows.sort(key=lambda task: (0 if str(task.get("status") or "") in {"open", "candidate"} else 1, int(task.get("id") or 0)))
    return rows


def _snapshot(store: Any) -> str:
    return _table(_snapshot_rows(store))


def _table(rows: list[dict[str, Any]]) -> str:
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
    user_text: str = "",
) -> dict[str, Any]:
    name = str(call.get("name") or "")
    try:
        if name == "day_plan":
            result = today_plan(store, settings)
            counts = result["counts"]
            detail = "排了今天的行程：%s 趟、%s 站、%s 米" % (
                counts["trips"], counts["stops"], counts["distance_m"],
            )
            if not result["trips"]:
                detail += "；今天没有可排的行程，请查看未排事项和检查说明"
            return {"ok": True, "changed": False, "detail": detail, "result": result}
        if name == "here":
            result = save_here(settings, {"place": call.get("place") or call.get("summary") or ""})
            here = result["here"]
            plan = today_plan(store, settings)
            counts = plan["counts"]
            return {
                "ok": True,
                "changed": False,
                "detail": "起点改成「%s」；今天 %s 趟、%s 站、%s 米" % (
                    here["name"], counts["trips"], counts["stops"], counts["distance_m"],
                ),
                "result": plan,
            }
        if name == "set_place":
            task_id, error = _resolve_for_place(store, call)
            if error:
                return {"ok": False, "changed": False, "detail": error}
            pois = load_pois(getattr(settings, "data_dir", None))
            # 认不出地点时 set_task_place_hint 抛的那句原样交回去，别自己改写成成功。
            if "place" in call:
                target = call.get("place")
            else:
                target = call.get("summary") or ""
                if not str(target).strip():
                    # 模型漏带参数时不许把它读成「清空」：用户标过的地点会被悄悄抹掉。
                    return {
                        "ok": False,
                        "changed": False,
                        "detail": "没说在哪栋楼。要清掉地点，把 place 传空串；要新标，说清是哪栋楼。",
                    }
            set_task_place_hint(store, str(task_id), pois, target)
            plan = today_plan(store, settings)
            counts = plan["counts"]
            placed = next(
                (stop for trip in plan["trips"] for stop in trip["stops"] if str(stop.get("task_id")) == str(task_id)),
                None,
            )
            where = "，已排进今天：%s" % placed["place"] if placed else "，今天先排不进去"
            return {
                "ok": True,
                "changed": True,
                "detail": "「%s」的地点标成「%s」%s" % (
                    (store.get_task(int(task_id)) or {}).get("summary") or task_id,
                    call.get("place"), where,
                ),
                "result": plan,
            }
        if name in {"plan_route", "poi_near"}:
            pois = load_pois(getattr(settings, "data_dir", None))
            if name == "plan_route":
                result = plan_route(call.get("points"), call.get("start"), pois=pois)
                saving = ("和输入顺序一致，没有更顺的顺序" if result["saved_m"] == 0
                          else "比输入顺序少 %s 米" % result["saved_m"])
                detail = "%s；直线估算 %s 米，步行约 %s 分钟，%s" % (
                    "→".join(result["order"]), result["distance_m"], result["walk_minutes"], saving,
                )
            else:
                result = poi_near(call.get("place"), call.get("radius_m"), pois=pois, tasks=store.list_tasks())
                detail = "%s周围 %g 米直线半径内有 %d 个地点、%d 件未完待办" % (
                    result["place"], result["radius_m"], len(result["places"]), len(result["tasks"]),
                )
            return {"ok": True, "changed": False, "detail": detail, "result": result}
        if name == "create":
            session["pending"] = ""
            # 模型偶尔发裸 {"name":"create"}：摘要本来就该保留用户原话，空的时候退回原话当标题，
            # 否则会抛「请填写待办内容」，什么都没建，界面却已经说了「已记」。
            summary = str(call.get("summary") or "").strip() or str(user_text or "").strip()
            result = create_task(store, summary, _coerce_deadline(call.get("deadline")))
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
                # 说「改到明天」却没给出能读懂的时间，就不能往下走：空值在 Store 里等于
                # 「清空截止」，既可能是空写（原本就空）也可能是把真截止抹掉，两种都不是用户说的。
                deadline = _coerce_deadline(call.get("deadline"))
                if parse_iso(deadline) is None:
                    return {
                        "ok": False,
                        "changed": False,
                        "detail": "没改成：没说准改到什么时候。说「明天下午三点」这种再试一次。",
                    }
                # correct_task 的 ok 是「库里真改了没」：改成跟原来一样，它回 False，
                # 这时候再说「已改期」就是假成功，屏幕上什么都没变。
                done = correct_task(store, int(task_id), "deadline", deadline)
                if not done.get("ok"):
                    return {
                        "ok": False,
                        "changed": False,
                        "detail": "没改成：跟原来一样，不用改。",
                    }
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


def _resolve_for_place(store: Any, call: dict[str, Any]) -> tuple[int | None, str]:
    """set_place 定位待办：给了数字 id 就用它（已完成的不许标），否则按 title 在未完成任务里做包含匹配。

    匹配到 0 条或不止 1 条都不猜，明说给用户听，一个字都不写库。
    """
    task_id = call.get("task_id")
    text = str(task_id or "").strip()
    if text.isdigit():
        task = store.get_task(int(text))
        if not task or str(task.get("status") or "") in {"dismissed", "expired"}:
            return None, "没有编号 %s 这件待办" % text
        if str(task.get("status") or "") == "done":
            return None, "「%s」已经办完了，不用再标地点。" % str(task.get("summary") or text)
        return int(text), ""
    title = str(call.get("title") or "").strip()
    if not title:
        return None, "说清是哪一件待办：给我编号，或者把标题说全一点。"
    hits = [
        task for task in store.list_tasks()
        if str(task.get("status") or "") not in {"dismissed", "expired", "done"}
        and title in str(task.get("summary") or "")
    ]
    if len(hits) == 1:
        return int(hits[0]["id"]), ""
    if not hits:
        return None, "没有标题里含「%s」的待办" % title
    titles = "、".join(str(task.get("summary") or "") for task in hits[:6])
    return None, "有好几件都叫「%s」：%s。说清是哪一件。" % (title, titles)


_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_HOUR = re.compile(r"([一二三四五六七八九十]{1,3})(?=\s*[点时：:])")


def _cn_to_int(text: str) -> int | None:
    """「四」「十一」「二十」这类中文数目。超出一百的不管，钟点用不到。"""
    if text in _CN_DIGITS:
        return _CN_DIGITS[text]
    if "十" not in text:
        return None
    left, _, right = text.partition("十")
    tens = _CN_DIGITS.get(left, 1) if left else 1
    ones = _CN_DIGITS.get(right, 0) if right else 0
    if (left and left not in _CN_DIGITS) or (right and right not in _CN_DIGITS):
        return None
    return tens * 10 + ones


def _coerce_deadline(raw: Any) -> str:
    """把「明天下午」「今天下午四点」这类说法换成时间；真看不懂的原样交回去。"""
    text = " ".join(str(raw or "").split())
    if not text or parse_iso(text) is not None:
        return text
    text = _CN_HOUR.sub(lambda m: str(_cn_to_int(m.group(1)) or ""), text)
    now = now_local()
    if "后天" in text:
        day = now + dt.timedelta(days=2)
    elif "明天" in text:
        day = now + dt.timedelta(days=1)
    elif "今天" in text:
        day = now
    else:
        day = now  # 只说「下午四点」就是今天，不用先回宿舍
    hour, minute = 23, 59
    after_noon = False
    if "上午" in text:
        hour, minute = 9, 0
    elif "中午" in text:
        hour, minute = 12, 0
    elif "下午" in text:
        hour, minute = 15, 0
        after_noon = True
    elif "晚上" in text or "傍晚" in text:
        hour, minute = 20, 0
        after_noon = True
    matched = re.search(r"(\d{1,2})\s*(?:[:：点时])\s*(\d{1,2})?", text)
    if matched:
        hour = int(matched.group(1))
        minute = int(matched.group(2) or 0)
        if not matched.group(2) and re.search(r"[点时]\s*半", text):
            minute = 30
        if after_noon and hour < 12:
            hour += 12
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return text
    elif day is now and "今天" not in text:
        # 没有钟点也没有日期：原样交回去，让调用方问清楚，别自己编一个时间。
        return text
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return text
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")


def _say_from(outcomes: list[dict[str, Any]]) -> str:
    details = [str(item.get("detail") or "") for item in outcomes if item.get("detail")]
    return "。".join(details)


def _asks_a_question(text: str) -> bool:
    return any(piece in text for piece in ("？", "?", "吗", "几件", "多少", "什么", "怎么", "哪", "几点"))


def _local_fallback(
    store: Any,
    settings: Any,
    raw: str,
    *,
    already_changed: bool,
    outcomes: list[dict[str, Any]],
) -> dict[str, Any]:
    """模型通道不通时的本地兜底：提事就真落一条待办，查事就报本地数出来的件数。

    复用现有本地能力：`_PLAN` 词判「是在交代一件事」，`create_task` 落库，
    `agent_brief` 数今天有几件。厂商原文一个字都不往外露。
    """
    step = {"kind": "fallback", "text": _LOCAL_STEP}
    if already_changed:
        return {"say": _DOWN_DONE + (_say_from(outcomes) or "改动已生效"), "changed": True, "trace": [step]}
    # 兜底时宁可多记一条（用户看得见、点得掉），也不假装回答；只有明显在问才走「数件数」。
    if any(piece in raw for piece in _PLAN) or not _asks_a_question(raw):
        deadline = _coerce_deadline(raw)
        call: dict[str, Any] = {"name": "create"}
        if parse_iso(deadline) is not None:
            call["deadline"] = deadline
        outcome = _run_call(store, settings, {}, call, allow_confirm=False, user_text=raw)
        step["detail"] = outcome.get("detail") or ""
        if outcome.get("changed"):
            return {"say": _DOWN_WRITE + str(outcome.get("detail") or ""), "changed": True, "trace": [step]}
        return {"say": _DOWN_WRITE.rstrip("：") + "这次没写进去：" + str(outcome.get("detail") or ""), "changed": False, "trace": [step]}
    brief = agent_brief(store.list_tasks(), now=now_local())
    step["detail"] = "今天 %s 件" % brief["count"]
    return {"say": _DOWN_READ + str(brief.get("sentence") or ""), "changed": False, "trace": [step]}


_RULE_STEP = "没有模型，这句按本地规则办的"
_RULE_DUNNO = "这句我按本地规则读不懂；放一把钥匙到 llm.key 我就能懂"
_WRITE_HEADS = ("记一件", "帮我记", "加一件", "记一下", "记上", "提醒我", "新建")
_QUERY_HINTS = ("还有哪些", "有哪些", "几件", "有没有", "什么事", "清单")


def _match_task(rows: list[dict[str, Any]], fragment: str) -> tuple[dict[str, Any] | None, str]:
    """本地规则只在能唯一定位时动手；对不上、对上好几条都交给用户说清。"""
    frag = " ".join(str(fragment or "").split())
    if not frag:
        return None, "没说清是哪一件，补上标题里的几个字"
    hits = [row for row in rows if frag in str(row.get("summary") or "")]
    if len(hits) == 1:
        return hits[0], ""
    if not hits:
        return None, "本地规则没找到「%s」这条" % frag
    return None, "本地规则对上 %d 条「%s」，说清是哪一件" % (len(hits), frag)


def _no_model_turn(store: Any, settings: Any, sid: str, raw: str, history: list[dict[str, str]]) -> dict[str, Any]:
    """没有模型（没钥匙、或关掉了）时的窄口径本地规则：只认查、提、改三类。

    trace 和 say 都自报「本地规则」，不许把这句算成模型的决定。
    """
    trace: list[dict[str, Any]] = [{"kind": "rule", "text": _RULE_STEP}]
    local: dict[str, Any] = {"pending": "", "kept": True}
    rows = _snapshot_rows(store)
    say, changed = "", False
    resched = re.match(r"^把\s*(.+?)\s*(?:改到|改期到|挪到|推迟到)\s*(.+)$", raw)
    finished = re.match(r"^(?:把)?\s*(.+?)\s*(?:做完了|完成了|办完了)$", raw)
    head = next((item for item in _WRITE_HEADS if raw.startswith(item)), "")
    # 「立德楼附近有什么」「今天几趟路」这类问题不写库，但要真去调工具，别一句「读不懂」把人挡回去。
    near = re.match(r"^(.{2,12}?)(?:附近|周边|周围)(?:有(?:什么|啥|哪些)?|的(?:地点|地方))?[？?。!\s]*$", raw)
    trip_ask = any(word in raw for word in ("几趟", "行程", "怎么安排", "怎么走", "顺路", "路线"))
    if resched:
        task, error = _match_task(rows, resched.group(1))
        deadline = _coerce_deadline(resched.group(2))
        if task is None:
            say = error + "，这次没改。"
        elif parse_iso(deadline) is None:
            # 「下午四点」这种本地规则先换成 16:00；换不出来的问清楚，别把内部校验的话端给用户。
            say = "「%s」这个时间我没算出来，说个钟点，比如「今天 16:00」。" % resched.group(2)
        else:
            outcome = _run_call(
                store, settings, local,
                {"name": "reschedule", "task_id": task.get("id"), "deadline": deadline},
                allow_confirm=False, user_text=raw,
            )
            trace.append(_call_trace({"name": "reschedule"}, outcome))
            say, changed = str(outcome.get("detail") or ""), bool(outcome.get("changed"))
    elif finished:
        task, error = _match_task(rows, finished.group(1))
        if task is None:
            say = error + "，这次没动。"
        else:
            outcome = _run_call(
                store, settings, local,
                {"name": "act", "task_id": task.get("id"), "action": "done"},
                allow_confirm=False, user_text=raw,
            )
            trace.append(_call_trace({"name": "act"}, outcome))
            say, changed = str(outcome.get("detail") or ""), bool(outcome.get("changed"))
    elif head:
        title = raw[len(head):].lstrip("：:,， ")
        if len(title) < 2:
            # 「记一件事」这种剥完只剩一个字的，宁可整句当标题，也别建一条叫「事」的待办。
            title = raw
        deadline = _coerce_deadline(raw)
        call: dict[str, Any] = {"name": "create", "summary": title or raw}
        if parse_iso(deadline) is not None:
            call["deadline"] = deadline
        outcome = _run_call(store, settings, local, call, allow_confirm=False, user_text=raw)
        trace.append(_call_trace(call, outcome))
        say, changed = str(outcome.get("detail") or ""), bool(outcome.get("changed"))
        if changed:
            try:
                advice, step = _route_advice(store, settings, [str(outcome.get("id"))])
            except Exception as error:  # noqa: BLE001 - 排程出错不该把这一轮对话弄挂
                LOGGER.warning("排程建议算不出来：%s", error)
            else:
                trace.append(step)
                say = _with_advice(say, advice)
    elif near is not None:
        place = near.group(1).strip()
        outcome = _run_call(
            store, settings, local,
            {"name": "poi_near", "place": place, "radius_m": 500},
            allow_confirm=False, user_text=raw,
        )
        trace.append(_call_trace({"name": "poi_near"}, outcome))
        say = str(outcome.get("detail") or "")
        places = (outcome.get("result") or {}).get("places") or []
        if places:
            say += "。由近到远：" + "、".join(
                "%s %s 米" % (item.get("name"), item.get("distance_m")) for item in places[:3]
            )
    elif trip_ask:
        outcome = _run_call(store, settings, local, {"name": "day_plan"}, allow_confirm=False, user_text=raw)
        trace.append(_call_trace({"name": "day_plan"}, outcome))
        say = str(outcome.get("detail") or "")
        trips = (outcome.get("result") or {}).get("trips") or []
        if trips:
            stops = trips[0].get("stops") or []
            # 行程里的站点字段叫 place（页面那张卡也是这么读的），别拿 name 猜。
            say += "。头一趟 %s 出发：%s" % (
                (stops[0].get("arrive") if stops else "") or "",
                "→".join(str(item.get("place") or "") for item in stops),
            )
    elif any(hint in raw for hint in _QUERY_HINTS):
        open_rows = [row for row in rows if str(row.get("status") or "") in {"open", "candidate"}]
        nearest = min((row for row in open_rows if row.get("deadline")), key=lambda row: str(row.get("deadline")), default=None)
        say = "本地规则数出来：还有 %d 件没办" % len(open_rows)
        if nearest is not None:
            say += "，最近一件「%s」%s 截止" % (nearest.get("summary"), nearest.get("deadline"))
        elif open_rows:
            say += "，都没填截止时间"
        trace.append({"kind": "read", "text": "本地规则读了 %d 件待办" % len(rows)})
    if not say:
        say = _RULE_DUNNO
    with _LOCK:
        kept = _SESSIONS.setdefault(sid, local)
        kept["history"] = (history + [{"role": "user", "content": raw}, {"role": "assistant", "content": say}])[-_HISTORY * 2 :]
        kept["pending"] = ""
    return {"ok": True, "fallback": True, "say": say, "changed": changed, "session_id": sid, "trace": trace}


def _with_advice(say: str, advice: str) -> str:
    """把顺路那句接在后面：前一句已经收过尾了就不再补终止符，别拼出「？。」。"""
    if not say:
        return advice
    tail = say.rstrip()
    if tail and tail[-1] in _TERMINATORS:
        return tail + advice
    return tail + "。" + advice


def _route_advice(store: Any, settings: Any, created: list[str]) -> tuple[str, dict[str, Any]]:
    """建单后回一句顺不顺路。地点、时刻、米数都从 today_plan 的返回值里取，不另算。"""
    plan = today_plan(store, settings)
    for trip_index, trip in enumerate(plan.get("trips") or [], start=1):
        stops = trip.get("stops") or []
        for index, stop in enumerate(stops):
            if str(stop.get("task_id")) not in created:
                continue
            previous = stops[index - 1] if index else None
            if previous is not None:
                text = "已经排进今天第 %d 趟：接着%s走，%s 到%s。" % (
                    trip_index, previous.get("place"), stop.get("arrive"), stop.get("place"),
                )
            else:
                text = "已经排进今天第 %d 趟：%s 先到%s。" % (
                    trip_index, stop.get("arrive"), stop.get("place"),
                )
            step = {
                "kind": "plan",
                "text": "排程：排进第 %d 趟第 %d 站，%s 到（%s）" % (
                    trip_index, index + 1, stop.get("arrive"), stop.get("why") or "按顺路算",
                ),
            }
            return text, step
    for item in plan.get("unscheduled") or []:
        if str(item.get("task_id")) not in created:
            continue
        reason = str(item.get("reason") or "")
        if "没认出地点" in reason:
            text = "今天排不进去了：「%s」的地点不在校园地点表里。" % item.get("title")
        else:
            text = "今天排不进去了：「%s」%s。" % (item.get("title"), reason)
        return text, {"kind": "plan", "text": "排程：这件没排进今天（%s）" % (reason or "没有可排的地点")}
    return "今天排不进去了：这件没有可排的地点。", {"kind": "plan", "text": "排程：这件没进今天的行程"}


def _call_key(call: dict[str, Any]) -> str:
    """同一个工具、同一组参数 = 同一件事，一轮里只做一次。"""
    args = {k: v for k, v in call.items() if k != "name"}
    return json.dumps([str(call.get("name") or ""), args], ensure_ascii=False, sort_keys=True)


def _call_trace(call: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "call",
        "tool": str(call.get("name") or ""),
        "args": {k: v for k, v in call.items() if k != "name"},
        "ok": bool(outcome.get("ok")),
        "detail": str(outcome.get("detail") or ""),
        **({"result": outcome["result"]} if "result" in outcome else {}),
    }


def _lesson_note(settings: Any, now: dt.datetime) -> str:
    """读的时候顺手报一句课表：排程靠它算空档，说一句才知道它真读了。"""
    try:
        from noticehub.application.agent_plan import schedule_payload

        lessons = schedule_payload(settings).get("lessons") or []
    except Exception:  # noqa: BLE001 - 课表读不到不该拖垮整轮对话
        return ""
    today = [
        item
        for item in lessons
        if isinstance(item, dict) and str(item.get("weekday") or "") == str(now.isoweekday())
    ]
    if not today:
        return "、今天没课"
    ends = sorted(str(item.get("end") or "") for item in today if str(item.get("end") or ""))
    rest = [end for end in ends if end >= now.strftime("%H:%M")]
    if not rest:
        return "、今天的课都上完了"
    return "、今天还有 %d 节课（最后一节 %s 下课）" % (len(rest), rest[-1])


def _payload(settings: Any, messages: list[dict[str, str]]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": str(getattr(settings, "dashscope_model", "") or "qwen-plus"),
        "temperature": 0.2,
        "messages": messages,
    }
    if str(getattr(settings, "llm_backend", "") or "").strip().lower() == "ollama":
        # 本机 Ollama 上的 qwen3.x 默认开着「思考」，一句话能磨掉将近一分钟，
        # 而这轮只让它挑工具、回一句话：关掉思考，答案反而更快出来。
        payload["reasoning_effort"] = "none"
    return payload


def _post(settings: Any, messages: list[dict[str, Any]], *, timeout: int | None = None) -> str:
    endpoint = str(getattr(settings, "dashscope_endpoint", "") or "")
    if str(getattr(settings, "llm_backend", "") or "").strip().lower() == "ollama" and not endpoint:
        endpoint = "http://127.0.0.1:11434/v1/chat/completions"
    data = json.dumps(_payload(settings, messages), ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    key = str(getattr(settings, "dashscope_api_key", "") or "")
    if key:
        headers["Authorization"] = "Bearer " + key
    request = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")
    # 本机大模型冷启动（把 35B 装进显存）实测要一分钟上下，25 秒的云端上限会把第一轮直接掐掉。
    # 本机模型固定用足 cap：通用的 llm_timeout 默认 60，第一次对话正好卡在装显存那一段，
    # 实测第一句 60.6 秒被判超时。云端仍按 llm_timeout 收紧，省得一条请求吊太久。
    local_model = str(getattr(settings, "llm_backend", "") or "").strip().lower() == "ollama"
    cap = 90 if local_model else _CALL_TIMEOUT
    budget = cap if local_model else min(cap, int(getattr(settings, "llm_timeout", cap) or cap))
    if timeout is not None:
        # 读图比读一句话重得多（本地 35B 认一张课表实测 94.8 秒），调用方要能单独放宽。
        budget = max(15, int(timeout))
    try:
        with urllib.request.urlopen(request, timeout=budget) as response:  # noqa: S310
            parsed = json.loads(response.read().decode("utf-8", "replace"))
        return str(parsed["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("模型接口返回结构不认识") from error
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            payload = json.loads(error.read(8192).decode("utf-8", "replace"))
            reason = payload.get("error") if isinstance(payload, dict) else None
            message = reason.get("message") if isinstance(reason, dict) else None
            if isinstance(message, str):
                # 先脱敏再截断，避免在长度边界留下半截密钥。
                if key:
                    for secret in (key, urllib.parse.quote(key, safe=""), urllib.parse.quote_plus(key)):
                        message = message.replace(secret, "***")
                detail = " ".join(sanitize(message).split())[:240]
        except (ValueError, OSError):
            pass  # 非 JSON、读失败时仍保留原 HTTP 状态码。
        finally:
            error.close()
        # 厂商正文只进日志：页面和接口里只留状态码，别把上游英文原文端给用户。
        LOGGER.warning("模型请求失败：HTTP %s %s", error.code, detail)
        raise RuntimeError("模型请求失败：HTTP %s" % error.code) from error
    except urllib.error.URLError as error:
        raise RuntimeError("模型请求失败：%s" % error) from error
    except (TimeoutError, OSError) as error:
        # 读超时抛的是裸 TimeoutError，不是 URLError：漏掉它会让这一轮直接断在
        # socket 上（页面上是「空回复」），所以这里也要换成一句人话。
        raise RuntimeError("模型太慢，这一轮没等到回应：%s" % error) from error


def ask_vision(settings: Any, prompt: str, data_url: str) -> str | None:
    """让模型读一张图（data URL）。读不成返回 None：厂商原文只进日志，一个字都不上页面。

    本地 35B 读一张课表截图实测 94.8 秒，比一句话慢得多，所以这条路径单独给 180 秒，
    不跟着对话的 90 秒走。"""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "你只按用户要求的格式抄写图片里的内容，不补造、不解释。"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        },
    ]
    try:
        return _post(settings, messages, timeout=180)
    except Exception as error:  # noqa: BLE001 - 读图失败一律降级成「没读到」，不往上抛栈
        LOGGER.warning("读图没成功，按没读到处理：%s", error)
        return None


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
