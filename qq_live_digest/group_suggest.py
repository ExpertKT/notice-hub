"""Suggest QQ groups to subscribe to using the configured LLM with safe heuristics fallback."""
from __future__ import annotations

import copy
import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any


_CACHE_TTL = 600.0
_cache_lock = threading.Lock()
_model_probe: dict[str, tuple[float, str]] = {}
_result_cache: dict[tuple[tuple[str, ...], str, str], tuple[float, dict[str, Any]]] = {}


def _clear_caches() -> None:
    """Reset process-local caches; intended for tests and controlled reloads."""
    with _cache_lock:
        _model_probe.clear()
        _result_cache.clear()


def _local_model(settings: Any) -> str:
    endpoint = str(getattr(settings, "dashscope_endpoint", "") or "")
    fallback = str(getattr(settings, "dashscope_model", "") or "")
    now = time.monotonic()
    with _cache_lock:
        cached = _model_probe.get(endpoint)
        if cached and now - cached[0] < _CACHE_TTL:
            return cached[1]
        if cached:
            _model_probe.pop(endpoint, None)
    try:
        tags_url = endpoint.split("/v1/", 1)[0].rstrip("/") + "/api/tags"
        request = urllib.request.Request(tags_url, method="GET")
        with urllib.request.urlopen(request, timeout=min(int(getattr(settings, "llm_timeout", 60) or 60), 10)) as response:
            models = json.loads(response.read().decode()).get("models", [])
        names = [str(item.get("name") or item.get("model") or "") for item in models if isinstance(item, dict)]
        small = next((name for name in names if "qwen3.5" in name.lower() and "9b" in name.lower()), "")
        selected = small or fallback
    except Exception:
        return fallback
    with _cache_lock:
        _model_probe[endpoint] = (now, selected)
    return selected

from .llm_json import parse_json

KEYWORDS = {
    "course": ("课程", "教务", "学院", "班级", "专业", "考试", "上课", "作业", "通知", "年级"),
    "activity": ("社团", "活动", "协会", "讲座", "比赛", "招新", "志愿"),
    "market": ("二手", "交易", "闲置", "买卖", "出物"),
    "chat": ("闲聊", "聊天", "水群", "摸鱼", "交流群"),
}
CATEGORIES = ("course", "activity", "market", "chat", "other")


def _heuristic(groups: list[dict[str, Any]], error: str = "") -> dict[str, Any]:
    output = []
    for group in groups:
        gid = str(group.get("group_id") or group.get("id") or "")
        name = str(group.get("group_name") or group.get("name") or gid)
        category = "other"
        for candidate in CATEGORIES[:-1]:
            if any(word in name for word in KEYWORDS[candidate]):
                category = candidate
                break
        output.append({"group_id": gid, "name": name, "category": category,
                       "suggested": category in {"course", "activity"},
                       "reason": {"course": "课程/教学通知", "activity": "社团/活动通知", "market": "二手/交易信息", "chat": "聊天交流", "other": "未识别"}[category]})
    return _result(output, "heuristic", error)


def _result(items: list[dict[str, Any]], source: str, error: str = "") -> dict[str, Any]:
    counts = {category: 0 for category in CATEGORIES}
    for item in items:
        counts[item["category"]] += 1
    return {"ok": True, "source": source, "groups": items, "counts": counts, "error": error}


