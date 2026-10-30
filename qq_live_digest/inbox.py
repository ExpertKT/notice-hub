"""Classify stored messages into notice, suspect, or noise and promote them."""
from __future__ import annotations
import json
import logging
import urllib.request
from types import SimpleNamespace
from typing import Any
from .llm_json import parse_json
from .summarizer import is_notice_item, is_quiet_group_item
from .group_suggest import _local_model

LOGGER = logging.getLogger(__name__)

def _heuristic(row, settings):
    item = dict(row)
    item["message"] = SimpleNamespace(text=str(row.get("content", "")))
    if is_notice_item(item, settings): return "notice", "已有通知判定命中"
    if is_quiet_group_item(item, settings) or len(str(row.get("content", "")).strip()) < 5: return "noise", "安静群或短闲聊"
    return "suspect", "疑似通知，需人工确认"

def _call(client, settings, rows):
    prompt = "将消息分类为 notice/suspect/noise，返回 JSON 数组，每项含 msg_id,verdict,reason。\n" + json.dumps([{"msg_id":r["msg_id"],"content":r["content"]} for r in rows], ensure_ascii=False)
    if client: return client(prompt)
    backend = str(getattr(settings, "llm_backend", "auto") or "auto").strip().lower()
    if backend not in {"auto", "ollama", "openai", "codebuddy"}:
        raise RuntimeError(f"不支持的 LLM backend: {backend}")
    # 保险：auto 且 LLM 关着时绝不去起 codebuddy CLI（起不来会一直等到超时）。
    # 显式选 codebuddy 的用户意图明确，仍按原逻辑走；其它 backend 与以前一致。
    try_codebuddy = backend == "codebuddy" or (
        backend == "auto" and bool(getattr(settings, "llm_enabled", False))
    )
    if try_codebuddy:
        import qq_digest
        schema = getattr(qq_digest, "_CODEBUDDY_SCHEMA", '{"type":"array"}')
        try:
            data = qq_digest.run_codebuddy(
                prompt,
                schema,
                cli=str(getattr(settings, "codebuddy_cli", "") or ""),
                timeout=int(getattr(settings, "llm_timeout", 120) or 120),
            )
        except RuntimeError:
            if backend == "codebuddy":
                raise
        else:
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                data = data["items"]
            return json.dumps(data, ensure_ascii=False) if isinstance(data, (dict, list)) else str(data)
    model = _local_model(settings) if backend == "ollama" else settings.dashscope_model
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0, "reasoning_effort": "none", "max_tokens": max(900, len(rows) * 140)}
    headers = {"Content-Type": "application/json"}
    key = str(getattr(settings, "dashscope_api_key", "") or "")
    if key: headers["Authorization"] = "Bearer " + key
    request = urllib.request.Request(settings.dashscope_endpoint, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=float(getattr(settings, "llm_timeout", 120))) as response:
        message = json.loads(response.read())["choices"][0]["message"]
        return message.get("content") or message.get("thinking") or ""

