"""服务编排：把「收消息 → 落库去重 → 攒窗口 → 出摘要 → 投递 → 记账」串起来。

本模块不解析消息、不发 HTTP：解析在 `summarizer` / `qq_digest`，通道在 `push`。
这里只做三件事——决定**什么时候**动作（`tick` 的节拍、三条定时任务、补采状态机）、
**动作完成后怎么记账**（摘要、待办、投递表、预算），以及**持有**长生命周期组件
（机器人、接收器、三个本地读取器、附件队列、Web 服务器、监督线程）。

时间一律由调用方注入：公开方法都收 `now`，缺省才读 `now_local()`。
"""

from __future__ import annotations

import datetime as dt
import html as html_module
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .application import ingest, schedule
from .application.attachments import AttachmentWorker
from .attachments import cleanup_files
from .bot import BotRunner
from .catchup import NapCatClient, backfill
from .chaoxing_receiver import ChaoxingReader
from .config import Settings, ensure_dirs
from .dingtalk_receiver import DingTalkLocalReader
from .domain.events import event_level_blocks, merge_extracted_event
from .domain.message import MessageEvent
from .event_extract import CONFIDENCE_RANK, extract_events
from .infrastructure.notify import PushManager
from .push import build_pushers
from .receiver import OneBotReceiver
from .store import Store
from .summarizer import Digest, html_push_text, record_to_message
from .timeutil import iso, now_local, parse_iso
from .webapp import TaskWebServer
from .weburl import build_web_url
from .wechat_receiver import WeChatLocalReader

LOGGER = logging.getLogger(__name__)

