"""把旧环境变量装成 Settings。本模块被调用时才读文件和环境变量。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Mapping


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
    """读取规则与 qq_live_digest.config.Settings.from_env 相同。"""
    if env_file is not None:
        load_env_file(env_file)
    elif env is None:
        env_file = Path(os.environ.get("QQ_DIGEST_ENV", runtime_root / ".env"))
        load_env_file(env_file)
    source = env if env is not None else os.environ

    def get(name: str, default: str = "") -> str:
        value = source.get(name)
        return default if value is None else str(value)

    data_dir_text = get("QQ_DIGEST_DATA_DIR").strip() or str(runtime_root / "data")
    log_dir_text = get("QQ_DIGEST_LOG_DIR").strip() or str(runtime_root / "logs")
    data_dir = Path(data_dir_text).expanduser()
    log_dir = Path(log_dir_text).expanduser()
    topic_ids = tuple(
        item for item in (parse_int(value, 0) for value in split_list(get("WXPUSHER_TOPIC_IDS"))) if item
    )
    onebot_token = get("QQ_DIGEST_ONEBOT_TOKEN").strip()
    napcat_api_token = get("QQ_DIGEST_NAPCAT_API_TOKEN").strip() or onebot_token
    chaoxing_cookie = get("QQ_DIGEST_CHAOXING_COOKIE").strip()
    if not chaoxing_cookie:
        cookie_path = data_dir / "chaoxing.cookie"
        try:
            chaoxing_cookie = cookie_path.read_text(encoding="utf-8").strip() if cookie_path.is_file() else ""
        except OSError:
            chaoxing_cookie = ""

    update_channel = get("QQ_DIGEST_UPDATE_CHANNEL").strip().lower()
    if update_channel not in {"stable", "beta"}:
        update_channel = ""

    return cls(
        appid=get("QQ_BOT_APPID").strip(),
        secret=get("QQ_BOT_SECRET").strip(),
        sandbox=parse_bool(get("QQ_BOT_SANDBOX"), False),
        official_bot_enabled=parse_bool(get("QQ_DIGEST_OFFICIAL_BOT_ENABLED", "1"), True),
        group_whitelist=split_list(get("QQ_DIGEST_GROUPS")),
        dingtalk_enabled=parse_bool(get("QQ_DIGEST_DINGTALK_ENABLED", "0"), False),
        dingtalk_group_whitelist=split_list(get("QQ_DIGEST_DINGTALK_GROUP_WHITELIST")),
        dingtalk_scan_interval_seconds=max(
            5, parse_int(get("QQ_DIGEST_DINGTALK_SCAN_INTERVAL_SECONDS", "30"), 30)
        ),
        chaoxing_enabled=parse_bool(get("QQ_DIGEST_CHAOXING_ENABLED", "0"), False),
        chaoxing_cookie=chaoxing_cookie,
        chaoxing_group_whitelist=split_list(get("QQ_DIGEST_CHAOXING_GROUP_WHITELIST")),
        chaoxing_scan_interval_seconds=max(
            1800, parse_int(get("QQ_DIGEST_CHAOXING_SCAN_INTERVAL_SECONDS", "1800"), 1800)
        ),
        wechat_enabled=parse_bool(get("QQ_DIGEST_WECHAT_ENABLED", "0"), False),
        wechat_group_whitelist=split_list(get("QQ_DIGEST_WECHAT_GROUP_WHITELIST")),
        wechat_scan_interval_seconds=max(
            30, parse_int(get("QQ_DIGEST_WECHAT_SCAN_INTERVAL_SECONDS", "300"), 300)
        ),
        wechat_group_aliases=parse_aliases(get("QQ_DIGEST_WECHAT_GROUP_ALIASES")),
        wechat_key=get("QQ_DIGEST_WECHAT_KEY", "").strip(),
        wechat_dir=get("QQ_DIGEST_WECHAT_DIR", "").strip(),
        group_aliases=parse_aliases(get("QQ_DIGEST_GROUP_ALIASES")),
        quiet_groups=split_list(get("QQ_DIGEST_QUIET_GROUPS")),
        immediate_groups=split_list(get("QQ_DIGEST_IMMEDIATE_GROUPS")),
        push_c2c_openids=split_list(get("QQ_DIGEST_PUSH_OPENIDS")),
        wxpusher_app_token=get("WXPUSHER_APP_TOKEN").strip(),
        wxpusher_uids=split_list(get("WXPUSHER_UIDS")),
        wxpusher_topic_ids=topic_ids,
        serverchan_keys=split_list(get("SERVERCHAN_KEYS")),
        pushplus_tokens=split_list(get("PUSHPLUS_TOKENS")),
        webhook_urls=split_list(get("QQ_DIGEST_WEBHOOKS")),
        dashscope_api_key=(get("QQ_DIGEST_LLM_API_KEY").strip() or get("DASHSCOPE_API_KEY").strip()),
        dashscope_model=get("QQ_DIGEST_LLM_MODEL", "qwen-plus").strip() or "qwen-plus",
        dashscope_endpoint=get("QQ_DIGEST_LLM_ENDPOINT", default_dashscope_endpoint).strip()
        or default_dashscope_endpoint,
        llm_backend=get("QQ_DIGEST_LLM_BACKEND", "auto").strip().lower() or "auto",
        codebuddy_cli=get("QQ_DIGEST_CODEBUDDY_CLI").strip(),
        llm_enabled=parse_bool(get("QQ_DIGEST_LLM", "1"), True),
        include_raw=parse_bool(get("QQ_DIGEST_INCLUDE_RAW", "0"), False),
        llm_max_retries=max(0, parse_int(get("QQ_DIGEST_LLM_MAX_RETRIES", "2"), 2)),
        llm_retry_backoff=max(
            0.0, parse_float(get("QQ_DIGEST_LLM_RETRY_BACKOFF", "1.5"), 1.5)
        ),
        llm_defer_max_attempts=max(
            0, parse_int(get("QQ_DIGEST_LLM_DEFER_MAX_ATTEMPTS", "3"), 3)
        ),
        llm_defer_window_minutes=max(
            1, parse_int(get("QQ_DIGEST_LLM_DEFER_WINDOW_MINUTES", "15"), 15)
        ),
        attachments_enabled=parse_bool(get("QQ_DIGEST_ATTACHMENTS", "1"), True),
        attachment_max_mb=max(1, parse_int(get("QQ_DIGEST_ATTACHMENT_MAX_MB", "10"), 10)),
        attachment_max_per_hour=max(
            1, parse_int(get("QQ_DIGEST_ATTACHMENT_MAX_PER_HOUR", "30"), 30)
        ),
        attachment_retention_days=max(
            1, parse_int(get("QQ_DIGEST_ATTACHMENT_RETENTION_DAYS", "7"), 7)
        ),
        vision_enabled=parse_bool(get("QQ_DIGEST_VISION", "1"), True),
        vision_model=get("QQ_DIGEST_VL_MODEL", default_vision_model).strip() or "qwen3-vl-plus",
        document_max_chars=max(
            1000, parse_int(get("QQ_DIGEST_DOCUMENT_MAX_CHARS", "30000"), 30000)
        ),
        pdf_ocr_max_pages=max(
            1, parse_int(get("QQ_DIGEST_PDF_OCR_MAX_PAGES", "20"), 20)
        ),
        dedupe_hours=max(0, parse_int(get("QQ_DIGEST_DEDUPE_HOURS", "6"), 6)),
        html_push=parse_bool(get("QQ_DIGEST_PUSH_HTML", "1"), True),
        deadline_reminders_enabled=parse_bool(
            get("QQ_DIGEST_DEADLINE_REMINDERS", "1"), True
        ),
        deadline_morning=get("QQ_DIGEST_DEADLINE_MORNING", "07:30").strip() or "07:30",
        deadline_evening=get("QQ_DIGEST_DEADLINE_EVENING", "21:00").strip() or "21:00",
        candidate_push_enabled=parse_bool(get("QQ_DIGEST_CANDIDATE_PUSH", "1"), True),
        candidate_min_confidence=min(
            1.0, max(0.0, parse_float(get("QQ_DIGEST_CANDIDATE_MIN_CONFIDENCE", "0.55"), 0.55))
        ),
        candidate_reminder_hours=max(
            6, parse_int(get("QQ_DIGEST_CANDIDATE_REMINDER_HOURS", "24"), 24)
        ),
        candidate_push_max_per_tick=max(
            0, parse_int(get("QQ_DIGEST_CANDIDATE_MAX_PER_TICK", "2"), 2)
        ),
        candidate_push_max_per_day=max(
            0, parse_int(get("QQ_DIGEST_CANDIDATE_MAX_PER_DAY", "4"), 4)
        ),
        weekly_review_enabled=parse_bool(get("QQ_DIGEST_WEEKLY_REVIEW", "1"), True),
        push_daily_budget=max(0, parse_int(get("QQ_DIGEST_PUSH_DAILY_BUDGET", "12"), 12)),
        quiet_hours=parse_quiet_hours(get("QQ_DIGEST_QUIET_HOURS", "23:00-07:00")),
        weekly_review_weekday=min(
            6, max(0, parse_int(get("QQ_DIGEST_WEEKLY_REVIEW_WEEKDAY", "6"), 6))
        ),
        weekly_review_time=get("QQ_DIGEST_WEEKLY_REVIEW_TIME", "20:30").strip() or "20:30",
        web_enabled=parse_bool(get("QQ_DIGEST_WEB", "1"), True),
        web_host=get("QQ_DIGEST_WEB_HOST", default_web_host()).strip() or default_web_host(),
        web_port=parse_int(get("QQ_DIGEST_WEB_PORT", "8766"), 8766),
        web_base_url=get("QQ_DIGEST_WEB_BASE_URL", "").strip().rstrip("/"),
        web_token=get("QQ_DIGEST_WEB_TOKEN", "").strip(),
        update_channel=update_channel,
        window_minutes=max(1, parse_int(get("QQ_DIGEST_WINDOW_MINUTES", "10"), 10)),
        poll_seconds=max(5, parse_int(get("QQ_DIGEST_POLL_SECONDS", "20"), 20)),
        urgent_immediate=parse_bool(get("QQ_DIGEST_URGENT_IMMEDIATE", "1"), True),
        min_score=parse_int(get("QQ_DIGEST_MIN_SCORE", "3"), 3),
        max_items=max(1, parse_int(get("QQ_DIGEST_MAX_ITEMS", "30"), 30)),
        max_batch=max(50, parse_int(get("QQ_DIGEST_MAX_BATCH", "400"), 400)),
        delivery_max_attempts=max(1, parse_int(get("QQ_DIGEST_MAX_ATTEMPTS", "4"), 4)),
        delivery_retry_seconds=max(10, parse_int(get("QQ_DIGEST_RETRY_SECONDS", "90"), 90)),
        message_retention_days=max(1, parse_int(get("QQ_DIGEST_RETENTION_DAYS", "30"), 30)),
        onebot_enabled=parse_bool(get("QQ_DIGEST_ONEBOT_ENABLED", "0"), False),
        onebot_host=get("QQ_DIGEST_ONEBOT_HOST", "127.0.0.1").strip() or "127.0.0.1",
        onebot_port=parse_int(get("QQ_DIGEST_ONEBOT_PORT", "8765"), 8765),
        onebot_token=onebot_token,
        catchup_enabled=parse_bool(get("QQ_DIGEST_CATCHUP_ENABLED", "0"), False),
        catchup_hours=max(1, parse_int(get("QQ_DIGEST_CATCHUP_HOURS", "24"), 24)),
        catchup_count=max(1, parse_int(get("QQ_DIGEST_CATCHUP_COUNT", "50"), 50)),
        catchup_interval_minutes=max(5, parse_int(get("QQ_DIGEST_CATCHUP_INTERVAL_MINUTES", "30"), 30)),
        napcat_api_url=get("QQ_DIGEST_NAPCAT_API_URL", "http://127.0.0.1:3001").strip()
        or "http://127.0.0.1:3001",
        napcat_api_token=napcat_api_token,
        qq_group_refresh_interval_seconds=max(
            30, parse_int(get("QQ_DIGEST_QQ_GROUP_REFRESH_INTERVAL_SECONDS", "300"), 300)
        ),
        napcat_qr_path=Path(get("QQ_DIGEST_NAPCAT_QR_PATH").strip() or str(data_dir / "qrcode.png")).expanduser(),
        hosting_quit_qq=parse_bool(get("QQ_DIGEST_HOSTING_QUIT_QQ", "1"), True),
        hosting_restore_qq=parse_bool(get("QQ_DIGEST_HOSTING_RESTORE_QQ", "1"), True),
        quit_restore_qq=parse_bool(get("QQ_DIGEST_QUIT_RESTORE_QQ", "0"), False),
        hosting_auto_on_start=parse_bool(get("QQ_DIGEST_HOSTING_AUTO_ON_START", "0"), False),
        autostart=parse_bool(get("QQ_DIGEST_AUTOSTART", "0"), False),
        lab_search=parse_bool(get("QQ_DIGEST_LAB_SEARCH", "0"), False),
        lab_replies=parse_bool(get("QQ_DIGEST_LAB_REPLIES", "0"), False),
        lab_events=parse_bool(get("QQ_DIGEST_LAB_EVENTS", "0"), False),
        lab_rules=parse_bool(get("QQ_DIGEST_LAB_RULES", "0"), False),
        community_telemetry=parse_bool(get("QQ_DIGEST_COMMUNITY_TELEMETRY", "0"), False),
        self_qq=get("QQ_DIGEST_SELF_QQ").strip(),
        self_nickname=get("QQ_DIGEST_SELF_NICKNAME").strip(),
        http_timeout=max(5, parse_int(get("QQ_DIGEST_HTTP_TIMEOUT", "15"), 15)),
        llm_timeout=max(10, parse_int(get("QQ_DIGEST_LLM_TIMEOUT", "60"), 60)),
        data_dir=data_dir,
        log_dir=log_dir,
        env_file=env_file,
    )
