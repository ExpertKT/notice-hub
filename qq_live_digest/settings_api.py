"""settings 域（/api/settings*）的写设置实现。

只放「把 payload 落成 .env + 热更新当前 Settings」的逻辑；HTTP 参数校验与响应仍由
webapp 的 Handler 负责。SettingsPostMixin 由 webapp._Handler 继承，
方法名与 routes.py 里的路由表一致。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .chaoxing_receiver import chaoxing_status
from .config import DEFAULT_VISION_MODEL, Settings, split_list, update_env_file
from .updates_api import _read_updates_state, _updates_state_path, _write_updates_state
from .wechat_receiver import parse_manual_key as wechat_parse_manual_key
from .wechat_receiver import clear_storage_cache as wechat_clear_storage_cache

try:  # 与 webapp.py 同款兜底：托管模块缺失时降级为「托管模块不可用」，不能把整站拖垮
    from . import hosting
except Exception:  # noqa: BLE001
    hosting = None  # type: ignore[assignment]


def _webapp_module():
    """webapp 模块对象：只在调用时取。webapp import 本模块时还没跑完，导入期拿不到。"""
    return sys.modules[__package__ + ".webapp"]

LOGGER = logging.getLogger(__name__)

# 请求体上限：/api/settings 用它做超大 body 早拒（webapp 其余路由共用同一数值）。
MAX_BODY_BYTES = 64 * 1024

def _clean_chaoxing_cookie(value: Any) -> str:
    """Normalize a manually copied DevTools Cookie string without changing values."""
    cookie = str(value or "").replace("\r", "").replace("\n", "").strip()
    if cookie.startswith(("'", '"')) and cookie.endswith(cookie[0]) and len(cookie) >= 2:
        cookie = cookie[1:-1].strip()
    cookie = re.sub(r"^(?:document\.cookie|Cookie:)\s*(?:=\s*)?", "", cookie, flags=re.IGNORECASE).strip()
    if cookie.startswith(("'", '"')) and cookie.endswith(cookie[0]) and len(cookie) >= 2:
        cookie = cookie[1:-1].strip()
    return cookie

def _invalidate_update_cache(server: ThreadingHTTPServer) -> None:
    # updates/state.json 的读改写统一归 state_lock（公告刷新、_persist_update 都是它）；
    # 原来这里用 update_lock，两把锁各读改写整文件会互相覆盖，可能把刚失效的 update_result 写回来。
    with server.state_lock:  # type: ignore[attr-defined]
        state_path = _updates_state_path(server)
        state = _read_updates_state(state_path)
        state.pop("update_result", None)
        state.pop("update_checked_epoch", None)
        _write_updates_state(state_path, state)

_PUSH_SETTING_ENV_KEYS = {
    "wxpusher_app_token": "WXPUSHER_APP_TOKEN",
    "wxpusher_uids": "WXPUSHER_UIDS",
    "wxpusher_topic_ids": "WXPUSHER_TOPIC_IDS",
    "serverchan_keys": "SERVERCHAN_KEYS",
    "pushplus_tokens": "PUSHPLUS_TOKENS",
    "webhook_urls": "QQ_DIGEST_WEBHOOKS",
}
_PUSH_SETTING_LABELS = {
    "wxpusher_app_token": "WxPusher App Token",
    "wxpusher_uids": "WxPusher UID",
    "wxpusher_topic_ids": "WxPusher Topic ID",
    "serverchan_keys": "Server 酱 SendKey",
    "pushplus_tokens": "PushPlus Token",
    "webhook_urls": "Webhook 地址",
}
# 每个通道「去哪里拿值」的官网入口。链接由页面用 JS 填进 <a>，
# 页面本身不写外链（保持离线可用、也避免把地址写死在 HTML 里）。
PUSH_HELP_LINKS = {
    "wxpusher": "https://wxpusher.zjiecode.com/admin/",
    "serverchan": "https://sct.ftqq.com/sendkey",
    "pushplus": "https://www.pushplus.plus/",
}


def _mask_push_secret(value: str) -> str:
    """只暴露尾 4 位，其余打码（短值全码）。"""
    text = str(value)
    if len(text) <= 4:
        return "*" * len(text)
    return "*" * (len(text) - 4) + text[-4:]


def _push_setting_values(payload: dict[str, Any], keys: set[str]) -> tuple[dict[str, str], dict[str, Any]]:
    """校验设置页提交的推送通道配置。

    返回 (写进 .env 的键值, 写回内存 Settings 的字段值)。任何一项非法就整体拒绝，
    调用方拿到 ValueError 后原样返回给页面，不做部分写入。
    """
    updates: dict[str, str] = {}
    applied: dict[str, Any] = {}
    for name in sorted(keys):
        raw = payload.get(name)
        if raw is None or isinstance(raw, (dict, bool, int, float)):
            raise ValueError(f"{_PUSH_SETTING_LABELS[name]}格式不正确")
        items = split_list(raw)
        if name == "wxpusher_app_token":
            if len(items) > 1:
                raise ValueError("WxPusher App Token 只能填一个，不要用逗号或空格分隔")
            value: Any = items[0] if items else ""
        elif name == "wxpusher_topic_ids":
            if any(not item.isdigit() or int(item) <= 0 for item in items):
                raise ValueError("WxPusher Topic ID 只能是正整数")
            value = tuple(int(item) for item in items)
        elif name == "webhook_urls":
            for item in items:
                if not item.lower().startswith(("http://", "https://")):
                    raise ValueError("Webhook 地址必须以 http:// 或 https:// 开头")
            value = items
        else:
            value = items
        if isinstance(value, str):
            updates[_PUSH_SETTING_ENV_KEYS[name]] = value
        else:
            updates[_PUSH_SETTING_ENV_KEYS[name]] = ",".join(str(item) for item in value)
        applied[name] = value
    return updates, applied


def _push_setting_state(settings: Settings) -> dict[str, Any]:
    """回给页面的脱敏状态：只给是否配置、数量与尾 4 位。"""
    state: dict[str, Any] = {}
    for name in _PUSH_SETTING_ENV_KEYS:
        value = getattr(settings, name, None)
        items = [str(item) for item in value] if isinstance(value, (tuple, list)) else ([str(value)] if value else [])
        state[name] = {
            "set": bool(items),
            "count": len(items),
            "masked": [_mask_push_secret(item) for item in items[:3]],
        }
    return state

def _llm_setting_state(settings: Settings) -> dict[str, Any]:
    """回给页面的脱敏状态：当前选的哪家、模型名、key 只给尾 4 位。"""
    key = str(settings.dashscope_api_key or "")
    return {
        "enabled": bool(settings.llm_enabled),
        "active": bool(settings.llm_active),
        "provider": _webapp_module()._llm_provider_id(settings),
        "endpoint": str(settings.dashscope_endpoint or ""),
        "model": str(settings.dashscope_model or ""),
        "vision": bool(settings.vision_active),
        "vision_model": str(settings.vision_model or ""),
        "key_set": bool(key),
        "key_masked": _mask_push_secret(key) if key else "",
        "timeout": int(settings.llm_timeout or 60),
    }


def _llm_setting_updates(payload: dict[str, Any], settings: Settings) -> tuple[dict[str, str], dict[str, Any]]:
    """校验设置页提交的 AI 接入配置，返回 (.env 更新, 写回内存的字段)。"""
    provider_id = str(payload.get("llm_provider") or "").strip()
    provider = _webapp_module()._llm_provider(provider_id)
    if provider is None:
        raise ValueError("请先选择一个 AI 服务商")
    raw_key = payload.get("llm_api_key")
    if raw_key is not None and isinstance(raw_key, (dict, bool, int, float)):
        raise ValueError("API Key 格式不正确")
    api_key = str(raw_key or "").strip()
    if provider_id == "off":
        return {"QQ_DIGEST_LLM": "0"}, {"llm_enabled": False}
    if provider.get("needs_key") and not (api_key or str(settings.dashscope_api_key or "").strip()):
        raise ValueError("还没填 API Key：点上面的链接拿到以后粘进来就行")
    endpoint = str(payload.get("llm_endpoint") or provider.get("endpoint") or "").strip()
    if not endpoint.lower().startswith(("http://", "https://")):
        raise ValueError("接口地址要以 http:// 或 https:// 开头")
    model = str(payload.get("llm_model") or provider.get("model") or "").strip()
    if not model:
        raise ValueError("还没填模型名：选好服务商以后它应该自动填上")
    vision_model = str(provider.get("vision_model") or "")
    updates = {
        "QQ_DIGEST_LLM": "1",
        "QQ_DIGEST_LLM_ENDPOINT": endpoint,
        "QQ_DIGEST_LLM_MODEL": model,
        "QQ_DIGEST_VL_MODEL": vision_model,
    }
    applied: dict[str, Any] = {
        "llm_enabled": True,
        "dashscope_endpoint": endpoint,
        "dashscope_model": model,
        "vision_model": vision_model or DEFAULT_VISION_MODEL,
    }
    if api_key:
        updates["QQ_DIGEST_LLM_API_KEY"] = api_key
        applied["dashscope_api_key"] = api_key
    return updates, applied


def _llm_test_error(error: Exception) -> str:
    """把各家服务商的报错翻译成用户看得懂的一句话。"""
    text = str(error) or error.__class__.__name__
    low = text.lower()
    if "dashscope_api_key" in text or "未配置" in text:
        return "还没有填钥匙：把服务商给你的那串 API Key 粘进去再试。"
    if "401" in low or "unauthorized" in low or "invalid api key" in low or "authentication" in low:
        return "钥匙不对或已经失效：回到拿钥匙的那个页面重新复制一次。"
    if "403" in low or "forbidden" in low:
        return "服务商拒绝了这次请求：确认钥匙有没有被停用，或者账号还没实名。"
    if "404" in low or ("model" in low and "not found" in low):
        return "服务商说不认识这个模型名：把模型名改回默认的试试。"
    if "429" in low or "rate limit" in low or "quota" in low or "insufficient" in low:
        return "额度用完或被限流了：换一个钥匙，或者过一会儿再试。"
    if "timed out" in low or "timeout" in low:
        return "等太久没回应：检查一下网络，本机 Ollama 的话确认模型已经下载完。"
    if "connection refused" in low or "10061" in text:
        return "连不上这个地址：本机 Ollama 要先启动，换服务商的话确认地址有没有写对。"
    return text[:200]

class SettingsPostMixin:
    # POST /api/settings/test-llm
    def _route_post_api_settings_test_llm(self, path: str) -> None:  # noqa: N802 - 路由处理
        settings = self.server.settings  # type: ignore[attr-defined]
        started = dt.datetime.now()
        model = str(settings.dashscope_model or "")
        try:
            from .attachments import chat_completion

            reply = chat_completion(
                settings,
                [{"role": "user", "content": "只回复三个字：已连通"}],
                model=model,
                timeout=min(30, max(10, int(settings.llm_timeout or 60))),
                max_tokens=16,
            )
        except Exception as error:  # noqa: BLE001 - 任何失败都原样翻译给页面
            self._json(200, {
                "ok": False,
                "error": _llm_test_error(error),
                "model": model,
                "endpoint": str(settings.dashscope_endpoint or ""),
            })
            return
        self._json(200, {
            "ok": True,
            "model": model,
            "reply": " ".join(str(reply or "").split())[:120],
            "seconds": round((dt.datetime.now() - started).total_seconds(), 1),
        })
        return

    # POST /api/settings/test-push
    def _route_post_api_settings_test_push(self, path: str) -> None:  # noqa: N802 - 路由处理
        settings = self.server.settings  # type: ignore[attr-defined]
        try:
            from .push import build_pushers
        except Exception as error:  # noqa: BLE001
            self._json(200, {"ok": False, "error": f"推送模块加载失败：{error}", "results": []})
            return
        try:
            pushers = build_pushers(settings)
        except Exception as error:  # noqa: BLE001
            self._json(200, {"ok": False, "error": f"推送通道初始化失败：{error}", "results": []})
            return
        if not pushers:
            self._json(200, {"ok": False, "error": "还没有配置任何推送通道", "results": []})
            return
        results: list[dict[str, Any]] = []
        for pusher in pushers:
            try:
                pusher.send("QQ 群消息台 · 测试消息", "看到这条消息，说明提醒已经能送到你手机上了。")
            except Exception as error:  # noqa: BLE001
                results.append({
                    "channel": str(getattr(pusher, "name", "通道")),
                    "target": str(getattr(pusher, "target", "")),
                    "ok": False,
                    "error": str(error)[:200] or "发送失败",
                })
                continue
            results.append({
                "channel": str(getattr(pusher, "name", "通道")),
                "target": str(getattr(pusher, "target", "")),
                "ok": True,
                "error": "",
            })
        self._json(200, {
            "ok": all(item["ok"] for item in results),
            "results": results,
        })
        return

    # POST /api/settings
    def _route_post_api_settings(self, path: str) -> None:  # noqa: N802 - 路由处理
        length = int(self.headers.get("Content-Length") or 0)
        LOGGER.info("settings 保存请求到达，body=%d bytes", length)
        if length > MAX_BODY_BYTES:
            self._reject_oversized_body(length); return
        if length < 0:
            self._json(400, {"ok": False, "error": "bad body"}); return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("settings must be a JSON object")
        except (ValueError, TypeError, UnicodeDecodeError):
            self._json(400, {"ok": False, "error": "bad json"}); return
        catchup_keys = {key for key in ("catchup_enabled", "catchup_hours") if key in payload}
        host_keys = {"quit_qq", "restore_qq", "quit_restore_qq", "auto_on_start", "autostart"}.intersection(payload)
        push_keys = set(_PUSH_SETTING_ENV_KEYS).intersection(payload)
        llm_keys = {"llm_provider", "llm_api_key", "llm_model", "llm_endpoint"}.intersection(payload)
        chaoxing_keys = {"chaoxing_enabled", "chaoxing_cookie", "chaoxing_group_whitelist", "chaoxing_clear_cookie"}.intersection(payload)
        dingtalk_keys = {"dingtalk_enabled", "dingtalk_group_whitelist"}.intersection(payload)
        wechat_keys = {"wechat_enabled", "wechat_group_whitelist", "wechat_group_aliases", "wechat_scan_interval_seconds", "wechat_key", "wechat_dir"}.intersection(payload)
        napcat_keys = {"napcat_dir"}.intersection(payload)
        lab_keys = {"lab_search", "lab_replies", "lab_events", "lab_rules"}.intersection(payload)
        telemetry_keys = {"community_telemetry"}.intersection(payload)
        update_channel_keys = {"update_channel"}.intersection(payload)
        if lab_keys and any((catchup_keys, host_keys, push_keys, llm_keys, chaoxing_keys, dingtalk_keys, wechat_keys, napcat_keys, update_channel_keys, telemetry_keys)):
            self._json(400, {"ok": False, "error": "实验室设置请单独保存"}); return
        if telemetry_keys and any((catchup_keys, host_keys, push_keys, llm_keys, chaoxing_keys, dingtalk_keys, wechat_keys, napcat_keys, update_channel_keys, lab_keys)):
            self._json(400, {"ok": False, "error": "社区在线数设置请单独保存"}); return
        if update_channel_keys and any((catchup_keys, host_keys, push_keys, llm_keys, chaoxing_keys, dingtalk_keys, wechat_keys, napcat_keys)):
            self._json(400, {"ok": False, "error": "更新通道设置请单独保存"}); return
        if catchup_keys and host_keys:
            self._json(400, {"ok": False, "error": "托管设置与回溯设置请分开保存"}); return
        if push_keys and (catchup_keys or host_keys):
            self._json(400, {"ok": False, "error": "推送通道设置请单独保存"}); return
        if llm_keys and (push_keys or catchup_keys or host_keys or chaoxing_keys or wechat_keys):
            self._json(400, {"ok": False, "error": "AI 接入设置请单独保存"}); return
        if dingtalk_keys and (push_keys or catchup_keys or host_keys or llm_keys or chaoxing_keys or wechat_keys):
            self._json(400, {"ok": False, "error": "钉钉设置请单独保存"}); return
        if chaoxing_keys and (push_keys or catchup_keys or host_keys or dingtalk_keys or wechat_keys):
            self._json(400, {"ok": False, "error": "学习通设置请单独保存"}); return
        if wechat_keys and (push_keys or catchup_keys or host_keys or llm_keys or dingtalk_keys or chaoxing_keys):
            self._json(400, {"ok": False, "error": "微信设置请单独保存"}); return
        if napcat_keys and (push_keys or catchup_keys or host_keys or llm_keys or dingtalk_keys or chaoxing_keys or wechat_keys):
            self._json(400, {"ok": False, "error": "NapCat 目录设置请单独保存"}); return
        try:
            if update_channel_keys:
                self._save_update_channel_settings(payload); return
            if lab_keys:
                self._save_lab_settings(payload); return
            if telemetry_keys:
                self._save_telemetry_settings(payload); return
            if napcat_keys:
                self._save_napcat_settings(payload); return
            if wechat_keys:
                self._save_wechat_settings(payload); return
            if dingtalk_keys:
                self._save_dingtalk_settings(payload); return
            if chaoxing_keys:
                self._save_chaoxing_settings(payload); return
            if llm_keys:
                self._save_llm_settings(payload); return
            if push_keys:
                self._save_push_settings(payload); return
            if catchup_keys:
                self._save_catchup_settings(payload); return
            self._save_hosting_settings(payload)
        except (ValueError, TypeError) as error:
            self._json(400, {"ok": False, "error": str(error)})
        except Exception as error:  # noqa: BLE001
            self._json(200, {"ok": False, "error": str(error) or "设置保存失败"})
        return

    # ---- 九个设置域：每个域一个保存方法，校验/写 .env/热更新都在方法内 ----

    def _save_update_channel_settings(self, payload: dict[str, Any]) -> None:
        settings = self.server.settings  # type: ignore[attr-defined]
        if not str(settings.env_file or ""):
            raise ValueError("找不到 .env 路径")
        raw_channel = payload["update_channel"]
        if not isinstance(raw_channel, str):
            raise ValueError("update_channel 必须是文本")
        channel = raw_channel.strip().lower()
        if channel not in {"stable", "beta"}:
            channel = ""
        result = update_env_file(Path(settings.env_file), {"QQ_DIGEST_UPDATE_CHANNEL": channel})
        if not result.get("ok"):
            raise ValueError(result.get("error") or ".env 写入失败")
        settings.update_channel = channel
        os.environ["QQ_DIGEST_UPDATE_CHANNEL"] = channel
        _invalidate_update_cache(self.server)
        self._json(200, {"ok": True, "update_channel": channel})
        return

    def _save_lab_settings(self, payload: dict[str, Any]) -> None:
        # 实验室四个开关写到 .env 的 QQ_DIGEST_LAB_*（与 wechat_enabled 同一条保存路径），
        # 并热更新当前 Settings：勾上就生效，不必再手改 .env、也不必重启。
        settings = self.server.settings  # type: ignore[attr-defined]
        if not str(settings.env_file or ""):
            raise ValueError("找不到 .env 路径")
        updates: dict[str, str] = {}
        for name in ("search", "replies", "events", "rules"):
            key = "lab_" + name
            if key not in payload:
                continue
            if type(payload[key]) is not bool:
                raise ValueError(f"{key} 必须是布尔值")
            updates["QQ_DIGEST_LAB_" + name.upper()] = "1" if payload[key] else "0"
        result = update_env_file(Path(settings.env_file), updates)
        if not result.get("ok"):
            raise ValueError(result.get("error") or ".env 写入失败")
        for key, text in updates.items():
            name = key.removeprefix("QQ_DIGEST_LAB_").lower()
            setattr(settings, "lab_" + name, text == "1")
            os.environ[key] = text
        self._json(200, {
            "ok": True,
            "lab": {name: bool(settings.lab_enabled(name)) for name in ("search", "replies", "events", "rules")},
        })
        return

    def _save_telemetry_settings(self, payload: dict[str, Any]) -> None:
        # 社区在线数上报开关：与 lab_* 同一套写法（写 .env + 热更新当前 Settings）。
        settings = self.server.settings  # type: ignore[attr-defined]
        if not str(settings.env_file or ""):
            raise ValueError("找不到 .env 路径")
        value = payload["community_telemetry"]
        if type(value) is not bool:
            raise ValueError("community_telemetry 必须是布尔值")
        updates = {"QQ_DIGEST_COMMUNITY_TELEMETRY": "1" if value else "0"}
        result = update_env_file(Path(settings.env_file), updates)
        if not result.get("ok"):
            raise ValueError(result.get("error") or ".env 写入失败")
        settings.community_telemetry = value
        os.environ["QQ_DIGEST_COMMUNITY_TELEMETRY"] = updates["QQ_DIGEST_COMMUNITY_TELEMETRY"]
        self._json(200, {"ok": True, "community_telemetry": bool(settings.community_telemetry)})
        return

    def _save_napcat_settings(self, payload: dict[str, Any]) -> None:
        settings = self.server.settings  # type: ignore[attr-defined]
        if not str(settings.env_file or ""):
            raise ValueError("找不到 .env 路径")
        value = payload["napcat_dir"]
        if not isinstance(value, str):
            raise ValueError("napcat_dir 必须是文本")
        value = value.strip()
        if any(char in value for char in "\r\n\0"):
            raise ValueError("NapCat 目录无效")
        updates = {"QQ_DIGEST_NAPCAT_DIR": value} if value else {}
        env_path = Path(settings.env_file)
        if value:
            result = update_env_file(env_path, updates)
        else:
            if not env_path.is_file():
                raise ValueError(".env not found")
            raw = env_path.read_bytes()
            kept = []
            for line in raw.splitlines(keepends=True):
                text = line.decode("utf-8")
                if text.split("=", 1)[0].strip() == "QQ_DIGEST_NAPCAT_DIR":
                    continue
                kept.append(line)
            env_path.write_bytes(b"".join(kept))
            result = {"ok": True}
        if not result.get("ok"):
            raise ValueError(result.get("error") or ".env 写入失败")
        if value:
            os.environ["QQ_DIGEST_NAPCAT_DIR"] = value
        else:
            os.environ.pop("QQ_DIGEST_NAPCAT_DIR", None)
        self._json(200, {"ok": True, "napcat_dir": value})
        return

    def _save_wechat_settings(self, payload: dict[str, Any]) -> None:
        settings = self.server.settings  # type: ignore[attr-defined]
        if not str(settings.env_file or ""):
            raise ValueError("找不到 .env 路径")
        if "wechat_enabled" in payload and type(payload["wechat_enabled"]) is not bool:
            raise ValueError("wechat_enabled 必须是布尔值")
        groups = settings.wechat_group_whitelist
        if "wechat_group_whitelist" in payload:
            raw_groups = payload["wechat_group_whitelist"]
            if not isinstance(raw_groups, list) or any(not isinstance(x, str) or not x.strip() for x in raw_groups):
                raise ValueError("wechat_group_whitelist 必须是文本列表")
            groups = tuple(dict.fromkeys(x.strip() for x in raw_groups))
        aliases = dict(settings.wechat_group_aliases or {})
        if "wechat_group_aliases" in payload:
            raw_aliases = payload["wechat_group_aliases"]
            if not isinstance(raw_aliases, dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in raw_aliases.items()
            ):
                raise ValueError("wechat_group_aliases 必须是「群ID: 别名」的对象")
            aliases = {key.strip(): value.strip() for key, value in raw_aliases.items() if key.strip() and value.strip()}
        interval = int(settings.wechat_scan_interval_seconds)
        if "wechat_scan_interval_seconds" in payload:
            try:
                interval = max(30, int(payload["wechat_scan_interval_seconds"]))
            except (TypeError, ValueError):
                raise ValueError("wechat_scan_interval_seconds 必须是整数")
        enabled = payload.get("wechat_enabled", settings.wechat_enabled)
        manual_key = str(getattr(settings, "wechat_key", "") or "").strip()
        if "wechat_key" in payload:
            # 手填密钥（64 位 hex）走和 .env 手改完全相同的校验：解析交给
            # wechat_receiver.parse_manual_key，这里只负责回人话 + 归一化小写。
            raw_key = payload["wechat_key"]
            if not isinstance(raw_key, str):
                raise ValueError("wechat_key 必须是文本（64 位十六进制 raw key，留空表示清除）")
            try:
                parsed_key = wechat_parse_manual_key(raw_key)
            except ValueError as error:
                raise ValueError(str(error)) from error
            manual_key = parsed_key.hex() if parsed_key else ""
        wechat_dir = str(getattr(settings, "wechat_dir", "") or "").strip()
        if "wechat_dir" in payload:
            # 微信存储位置：写 .env 但保存即生效（清探测缓存 + 重载读取器），不用重启程序。
            raw_dir = payload["wechat_dir"]
            if not isinstance(raw_dir, str):
                raise ValueError("wechat_dir 必须是文本（微信的存储位置目录，留空表示自动探测）")
            wechat_dir = raw_dir.strip().strip('"')
            if wechat_dir:
                try:
                    exists = Path(wechat_dir).is_dir()
                except (OSError, ValueError):  # 畸形路径（超长/非法字符/内嵌 \0）也当「不存在」，别变成 500
                    exists = False
                if not exists:
                    raise ValueError(f"微信数据目录不存在：{wechat_dir}，请填微信「设置 → 文件管理」里显示的存储位置目录")
        updates = {}
        if "wechat_enabled" in payload:
            updates["QQ_DIGEST_WECHAT_ENABLED"] = "1" if enabled else "0"
        if "wechat_group_whitelist" in payload:
            updates["QQ_DIGEST_WECHAT_GROUP_WHITELIST"] = ",".join(groups)
        if "wechat_group_aliases" in payload:
            updates["QQ_DIGEST_WECHAT_GROUP_ALIASES"] = ",".join(f"{key}={value}" for key, value in aliases.items())
        if "wechat_scan_interval_seconds" in payload:
            updates["QQ_DIGEST_WECHAT_SCAN_INTERVAL_SECONDS"] = str(interval)
        if "wechat_key" in payload:
            # 空字符串 = 清除：写空值行，parse_manual_key("") 返回 None，等价于没配。
            updates["QQ_DIGEST_WECHAT_KEY"] = manual_key
        if "wechat_dir" in payload:
            # 空字符串 = 清除：写空值行，locate_storage 会退回自动探测。
            updates["QQ_DIGEST_WECHAT_DIR"] = wechat_dir
        result = update_env_file(Path(settings.env_file), updates)
        if not result.get("ok"):
            raise ValueError(result.get("error") or ".env 写入失败")
        settings.wechat_enabled = enabled
        settings.wechat_group_whitelist = groups
        settings.wechat_group_aliases = aliases
        settings.wechat_scan_interval_seconds = interval
        if "wechat_key" in payload:
            settings.wechat_key = manual_key
            if manual_key:
                os.environ["QQ_DIGEST_WECHAT_KEY"] = manual_key
            else:
                os.environ.pop("QQ_DIGEST_WECHAT_KEY", None)
        if "wechat_dir" in payload:
            settings.wechat_dir = wechat_dir
            if wechat_dir:
                os.environ["QQ_DIGEST_WECHAT_DIR"] = wechat_dir
            else:
                os.environ.pop("QQ_DIGEST_WECHAT_DIR", None)
            # 探测缓存要立刻失效：用户刚把位置从 A 改到 B，不能先命中 A。
            wechat_clear_storage_cache()
        reload_reader = getattr(self.server, "reload_wechat", None)
        if callable(reload_reader): reload_reader()
        # 密钥只回「有没有」，不把原文带回页面（与 /api/wechat/status 口径一致）；目录回显是安全的。
        self._json(200, {"ok": True, "wechat_enabled": bool(settings.wechat_enabled), "wechat_group_whitelist": list(settings.wechat_group_whitelist), "wechat_group_aliases": dict(settings.wechat_group_aliases), "wechat_scan_interval_seconds": int(settings.wechat_scan_interval_seconds), "manual_key": bool(str(getattr(settings, "wechat_key", "") or "").strip()), "wechat_dir": str(getattr(settings, "wechat_dir", "") or "")})
        return

    def _save_dingtalk_settings(self, payload: dict[str, Any]) -> None:
        settings = self.server.settings  # type: ignore[attr-defined]
        if not str(settings.env_file or ""):
            raise ValueError("找不到 .env 路径")
        if "dingtalk_enabled" in payload and type(payload["dingtalk_enabled"]) is not bool:
            raise ValueError("dingtalk_enabled 必须是布尔值")
        groups = settings.dingtalk_group_whitelist
        if "dingtalk_group_whitelist" in payload:
            raw_groups = payload["dingtalk_group_whitelist"]
            if not isinstance(raw_groups, list) or any(not isinstance(x, str) or not x.strip() for x in raw_groups):
                raise ValueError("dingtalk_group_whitelist 必须是文本列表")
            groups = tuple(dict.fromkeys(x.strip() for x in raw_groups))
        enabled = payload.get("dingtalk_enabled", settings.dingtalk_enabled)
        updates = {}
        if "dingtalk_enabled" in payload:
            updates["QQ_DIGEST_DINGTALK_ENABLED"] = "1" if enabled else "0"
        if "dingtalk_group_whitelist" in payload:
            updates["QQ_DIGEST_DINGTALK_GROUP_WHITELIST"] = ",".join(groups)
        result = update_env_file(Path(settings.env_file), updates)
        if not result.get("ok"):
            raise ValueError(result.get("error") or ".env 写入失败")
        settings.dingtalk_enabled = enabled
        settings.dingtalk_group_whitelist = groups
        reload_reader = getattr(self.server, "reload_dingtalk", None)
        if callable(reload_reader): reload_reader()
        self._json(200, {"ok": True, "dingtalk_enabled": bool(settings.dingtalk_enabled), "dingtalk_group_whitelist": list(settings.dingtalk_group_whitelist)})
        return

    def _save_chaoxing_settings(self, payload: dict[str, Any]) -> None:
        settings = self.server.settings  # type: ignore[attr-defined]
        if "chaoxing_enabled" in payload and type(payload["chaoxing_enabled"]) is not bool:
            raise ValueError("chaoxing_enabled 必须是布尔值")
        if "chaoxing_group_whitelist" in payload:
            groups = payload["chaoxing_group_whitelist"]
            if not isinstance(groups, list) or any(not isinstance(x, str) or not x.strip() for x in groups):
                raise ValueError("chaoxing_group_whitelist 必须是文本列表")
            settings.chaoxing_group_whitelist = tuple(x.strip() for x in groups)
        if "chaoxing_enabled" in payload: settings.chaoxing_enabled = payload["chaoxing_enabled"]
        cookie_path = Path(settings.data_dir) / "chaoxing.cookie"
        if payload.get("chaoxing_clear_cookie"):
            cookie_path.unlink(missing_ok=True); settings.chaoxing_cookie = ""
        elif "chaoxing_cookie" in payload:
            cleaned_cookie = _clean_chaoxing_cookie(payload["chaoxing_cookie"])
            if cleaned_cookie:
                cookie_path.parent.mkdir(parents=True, exist_ok=True); cookie_path.write_text(cleaned_cookie, encoding="utf-8"); settings.chaoxing_cookie = cleaned_cookie
        cookie = str(settings.chaoxing_cookie or "")
        if payload.get("chaoxing_clear_cookie") or ("chaoxing_cookie" in payload and _clean_chaoxing_cookie(payload["chaoxing_cookie"])):
            lock = getattr(settings, "chaoxing_status_lock", None)
            if lock:
                with lock:
                    status = dict(getattr(settings, "chaoxing_status", {}) or {})
                    status.update({"ok": False, "expired": False, "last_error": ""})
                    settings.chaoxing_status = status
            else:
                status = {"ok": False, "expired": False, "last_sync_at": "", "last_error": ""}
                settings.chaoxing_status = status
            self.store.meta_set("chaoxing_status", json.dumps(status, ensure_ascii=False))
        reload_reader = getattr(self.server, "reload_chaoxing", None)
        if callable(reload_reader): reload_reader()
        self._json(200, {"ok": True, "chaoxing_enabled": bool(settings.chaoxing_enabled), "chaoxing_cookie": {"has_cookie": bool(cookie)}, "chaoxing_status": chaoxing_status(settings, self.store), "chaoxing_group_whitelist": list(settings.chaoxing_group_whitelist)})
        return

    def _save_llm_settings(self, payload: dict[str, Any]) -> None:
        settings = self.server.settings  # type: ignore[attr-defined]
        if not str(settings.env_file or ""):
            raise ValueError("找不到 .env 路径")
        updates, applied = _llm_setting_updates(payload, settings)
        result = update_env_file(Path(settings.env_file), updates)
        if not result.get("ok"):
            raise ValueError(result.get("error") or ".env 写入失败")
        # 热生效：保存后立刻用新服务商，不必重启服务。
        for field_name, field_value in applied.items():
            setattr(settings, field_name, field_value)
        self._json(200, {"ok": True, "llm": _llm_setting_state(settings)})
        return

    def _save_push_settings(self, payload: dict[str, Any]) -> None:
        settings = self.server.settings  # type: ignore[attr-defined]
        if not str(settings.env_file or ""):
            raise ValueError("找不到 .env 路径")
        updates, applied = _push_setting_values(payload, set(_PUSH_SETTING_ENV_KEYS).intersection(payload))
        result = update_env_file(Path(settings.env_file), updates)
        if not result.get("ok"):
            raise ValueError(result.get("error") or ".env 写入失败")
        # 热生效：同一进程内的 Settings 立刻反映新通道，不必重启。
        for field_name, field_value in applied.items():
            setattr(settings, field_name, field_value)
        self._json(200, {
            "ok": True,
            "channels": settings.push_channels(),
            "push": _push_setting_state(settings),
        })
        return

    def _save_catchup_settings(self, payload: dict[str, Any]) -> None:
        settings = self.server.settings  # type: ignore[attr-defined]
        if "catchup_enabled" in payload and not isinstance(payload["catchup_enabled"], bool):
            raise ValueError("catchup_enabled 必须是布尔值")
        with self.server.catchup_preferences_lock:  # type: ignore[attr-defined]
            current_prefs = dict(self.server.catchup_preferences)  # type: ignore[attr-defined]
        hours = payload.get("catchup_hours", current_prefs["catchup_hours"])
        if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 8760:
            raise ValueError("回溯小时数必须在 1 到 8760 之间")
        env_path = Path(settings.env_file or "")
        if not str(settings.env_file or ""):
            raise ValueError("找不到 .env 路径")
        updates = {}
        if "catchup_enabled" in payload:
            updates["QQ_DIGEST_CATCHUP_ENABLED"] = "1" if payload["catchup_enabled"] else "0"
        if "catchup_hours" in payload:
            updates["QQ_DIGEST_CATCHUP_HOURS"] = str(hours)
        result = update_env_file(env_path, updates)
        if not result.get("ok"):
            self._json(400, {"ok": False, "error": result.get("error") or ".env 写入失败"}); return
        with self.server.catchup_preferences_lock:  # type: ignore[attr-defined]
            saved_prefs = dict(self.server.catchup_preferences)  # type: ignore[attr-defined]
            if "catchup_enabled" in payload:
                saved_prefs["catchup_enabled"] = payload["catchup_enabled"]
            if "catchup_hours" in payload:
                saved_prefs["catchup_hours"] = hours
            self.server.catchup_preferences = saved_prefs  # type: ignore[attr-defined]
        self._json(200, {"ok": True, "updated": result.get("updated"), "added": result.get("added"), **saved_prefs})
        return

    def _save_hosting_settings(self, payload: dict[str, Any]) -> None:
        if hosting is None:
            self._json(200, {"ok": False, "error": "托管模块不可用"}); return
        result = hosting.save_prefs(self.server.settings, payload)  # type: ignore[attr-defined]
        self._json(200 if result.get("ok") else 400, result)
