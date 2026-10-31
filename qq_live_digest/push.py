"""推送通道：QQ 机器人私聊优先，HTTP 通道兜底；全部投递写入 deliveries 表去重。"""

from __future__ import annotations

import abc
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable

from noticehub.infrastructure.notify import PushManager
from .config import Settings

LOGGER = logging.getLogger(__name__)

QQ_TEXT_LIMIT = 900
HTTP_TEXT_LIMIT = 1800
WECOM_TEXT_BYTE_LIMIT = 4000  # 企业微信 markdown content 上限 4096 字节（UTF-8），留点余量
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


def _webhook_kind(parsed: urllib.parse.ParseResult) -> str:
    """按 host / path 识别钉钉、企业微信、飞书群机器人，其余按通用 webhook 处理。"""
    host = (parsed.netloc or "").rsplit("@", 1)[-1].split(":")[0].lower()
    path = parsed.path or ""

    def matches(domain: str) -> bool:
        return host == domain or host.endswith("." + domain)

    if matches("oapi.dingtalk.com") or "/robot/send" in path:
        return "dingtalk"
    if matches("qyapi.weixin.qq.com") or "/cgi-bin/webhook/send" in path:
        return "wecom"
    if matches("open.feishu.cn") or matches("open.larksuite.com") or "/open-apis/bot/" in path:
        return "feishu"
    return "generic"


_WEBHOOK_LABELS = {"dingtalk": "钉钉", "wecom": "企业微信", "feishu": "飞书"}


def _dingtalk_signed_url(url: str, secret: str, timestamp: int | None = None) -> str:
    """钉钉「加签」：待签串 = 毫秒时间戳 + "\\n" + 密钥，HMAC-SHA256 → Base64 → urlencode。"""
    stamp = str(int(timestamp if timestamp is not None else time.time() * 1000))
    digest = hmac.new(secret.encode("utf-8"), ("%s\n%s" % (stamp, secret)).encode("utf-8"), hashlib.sha256).digest()
    signature = urllib.parse.quote_plus(base64.b64encode(digest).decode("ascii"))
    separator = "&" if urllib.parse.urlparse(url).query else "?"
    return "%s%stimestamp=%s&sign=%s" % (url, separator, stamp, signature)


def _post_webhook(url: str, payload: dict[str, Any], timeout: int, kind: str) -> None:
    """发送 webhook 并校验业务返回；群机器人校验失败时 HTTP 仍是 200，必须解析响应体。"""
    result = post_json(url, payload, timeout)
    if kind == "generic":
        return
    if kind == "feishu":
        code_key = "code"
    else:
        code_key = "errcode"
    code = result.get(code_key)
    try:
        ok = code is not None and int(code) == 0
    except (TypeError, ValueError):
        ok = False
    if not ok:
        raise PushError(f"{_WEBHOOK_LABELS[kind]} webhook 返回异常: {str(result)[:200]}")


class WebhookPusher(Pusher):
    name = "webhook"

    def __init__(self, url: str, timeout: int = 15) -> None:
        parsed = urllib.parse.urlparse(url)
        fingerprint = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
        super().__init__(f"{parsed.netloc or 'webhook'}#{fingerprint}")
        self.url = url
        self.timeout = timeout
        self.kind = _webhook_kind(parsed)
        self.sign_secret = ""
        # 钉钉「加签」：用户按约定写成 `...?access_token=xxx|SECyyyy`，这里把密钥剥出来，
        # 发的时候再拿它算 sign（密钥不能留在实际请求的 URL 上）。
        if self.kind == "dingtalk":
            fixed: list[tuple[str, str]] = []
            secret = ""
            for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
                if key == "access_token" and "|" in value:
                    value, secret = value.split("|", 1)
                    secret = secret.strip()
                fixed.append((key, value))
            if secret:
                self.sign_secret = secret
                self.url = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(fixed)))

    def send(self, title: str, body: str, *, summary: str = "", html: str = "") -> None:
        if self.kind == "dingtalk":
            text = ("### " + title + "\n" + body)[:HTTP_TEXT_LIMIT]
            payload: dict[str, Any] = {
                "msgtype": "markdown",
                "markdown": {"title": title[:64], "text": text},
            }
        elif self.kind == "wecom":
            text = ("### " + title + "\n" + body)[:HTTP_TEXT_LIMIT]
            # 企微 markdown 限 4096 字节（不是字符），CJK 正文按字符截断会超限被拒。
            text = text.encode("utf-8")[:WECOM_TEXT_BYTE_LIMIT].decode("utf-8", "ignore")
            payload = {"msgtype": "markdown", "markdown": {"content": text}}
        elif self.kind == "feishu":
            text = (title + "\n" + body)[:HTTP_TEXT_LIMIT]
            payload = {"msg_type": "text", "content": {"text": text}}
        else:
            payload = {"title": title, "content": body[:HTTP_TEXT_LIMIT], "source": "qq-live-digest"}
        url = _dingtalk_signed_url(self.url, self.sign_secret) if (self.kind == "dingtalk" and self.sign_secret) else self.url
        _post_webhook(url, payload, self.timeout, self.kind)


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
