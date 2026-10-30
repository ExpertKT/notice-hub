"""从环境变量 / .env 读取配置。密钥只存本机，不写入日志。"""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else PROJECT_ROOT

DEFAULT_DASHSCOPE_ENDPOINT = (
    "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
)
DEFAULT_VISION_MODEL = "qwen3-vl-plus"

TRUE_VALUES = {"1", "true", "yes", "on", "y", "是"}
FALSE_VALUES = {"0", "false", "no", "off", "n", "否", ""}


def _default_web_host() -> str:
    return "0.0.0.0" if os.name == "nt" else "127.0.0.1"


def parse_env_file(path: Path | str) -> dict[str, str]:
    """极简 .env 解析：KEY=VALUE，支持 # 注释与引号；不修改环境变量。"""
    path = Path(path)
    loaded: dict[str, str] = {}
    if not path.is_file():
        return loaded
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return loaded
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            loaded[key] = value
    return loaded


def load_env_file(path: Path | str) -> dict[str, str]:
    """解析 .env 并写入 os.environ（不覆盖已存在环境变量）。"""
    loaded = parse_env_file(path)
    for key, value in loaded.items():
        os.environ.setdefault(key, value)
    return loaded


def _write_env_atomic(target: Path, payload: bytes) -> str:
    """同目录临时文件 + ``os.replace`` 原子替换；失败返回错误文本，成功返回空串。

    `.env` 里存着访问 token：原地覆盖时断电/强杀会留下半截文件，用户所有已保存
    的链接和配对就一起废了。所以这里必须原子写（与 rules.py、hosting.py 同一套写法）。
    """
    temporary = target.with_name("%s.tmp-%d" % (target.name, os.getpid()))
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError as error:
        try:
            temporary.unlink()
        except OSError:
            pass
        return "写入 .env 失败：%s" % error
    return ""


def update_env_file(
    path: Path | str, updates: Mapping[str, str], *, append_missing: bool = True
) -> dict[str, Any]:
    """只重写命中的赋值行，其余行（注释、空行、CRLF/LF 混用、有无尾换行）逐字节保留。

    已存在的键就地改写；`append_missing` 为真时把缺失的键追加到文件末尾。
    """
    target = Path(path)
    if not target.is_file():
        return {"ok": False, "updated": [], "added": [], "error": ".env not found"}
    raw = target.read_bytes()
    pending = {str(key): str(value) for key, value in updates.items()}
    updated: list[str] = []
    out: list[bytes] = []
    for line in raw.splitlines(keepends=True):
        text = line.decode("utf-8")
        key = text.split("=", 1)[0].strip() if "=" in text and not text.lstrip().startswith("#") else ""
        if key and key in pending:
            newline = "\r\n" if text.endswith("\r\n") else "\n" if text.endswith("\n") else ""
            out.append((key + "=" + pending.pop(key) + newline).encode("utf-8"))
            updated.append(key)
        else:
            out.append(line)
    added: list[str] = []
    if pending:
        if not append_missing:
            return {
                "ok": False,
                "updated": updated,
                "added": [],
                "error": "missing keys: " + ",".join(sorted(pending)),
            }
        if raw and not raw.endswith(b"\n"):
            out.append(b"\r\n")
        for key, value in pending.items():
            out.append((key + "=" + value + "\r\n").encode("utf-8"))
            added.append(key)
    error = _write_env_atomic(target, b"".join(out))
    if error:
        return {"ok": False, "updated": updated, "added": added, "error": error}
    return {"ok": True, "updated": updated, "added": added, "error": None}


def remove_env_keys(path: Path | str, keys: Iterable[str]) -> dict[str, Any]:
    """删掉 .env 里的指定键（其余行逐字节保留），同样走原子替换。"""
    target = Path(path)
    if not target.is_file():
        return {"ok": False, "removed": [], "error": ".env not found"}
    wanted = {str(key) for key in keys}
    removed: list[str] = []
    kept: list[bytes] = []
    for line in target.read_bytes().splitlines(keepends=True):
        text = line.decode("utf-8")
        key = text.split("=", 1)[0].strip() if "=" in text and not text.lstrip().startswith("#") else ""
        if key and key in wanted:
            removed.append(key)
            continue
        kept.append(line)
    error = _write_env_atomic(target, b"".join(kept))
    if error:
        return {"ok": False, "removed": [], "error": error}
    return {"ok": True, "removed": removed, "error": None}