def classify_messages(settings, store, *, since=None, until=None, groups=None, limit=1000, client=None, progress=None, cancel=None):
    """notice=确定有事项/时间/要求；suspect=像通知但信息不全；noise=不是通知。

    ``llm_enabled`` 为假时整段跳过 LLM：不问 codebuddy、不发 HTTP，直接启发式。
    否则 ``llm_backend=auto`` 会先去起 codebuddy CLI，CLI 起不来就卡到 llm_timeout
    （默认 120s）× 每批 2 次重试，用户看到的是「正在启动」几百秒不动。
    显式传 ``client`` 属于调用方点名要用，照旧执行。
    """
    rows=store.unmarked_messages(since=since, until=until, groups=groups, limit=limit); counts={"notice":0,"suspect":0,"noise":0}; methods=[]
    use_llm = client is not None or bool(getattr(settings, "llm_enabled", False))
    llm_error = ""

    def _method() -> str:
        if methods and all(x == "heuristic" for x in methods): return "heuristic"
        if methods and all(x == "llm" for x in methods): return "llm"
        return "mixed" if methods else "heuristic"

    def _cancelled() -> dict:
        # 已落库的保留；不报错，由调用方把状态标成 cancelled。
        return {"ok":True,"cancelled":True,"classified":sum(counts.values()),"counts":counts,"method":_method(),"error":"","llm_error":llm_error}

    for start in range(0,len(rows),20):
        if cancel and cancel(): return _cancelled()
        batch=rows[start:start+20]; parsed=None
        if use_llm:
            for _ in range(2):
                if cancel and cancel(): return _cancelled()
                try: parsed=parse_json(_call(client,settings,batch), expect="array"); break
                except Exception as exc:
                    LOGGER.warning("LLM 判定第 %d 批失败，本批回退关键词判定：%s", start//20+1, exc)
                    if not llm_error: llm_error = str(exc) or exc.__class__.__name__
        if parsed is None:
            methods.append("heuristic"); results=[(r["msg_id"], *_heuristic(r,settings)) for r in batch]
        else:
            ids = {str(r["msg_id"]) for r in batch}
            by_id = {str(x.get("msg_id")): x for x in parsed if str(x.get("msg_id", "")) in ids}
            missing = [r for r in batch if str(r["msg_id"]) not in by_id]
            for offset in range(0, len(missing), 8):
                retry_batch = missing[offset:offset + 8]
                if not retry_batch: break
                if cancel and cancel(): break  # 停掉本批补齐；本批已拿到的结果照常落库
                try:
                    retry = parse_json(_call(client, settings, retry_batch), expect="array")
                    retry_ids = {str(r["msg_id"]) for r in retry_batch}
                    by_id.update({str(x.get("msg_id")): x for x in retry if str(x.get("msg_id", "")) in retry_ids})
                except Exception as exc:
                    LOGGER.warning("LLM 判定第 %d 批补齐失败，回退关键词判定：%s", start//20+1, exc)
                    if not llm_error: llm_error = str(exc) or exc.__class__.__name__
            methods.append("llm" if len(by_id) == len(batch) else "mixed")
            results = []
            for row in batch:
                item = by_id.get(str(row["msg_id"]))
                verdict, reason = ((str(item.get("verdict", "suspect")), str(item.get("reason", ""))) if item else _heuristic(row, settings))
                results.append((str(row["msg_id"]), verdict, reason))
        for msg_id, verdict, reason in results:
            if verdict not in counts: verdict="suspect"
            store.save_mark(msg_id,verdict,reason,methods[-1]); counts[verdict]+=1
        if progress: progress(len(batch),len(rows))
    return {"ok":True,"classified":sum(counts.values()),"counts":counts,"method":_method(),"error":"","llm_error":llm_error}

def promote(settings, store, msg_id, *, title=None, due=None):
    items=store.inbox_items(limit=100000,q=None); row=next((x for x in items if x["msg_id"]==str(msg_id)),None)
    if not row: return {"ok":False,"task_id":None,"error":"消息不存在或未分类"}
    existing=store.inbox_items(limit=1000); old=next((x for x in existing if x["msg_id"]==str(msg_id) and x.get("task_id")),None)
    if old: return {"ok":True,"task_id":old["task_id"],"error":""}
    # ponytail: 与 service._sync_tasks 保持同一 task_key（裸 msg_id），否则同一条消息
    # 先被自动建任务、再 promote 会生成第二条任务。
    task_id=store.upsert_task(task_key=str(msg_id),summary=title or row["content"],deadline=due or "",groups=[row["group_name"]],sender=row["sender_name"],evidence=row["content"],source="inbox")
    store.save_mark(msg_id,row["verdict"],row["reason"],row["method"],task_id)
    return {"ok":bool(task_id),"task_id":task_id,"error":"" if task_id else "任务创建失败"}
