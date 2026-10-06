"""推送通道：QQ 机器人私聊优先，HTTP 通道兜底；全部投递写入 deliveries 表去重。"""

from __future__ import annotations

import abc
import asyncio
import hashlib
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable

from .config import Settings
from .store import Store
from .summarizer import Digest

LOGGER = logging.getLogger(__name__)

QQ_TEXT_LIMIT = 900
HTTP_TEXT_LIMIT = 1800
HTML_BYTE_LIMIT = 60000  # WxPusher HTML 正文上限 65535 字节，留点余量
USER_AGENT = "qq-live-digest/1.0"


class PushError(RuntimeError):
    pass


@dataclass
class PushOutcome:
    channel: str
    target: str
    ok: bool
    skipped: bool = False
    error: str = ""
    attempts: int = 0
    status: str = ""


class Pusher(abc.ABC):
    name = "pusher"
    tier = 0

    def __init__(self, target: str) -> None:
        self.target = target

    @abc.abstractmethod
    def send(self, title: str, body: str, *, summary: str = "", html: str = "") -> None:
        """发送失败时必须抛 PushError。"""

    def describe(self) -> str:
        return f"{self.name}:{self.target}"


def post_json(url: str, payload: dict[str, Any], timeout: int = 15) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:300]
        raise PushError(f"HTTP {error.code}: {detail}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise PushError(f"请求失败: {error}") from error
    try:
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as error:
        raise PushError(f"响应不是 JSON: {raw[:200]}") from error


def post_form(url: str, payload: dict[str, Any], timeout: int = 15) -> dict[str, Any]:
    data = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:300]
        raise PushError(f"HTTP {error.code}: {detail}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise PushError(f"请求失败: {error}") from error
    try:
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {"raw": raw[:300]}


def chunk_text(text: str, limit: int) -> list[str]:
    """按行切分，保留可读性；超长单行硬切。"""
    value = (text or "").strip()
    if not value:
        return []
    if len(value) <= limit:
        return [value]
    lines: list[str] = []
    for line in value.splitlines() or [value]:
        while len(line) > limit:
            lines.append(line[:limit])
            line = line[limit:]
        lines.append(line)

    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = line
    if current:
        chunks.append(current)
    total = len(chunks)
    if total > 1:
        chunks = [f"({index}/{total})\n{chunk}" for index, chunk in enumerate(chunks, start=1)]
    return chunks


class QQBotPusher(Pusher):
    """通过官方机器人 C2C 私聊推送。"""

    name = "qq-bot"
    tier = 0

    def __init__(self, api: Any, openid: str, loop: asyncio.AbstractEventLoop, *, timeout: int = 30) -> None:
        super().__init__(openid)
        self.api = api
        self.loop = loop
        self.timeout = timeout
        self._sequence = 0

    def send(self, title: str, body: str, *, summary: str = "", html: str = "") -> None:
        # QQ 私聊只支持纯文本，忽略 HTML 和 summary。
        chunks = chunk_text(f"{title}\n{body}", QQ_TEXT_LIMIT)
        for chunk in chunks:
            self._sequence += 1
            coroutine = self.api.post_c2c_message(
                openid=self.target,
                msg_type=0,
                content=chunk,
                msg_seq=self._sequence,
            )
            try:
                future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
                future.result(timeout=self.timeout)
            except (RuntimeError, TimeoutError, asyncio.TimeoutError) as error:
                raise PushError(f"QQ 私聊发送失败: {error}") from error
            except Exception as error:  # botpy 抛出的 HTTP/业务错误
                raise PushError(f"QQ 私聊发送失败: {error}") from error


class WxPusherPusher(Pusher):
    name = "wxpusher"

    def __init__(
        self,
        app_token: str,
        *,
        uids: Iterable[str] = (),
        topic_ids: Iterable[int] = (),
        timeout: int = 15,
    ) -> None:
        self.app_token = app_token
        self.uids = [str(item) for item in uids]
        self.topic_ids = [int(item) for item in topic_ids]
        target = f"uids={len(self.uids)},topics={len(self.topic_ids)}"
        super().__init__(target)
        self.timeout = timeout

    def send(self, title: str, body: str, *, summary: str = "", html: str = "") -> None:
        """有 HTML 就发 contentType=2 卡片；否则回退纯文本，超长自动分条。"""
        summary_text = (summary or title).strip()[:100]
        if html and len(html.encode("utf-8")) <= HTML_BYTE_LIMIT:
            self._post({"content": html, "summary": summary_text, "contentType": 2})
            return
        for chunk in chunk_text(f"{title}\n{body}", HTTP_TEXT_LIMIT):
            self._post({"content": chunk, "summary": summary_text, "contentType": 1})

    def _post(self, extra: dict[str, Any]) -> None:
        payload: dict[str, Any] = {"appToken": self.app_token, **extra}
        if self.uids:
            payload["uids"] = self.uids
        if self.topic_ids:
            payload["topicIds"] = self.topic_ids
        result = post_json("https://wxpusher.zjiecode.com/api/send/message", payload, self.timeout)
        if int(result.get("code") or 0) != 1000:
            raise PushError(f"WxPusher 返回异常: {str(result)[:200]}")


class ServerChanPusher(Pusher):
    name = "serverchan"

    def __init__(self, send_key: str, timeout: int = 15) -> None:
        super().__init__("key=" + send_key[:6] + "***")
        self.send_key = send_key
        self.timeout = timeout

    def send(self, title: str, body: str, *, summary: str = "", html: str = "") -> None:
        for chunk in chunk_text(body, HTTP_TEXT_LIMIT):
            result = post_form(
                f"https://sctapi.ftqq.com/{self.send_key}.send",
                {"title": title[:60], "desp": chunk},
                self.timeout,
            )
            if int(result.get("code") or 0) != 0:
                raise PushError(f"Server酱返回异常: {str(result)[:200]}")


class PushPlusPusher(Pusher):
    name = "pushplus"

    def __init__(self, token: str, timeout: int = 15) -> None:
        super().__init__("token=" + token[:6] + "***")
        self.token = token
        self.timeout = timeout

    def send(self, title: str, body: str, *, summary: str = "", html: str = "") -> None:
        result = post_json(
            "https://www.pushplus.plus/send",
            {"token": self.token, "title": title[:100], "content": body[:HTTP_TEXT_LIMIT], "template": "txt"},
            self.timeout,
        )
        if int(result.get("code") or 0) != 200:
            raise PushError(f"PushPlus 返回异常: {str(result)[:200]}")


class WebhookPusher(Pusher):
    name = "webhook"

    def __init__(self, url: str, timeout: int = 15) -> None:
        parsed = urllib.parse.urlparse(url)
        fingerprint = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
        super().__init__(f"{parsed.netloc or 'webhook'}#{fingerprint}")
        self.url = url
        self.timeout = timeout

    def send(self, title: str, body: str, *, summary: str = "", html: str = "") -> None:
        post_json(
            self.url,
            {"title": title, "content": body[:HTTP_TEXT_LIMIT], "source": "qq-live-digest"},
            self.timeout,
        )


def build_pushers(settings: Settings, *, api: Any = None, loop: asyncio.AbstractEventLoop | None = None) -> list[Pusher]:
    pushers: list[Pusher] = []
    if settings.push_c2c_openids and api is not None and loop is not None:
        for openid in settings.push_c2c_openids:
            pushers.append(QQBotPusher(api, openid, loop))
    elif settings.push_c2c_openids:
        LOGGER.warning("已配置 QQ 私聊推送，但机器人尚未启动，跳过 QQ 通道。")

    if settings.wxpusher_app_token and (settings.wxpusher_uids or settings.wxpusher_topic_ids):
        pushers.append(
            WxPusherPusher(
                settings.wxpusher_app_token,
                uids=settings.wxpusher_uids,
                topic_ids=settings.wxpusher_topic_ids,
                timeout=settings.http_timeout,
            )
        )
    for key in settings.serverchan_keys:
        pushers.append(ServerChanPusher(key, settings.http_timeout))
    for token in settings.pushplus_tokens:
        pushers.append(PushPlusPusher(token, settings.http_timeout))
    for url in settings.webhook_urls:
        pushers.append(WebhookPusher(url, settings.http_timeout))

    if not any(pusher.tier == 0 for pusher in pushers):
        # 没有 QQ 通道时，HTTP 通道就是主通道
        for pusher in pushers:
            pusher.tier = 0
    elif len(pushers) > 1:
        for pusher in pushers:
            if pusher.name != "qq-bot":
                pusher.tier = 1
    return pushers


class PushManager:
    def __init__(self, store: Store, settings: Settings, pushers: list[Pusher], logger: logging.Logger | None = None) -> None:
        self.store = store
        self.settings = settings
        self.pushers = pushers
        self.logger = logger or LOGGER

    @property
    def has_channels(self) -> bool:
        return bool(self.pushers)

    def send_digest(self, digest: Digest, *, dedupe_key: str = "") -> list[PushOutcome]:
        if digest.id <= 0 or not digest.items:
            return []
        dedupe_key = dedupe_key or f"digest:{digest.id}"
        summary = digest.summary or digest.title
        outcomes = self._dispatch(
            digest.id, digest.title, digest.body, dedupe_key, tier=0, summary=summary, html=digest.html
        )
        if not any(item.ok for item in outcomes):
            fallback = self._dispatch(
                digest.id, digest.title, digest.body, dedupe_key, tier=1, summary=summary, html=digest.html
            )
            outcomes.extend(fallback)
        return outcomes

    def send_text(self, title: str, body: str, *, dedupe_key: str, digest_id: int = 0) -> list[PushOutcome]:
        return self._dispatch(digest_id, title, body, dedupe_key, tier=0)

    def send_card(
        self,
        title: str,
        body: str,
        *,
        dedupe_key: str,
        summary: str = "",
        html: str = "",
    ) -> list[PushOutcome]:
        """一次性卡片推送（用于测试），不写入摘要表。"""
        return self._dispatch(0, title, body, dedupe_key, tier=0, summary=summary, html=html)

    def _dispatch(
        self,
        digest_id: int,
        title: str,
        body: str,
        dedupe_key: str,
        *,
        tier: int,
        summary: str = "",
        html: str = "",
    ) -> list[PushOutcome]:
        outcomes: list[PushOutcome] = []
        for pusher in self.pushers:
            if pusher.tier != tier:
                continue
            should_send, delivery_id = self.store.claim_delivery(
                digest_id=digest_id,
                channel=pusher.name,
                target=pusher.target,
                dedupe_key=dedupe_key,
                max_attempts=self.settings.delivery_max_attempts,
                retry_seconds=self.settings.delivery_retry_seconds,
            )
            if not should_send:
                existing = self.store.get_delivery(delivery_id or 0) or {}
                outcomes.append(
                    PushOutcome(
                        pusher.name,
                        pusher.target,
                        ok=True,
                        skipped=True,
                        error=str(existing.get("status") or "skipped"),
                        status=str(existing.get("status") or "skipped"),
                    )
                )
                continue
            try:
                pusher.send(title, body, summary=summary, html=html)
            except Exception as error:  # noqa: BLE001 - 需要记录任意通道异常
                message = str(error)[:400]
                self.logger.warning("推送失败 %s: %s", pusher.describe(), message)
                self.store.mark_delivery(delivery_id or 0, ok=False, error=message)
                outcomes.append(PushOutcome(pusher.name, pusher.target, ok=False, error=message, status="failed"))
            else:
                self.logger.info("推送成功 %s", pusher.describe())
                self.store.mark_delivery(delivery_id or 0, ok=True)
                outcomes.append(PushOutcome(pusher.name, pusher.target, ok=True, status="sent"))
        return outcomes

    def retry_pending(self, *, retry_seconds: int | None = None) -> list[PushOutcome]:
        cooldown = self.settings.delivery_retry_seconds if retry_seconds is None else int(retry_seconds)
        outcomes: list[PushOutcome] = []
        for row in self.store.pending_deliveries(
            max_attempts=self.settings.delivery_max_attempts,
            retry_seconds=cooldown,
        ):
            digest = self.store.get_digest(int(row["digest_id"]))
            if not digest or not digest.get("body"):
                self.store.mark_delivery(int(row["id"]), ok=False, error="摘要正文为空，放弃重试")
                continue
            pusher = next(
                (item for item in self.pushers if item.name == row["channel"] and item.target == row["target"]),
                None,
            )
            if pusher is None:
                self.store.mark_delivery(int(row["id"]), ok=False, error="通道已移除")
                continue
            attempts = self.store.increment_delivery_attempt(int(row["id"]))
            title = f"QQ群通知（{row.get('window_end') or ''}）"
            try:
                pusher.send(title, digest["body"])
            except Exception as error:  # noqa: BLE001
                message = str(error)[:400]
                self.store.mark_delivery(int(row["id"]), ok=False, error=message)
                outcomes.append(
                    PushOutcome(pusher.name, pusher.target, ok=False, error=message, attempts=attempts)
                )
            else:
                self.store.mark_delivery(int(row["id"]), ok=True)
                outcomes.append(PushOutcome(pusher.name, pusher.target, ok=True, attempts=attempts))
        return outcomes