def is_loopback_host(host: Any) -> bool:
    """绑定地址是否只在本机（localhost / 127.0.0.0/8 / ::1）。"""
    text = str(host or "").strip().strip("[]").lower()
    if not text:
        return True
    if text == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    return default


def parse_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def split_list(value: Any) -> tuple[str, ...]:
    """逗号/分号/换行分隔的列表。"""
    if value is None or isinstance(value, (list, tuple, set)):
        items = list(value) if not isinstance(value, str) else []
    else:
        text = str(value)
        for separator in (";", "；", "\n", "，", "、"):
            text = text.replace(separator, ",")
        items = text.split(",")
    return tuple(str(item).strip() for item in items if str(item).strip())


def parse_quiet_hours(value: Any) -> str:
    """规范化 23:00-07:00 形式的静默时段；非法或等长时段视为关闭。"""
    text = str(value or "").strip().replace("～", "-").replace("~", "-")
    if not text or "-" not in text:
        return ""
    start_text, _, end_text = text.partition("-")

    def parse_clock(raw: str) -> int | None:
        parts = str(raw or "").strip().split(":")
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            return None
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour * 60 + minute
    start, end = parse_clock(start_text), parse_clock(end_text)
    if start is None or end is None or start == end:
        return ""
    return f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}"


def parse_aliases(value: Any) -> dict[str, str]:
    """解析 openid=群名 形式的别名表。"""
    aliases: dict[str, str] = {}
    for chunk in split_list(value):
        key, separator, name = chunk.partition("=")
        if separator:
            key, name = key.strip(), name.strip()
            if key and name:
                aliases[key] = name
    return aliases


