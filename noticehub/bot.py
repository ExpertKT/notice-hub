"""官方 QQ 机器人常驻客户端：生命周期管理、群消息/私聊转记录、线程内运行与停止。

网络细节交给第三方库（`qq-botpy`）；本模块只负责把它接到我们的消息流程上，
并保证「回调出错不断线、线程退出不炸进程」这两条底线。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any, Callable, Dict

from .timeutil import iso, now_local

try:
    import botpy
    from botpy.connection import ConnectionState
    from botpy.message import C2CMessage, GroupMessage
except ImportError as error:  # pragma: no cover - 依赖缺失时直接给运维一句话
    raise SystemExit("缺少 qq-botpy，请先执行: pip install -r requirements.txt") from error

LOGGER = logging.getLogger(__name__)

RecordCallback = Callable[[dict], Any]
OpenIdCallback = Callable[[dict], Any]

# 官方机器人拿不到群名，记录里没有 group_name 这个键（下游按 msg_id 去重入库）。
_NO_GROUP_NAME = object()


def botpy_version() -> str:
    """已安装发行包版本；查不到就给 unknown，不抛。"""
    try:
        from importlib.metadata import version

        return str(version("qq-botpy"))
    except Exception:  # noqa: BLE001 - 包元数据缺失不值得中断
        return "unknown"


def _group_message_parser(self: Any, payload: dict) -> None:
    """补丁：把群消息创建事件解析成 `GroupMessage` 再派发。"""
    message = GroupMessage(self.api, payload.get("id", None), payload.get("d", {}))
    self._dispatch("group_message_create", message)


def install_full_group_message_parser() -> bool:
    """装上「群消息创建」解析器；已有实现就不动，返回 False。"""
    if hasattr(ConnectionState, "parse_group_message_create"):
        return False
    ConnectionState.parse_group_message_create = _group_message_parser
    return True


def _stamp(value: Any) -> str:
    """消息时间戳 → 秒精度 ISO；给不出就用现在。"""
    text = iso(value)
    return text or iso(now_local())


class LiveBotClient(botpy.Client):
    """把第三方客户端的事件翻译成我们的记录形状，再交给回调。"""

    def __init__(self, *args: Any, on_message: RecordCallback, on_openid: OpenIdCallback | None = None, logger: logging.Logger | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.ready = threading.Event()
        self.logger = logger or LOGGER
        self._on_message = on_message
        self._on_openid = on_openid

    async def on_ready(self) -> None:
        name = "unknown"
        try:
            state = getattr(self, "robot", None)
            name = str(getattr(state, "name", "") or "unknown")
        except Exception:  # noqa: BLE001 - 取名字失败不影响连接
            name = "unknown"
        self.logger.info("QQ 机器人已连接：%s", name)
        self.ready.set()

    async def on_error(self, event_method: str, *args: Any, **kwargs: Any) -> None:
        # 第三方库里回调异常会走这里；吞掉，别让一条坏消息把连接打断。
        self.logger.exception("QQ 机器人事件异常：%s", event_method)

    def _record(self, message: Any, event: str) -> dict:
        author = getattr(message, "author", None)
        raw_time = getattr(message, "timestamp", None)
        return {
            "msg_id": str(getattr(message, "id", "") or ""),
            "source": "qqbot",
            "event": event,
            "group_id": str(getattr(message, "group_openid", "") or ""),
            "sender_id": str(getattr(author, "member_openid", "") or ""),
            "sender_name": str(getattr(author, "username", "") or ""),
            "ts": _stamp(raw_time),
            "received_at": iso(now_local()),
            "content": str(getattr(message, "content", "") or ""),
        }

    def _emit(self, message: Any, event: str) -> None:
        record = self._record(message, event)
        try:
            self._on_message(record)
        except Exception:  # noqa: BLE001 - 回调出错不许断线
            self.logger.exception("处理群消息回调失败：%s", record["msg_id"])

    async def on_group_message_create(self, message: Any) -> None:
        self._emit(message, "GROUP_MESSAGE_CREATE")

    async def on_group_at_message_create(self, message: Any) -> None:
        self._emit(message, "GROUP_AT_MESSAGE_CREATE")

    async def on_c2c_message_create(self, message: Any) -> None:
        author = getattr(message, "author", None)
        openid = str(getattr(author, "user_openid", "") or "")
        record = {
            "msg_id": str(getattr(message, "id", "") or ""),
            "user_openid": openid,
            "ts": _stamp(getattr(message, "timestamp", None)),
            "content": str(getattr(message, "content", "") or ""),
        }
        self.logger.info("收到私聊，user_openid=%s（可写入 NOTICEHUB_PUSH_OPENIDS）", openid)
        if self._on_openid is None:
            return
        try:
            self._on_openid(record)
        except Exception:  # noqa: BLE001 - 同上
            self.logger.exception("处理私聊回调失败")


class BotRunner:
    """一条独立线程 + 一个独立事件循环；启动立刻返回，连接建立与否看 `is_ready`。"""

    def __init__(self, settings: Any, on_message: RecordCallback, *, data_dir: Any = None, logger: logging.Logger | None = None) -> None:
        self.settings = settings
        self.logger = logger or LOGGER
        self._on_message = on_message
        self._data_dir = data_dir if data_dir is not None else getattr(settings, "data_dir", None)
        self.client: LiveBotClient | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.stopped = threading.Event()
        self.last_error = ""

    @property
    def api(self) -> Any:
        return getattr(self.client, "api", None) if self.client is not None else None

    @property
    def is_alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    @property
    def is_ready(self) -> bool:
        return self.client is not None and self.client.ready.is_set()

    def _save_openid(self, record: dict) -> None:
        """私聊 openid 落成 JSON 行，给用户自己复制进推送白名单（没有自动读者）。"""
        if self._data_dir is None:
            return
        from pathlib import Path

        target = Path(self._data_dir) / "c2c_openids.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    async def _shutdown(self) -> None:
        if self.client is not None:
            await self.client.close()

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        error_text = ""
        try:
            patched = install_full_group_message_parser()
            intents = botpy.Intents(public_messages=True)
            self.client = LiveBotClient(
                intents=intents,
                timeout=10,
                is_sandbox=bool(getattr(self.settings, "sandbox", False)),
                on_message=self._on_message,
                on_openid=self._save_openid if self._data_dir is not None else None,
                logger=self.logger,
            )
            appid = str(getattr(self.settings, "appid", "") or "")
            self.logger.info(
                "启动 QQ 官方机器人 appid=%s*** sandbox=%s full_group_parser=%s botpy=%s",
                appid[:4],
                bool(getattr(self.settings, "sandbox", False)),
                patched,
                botpy_version(),
            )
            asyncio.ensure_future(self.client.start(appid=appid, secret=str(getattr(self.settings, "secret", "") or "")), loop=loop)
            loop.run_forever()
        except BaseException as error:  # noqa: BLE001 - 线程函数不许向外抛
            error_text = str(error)
            self.last_error = error_text
            self.logger.exception("QQ 机器人线程退出：%s", error)
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:  # noqa: BLE001 - 收尾失败不该盖过真正的原因
                pass
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass
            self.stopped.set()
            self.logger.info("QQ 机器人线程已退出。")

    def start(self) -> bool:
        """起线程；已在线或凭据缺失都返回 False（缺失时只写 last_error，不抛）。"""
        if self.is_alive:
            return False
        appid = str(getattr(self.settings, "appid", "") or "")
        secret = str(getattr(self.settings, "secret", "") or "")
        if not appid or not secret:
            self.last_error = "缺少 AppID/AppSecret"
            self.logger.error("未配置 QQ_BOT_APPID/QQ_BOT_SECRET，无法启动官方机器人。")
            return False
        self.stopped.clear()
        self.thread = threading.Thread(target=self._run, name="qq-bot", daemon=True)
        self.thread.start()
        return True

    def wait_ready(self, timeout: float = 30) -> bool:
        if self.client is None:
            return False
        return bool(self.client.ready.wait(timeout))

    def stop(self, timeout: float = 10) -> None:
        """停：先把关闭协程丢回它自己的循环（最多等 3 秒），再停循环并 join。"""
        if self.thread is None or self.loop is None:
            return
        try:
            future = asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop)
            # 关闭客户端固定给 3 秒，与下面的 join 超时是两个不同的事（见 D-09）。
            future.result(timeout=3)
        except Exception:  # noqa: BLE001 - 连接可能已经断了
            pass
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except Exception:  # noqa: BLE001
            pass
        self.thread.join(timeout)
        self.logger.info("QQ 机器人已停止。")