def _llm(settings: Any, groups: list[dict[str, Any]], sample: dict[str, list[str]] | None) -> list[dict[str, Any]]:
    import qq_digest
    prompt = {"groups": [{"group_id": str(g.get("group_id") or g.get("id") or ""), "name": str(g.get("group_name") or g.get("name") or ""), "sample": (sample or {}).get(str(g.get("group_id") or g.get("id") or ""), [])[:3]} for g in groups]}
    instruction = ('仅输出严格 JSON 对象 {"groups":[{"group_id":"","category":"course|activity|market|chat|other","reason":""}]}。'
                   '只按群名和样本分类，不要猜测；course/activity 建议订阅。输入：' + json.dumps(prompt, ensure_ascii=False))
    backend = str(getattr(settings, "llm_backend", "auto") or "auto").lower()
    codebuddy_error = ""
    data = None
    if backend in {"auto", "codebuddy"}:
        schema = {"type": "object", "properties": {"groups": {"type": "array", "items": {"type": "object"}}}, "required": ["groups"]}
        try:
            data = qq_digest.run_codebuddy(instruction, json.dumps(schema, ensure_ascii=False), cli=getattr(settings, "codebuddy_cli", ""), timeout=settings.llm_timeout)
            if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
                raise ValueError("CodeBuddy 返回缺少 groups")
        except Exception as exc:
            if backend == "codebuddy":
                raise
            codebuddy_error = f"CodeBuddy: {exc}"
            data = None
    if backend not in {"codebuddy"} and data is None:
        merged = []
        model = settings.dashscope_model if backend == "openai" else _local_model(settings)
        batch_groups = [groups[index:index + 12] for index in range(0, len(groups), 12)] or [[]]
        try:
            for batch in batch_groups:
                batch_prompt = {"groups": [{"group_id": str(g.get("group_id") or g.get("id") or ""), "name": str(g.get("group_name") or g.get("name") or ""), "sample": (sample or {}).get(str(g.get("group_id") or g.get("id") or ""), [])[:3]} for g in batch]}
                batch_instruction = instruction.split("输入：", 1)[0] + "输入：" + json.dumps(batch_prompt, ensure_ascii=False)
                last_error = None
                for _attempt in range(2):
                    try:
                        request = urllib.request.Request(settings.dashscope_endpoint, data=json.dumps({"model": model, "messages":[{"role":"user","content":batch_instruction}], "temperature":0, "reasoning_effort":"none", "max_tokens":900}, ensure_ascii=False).encode(), headers={"Authorization": "Bearer " + str(settings.dashscope_api_key), "Content-Type":"application/json"}, method="POST")
                        with urllib.request.urlopen(request, timeout=settings.llm_timeout) as response:
                            body = json.loads(response.read().decode())
                            message = body["choices"][0]["message"]
                            content = message.get("content") or message.get("thinking") or ""
                            parsed = parse_json(content)
                            raw_batch = parsed.get("groups") if isinstance(parsed, dict) else parsed
                            if not isinstance(raw_batch, list):
                                raise ValueError("LLM 返回缺少 groups")
                            merged.extend(raw_batch)
                            last_error = None
                            break
                    except Exception as exc:
                        last_error = exc
                if last_error is not None:
                    raise last_error
            data = {"groups": merged}
        except Exception as exc:
            raise RuntimeError(f"{codebuddy_error}; 本机模型: {exc}" if codebuddy_error else f"本机模型: {exc}") from exc
    raw = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise ValueError("LLM 返回缺少 groups")
    by_id = {str(g.get("group_id") or g.get("id") or ""): g for g in groups}
    result = []
    for item in raw:
        gid = str(item.get("group_id") or "")
        category = str(item.get("category") or "other").lower()
        if gid not in by_id or category not in CATEGORIES:
            raise ValueError("LLM 返回非法群分类")
        g = by_id[gid]; name = str(g.get("group_name") or g.get("name") or gid)
        result.append({"group_id": gid, "name": name, "category": category, "suggested": category in {"course", "activity"}, "reason": str(item.get("reason") or "LLM 分类")})
    if len(result) != len(groups):
        raise ValueError("LLM 未覆盖全部群")
    return result


def suggest(settings: Any, groups: list[dict], *, sample: dict[str, list[str]] | None = None) -> dict:
    groups = list(groups or [])
    if not bool(getattr(settings, "llm_enabled", False)):
        return _heuristic(groups, "LLM 未启用")
    backend = str(getattr(settings, "llm_backend", "auto") or "auto").lower()
    model = str(getattr(settings, "dashscope_model", "") or "")
    if backend not in {"codebuddy", "openai"}:
        model = _local_model(settings)
    key = (tuple(sorted(str(g.get("group_id") or g.get("id") or "") for g in groups)), backend, model)
    now = time.monotonic()
    with _cache_lock:
        cached = _result_cache.get(key)
        if cached and now - cached[0] < _CACHE_TTL:
            return copy.deepcopy(cached[1])
        if cached:
            _result_cache.pop(key, None)
    try:
        result = _result(_llm(settings, groups, sample), "llm", "")
        with _cache_lock:
            _result_cache[key] = (now, copy.deepcopy(result))
            while len(_result_cache) > 8:
                oldest = min(_result_cache, key=lambda item: _result_cache[item][0])
                _result_cache.pop(oldest, None)
        return result
    except Exception as exc:  # LLM is optional; group selection must never fail.
        return _heuristic(groups, str(exc))