# 截止提醒一次最多列几条：手机上再多就滑不完了。
DEADLINE_MAX_ITEMS = 5
# 等引擎时的短轮询；引擎登录后能立刻补采，不必等到下一次成功间隔。
CATCHUP_WAIT_ENGINE_SECONDS = 20
# 失败退避阶梯，按失败次数取下标，最后一档封顶。
CATCHUP_BACKOFF_STEPS = (60, 120, 300, 600)
# 裁剪与快照的节奏：裁剪每 6 小时一次，快照每天一次。
PRUNE_INTERVAL_SECONDS = 6 * 3600
SNAPSHOT_KEEP = 7
# 监督线程多久检查一次机器人是否还活着。
SUPERVISOR_INTERVAL_SECONDS = 30
# 「别刷屏」类告警的最小间隔。
WARN_REPEAT_SECONDS = 30 * 60
# 候选提醒一次最多看多少条候选。
CANDIDATE_SCAN_LIMIT = 30
# 各种文案截断，避免一条消息把日志/推送撑坏。
TEXT_LIMIT = 60


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clock_minutes(text: Any) -> int | None:
    """`07:30` → 450。认不出就给 None（调用方按「没到点」处理）。"""
    parts = str(text or "").strip().split(":")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def _clock_text(value: dt.datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if isinstance(value, dt.datetime) else ""


class DigestService:
    """一个进程一套编排：注入的组件永不重建，自建的组件懒加载一次。"""

    def __init__(
        self,
        settings: Settings,
        *,
        store: Store | None = None,
        logger: logging.Logger | None = None,
        bot: Any = None,
        receiver: Any = None,
        pushers: Sequence[Any] | None = None,
    ) -> None:
        ensure_dirs(settings)
        self.settings = settings
        self.logger = logger or LOGGER
        self.store = (
            store
            if store is not None
            else Store(settings.data_dir / "digest.sqlite3", retention_days=settings.message_retention_days)
        )

        self._bot = bot
        self._receiver = receiver
        self._attachment_worker: AttachmentWorker | None = None
        self._pushers = list(pushers or ())
        self._pushers_locked = pushers is not None
        self._push_manager: PushManager | None = None
        self._web: TaskWebServer | None = None

        self._dingtalk_reader: DingTalkLocalReader | None = None
        self._wechat_reader: WeChatLocalReader | None = None
        self._chaoxing_reader: ChaoxingReader | None = None

        self._stop_event = threading.Event()
        self._supervisor: threading.Thread | None = None
        self._catchup_thread: threading.Thread | None = None
        self._catchup_failures = 0
        self._catchup_lock = threading.Lock()
        self._warned: dict[str, tuple[str, dt.datetime]] = {}

        # 公开状态：测试与健康检查直接读，必须是能 JSON 序列化的原样值。
        self.started_at: dt.datetime | None = None
        self.last_tick_at: dt.datetime | None = None
        self.last_catchup_at: dt.datetime | None = None
        self.last_catchup_attempt_at: dt.datetime | None = None
        self.catchup_state = "idle"
        self.last_catchup_ok_at: dt.datetime | None = None
        self.last_catchup_error = ""
        self.last_llm_error = ""
        self.last_llm_error_at: dt.datetime | None = None
        self.last_defer_reason = ""
        self.last_defer_at: dt.datetime | None = None
        self.last_push_at: dt.datetime | None = None

        self._ensure_scan_status()

    # ---------------------------------------------------------------- 基础访问器

    def now(self) -> dt.datetime:
        """服务时钟；所有「现在」都从这里取，测试才好钉住。"""
        return now_local()

    @property
    def bot(self) -> Any:
        if self._bot is None:
            self._bot = BotRunner(
                self.settings, self.on_message, data_dir=self.settings.data_dir, logger=self.logger
            )
        return self._bot

    @property
    def receiver(self) -> Any:
        if self._receiver is None:
            self._receiver = OneBotReceiver(
                self.settings,
                self.on_message,
                self._submit_attachment,
                logger=self.logger,
                health_provider=self.health_payload,
            )
        return self._receiver

    @property
    def attachment_worker(self) -> AttachmentWorker:
        if self._attachment_worker is None:
            self._attachment_worker = AttachmentWorker(
                self.settings,
                self.on_message,
                logger=self.logger,
                napcat_call=self._napcat_call,
            )
        return self._attachment_worker

    def push_manager(self) -> PushManager:
        """与投递流程同一个实例；诊断页与「发送测试」共用同一张去重表。"""
        if self._push_manager is None:
            self._push_manager = self._build_push_manager()
        elif not self._pushers_locked:
            self._upgrade_qq_channel()
        return self._push_manager

    def _build_push_manager(self) -> PushManager:
        return PushManager(self.store, self.settings, list(self._pushers), logger=self.logger)

    def _upgrade_qq_channel(self) -> None:
        """官方机器人上线后补上 QQ 通道；已经有过就不重复重建。"""
        bot = self._bot
        api = getattr(bot, "api", None) if bot is not None else None
        if api is None:
            return
        if any(str(getattr(pusher, "name", "")) == "qq-bot" for pusher in self._pushers):
            return
        loop = getattr(bot, "loop", None)
        if loop is None:
            return
        rebuilt = build_pushers(self.settings, api=api, loop=loop)
        if not rebuilt:
            return
        self._pushers = rebuilt
        self._push_manager = self._build_push_manager()

    # ---------------------------------------------------------------- 收消息

    def on_message(self, record: Mapping[str, Any]) -> bool:
        """是否新插入。重复消息返回假且不跑规则；任何故障都不抛给采集线程。"""
        event = MessageEvent.from_mapping(record)
        return bool(ingest.accept_message(event, service=self, record=record, now=self.now))

    def _apply_rules(self, record: Mapping[str, Any], msg_id: str, group_id: str, group_name: str) -> None:
        ingest.apply_rules(
            record,
            msg_id,
            group_id,
            group_name,
            store=self.store,
            settings=self.settings,
            logger=self.logger,
        )

    def _submit_attachment(self, attachment: Any) -> bool:
        try:
            return bool(self.attachment_worker.submit(attachment))
        except Exception:  # noqa: BLE001 - 附件入队失败不能影响收消息
            self.logger.exception("附件入队失败")
            return False

    def _napcat_call(self, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._napcat_client().call(action, dict(payload or {}))

    def _napcat_client(self) -> NapCatClient:
        return NapCatClient(
            self.settings.napcat_api_url,
            self.settings.napcat_api_token,
            self.settings.http_timeout,
        )

    # ---------------------------------------------------------------- 扫描状态

    _SCAN_DEFAULTS = (
        "scan_state",
        "scan_stage",
        "scan_started_monotonic",
        "scan_started_at",
        "last_read_at",
        "last_read_seconds",
        "last_error",
    )

    def _ensure_scan_status(self) -> None:
        """两张扫描状态字典由本类负责建；读取器只往里写。"""
        self._refresh_scan_status("dingtalk_status", bool(self.settings.dingtalk_enabled and self.settings.dingtalk_group_whitelist))
        self._refresh_scan_status("wechat_status", bool(self.settings.wechat_enabled and self.settings.wechat_group_whitelist))

    def _refresh_scan_status(self, name: str, enabled: bool) -> dict[str, Any]:
        status = getattr(self.settings, name, None)
        if not isinstance(status, dict):
            status = {}
            setattr(self.settings, name, status)
        defaults: dict[str, Any] = {
            "scan_state": "idle" if enabled else "disabled",
            "scan_stage": "等待扫描" if enabled else "未启用",
            "scan_started_monotonic": 0.0,
            "scan_started_at": "",
            "last_read_at": "",
            "last_read_seconds": 0.0,
            "last_error": "",
        }
        for key in self._SCAN_DEFAULTS:
            status.setdefault(key, defaults[key])
        if status.get("scan_state") != "running":
            status["scan_state"] = defaults["scan_state"]
            status["scan_stage"] = defaults["scan_stage"]
        return status

    def start_scan(self, source: Any) -> bool:
        """手动触发一次扫描；来源不认识、没启用、没读取器一律假，绝不抛。"""
        name = str(source or "").strip().lower()
        if name == "dingtalk":
            if not (self.settings.dingtalk_enabled and self.settings.dingtalk_group_whitelist):
                return False
            reader = self._dingtalk_reader
        elif name == "wechat":
            if not (self.settings.wechat_enabled and self.settings.wechat_group_whitelist):
                return False
            reader = self._wechat_reader
        else:
            return False
        if reader is None:
            return False
        try:
            return bool(reader.start_scan())
        except Exception:  # noqa: BLE001 - 手动触发失败只记日志
            self.logger.exception("手动扫描失败：%s", name)
            return False

    # ---------------------------------------------------------------- 组件启停

    def start(self, *, start_bot: bool = True) -> None:
        if self.started_at is not None:
            return
        self.started_at = self.now()
        self._stop_event.clear()
        if start_bot:
            self._start_official_bot()
        self._start_onebot_receiver()
        self._start_dingtalk_reader()
        self._start_chaoxing_reader()
        self._start_wechat_reader()
        self._start_attachments()
        self._start_web()
        if self.settings.catchup_enabled:
            self._maybe_catchup(self.now())
        self._start_supervisor()
        self.logger.info(
            "服务已启动，窗口 %d 分钟，轮询 %d 秒。",
            int(self.settings.window_minutes),
            max(5, int(self.settings.poll_seconds or 20)),
        )

    def _start_official_bot(self) -> None:
        if not self.settings.official_bot_enabled:
            self.logger.info("官方 QQ 机器人已禁用，仅使用 OneBot/NapCat。")
            return
        if not (self.settings.appid and self.settings.secret):
            self.logger.error("缺少 AppID/AppSecret，官方机器人未启动。")
            return
        try:
            started = bool(self.bot.start())
        except Exception as error:  # noqa: BLE001 - 机器人起不来不阻断其它组件
            self.logger.exception("官方机器人启动失败：%s", error)
            return
        self.logger.info("官方机器人启动：%s", started)

    def _start_onebot_receiver(self) -> None:
        if not self.settings.onebot_enabled:
            return
        try:
            self.receiver.start()
        except Exception as error:  # noqa: BLE001
            self.logger.error("OneBot 接收器启动失败：%s", error)

    def _start_dingtalk_reader(self) -> None:
        if not (self.settings.dingtalk_enabled and self.settings.dingtalk_group_whitelist):
            self._dingtalk_reader = None
            return
        try:
            reader = DingTalkLocalReader(
                self.settings, self.on_message, logger=self.logger, store=self.store
            )
            reader.start()
            self._dingtalk_reader = reader
        except Exception:  # noqa: BLE001
            self.logger.exception("钉钉读取器启动失败")
            self._dingtalk_reader = None

    def _start_chaoxing_reader(self) -> None:
        if not (self.settings.chaoxing_enabled and self.settings.chaoxing_cookie):
            self._chaoxing_reader = None
            return
        try:
            reader = ChaoxingReader(self.settings, self.on_message, self.logger, self.store)
            reader.start()
            self._chaoxing_reader = reader
        except Exception:  # noqa: BLE001
            self.logger.exception("学习通读取器启动失败")
            self._chaoxing_reader = None

    def _start_wechat_reader(self) -> None:
        if not (self.settings.wechat_enabled and self.settings.wechat_group_whitelist):
            self._wechat_reader = None
            return
        try:
            # 形状即契约：测试按「位置传群列表、关键字传其余」锁这条调用，别改成别的位置顺序。
            reader = WeChatLocalReader(
                tuple(str(group) for group in self.settings.wechat_group_whitelist),
                interval_seconds=int(self.settings.wechat_scan_interval_seconds),
                on_message=self.on_message,
                logger=self.logger,
                status=self.settings.wechat_status,
            )
            reader.start()
            self._wechat_reader = reader
        except Exception:  # noqa: BLE001
            self.logger.exception("微信读取器启动失败")
            self._wechat_reader = None

    def _start_attachments(self) -> None:
        if not self.settings.attachments_enabled:
            return
        worker = self.attachment_worker
        worker.start()
        self.logger.info(
            "附件解析已启用：单文件上限 %dMB、每小时最多 %d 个、图片OCR=%s（%s）、扫描PDF限%d页。",
            int(self.settings.attachment_max_mb),
            int(self.settings.attachment_max_per_hour),
            "开" if self.settings.vision_active else "关",
            self.settings.vision_model,
            int(self.settings.pdf_ocr_max_pages),
        )

    def _start_web(self) -> None:
        if not self.settings.web_enabled:
            return
        try:
            web = TaskWebServer(
                self.settings, self.store, logger=self.logger, meta_provider=self._meta_info
            )
            web.reload_chaoxing = self.reload_chaoxing_reader
            web.reload_dingtalk = self.reload_dingtalk_reader
            web.reload_wechat = self.reload_wechat_reader
            web.dingtalk_reader_provider = lambda: self._dingtalk_reader
            web.wechat_reader_provider = lambda: self._wechat_reader
            web.start_scan = self.start_scan
            web.start()
            self._web = web
        except Exception:  # noqa: BLE001 - 待办台起不来不影响收消息
            self.logger.exception("待办台启动失败")

    def _start_supervisor(self) -> None:
        if self._supervisor is not None and self._supervisor.is_alive():
            return
        self._supervisor = threading.Thread(
            target=self._supervise_loop, name="service-supervisor", daemon=True
        )
        self._supervisor.start()

    def _supervise_loop(self) -> None:
        while not self._stop_event.wait(SUPERVISOR_INTERVAL_SECONDS):
            bot = self._bot
            if bot is None or getattr(bot, "is_alive", False):
                continue
            reason = str(getattr(bot, "last_error", "") or "进程已退出")
            self.logger.warning("检测到 QQ 机器人未运行（%s），尝试重启。", reason)
            try:
                bot.start()
            except Exception as error:  # noqa: BLE001
                self.logger.exception("QQ 机器人重启失败：%s", error)

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._catchup_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        self._catchup_thread = None

        if self._attachment_worker is not None:
            try:
                self._attachment_worker.stop()
            except Exception:  # noqa: BLE001
                self.logger.exception("附件队列停止失败")

        if self._web is not None:
            try:
                self._web.stop()
            except Exception:  # noqa: BLE001
                self.logger.exception("待办台停止失败")
            self._web = None

        supervisor = self._supervisor
        if supervisor is not None and supervisor.is_alive():
            supervisor.join(timeout=5)
        self._supervisor = None

        self._stop_component("QQ 机器人", self._bot, "stop")
        self._stop_component("OneBot 接收器", self._receiver, "stop")
        self._stop_component("钉钉读取器", self._dingtalk_reader, "stop")
        self._stop_component("学习通读取器", self._chaoxing_reader, "stop")
        self._stop_component("微信读取器", self._wechat_reader, "stop")
        self._dingtalk_reader = None
        self._wechat_reader = None
        self._chaoxing_reader = None

        self.started_at = None
        self.logger.info("服务已停止。")

    def _stop_component(self, label: str, component: Any, method: str) -> None:
        if component is None:
            return
        action = getattr(component, method, None)
        if not callable(action):
            return
        try:
            action()
        except Exception:  # noqa: BLE001 - 一处停不下来不能中断后面的停止
            self.logger.exception("%s停止失败", label)

    def reload_dingtalk_reader(self) -> None:
        if self.started_at is None:
            return
        self._stop_component("钉钉读取器", self._dingtalk_reader, "stop")
        self._dingtalk_reader = None
        self._refresh_scan_status(
            "dingtalk_status",
            bool(self.settings.dingtalk_enabled and self.settings.dingtalk_group_whitelist),
        )
        self._start_dingtalk_reader()

    def reload_chaoxing_reader(self) -> None:
        if self.started_at is None:
            return
        self._stop_component("学习通读取器", self._chaoxing_reader, "stop")
        self._chaoxing_reader = None
        self._start_chaoxing_reader()

    def reload_wechat_reader(self) -> None:
        if self.started_at is None:
            return
        self._stop_component("微信读取器", self._wechat_reader, "stop")
        self._wechat_reader = None
        self._refresh_scan_status(
            "wechat_status",
            bool(self.settings.wechat_enabled and self.settings.wechat_group_whitelist),
        )
        self._start_wechat_reader()

    # ---------------------------------------------------------------- 主循环

    def run_forever(self, poll_seconds: int | None = None) -> None:
        interval = max(5, int(poll_seconds or self.settings.poll_seconds))
        self.start()
        try:
            while not self._stop_event.is_set():
                if self._stop_event.wait(interval):
                    break
                try:
                    self.tick()
                except Exception:  # noqa: BLE001 - 单轮失败不能让常驻进程退出
                    self.logger.exception("调度轮询失败")
        except KeyboardInterrupt:
            self.logger.info("收到中断信号，正在停止。")
        finally:
            self.stop()

    def tick(self, now: dt.datetime | None = None) -> list[Digest]:
        """跑一轮调度；返回本轮真正产出的摘要（含静默归档那一条）。"""
        return schedule.tick(now, service=self)

    def import_existing_tasks(self, *, days: int = 7) -> int:
        return schedule.import_existing_tasks(days, service=self, now=self.now)

    # ---------------------------------------------------------------- 投递

    def _push_gate(self, now: dt.datetime, *, ignore_quiet: bool = False) -> tuple[bool, str]:
        """免打扰与当日预算。紧急推送不经过这里（调用方直接投递）。"""
        if not ignore_quiet and self.settings.in_quiet_hours(now):
            return False, "quiet_hours"
        budget = _as_int(self.settings.push_daily_budget)
        if budget > 0 and self._push_count(now) >= budget:
            return False, "daily_budget"
        return True, ""

    def _push_budget_key(self, now: dt.datetime) -> str:
        return "push_budget:%s" % now.strftime("%Y-%m-%d")

    def _push_count(self, now: dt.datetime) -> int:
        return _as_int(self.store.meta_get(self._push_budget_key(now), "0"))

    def _count_push(self, now: dt.datetime) -> None:
        self.store.meta_set(self._push_budget_key(now), str(self._push_count(now) + 1))
        self.last_push_at = now

    def _hold_batch(self, now: dt.datetime, count: int, reason: str) -> None:
        self.last_defer_reason = reason
        self.last_defer_at = now
        self.logger.info("本批 %d 条消息暂不推送（%s），留到下次窗口。", count, reason)

    def _bump(self, key: str) -> int:
        value = _as_int(self.store.meta_get(key, "0")) + 1
        self.store.meta_set(key, str(value))
        return value

    def _publish(self, digest: Digest, *, when: dt.datetime | None = None, dedupe_key: str = "") -> bool:
        """内部唯一投递入口：先归档并标记已处理，再同步待办，最后才发。"""
        now = when or self.now()

        if not digest.items:
            digest.kind = "silent"
            digest.id = self.store.insert_digest(
                kind="silent",
                window_start=iso(digest.window_start),
                window_end=iso(digest.window_end),
                body="",
                payload=[],
                message_count=digest.message_count,
                llm_used=False,
            )
            self.store.mark_processed(digest.message_ids, digest.id)
            self.logger.info(
                "本次 %d 条消息无可推送内容（重复或无需动手），静默归档未推送。",
                digest.message_count,
            )
            return False

        if digest.llm_error:
            error_text = digest.llm_error[:400]
            self.last_llm_error = error_text
            self.last_llm_error_at = now
            self._bump("llm_failures_total")
            if digest.llm_retryable and self._llm_should_defer(now):
                self._bump("llm_deferred_total")
                self.logger.info("大模型暂时不可用，本批 %d 条消息推迟重试：%s", digest.message_count, error_text)
                self.last_defer_reason = "llm_retry"
                self.last_defer_at = now
                return False
            self._bump("llm_fallbacks_total")
            self._clear_llm_defer()
            if digest.llm_retryable:
                self.logger.error(
                    "大模型重试耗尽，本批 %d 条消息回退本地规则推送：%s",
                    digest.message_count,
                    error_text,
                )
            else:
                self.logger.warning(
                    "大模型不可用（%s），本批 %d 条消息使用本地规则推送。",
                    error_text,
                    digest.message_count,
                )
        else:
            self._clear_llm_defer()

        digest.id = self.store.insert_digest(
            kind=digest.kind,
            window_start=iso(digest.window_start),
            window_end=iso(digest.window_end),
            body=digest.body,
            payload=digest.payload(),
            message_count=digest.message_count,
            llm_used=digest.llm_used,
        )
        self.store.mark_processed(digest.message_ids, digest.id)

        added = 0
        try:
            added = len(self._sync_tasks(digest))
        except Exception:  # noqa: BLE001 - 建任务失败不能连累推送
            self.logger.exception(
                "摘要 #%s 任务同步失败，本批摘要照常投递；可用 import_existing_tasks 从摘要补建任务。",
                digest.id,
            )

        if self.settings.html_push and digest.items:
            digest.html = html_push_text(digest, self.settings)

        manager = self.push_manager()
        if not manager.has_channels:
            self.logger.error("没有可用推送通道，摘要仅归档到数据库（digest_id=%s）。", digest.id)
            return False

        outcomes = manager.send_digest(digest, dedupe_key=dedupe_key)
        sent = failed = skipped = 0
        for outcome in outcomes:
            if outcome.ok and not outcome.skipped:
                sent += 1
            elif not outcome.ok:
                failed += 1
            else:
                skipped += 1
        if sent:
            self._count_push(now)
        self.logger.info(
            "摘要 #%s（%s）投递：成功 %d，失败 %d，静默跳过 %d，新增任务 %d",
            digest.id,
            digest.kind,
            sent,
            failed,
            skipped,
            added,
        )
        for outcome in outcomes:
            if not outcome.ok:
                self.logger.warning(
                    "待重试 %s:%s -> %s", outcome.channel, outcome.target, outcome.error
                )
        return sent > 0

    def _retry(self, now: dt.datetime | None = None) -> list[Any]:
        """重试挂起的投递；同一轮内重试成功也算这一轮发的。"""
        stamp = now or self.now()
        manager = self.push_manager()
        if not manager.has_channels:
            return []
        try:
            outcomes = manager.retry_pending()
        except Exception:  # noqa: BLE001
            self.logger.exception("挂起投递重试失败")
            return []
        for outcome in outcomes:
            attempts = _as_int(getattr(outcome, "attempts", 0))
            if outcome.ok:
                self.logger.info("重试成功 %s:%s（第 %d 次）", outcome.channel, outcome.target, attempts)
                self._count_push(stamp)
            else:
                self.logger.warning(
                    "重试失败 %s:%s -> %s", outcome.channel, outcome.target, outcome.error
                )
        return outcomes

    # ---------------------------------------------------------------- LLM 推迟

    def _llm_should_defer(self, now: dt.datetime) -> bool:
        """可重试的模型故障先等等，别把半成品推给用户；次数用完才降级。"""
        limit = _as_int(self.settings.llm_defer_max_attempts)
        if limit <= 0:
            return False
        window = max(1, _as_int(self.settings.llm_defer_window_minutes, 15)) * 60
        started = parse_iso(self.store.meta_get("llm_defer:started"))
        count = _as_int(self.store.meta_get("llm_defer:count", "0"))
        if started is None or (now - started).total_seconds() > window:
            self.store.meta_set("llm_defer:started", iso(now))
            self.store.meta_set("llm_defer:count", "1")
            return True
        if count >= limit:
            return False
        self.store.meta_set("llm_defer:count", str(count + 1))
        return True

    def _clear_llm_defer(self) -> None:
        self.store.meta_set("llm_defer:started", "")
        self.store.meta_set("llm_defer:count", "0")

    # ---------------------------------------------------------------- 待办同步

    def _sync_tasks(self, digest: Digest) -> list[dict[str, Any]]:
        return schedule.sync_tasks(self, digest)

    def _merge_extracted_event(self, item: dict[str, Any], *, not_before: dt.datetime | None = None) -> str:
        """把规则抽出来的时间/地点并进条目；返回最高置信度（无事件给空串）。"""
        message = item.get("message")
        text = str(getattr(message, "text", "") or "")
        if not text.strip():
            return ""
        events = extract_events(text, min_level="medium")
        return merge_extracted_event(
            item, events, not_before=not_before, confidence_rank=CONFIDENCE_RANK
        )

    def _event_level_blocks(self, event_level: str, item: Mapping[str, Any]) -> bool:
        return event_level_blocks(event_level, item)

    def _task_url(self, task_id: Any) -> str:
        base = str(self.store.meta_get("web_base_url", "") or "")
        token = str(self.settings.web_token or self.store.meta_get("web_token", "") or "")
        return build_web_url(self.settings, token=token, task_id=_as_int(task_id), base_url=base)

    # ---------------------------------------------------------------- 截止提醒

    def _clock_reached(self, now: dt.datetime, text: Any) -> bool:
        target = _clock_minutes(text)
        if target is None:
            return False
        return now.hour * 60 + now.minute >= target

    def _deadline_items(self, kind: str, now: dt.datetime) -> list[dict[str, Any]]:
        today = now.date()
        tomorrow = today + dt.timedelta(days=1)
        picked: list[dict[str, Any]] = []
        for task in self.store.list_open_tasks(limit=2000):
            deadline = parse_iso(task.get("deadline"))
            if deadline is None:
                continue
            if kind == "morning":
                if deadline.date() > today:
                    continue
            elif deadline.date() != tomorrow:
                continue
            reminded = parse_iso(task.get("last_reminded_at"))
            if reminded is not None and reminded.date() == today:
                continue
            snooze = parse_iso(task.get("snooze_until"))
            if snooze is not None and snooze > now:
                continue
            picked.append({**task, "_deadline": deadline})
        picked.sort(key=lambda row: (row["_deadline"], _as_int(row.get("id"))))
        return picked[:DEADLINE_MAX_ITEMS]

    def _reminder_digest(self, kind: str, tasks: Sequence[Mapping[str, Any]], now: dt.datetime) -> Digest:
        title = "今日截止" if kind == "morning" else "截止提醒"
        lines: list[str] = ["【%s】%s" % (title, now.strftime("%Y-%m-%d %H:%M")), ""]
        items: list[dict[str, Any]] = []
        for index, task in enumerate(tasks, 1):
            summary = str(task.get("summary") or task.get("action") or "")
            deadline = task.get("_deadline")
            lines.append("%d. %s" % (index, summary))
            if isinstance(deadline, dt.datetime):
                lines.append("   截止：%s" % deadline.strftime("%Y-%m-%d %H:%M"))
            groups = [str(name) for name in (task.get("groups") or []) if str(name).strip()]
            if groups:
                lines.append("   来源：%s" % "、".join(groups[:2]))
            lines.append("")
            items.append(
                {
                    "category": str(task.get("category") or "action"),
                    "importance": _as_int(task.get("importance"), 3),
                    "summary": summary,
                    "action": str(task.get("action") or ""),
                    "deadline_dt": deadline,
                    "group_name": groups[0] if groups else "",
                    "msg_id": str(task.get("task_key") or ""),
                    "message": record_to_message(
                        {
                            "content": summary,
                            "group_name": groups[0] if groups else "",
                            "sender_name": "",
                            "ts": iso(now),
                        }
                    ),
                }
            )
        body = "\n".join(lines).rstrip()
        return Digest(
            kind=kind,
            window_start=now,
            window_end=now,
            items=items,
            body=body,
            summary=title,
            message_count=len(items),
            groups=[str(task.get("groups") or [""])[0] if isinstance(task.get("groups"), list) and task.get("groups") else "" for task in tasks],
        )

    def _deliver_digest(self, digest: Digest, *, dedupe_key: str, now: dt.datetime) -> bool:
        digest.id = self.store.insert_digest(
            kind=digest.kind,
            window_start=iso(digest.window_start),
            window_end=iso(digest.window_end),
            body=digest.body,
            payload=digest.payload(),
            message_count=digest.message_count,
            llm_used=False,
        )
        outcomes = self.push_manager().send_digest(digest, dedupe_key=dedupe_key)
        sent = any(outcome.ok and not outcome.skipped for outcome in outcomes)
        if sent:
            self._count_push(now)
        return sent

    def _warn_repeated(self, text: str, now: dt.datetime, *, signature: str = "") -> None:
        """同一句告警最多每 30 分钟一次；状态变了立刻再说一次。"""
        last = self._warned.get(text)
        if last is not None:
            previous, at = last
            if previous == signature and (now - at).total_seconds() < WARN_REPEAT_SECONDS:
                return
        self._warned[text] = (signature, now)
        self.logger.warning(text)

    def _maybe_deadline_reminders(self, now: dt.datetime | None = None) -> None:
        stamp = now or self.now()
        if not self.settings.deadline_reminders_enabled:
            return
        evening_reached = self._clock_reached(stamp, self.settings.deadline_evening)
        for kind, at in (("morning", self.settings.deadline_morning), ("evening", self.settings.deadline_evening)):
            if not self._clock_reached(stamp, at):
                continue
            key = "deadline_reminder:%s:%s" % (kind, stamp.strftime("%Y-%m-%d"))
            if self.store.meta_get(key, ""):
                continue
            if kind == "morning" and evening_reached:
                # 一开机就是晚上：早上那次不补发，只把当天的键写掉。
                self.store.meta_set(key, iso(stamp))
                continue
            items = self._deadline_items(kind, stamp)
            if not items:
                continue
            if not self.push_manager().has_channels:
                self._warn_repeated(
                    "截止提醒无可用推送通道，保留待下次重试。",
                    stamp,
                    signature="%s:%d" % (kind, len(items)),
                )
                continue
            allowed, reason = self._push_gate(stamp, ignore_quiet=True)
            if not allowed:
                self.logger.info("截止提醒暂不推送（%s），保留待下次重试。", reason)
                continue
            title = "今日截止" if kind == "morning" else "截止提醒"
            digest = self._reminder_digest(kind, items, stamp)
            if not self._deliver_digest(digest, dedupe_key="deadline:%s:%s" % (kind, stamp.strftime("%Y-%m-%d")), now=stamp):
                self.logger.warning("截止提醒投递失败，保留待下次重试。")
                continue
            self.store.meta_set(key, iso(stamp))
            for task in items:
                self.store.mark_task_reminded(
                    task.get("id"),
                    detail={"kind": kind, "deadline": task.get("deadline") or ""},
                    when=stamp,
                )
            self.logger.info("已推送%s，涵盖 %d 条截止事项。", title, len(items))

    # ---------------------------------------------------------------- 候选提醒

    def _candidate_body(self, task: Mapping[str, Any], now: dt.datetime) -> tuple[str, str, str]:
        summary = str(task.get("summary") or "").strip() or "有一条通知需要确认"
        groups = [str(name) for name in (task.get("groups") or []) if str(name).strip()]
        deadline = parse_iso(task.get("deadline"))
        evidence = str(task.get("evidence") or "").strip() or str(task.get("source_text") or "").strip()
        url = self._task_url(task.get("id"))
        lines = [
            "可能要做：%s" % summary,
            "来源：%s" % (groups[0] if groups else "来源群未知"),
            "推测截止：%s" % (_clock_text(deadline) or "未识别"),
            "依据：%s" % (evidence[:120] if evidence else "原文里没有更明确的依据"),
            "打开待办台确认：%s" % url,
        ]
        body = "\n".join(lines)
        card = [
            '<div style="font-size:12px;color:#b26a00;font-weight:600">待确认</div>',
            '<div style="font-size:14px;line-height:1.5;color:#1f1f1f">%s</div>'
            % html_module.escape(summary[:120]),
            '<div style="font-size:12px;color:#777">%s · 截止 %s</div>'
            % (html_module.escape(groups[0] if groups else "来源群未知"), html_module.escape(_clock_text(deadline) or "未识别")),
            '<div style="font-size:12px"><a href="%s">在待办台确认或忽略</a></div>'
            % html_module.escape(url),
        ]
        return body, "待确认：%s" % summary[:40], "".join(card)

    def _maybe_candidate_reminders(self, now: dt.datetime | None = None) -> None:
        stamp = now or self.now()
        if not self.settings.candidate_push_enabled:
            return
        per_tick = _as_int(self.settings.candidate_push_max_per_tick)
        if per_tick <= 0:
            return
        day_cap = _as_int(self.settings.candidate_push_max_per_day)
        day_key = "candidate_push:%s" % stamp.strftime("%Y-%m-%d")
        used_today = _as_int(self.store.meta_get(day_key, "0"))
        if day_cap > 0 and used_today >= day_cap:
            return
        manager = self.push_manager()
        if not manager.has_channels:
            return
        min_confidence = _as_float(self.settings.candidate_min_confidence, 0.55)
        interval = max(1, _as_int(self.settings.candidate_reminder_hours, 24)) * 3600
        sent_this_tick = 0
        for task in self.store.list_tasks(statuses=("candidate",), limit=CANDIDATE_SCAN_LIMIT):
            if sent_this_tick >= per_tick:
                break
            if day_cap > 0 and used_today >= day_cap:
                break
            if _as_float(task.get("confidence")) < min_confidence:
                continue
            reminded = parse_iso(task.get("last_reminded_at"))
            if reminded is not None and (stamp - reminded).total_seconds() < interval:
                continue
            snooze = parse_iso(task.get("snooze_until"))
            if snooze is not None and snooze > stamp:
                continue
            allowed, reason = self._push_gate(stamp)
            if not allowed:
                self.logger.info("候选确认提醒暂不推送（%s）。", reason)
                return
            body, summary_text, card = self._candidate_body(task, stamp)
            outcomes = manager.send_card(
                "有一条通知可能需要处理",
                body,
                dedupe_key="candidate:%s:%s" % (task.get("id"), stamp.strftime("%Y-%m-%d")),
                summary=summary_text,
                html=card,
            )
            touched = any(outcome.ok for outcome in outcomes)
            if not touched:
                continue
            self.store.mark_task_reminded(
                task.get("id"),
                detail={"kind": "candidate_confirmation", "confidence": _as_float(task.get("confidence"))},
                when=stamp,
            )
            sent_this_tick += 1
            used_today += 1
            self.store.meta_set(day_key, str(used_today))
            if any(outcome.ok and not outcome.skipped for outcome in outcomes):
                self._count_push(stamp)

    # ---------------------------------------------------------------- 每周复盘

    def _weekly_review_body(self, now: dt.datetime) -> str:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) - dt.timedelta(days=7)
        metrics = self.store.task_metrics(start=start, end=now)
        lines = [
            "新增候选 %d 条，确认 %d 条，忽略 %d 条"
            % (
                _as_int(metrics.get("candidates")),
                _as_int(metrics.get("confirmed")),
                _as_int(metrics.get("dismissed")),
            ),
            "完成 %d 条，提醒 %d 次，提醒后完成 %d 条"
            % (
                _as_int(metrics.get("completions")),
                _as_int(metrics.get("reminders")),
                _as_int(metrics.get("completions_after_reminder")),
            ),
            "确认率 %s%%，忽略率 %s%%，逾期未完成 %d 条"
            % (
                metrics.get("confirmation_rate", 0.0),
                metrics.get("dismissal_rate", 0.0),
                _as_int(metrics.get("overdue")),
            ),
        ]
        try:
            insights = self.store.correction_insights(days=7, min_group_hits=2)
        except Exception:  # noqa: BLE001 - 复盘不能因为纠错统计挂掉
            self.logger.exception("纠错统计失败，本次复盘略过提示")
            insights = []
        for line in insights[:3]:
            lines.append("纠错提示：%s" % line)
        return "\n".join(lines)

    def _maybe_weekly_review(self, now: dt.datetime | None = None) -> None:
        stamp = now or self.now()
        if not self.settings.weekly_review_enabled:
            return
        if stamp.weekday() != _as_int(self.settings.weekly_review_weekday, 6):
            return
        if not self._clock_reached(stamp, self.settings.weekly_review_time):
            return
        iso_year, iso_week, _ = stamp.isocalendar()
        key = "weekly_review:%s-%s" % (iso_year, iso_week)
        if self.store.meta_get(key, ""):
            return
        manager = self.push_manager()
        if not manager.has_channels:
            self._warn_repeated("每周复盘无可用推送通道，保留待下次重试。", stamp, signature=key)
            return
        allowed, reason = self._push_gate(stamp, ignore_quiet=True)
        if not allowed:
            self.logger.info("每周复盘暂不推送（%s），保留待下次重试。", reason)
            return
        body = self._weekly_review_body(stamp)
        outcomes = manager.send_text("每周复盘", body, dedupe_key=key)
        if not any(outcome.ok and not outcome.skipped for outcome in outcomes):
            self.logger.warning("每周复盘投递失败，保留待下次重试。")
            return
        self.store.meta_set(key, iso(stamp))
        self._count_push(stamp)
        self.logger.info("已生成每周复盘。")

    # ---------------------------------------------------------------- 历史补采

    def _catchup_retry_seconds(self) -> int:
        index = max(1, self._catchup_failures) - 1
        return CATCHUP_BACKOFF_STEPS[min(index, len(CATCHUP_BACKOFF_STEPS) - 1)]

    def _engine_ready(self) -> tuple[bool, str]:
        """引擎可达且 QQ 已登录。问不到一律当没就绪，绝不据此计失败。"""
        try:
            client = self._napcat_client()
            response = client.call("get_status", {})
            data = response.get("data") if isinstance(response, dict) else None
            if isinstance(data, dict) and bool(data.get("online")):
                return True, ""
            login = client.login_info()
            if isinstance(login, dict) and login.get("user_id"):
                return True, ""
            return False, "引擎还没上线"
        except Exception as error:  # noqa: BLE001 - 探不到就是没就绪
            return False, "引擎还没上线（%s）" % (str(error)[:120] or error.__class__.__name__)

    def run_catchup(self, *, force: bool = False, now: dt.datetime | None = None) -> int:
        stamp = now or self.now()
        self.last_catchup_attempt_at = stamp
        ready, reason = self._engine_ready()
        if not ready:
            self.catchup_state = "waiting_engine"
            self.last_catchup_error = reason or "引擎还没上线"
            self.logger.info(
                "历史补采暂不执行：%s，引擎上线后自动补采。", self.last_catchup_error
            )
            return 0

        self.catchup_state = "running"
        stats: dict[str, int] = {}
        try:
            inserted = int(
                backfill(
                    self.settings,
                    self.store,
                    self.logger,
                    client=None,
                    now=stamp,
                    force=force,
                    stats=stats,
                    attachment_sink=self._submit_attachment,
                )
                or 0
            )
        except Exception:  # noqa: BLE001 - 补采失败只影响补采状态机
            self.catchup_state = "failed"
            self.last_catchup_error = "历史补采异常"
            self.logger.exception("历史补采异常")
            return 0

        groups = _as_int(stats.get("groups"))
        ok_groups = _as_int(stats.get("ok_groups"))
        if groups > 0 and ok_groups == 0:
            self._catchup_failures += 1
            self.catchup_state = "failed"
            self.last_catchup_error = "历史补采全部失败（%d 个群）" % groups
            self.logger.warning(
                "历史补采全部失败（%d 个群），%d 分钟后自动重试。",
                groups,
                max(1, self._catchup_retry_seconds() // 60),
            )
            return 0

        self._catchup_failures = 0
        self.catchup_state = "ok"
        self.last_catchup_at = stamp
        self.last_catchup_ok_at = stamp
        self.last_catchup_error = ""
        return inserted

    def _maybe_catchup(self, now: dt.datetime | None = None) -> None:
        stamp = now or self.now()
        if not self.settings.catchup_enabled:
            return
        thread = self._catchup_thread
        if thread is not None and thread.is_alive():
            return
        if self.catchup_state == "running":
            return
        attempt = self.last_catchup_attempt_at
        elapsed = (stamp - attempt).total_seconds() if isinstance(attempt, dt.datetime) else None
        if self.catchup_state == "waiting_engine":
            if elapsed is not None and elapsed < CATCHUP_WAIT_ENGINE_SECONDS:
                return
            self._schedule_catchup()
            return
        if isinstance(self.last_catchup_at, dt.datetime):
            interval = max(5, _as_int(self.settings.catchup_interval_minutes, 30)) * 60
            if (stamp - self.last_catchup_at).total_seconds() < interval:
                return
        elif elapsed is not None and elapsed < self._catchup_retry_seconds():
            return
        self._schedule_catchup()

    def _schedule_catchup(self) -> None:
        thread = self._catchup_thread
        if thread is not None and thread.is_alive():
            return
        with self._catchup_lock:
            if self._catchup_thread is not None and self._catchup_thread.is_alive():
                return
            self._catchup_thread = threading.Thread(
                target=self._catchup_worker, name="service-catchup", daemon=True
            )
            self._catchup_thread.start()

    def _catchup_worker(self) -> None:
        try:
            self.run_catchup()
        except Exception:  # noqa: BLE001 - 线程里更不能漏
            self.logger.exception("历史补采异常")

    def _catchup_engine_arrived(self) -> None:
        """实时消息能进来 ⇒ 引擎已经登录，别等退避。"""
        self._catchup_failures = 0
        self.catchup_state = "idle"
        self.last_catchup_error = ""
        self._schedule_catchup()

    # ---------------------------------------------------------------- 维护

    def _maybe_prune(self, now: dt.datetime | None = None) -> None:
        stamp = now or self.now()
        last = parse_iso(self.store.meta_get("last_prune", ""))
        if last is not None and (stamp - last).total_seconds() < PRUNE_INTERVAL_SECONDS:
            return
        try:
            removed = self.store.prune()
        except Exception:  # noqa: BLE001 - 裁剪失败下次再来
            self.logger.exception("数据裁剪失败")
            return
        self.store.meta_set("last_prune", iso(stamp))
        if removed:
            self.logger.info("清理 %d 条过期消息。", removed)
        try:
            files = cleanup_files(
                self.settings.data_dir / "files",
                days=_as_int(self.settings.attachment_retention_days),
            )
        except Exception:  # noqa: BLE001
            self.logger.exception("过期附件清理失败")
            files = 0
        if files:
            self.logger.info("清理 %d 个过期附件文件。", files)

    def _snapshot_path(self, stamp: dt.datetime) -> Path:
        return self.settings.data_dir / "snapshots" / ("digest-%s.sqlite3" % stamp.strftime("%Y%m%d"))

    def _maybe_snapshot(self, now: dt.datetime | None = None) -> None:
        stamp = now or self.now()
        today = stamp.strftime("%Y%m%d")
        if str(self.store.meta_get("last_snapshot", "")) == today:
            return
        target = self._snapshot_path(stamp)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            source = sqlite3.connect(str(self.store.path))
            try:
                destination = sqlite3.connect(str(target))
                try:
                    source.backup(destination)
                finally:
                    destination.close()
            finally:
                source.close()
        except (OSError, sqlite3.Error) as error:
            self.logger.warning("数据库快照失败（%s）：%s", target, error)
            return
        self.store.meta_set("last_snapshot", today)
        self._trim_snapshots(target.parent)

    def _trim_snapshots(self, directory: Path) -> None:
        try:
            files = sorted(directory.glob("digest-*.sqlite3"))
        except OSError:
            return
        for stale in files[:-SNAPSHOT_KEEP] if len(files) > SNAPSHOT_KEEP else []:
            try:
                stale.unlink()
            except OSError:
                continue

    # ---------------------------------------------------------------- 状态载荷

    def _catchup_next_retry_at(self, now: dt.datetime) -> str:
        base = self.last_catchup_attempt_at or now
        if self.catchup_state == "waiting_engine":
            return iso(base + dt.timedelta(seconds=CATCHUP_WAIT_ENGINE_SECONDS))
        if self.catchup_state == "failed":
            return iso(base + dt.timedelta(seconds=self._catchup_retry_seconds()))
        return ""

    def _catchup_hint(self) -> str:
        return {
            "waiting_engine": "引擎还没上线，登录成功后会自动补采",
            "running": "正在补采历史消息",
            "failed": "上次补采失败，稍后自动重试",
            "ok": "历史消息已补采",
        }.get(self.catchup_state, "尚未补采")

    def _catchup_fields(self, now: dt.datetime) -> dict[str, Any]:
        return {
            "catchup_enabled": bool(self.settings.catchup_enabled),
            "last_catchup_at": iso(self.last_catchup_at) if self.last_catchup_at else "",
            "last_catchup_attempt_at": iso(self.last_catchup_attempt_at) if self.last_catchup_attempt_at else "",
            "catchup_state": self.catchup_state,
            "catchup_waiting_engine": self.catchup_state == "waiting_engine",
            "last_catchup_ok_at": iso(self.last_catchup_ok_at) if self.last_catchup_ok_at else "",
            "last_catchup_error": self.last_catchup_error,
            "catchup_failures": self._catchup_failures,
            "catchup_next_retry_at": self._catchup_next_retry_at(now),
            "catchup_hint": self._catchup_hint(),
        }

    def _channel_texts(self) -> list[str]:
        return [str(pusher.describe()) for pusher in self.push_manager().pushers]

    def _llm_payload(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.settings.llm_enabled),
            "model": str(self.settings.dashscope_model or ""),
            "failures_total": _as_int(self.store.meta_get("llm_failures_total", "0")),
            "deferred_total": _as_int(self.store.meta_get("llm_deferred_total", "0")),
            "fallbacks_total": _as_int(self.store.meta_get("llm_fallbacks_total", "0")),
            "last_error": self.last_llm_error,
            "last_error_at": iso(self.last_llm_error_at) if self.last_llm_error_at else "",
        }

    def _push_payload(self, now: dt.datetime) -> dict[str, Any]:
        manager = self._push_manager
        channels = (
            [str(pusher.describe()) for pusher in manager.pushers]
            if manager is not None
            else list(self.settings.push_channels())
        )
        return {
            "channels": channels,
            "sent_today": self._push_count(now),
            "daily_budget": _as_int(self.settings.push_daily_budget),
            "quiet_hours": str(self.settings.quiet_hours or ""),
            "last_push_at": iso(self.last_push_at) if self.last_push_at else "",
            "last_defer_reason": self.last_defer_reason,
            "last_defer_at": iso(self.last_defer_at) if self.last_defer_at else "",
            "deliveries": self.store.delivery_stats(),
        }

    def _web_payload(self) -> dict[str, Any]:
        token = str(self.settings.web_token or self.store.meta_get("web_token", "") or "")
        return {
            "enabled": bool(self.settings.web_enabled),
            "url": build_web_url(self.settings, token=token) if self.settings.web_enabled else "",
            "alive": bool(getattr(self._web, "is_alive", False)),
        }

    def _attachments_payload(self) -> dict[str, Any]:
        if self._attachment_worker is not None:
            try:
                return dict(self._attachment_worker.describe())
            except Exception:  # noqa: BLE001 - 统计失败不算健康问题
                self.logger.exception("附件统计读取失败")
        return {"enabled": bool(self.settings.attachments_enabled)}

    def health_payload(self) -> dict[str, Any]:
        now = self.now()
        payload: dict[str, Any] = {
            "started_at": iso(self.started_at) if self.started_at else "",
            "last_tick_at": iso(self.last_tick_at) if self.last_tick_at else "",
            "attachments": self._attachments_payload(),
            "tasks": self.store.task_stats(),
            "web": self._web_payload(),
            "llm": self._llm_payload(),
            "push": self._push_payload(now),
            "counts": self.store.counts(),
        }
        payload.update(self._catchup_fields(now))
        return payload

    def status(self) -> dict[str, Any]:
        now = self.now()
        bot = self._bot
        status: dict[str, Any] = {
            "started_at": iso(self.started_at) if self.started_at else "",
            "last_tick_at": iso(self.last_tick_at) if self.last_tick_at else "",
            "bot_alive": bool(getattr(bot, "is_alive", False)) if bot is not None else False,
            "bot_ready": bool(getattr(bot, "is_ready", False)) if bot is not None else False,
            "bot_error": str(getattr(bot, "last_error", "") or "") if bot is not None else "",
            "receiver_alive": bool(getattr(self._receiver, "is_alive", False)) if self._receiver is not None else False,
            "channels": self._channel_texts(),
            "counts": self.store.counts(),
            "tasks": self.store.task_stats(),
        }
        status.update(self._catchup_fields(now))
        return status

    def _reminders_summary(self) -> str:
        parts: list[str] = []
        if self.settings.deadline_reminders_enabled:
            parts.append("截止 %s / %s" % (self.settings.deadline_morning, self.settings.deadline_evening))
        if self.settings.candidate_push_enabled:
            parts.append("候选确认")
        if self.settings.weekly_review_enabled:
            parts.append("每周复盘")
        return "、".join(parts)

    def _meta_info(self) -> dict[str, Any]:
        """Web 页脚/总览用的元信息；任何一块读失败都不能让页面 500。"""
        try:
            channels = self._channel_texts()
        except Exception:  # noqa: BLE001
            self.logger.exception("推送通道读取失败")
            channels = list(self.settings.push_channels())
        try:
            insights = self.store.correction_insights(days=7, min_group_hits=2)[:3]
        except Exception:  # noqa: BLE001
            self.logger.exception("纠错统计读取失败")
            insights = []
        return {
            "groups": [self.settings.group_name(str(group)) for group in self.settings.group_whitelist],
            "channels": channels,
            "reminders": self._reminders_summary(),
            "started_at": iso(self.started_at) if self.started_at else "",
            "insights": insights,
        }


__all__ = ["DigestService", "LOGGER"]