@dataclass
class Settings:
    appid: str = ""
    secret: str = ""
    sandbox: bool = False
    official_bot_enabled: bool = True
    group_whitelist: tuple[str, ...] = ()
    dingtalk_enabled: bool = False
    dingtalk_group_whitelist: tuple[str, ...] = ()
    dingtalk_scan_interval_seconds: int = 30
    chaoxing_enabled: bool = False
    chaoxing_cookie: str = ""
    chaoxing_group_whitelist: tuple[str, ...] = ()
    chaoxing_scan_interval_seconds: int = 1800
    wechat_enabled: bool = False
    wechat_group_whitelist: tuple[str, ...] = ()
    wechat_scan_interval_seconds: int = 300
    wechat_group_aliases: Mapping[str, str] = field(default_factory=dict)
    # 手填的微信数据库 raw key（.env: QQ_DIGEST_WECHAT_KEY=<64 位十六进制>）：
    # 微信 4.1.x 自动提取解不开时，高级用户可以先靠它跑起来。
    wechat_key: str = ""
    # 微信存储位置（.env: QQ_DIGEST_WECHAT_DIR）：用户改过微信的存储位置时，让他在设置页填一次，
    # 不用改文件也不用重启（保存即生效）。
    wechat_dir: str = ""
    group_aliases: Mapping[str, str] = field(default_factory=dict)
    quiet_groups: tuple[str, ...] = ()
    immediate_groups: tuple[str, ...] = ()
    push_c2c_openids: tuple[str, ...] = ()

    wxpusher_app_token: str = ""
    wxpusher_uids: tuple[str, ...] = ()
    wxpusher_topic_ids: tuple[int, ...] = ()
    serverchan_keys: tuple[str, ...] = ()
    pushplus_tokens: tuple[str, ...] = ()
    webhook_urls: tuple[str, ...] = ()

    dashscope_api_key: str = ""
    dashscope_model: str = "qwen-plus"
    dashscope_endpoint: str = DEFAULT_DASHSCOPE_ENDPOINT
    llm_backend: str = "auto"
    codebuddy_cli: str = ""
    llm_enabled: bool = True
    include_raw: bool = False

    # LLM 失败重试与降级：先原地重试，仍失败则推迟这一批，超过上限才回退本地规则。
    llm_max_retries: int = 2
    llm_retry_backoff: float = 1.5
    llm_defer_max_attempts: int = 3
    llm_defer_window_minutes: int = 15

    # 群文件 / 群图片解析
    attachments_enabled: bool = True
    attachment_max_mb: int = 10
    attachment_max_per_hour: int = 30
    attachment_retention_days: int = 7
    vision_enabled: bool = True
    vision_model: str = DEFAULT_VISION_MODEL
    document_max_chars: int = 30000
    pdf_ocr_max_pages: int = 20
    dedupe_hours: int = 6
    html_push: bool = True

    # 截止提醒：早上列今天要做的，晚上再提醒一次
    deadline_reminders_enabled: bool = True
    deadline_morning: str = "07:30"
    deadline_evening: str = "21:00"

    # 事务闭环：候选确认卡和每周复盘
    candidate_push_enabled: bool = True
    candidate_min_confidence: float = 0.55
    candidate_reminder_hours: int = 24
    candidate_push_max_per_tick: int = 2
    candidate_push_max_per_day: int = 4
    weekly_review_enabled: bool = True
    # 推送预算与夜间静默：非紧急推送统一限流，紧急事项始终放行。
    push_daily_budget: int = 12
    quiet_hours: str = "23:00-07:00"
    weekly_review_weekday: int = 6
    weekly_review_time: str = "20:30"

    # 手机端待办台（Tailscale 或局域网访问）
    web_enabled: bool = True
    web_host: str = field(default_factory=_default_web_host)
    web_port: int = 8766
    web_base_url: str = ""
    web_token: str = ""
    update_channel: str = ""

    window_minutes: int = 10
    poll_seconds: int = 20
    urgent_immediate: bool = True
    min_score: int = 3
    max_items: int = 30
    max_batch: int = 400

    delivery_max_attempts: int = 4
    delivery_retry_seconds: int = 90
    message_retention_days: int = 30

    onebot_enabled: bool = False
    onebot_host: str = "127.0.0.1"
    onebot_port: int = 8765
    onebot_token: str = ""

    # NapCat 历史消息补采：用于服务重启、NapCat 掉线、关机期间的消息找回。
    catchup_enabled: bool = False
    catchup_hours: int = 24
    catchup_count: int = 50
    catchup_interval_minutes: int = 30
    napcat_api_url: str = "http://127.0.0.1:3001"
    napcat_api_token: str = ""
    qq_group_refresh_interval_seconds: int = 300
    napcat_qr_path: Path = field(default_factory=lambda: RUNTIME_ROOT / "data" / "qrcode.png")

    # 「托管」：让 NapCat 独占该 QQ 号时，临时退出/恢复用户自己的电脑版 QQ。
    hosting_quit_qq: bool = True
    hosting_restore_qq: bool = True
    # 托盘「退出」是否顺手把电脑版 QQ 开回来。默认关：用户 2026-10-12 反馈
    # 「托盘右键退出会自动开 QQ」，而 QQ 自己开着并不影响群务台任何功能，
    # 退出时多开一个进程属于越权。需要的人去设置页勾上即可。
    quit_restore_qq: bool = False
    hosting_auto_on_start: bool = False
    autostart: bool = False

    # 「实验室」：实验性功能开关，全部默认关闭，不影响现有行为。
    lab_search: bool = False
    lab_replies: bool = False
    lab_events: bool = False
    lab_rules: bool = False
    # 社区在线数上报（向公网汇总「同时在线几台」，不涉及任何消息内容）。默认关：
    # 用户没主动开就一个字节都不外发。
    community_telemetry: bool = False
    # 待回清单用来认「我」：QQ 号与昵称，都可留空，不写死。
    self_qq: str = ""
    self_nickname: str = ""

    http_timeout: int = 15
    llm_timeout: int = 60
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data")
    log_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "logs")
    env_file: Path | None = None

    @classmethod
    def from_legacy(cls, env: Mapping[str, str] | None = None, env_file: Path | None = None) -> "Settings":
        """按旧环境变量构造配置。运行根和默认 Web 地址在调用时从本模块读取。"""
        from noticehub.bootstrap.settings import build_settings

        return build_settings(
            cls,
            env,
            env_file,
            runtime_root=RUNTIME_ROOT,
            load_env_file=load_env_file,
            default_web_host=_default_web_host,
            parse_bool=parse_bool,
            parse_int=parse_int,
            parse_float=parse_float,
            split_list=split_list,
            parse_quiet_hours=parse_quiet_hours,
            parse_aliases=parse_aliases,
            default_dashscope_endpoint=DEFAULT_DASHSCOPE_ENDPOINT,
            default_vision_model=DEFAULT_VISION_MODEL,
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, env_file: Path | None = None) -> "Settings":
        return cls.from_legacy(env, env_file)

    def group_name(self, group_id: str) -> str:
        return self.group_aliases.get(group_id) or group_id or "未知群"

    _LAB_NAMES = ("search", "replies", "events", "rules")

    def lab_enabled(self, name: str) -> bool:
        """查询「实验室」开关；未知名字一律 False（fail-closed）。"""
        normalized = (name or "").strip().lower()
        return normalized in self._LAB_NAMES and getattr(self, f"lab_{normalized}")

    def accepts_group(self, group_id: str) -> bool:
        # fail-closed：未配置白名单时不处理任何群，避免照抄模板后默认接收全部群。
        return bool(self.group_whitelist) and group_id in self.group_whitelist

    def is_immediate_group(self, group_id: str) -> bool:
        return str(group_id or "") in self.immediate_groups

    def is_quiet_group(self, group_id: str) -> bool:
        return str(group_id or "") in self.quiet_groups

    def is_quiet_group_name(self, name: str) -> bool:
        target = str(name or "").strip()
        if not target:
            return False
        return any(
            target == str(group_id) or self.group_name(str(group_id)) == target
            for group_id in self.quiet_groups
        )

    def quiet_hours_range(self) -> tuple[int, int] | None:
        """返回 (起始分钟, 结束分钟)；未配置或非法时返回 None。"""
        text = str(self.quiet_hours or "").strip()
        if "-" not in text:
            return None
        start_text, _, end_text = text.partition("-")
        try:
            start_hour, start_minute = (int(part) for part in start_text.split(":", 1))
            end_hour, end_minute = (int(part) for part in end_text.split(":", 1))
        except (TypeError, ValueError):
            return None
        if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23):
            return None
        start, end = start_hour * 60 + start_minute, end_hour * 60 + end_minute
        if start == end:
            return None
        return start, end

    def in_quiet_hours(self, moment: dt.datetime) -> bool:
        bounds = self.quiet_hours_range()
        if bounds is None:
            return False
        start, end = bounds
        current = moment.hour * 60 + moment.minute
        if start < end:
            return start <= current < end
        # 跨零点，例如 23:00-07:00
        return current >= start or current < end

    def push_channels(self) -> list[str]:
        channels: list[str] = []
        if self.official_bot_enabled and self.push_c2c_openids and self.appid and self.secret:
            channels.append("qq-bot")
        if self.wxpusher_app_token and (self.wxpusher_uids or self.wxpusher_topic_ids):
            channels.append("wxpusher")
        if self.serverchan_keys:
            channels.append("serverchan")
        if self.pushplus_tokens:
            channels.append("pushplus")
        if self.webhook_urls:
            channels.append("webhook")
        return channels

    def problems(self) -> list[str]:
        """返回阻塞启动的问题列表（空列表表示可启动）。"""
        issues: list[str] = []
        if self.official_bot_enabled and (not self.appid or not self.secret):
            issues.append("缺少 QQ_BOT_APPID / QQ_BOT_SECRET，官方机器人无法登录。")
        if not self.push_channels():
            issues.append("未配置任何推送通道（QQ 私聊 openid / WxPusher / Server酱 / PushPlus / Webhook）。")
        if not self.group_whitelist:
            issues.append("QQ_DIGEST_GROUPS 未配置：不会处理任何群；请在 .env 填写要监控的群号。")
        if self.onebot_enabled and not self.onebot_token:
            issues.append(
                "已启用 OneBot 接收但未设置 QQ_DIGEST_ONEBOT_TOKEN：环回地址仍会启动（否则 NapCat 的消息会被静默拒收），"
                "接口对本机任何进程开放——属已知残留风险，建议在设置页配一个令牌。"
            )
        if self.catchup_enabled and not self.napcat_api_token:
            issues.append("已启用历史补采但未设置 NapCat API Token，补采会失败。")
        return issues

    @property
    def llm_active(self) -> bool:
        """Whether configured LLM processing can run without an API key."""
        return bool(self.llm_enabled and (self.llm_backend in {"auto", "codebuddy"} or self.dashscope_api_key))

    @property
    def endpoint_host(self) -> str:
        """当前 LLM 端点的主机名（小写），用于判断供应商能力。"""
        endpoint = str(self.dashscope_endpoint or "")
        without_scheme = endpoint.split("://", 1)[-1]
        return without_scheme.split("/", 1)[0].split("@", 1)[-1].split(":", 1)[0].strip().lower()

    @property
    def endpoint_is_dashscope(self) -> bool:
        """端点是否为 DashScope（含国际站/美国站子域）。"""
        host = self.endpoint_host
        return "dashscope" in host and host.endswith(".aliyuncs.com")

    @property
    def vision_active(self) -> bool:
        """读图是否真的可用（不只看开关）。

        非 DashScope 端点（DeepSeek / 本地 ollama / 其它 OpenAI 兼容服务）默认没有
        qwen 视觉模型，此时除非用户显式指定了 QQ_DIGEST_VL_MODEL，否则一律算不可用，
        免得每张图都去撞 404、白花 token 又刷屏日志。
        """
        if not (self.vision_enabled and self.dashscope_api_key):
            return False
        if self.vision_model != DEFAULT_VISION_MODEL:
            return True
        return self.endpoint_is_dashscope

    def describe(self) -> dict[str, Any]:
        """用于日志/doctor 的脱敏描述。"""
        return {
            "appid": _mask(self.appid),
            "official_bot_enabled": self.official_bot_enabled,
            "sandbox": self.sandbox,
            # 见 accepts_group：空白名单是 fail-closed，不是“全部群”。
            "groups": list(self.group_whitelist) or ["<未配置：不处理任何群>"],
            "group_aliases": dict(self.group_aliases),
            "quiet_groups": list(self.quiet_groups),
            "immediate_groups": list(self.immediate_groups),
            "push_channels": self.push_channels(),
            "push_openids": [_mask(item) for item in self.push_c2c_openids],
            "llm": self.llm_active,
            "llm_model": self.dashscope_model,
            "llm_timeout": self.llm_timeout,
            "llm_retries": self.llm_max_retries,
            "llm_defer_max_attempts": self.llm_defer_max_attempts,
            "include_raw": self.include_raw,
            "attachments": {
                "enabled": bool(self.attachments_enabled and self.dashscope_api_key),
                "max_mb": self.attachment_max_mb,
                "max_per_hour": self.attachment_max_per_hour,
                "retention_days": self.attachment_retention_days,
                "vision": self.vision_active,
                "vision_model": self.vision_model,
                "document_max_chars": self.document_max_chars,
                "pdf_ocr_max_pages": self.pdf_ocr_max_pages,
            },
            "dedupe_hours": self.dedupe_hours,
            "html_push": self.html_push,
            "deadline_reminders": {
                "enabled": self.deadline_reminders_enabled,
                "morning": self.deadline_morning,
                "evening": self.deadline_evening,
            },
            "web": {
                "enabled": bool(self.web_enabled),
                "host": self.web_host,
                "port": self.web_port,
                "base_url": self.web_base_url,
                "auth": bool(self.web_token),
            },
            "transaction_loop": {
                "candidate_push": bool(self.candidate_push_enabled),
                "candidate_min_confidence": self.candidate_min_confidence,
                "candidate_reminder_hours": self.candidate_reminder_hours,
                "candidate_push_max_per_day": self.candidate_push_max_per_day,
                "weekly_review": bool(self.weekly_review_enabled),
                "weekly_review_weekday": self.weekly_review_weekday,
                "weekly_review_time": self.weekly_review_time,
            },
            "window_minutes": self.window_minutes,
            "push_budget": {
                "daily": self.push_daily_budget,
                "quiet_hours": self.quiet_hours,
            },
            "urgent_immediate": self.urgent_immediate,
            "min_score": self.min_score,
            "onebot": {"enabled": self.onebot_enabled, "host": self.onebot_host, "port": self.onebot_port},
            "community_telemetry": bool(self.community_telemetry),
            "catchup": {
                "enabled": self.catchup_enabled,
                "hours": self.catchup_hours,
                "count": self.catchup_count,
                "interval_minutes": self.catchup_interval_minutes,
                "api_url": self.napcat_api_url,
            },
            "data_dir": str(self.data_dir),
            "log_dir": str(self.log_dir),
        }


def _mask(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-2:]}"


def ensure_dirs(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)
