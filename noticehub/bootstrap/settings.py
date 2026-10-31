"""把旧环境变量装成 Settings。本模块被调用时才读文件和环境变量。"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable, Mapping

NEW_ENV_PREFIX = "NOTICEHUB_"
LEGACY_ENV_PREFIX = "QQ_DIGEST_"
_legacy_env_warned = False


def _warn_legacy_env_once() -> None:
    """只警告一次：旧名 QQ_DIGEST_* 仍被兼容读取。"""
    global _legacy_env_warned
    if _legacy_env_warned:
        return
    _legacy_env_warned = True
    logging.getLogger("noticehub.config").warning(
        "检测到旧环境变量名 QQ_DIGEST_*，已按兼容读取；建议改成 NOTICEHUB_*"
    )


def env_value(env: Mapping[str, str], suffix: str, default: str = "") -> str:
    """取一个环境键：NOTICEHUB_<suffix> 优先，没有再回退 QQ_DIGEST_<suffix>。

    两个名字都在时按新名走（哪怕新名是空串，也以用户显式写的新名为准）。
    """
    new_key = NEW_ENV_PREFIX + suffix
    legacy_key = LEGACY_ENV_PREFIX + suffix
    if new_key in env:
        return str(env.get(new_key))
    if legacy_key in env:
        _warn_legacy_env_once()
        return str(env.get(legacy_key))
    return default


def build_settings(
    cls: type,
    env: Mapping[str, str] | None,
    env_file: Path | None,
    *,
    runtime_root: Path,
    load_env_file: Callable[[Path | str], dict[str, str]],
    default_web_host: Callable[[], str],
    parse_bool: Callable[..., bool],
    parse_int: Callable[..., int],
    parse_float: Callable[..., float],
    split_list: Callable[[Any], tuple[str, ...]],
    parse_quiet_hours: Callable[[Any], str],
    parse_aliases: Callable[[Any], dict[str, str]],
    default_dashscope_endpoint: str,
    default_vision_model: str,
):
    """读取规则与 noticehub.config.Settings.from_env 相同。"""
    if env_file is not None:
        warn_if_env_overrides_dotenv_port(env_file)
        load_env_file(env_file)
    elif env is None:
        env_file = Path(env_value(os.environ, "ENV", str(runtime_root / ".env")))
        warn_if_env_overrides_dotenv_port(env_file)
        load_env_file(env_file)
    source = env if env is not None else os.environ

    def get(name: str, default: str = "") -> str:
        value = source.get(name)
        return default if value is None else str(value)

    data_dir_text = env_value(source, "DATA_DIR").strip() or str(runtime_root / "data")
    log_dir_text = env_value(source, "LOG_DIR").strip() or str(runtime_root / "logs")
    data_dir = Path(data_dir_text).expanduser()
    log_dir = Path(log_dir_text).expanduser()
    topic_ids = tuple(
        item for item in (parse_int(value, 0) for value in split_list(get("WXPUSHER_TOPIC_IDS"))) if item
    )
    onebot_token = env_value(source, "ONEBOT_TOKEN").strip()
    napcat_api_token = env_value(source, "NAPCAT_API_TOKEN").strip() or onebot_token
    chaoxing_cookie = env_value(source, "CHAOXING_COOKIE").strip()
    if not chaoxing_cookie:
        cookie_path = data_dir / "chaoxing.cookie"
        try:
            chaoxing_cookie = cookie_path.read_text(encoding="utf-8").strip() if cookie_path.is_file() else ""
        except OSError:
            chaoxing_cookie = ""

    update_channel = env_value(source, "UPDATE_CHANNEL").strip().lower()
    if update_channel not in {"stable", "beta"}:
        update_channel = ""

    return cls(
        appid=get("QQ_BOT_APPID").strip(),
        secret=get("QQ_BOT_SECRET").strip(),
        sandbox=parse_bool(get("QQ_BOT_SANDBOX"), False),
        official_bot_enabled=parse_bool(env_value(source, "OFFICIAL_BOT_ENABLED", "1"), True),
        group_whitelist=split_list(env_value(source, "GROUPS")),
        dingtalk_enabled=parse_bool(env_value(source, "DINGTALK_ENABLED", "0"), False),
        dingtalk_group_whitelist=split_list(env_value(source, "DINGTALK_GROUP_WHITELIST")),
        dingtalk_scan_interval_seconds=max(
            5, parse_int(env_value(source, "DINGTALK_SCAN_INTERVAL_SECONDS", "30"), 30)
        ),
        chaoxing_enabled=parse_bool(env_value(source, "CHAOXING_ENABLED", "0"), False),
        chaoxing_cookie=chaoxing_cookie,
        chaoxing_group_whitelist=split_list(env_value(source, "CHAOXING_GROUP_WHITELIST")),
        chaoxing_scan_interval_seconds=max(
            1800, parse_int(env_value(source, "CHAOXING_SCAN_INTERVAL_SECONDS", "1800"), 1800)
        ),
        wechat_enabled=parse_bool(env_value(source, "WECHAT_ENABLED", "0"), False),
        wechat_group_whitelist=split_list(env_value(source, "WECHAT_GROUP_WHITELIST")),
        wechat_scan_interval_seconds=max(
            30, parse_int(env_value(source, "WECHAT_SCAN_INTERVAL_SECONDS", "300"), 300)
        ),
        wechat_group_aliases=parse_aliases(env_value(source, "WECHAT_GROUP_ALIASES")),
        wechat_key=env_value(source, "WECHAT_KEY", "").strip(),
        wechat_dir=env_value(source, "WECHAT_DIR", "").strip(),
        group_aliases=parse_aliases(env_value(source, "GROUP_ALIASES")),
        quiet_groups=split_list(env_value(source, "QUIET_GROUPS")),
        immediate_groups=split_list(env_value(source, "IMMEDIATE_GROUPS")),
        push_c2c_openids=split_list(env_value(source, "PUSH_OPENIDS")),
        wxpusher_app_token=get("WXPUSHER_APP_TOKEN").strip(),
        wxpusher_uids=split_list(get("WXPUSHER_UIDS")),
        wxpusher_topic_ids=topic_ids,
        serverchan_keys=split_list(get("SERVERCHAN_KEYS")),
        pushplus_tokens=split_list(get("PUSHPLUS_TOKENS")),
        webhook_urls=split_list(env_value(source, "WEBHOOKS")),
        dashscope_api_key=(env_value(source, "LLM_API_KEY").strip() or get("DASHSCOPE_API_KEY").strip()),
        dashscope_model=env_value(source, "LLM_MODEL", "qwen-plus").strip() or "qwen-plus",
        dashscope_endpoint=env_value(source, "LLM_ENDPOINT", default_dashscope_endpoint).strip()
        or default_dashscope_endpoint,
        llm_backend=env_value(source, "LLM_BACKEND", "auto").strip().lower() or "auto",
        codebuddy_cli=env_value(source, "CODEBUDDY_CLI").strip(),
        llm_enabled=parse_bool(env_value(source, "LLM", "1"), True),
        include_raw=parse_bool(env_value(source, "INCLUDE_RAW", "0"), False),
        llm_max_retries=max(0, parse_int(env_value(source, "LLM_MAX_RETRIES", "2"), 2)),
        llm_retry_backoff=max(
            0.0, parse_float(env_value(source, "LLM_RETRY_BACKOFF", "1.5"), 1.5)
        ),
        llm_defer_max_attempts=max(
            0, parse_int(env_value(source, "LLM_DEFER_MAX_ATTEMPTS", "3"), 3)
        ),
        llm_defer_window_minutes=max(
            1, parse_int(env_value(source, "LLM_DEFER_WINDOW_MINUTES", "15"), 15)
        ),
        attachments_enabled=parse_bool(env_value(source, "ATTACHMENTS", "1"), True),
        attachment_max_mb=max(1, parse_int(env_value(source, "ATTACHMENT_MAX_MB", "10"), 10)),
        attachment_max_per_hour=max(
            1, parse_int(env_value(source, "ATTACHMENT_MAX_PER_HOUR", "30"), 30)
        ),
        attachment_retention_days=max(
            1, parse_int(env_value(source, "ATTACHMENT_RETENTION_DAYS", "7"), 7)
        ),
        vision_enabled=parse_bool(env_value(source, "VISION", "1"), True),
        vision_model=env_value(source, "VL_MODEL", default_vision_model).strip() or "qwen3-vl-plus",
        document_max_chars=max(
            1000, parse_int(env_value(source, "DOCUMENT_MAX_CHARS", "30000"), 30000)
        ),
        pdf_ocr_max_pages=max(
            1, parse_int(env_value(source, "PDF_OCR_MAX_PAGES", "20"), 20)
        ),
        dedupe_hours=max(0, parse_int(env_value(source, "DEDUPE_HOURS", "6"), 6)),
        html_push=parse_bool(env_value(source, "PUSH_HTML", "1"), True),
        deadline_reminders_enabled=parse_bool(
            env_value(source, "DEADLINE_REMINDERS", "1"), True
        ),
        deadline_morning=env_value(source, "DEADLINE_MORNING", "07:30").strip() or "07:30",
        deadline_evening=env_value(source, "DEADLINE_EVENING", "21:00").strip() or "21:00",
        candidate_push_enabled=parse_bool(env_value(source, "CANDIDATE_PUSH", "1"), True),
        candidate_min_confidence=min(
            1.0, max(0.0, parse_float(env_value(source, "CANDIDATE_MIN_CONFIDENCE", "0.55"), 0.55))
        ),
        candidate_reminder_hours=max(
            6, parse_int(env_value(source, "CANDIDATE_REMINDER_HOURS", "24"), 24)
        ),
        candidate_push_max_per_tick=max(
            0, parse_int(env_value(source, "CANDIDATE_MAX_PER_TICK", "2"), 2)
        ),
        candidate_push_max_per_day=max(
            0, parse_int(env_value(source, "CANDIDATE_MAX_PER_DAY", "4"), 4)
        ),
        weekly_review_enabled=parse_bool(env_value(source, "WEEKLY_REVIEW", "1"), True),
        push_daily_budget=max(0, parse_int(env_value(source, "PUSH_DAILY_BUDGET", "12"), 12)),
        quiet_hours=parse_quiet_hours(env_value(source, "QUIET_HOURS", "23:00-07:00")),
        weekly_review_weekday=min(
            6, max(0, parse_int(env_value(source, "WEEKLY_REVIEW_WEEKDAY", "6"), 6))
        ),
        weekly_review_time=env_value(source, "WEEKLY_REVIEW_TIME", "20:30").strip() or "20:30",
        web_enabled=parse_bool(env_value(source, "WEB", "1"), True),
        web_host=env_value(source, "WEB_HOST", default_web_host()).strip() or default_web_host(),
        web_port=parse_int(env_value(source, "WEB_PORT", "8766"), 8766),
        web_base_url=env_value(source, "WEB_BASE_URL", "").strip().rstrip("/"),
        web_token=env_value(source, "WEB_TOKEN", "").strip(),
        update_channel=update_channel,
        window_minutes=max(1, parse_int(env_value(source, "WINDOW_MINUTES", "10"), 10)),
        poll_seconds=max(5, parse_int(env_value(source, "POLL_SECONDS", "20"), 20)),
        urgent_immediate=parse_bool(env_value(source, "URGENT_IMMEDIATE", "1"), True),
        min_score=parse_int(env_value(source, "MIN_SCORE", "3"), 3),
        max_items=max(1, parse_int(env_value(source, "MAX_ITEMS", "30"), 30)),
        max_batch=max(50, parse_int(env_value(source, "MAX_BATCH", "400"), 400)),
        delivery_max_attempts=max(1, parse_int(env_value(source, "MAX_ATTEMPTS", "4"), 4)),
        delivery_retry_seconds=max(10, parse_int(env_value(source, "RETRY_SECONDS", "90"), 90)),
        message_retention_days=max(1, parse_int(env_value(source, "RETENTION_DAYS", "30"), 30)),
        onebot_enabled=parse_bool(env_value(source, "ONEBOT_ENABLED", "0"), False),
        onebot_host=env_value(source, "ONEBOT_HOST", "127.0.0.1").strip() or "127.0.0.1",
        onebot_port=parse_int(env_value(source, "ONEBOT_PORT", "8765"), 8765),
        onebot_token=onebot_token,
        catchup_enabled=parse_bool(env_value(source, "CATCHUP_ENABLED", "0"), False),
        catchup_hours=max(1, parse_int(env_value(source, "CATCHUP_HOURS", "24"), 24)),
        catchup_count=max(1, parse_int(env_value(source, "CATCHUP_COUNT", "50"), 50)),
        catchup_interval_minutes=max(5, parse_int(env_value(source, "CATCHUP_INTERVAL_MINUTES", "30"), 30)),
        napcat_api_url=env_value(source, "NAPCAT_API_URL", "http://127.0.0.1:3001").strip()
        or "http://127.0.0.1:3001",
        napcat_api_token=napcat_api_token,
        qq_group_refresh_interval_seconds=max(
            30, parse_int(env_value(source, "QQ_GROUP_REFRESH_INTERVAL_SECONDS", "300"), 300)
        ),
        napcat_qr_path=Path(env_value(source, "NAPCAT_QR_PATH").strip() or str(data_dir / "qrcode.png")).expanduser(),
        hosting_quit_qq=parse_bool(env_value(source, "HOSTING_QUIT_QQ", "1"), True),
        hosting_restore_qq=parse_bool(env_value(source, "HOSTING_RESTORE_QQ", "1"), True),
        quit_restore_qq=parse_bool(env_value(source, "QUIT_RESTORE_QQ", "0"), False),
        hosting_auto_on_start=parse_bool(env_value(source, "HOSTING_AUTO_ON_START", "0"), False),
        autostart=parse_bool(env_value(source, "AUTOSTART", "0"), False),
        lab_search=parse_bool(env_value(source, "LAB_SEARCH", "0"), False),
        lab_replies=parse_bool(env_value(source, "LAB_REPLIES", "0"), False),
        lab_events=parse_bool(env_value(source, "LAB_EVENTS", "0"), False),
        lab_rules=parse_bool(env_value(source, "LAB_RULES", "0"), False),
        community_telemetry=parse_bool(env_value(source, "COMMUNITY_TELEMETRY", "0"), False),
        self_qq=env_value(source, "SELF_QQ").strip(),
        self_nickname=env_value(source, "SELF_NICKNAME").strip(),
        http_timeout=max(5, parse_int(env_value(source, "HTTP_TIMEOUT", "15"), 15)),
        llm_timeout=max(10, parse_int(env_value(source, "LLM_TIMEOUT", "60"), 60)),
        data_dir=data_dir,
        log_dir=log_dir,
        env_file=env_file,
    )


def _config_helpers():
    """取旧 config 模块里的解析函数；延迟导入避免 config ↔ bootstrap 循环。"""
    from noticehub.config import parse_env_file, parse_int

    return parse_env_file, parse_int


def _pick(mapping: Mapping[str, str], suffix: str) -> str:
    """在给定映射里取一个键：新名优先、旧名兜底；都没有就是空串。"""
    new_key = NEW_ENV_PREFIX + suffix
    if new_key in mapping:
        return str(mapping.get(new_key) or "")
    return str(mapping.get(LEGACY_ENV_PREFIX + suffix) or "")


def env_get(suffix: str, default: str = "", env: Mapping[str, str] | None = None) -> str:
    """读当前进程环境：NOTICEHUB_<suffix> 优先，旧名 QQ_DIGEST_<suffix> 兜底。"""
    return env_value(os.environ if env is None else env, suffix, default)


def env_name(suffix: str) -> str:
    """写入/展示用的新键名。"""
    return NEW_ENV_PREFIX + suffix


def _dotenv_port_values(env_file: Path | str) -> tuple[str, str]:
    """返回（环境变量里的端口, .env 里的端口）；没写的那个是空串。两套名字都认。"""
    parse_env_file, _parse_int = _config_helpers()
    inherited = _pick(os.environ, "WEB_PORT").strip()
    from_file = _pick(parse_env_file(env_file), "WEB_PORT").strip()
    return inherited, from_file


def _port_values_differ(left: str, right: str) -> bool:
    """按解析后的整数比，``058066`` 与 ``58066`` 不算不一致。"""
    _parse_env_file, parse_int = _config_helpers()
    return parse_int(left, -1) != parse_int(right, -2)


def web_port_source(env_file: Path | str | None = None) -> str:
    """端口实际来源：``"env"`` = 环境变量在起作用，``"dotenv"`` = 用 .env（或默认值）。

    `.env` 里没写这个键、环境变量写了，也算 env：用户看到的值确实来自环境变量。
    """
    if env_file is None:
        return "dotenv"
    inherited, from_file = _dotenv_port_values(env_file)
    if not inherited:
        return "dotenv"
    if not from_file:
        return "env"
    return "env" if _port_values_differ(inherited, from_file) else "dotenv"


def warn_if_env_overrides_dotenv_port(env_file: Path | str) -> None:
    """`.env` 里的端口被环境变量压掉时记一条 warning。

    `load_env_file` 用 ``os.environ.setdefault``，所以父进程（启动器所在的 shell、
    上一个测试脚本）里残留的 ``WEB_PORT``（新名或旧名）会静默盖掉 .env：用户改了 .env、
    界面却听着另一个端口，日志里连一句话都没有。只在两者**真的不一致**时记，不刷屏。
    """
    inherited, from_file = _dotenv_port_values(env_file)
    if not inherited or not from_file:
        return
    if not _port_values_differ(inherited, from_file):
        return
    # 日志频道沿用旧模块名：用户和测试都按 noticehub.config 过滤这条 warning。
    logging.getLogger("noticehub.config").warning(
        "端口 %s 来自环境变量，覆盖了 .env 里的 %s", inherited, from_file
    )
