#!/usr/bin/env python3
"""群消息本地分析内核。

这个模块只做「不碰网络、不碰数据库」的纯计算：
文本清洗、时间解析、各种导出文件的解包、截止时间抽取、打分分类、
去重与文案，以及可选的模型精炼（调用方传 api_key 才会联网）。

命令行入口在 ``main.py``，这里保留 ``build_parser``/``main`` 只是为了兼容旧调用。
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import html
import html.parser
import io
import json
import math
import mimetypes
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from email import policy as email_policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

# --------------------------------------------------------------------------
# 词表：这些是行为契约，改动会直接改变打分与筛选结果
# --------------------------------------------------------------------------

URGENT_WORDS = (
    "紧急",
    "加急",
    "火速",
    "立刻",
    "立即",
    "马上",
    "尽快",
    "务必",
    "速交",
    "速回",
    "截至",
    "截止",
    "最晚",
    "过期不候",
    "逾期",
    "最后期限",
)

ACTION_WORDS = (
    "提交",
    "上交",
    "交到",
    "交至",
    "填写",
    "填好",
    "填报",
    "完成",
    "报名",
    "签到",
    "确认",
    "回复",
    "参加",
    "准备",
    "上传",
    "下载",
    "打印",
    "领取",
    "缴纳",
    "缴费",
    "注册",
    "选课",
    "集合",
    "开会",
    "参加考试",
    "复习",
    "预习",
    "背诵",
    "完成作业",
    "汇总",
    "统计",
    "登记",
    "核对",
    "投票",
    "接龙",
    "交表",
    "发到",
    "发给",
    "发送给",
)

ACADEMIC_WORDS = (
    "作业",
    "实验",
    "报告",
    "论文",
    "课程",
    "考试",
    "测验",
    "期中",
    "期末",
    "课设",
    "课程设计",
    "实践",
    "实习",
    "学分",
    "选课",
    "习题",
    "教材",
    "课内",
    "课后",
    "大作业",
    "毕设",
    "答辩",
    "开题",
    "查重",
    "参考文献",
)

ADMIN_WORDS = (
    "通知",
    "安排",
    "表格",
    "名单",
    "材料",
    "盖章",
    "签字",
    "审核",
    "备案",
    "报名表",
    "统一",
    "班级",
    "年级",
    "学院",
    "教务",
    "班会",
    "会议",
    "场地",
    "宿舍",
)

AUTHORITY_WORDS = (
    "老师",
    "辅导员",
    "教务",
    "班主任",
    "班长",
    "学委",
    "学习委员",
    "年级",
    "学院",
    "办公室",
    "官方",
    "管理员",
    "群主",
    "助教",
    "任课",
    "院办",
    "系主任",
)

DIRECTIVE_WORDS = (
    "请",
    "务必",
    "需要",
    "要求",
    "必须",
    "记得",
    "别忘",
    "不要忘",
    "统一",
    "一律",
    "按时",
    "尽快",
    "大家",
    "全体",
    "各位",
    "所有",
)

# 纯应对话术：只有这些内容的群聊消息不算通知
ACK_ONLY = ("收到", "好的", "好", "谢谢", "谢啦", "嗯", "嗯嗯", "ok", "OK", "1", "+1", "辛苦了")

# 附件占位符：QQ/微信把文件、图片折叠成这种前缀
ATTACHMENT_PLACEHOLDER = re.compile(
    r"^\s*\[(?:文件|图片|视频|语音|表情|动画表情|位置|链接|音乐|小程序)\]"
)

CHATTER_MARKERS = ("哈哈哈", "哈哈", "hhhh", "233", "草", "笑死", "梗图", "表情包", "狗头")

# 结构化解包时，哪些键名大概率装正文 / 发送者 / 时间 / 群名
TEXT_KEYS = ("text", "content", "message", "msg", "body", "plaintext", "plain_text", "strContent", "str_content")
SENDER_KEYS = ("sender", "senderName", "sender_name", "from", "nickname", "nickName", "displayName", "talkerName", "name")
TIME_KEYS = ("timestamp", "time", "createTime", "create_time", "sendTime", "send_time", "date", "datetime", "ts")
GROUP_NAME_KEYS = ("groupName", "group_name", "chatName", "chat_name", "peerName", "peer_name", "roomName", "title")

SUPPORTED_EXTS = (
    ".json",
    ".jsonl",
    ".txt",
    ".html",
    ".htm",
    ".mhtml",
    ".mht",
    ".eml",
    ".zip",
)

_CODEBUDDY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "messages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "timestamp": {"type": "string"},
                    "sender": {"type": "string"},
                    "text": {"type": "string"},
                    "source": {"type": "string"},
                },
                "required": ["timestamp", "sender", "text"],
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["messages"],
}

# --------------------------------------------------------------------------
# 消息对象
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Message:
    """一条被解析出来的群消息。时间一律是「本地朴素时间、精确到秒」。"""

    timestamp: dt.datetime | None
    sender: str
    text: str
    source: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "sender", "" if self.sender is None else str(self.sender))
        object.__setattr__(self, "text", "" if self.text is None else str(self.text))
        object.__setattr__(self, "source", "" if self.source is None else str(self.source))
        stamp = self.timestamp
        if stamp is None or stamp == "":
            object.__setattr__(self, "timestamp", None)
            return
        if isinstance(stamp, str):
            stamp = parse_timestamp(stamp)
            object.__setattr__(self, "timestamp", stamp)
            return
        if not isinstance(stamp, dt.datetime):
            raise TypeError("timestamp 必须是 datetime 或 None")
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone().replace(tzinfo=None)
        object.__setattr__(self, "timestamp", stamp.replace(microsecond=0))


# --------------------------------------------------------------------------
# 文本基础处理
# --------------------------------------------------------------------------

_SPACE_RUN = re.compile(r"[\s\u00a0\u3000]+")
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)


def clean_text(value: Any) -> str:
    """把任意输入压成一行干净文本：去不可见字符、把连续空白压成一个空格。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    text = value.translate(_INVISIBLE).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\n", " ")
    return _SPACE_RUN.sub(" ", text).strip()


def normalize_for_match(value: Any) -> str:
    """用于「像不像同一句话」的归一化：去标点、统一大小写、只留文字数字。"""
    text = clean_text(value).lower()
    if not text:
        return ""
    kept = []
    for char in text:
        if char.isalnum():
            kept.append(char)
        elif char.isspace():
            kept.append(" ")
    return " ".join("".join(kept).split())


def decode_bytes(raw: bytes, encoding: str = "") -> str:
    """尽量把字节解成文本：显式编码优先，其次 BOM，最后 UTF-8/GB18030 兜底。"""
    if not isinstance(raw, (bytes, bytearray)):
        return raw if isinstance(raw, str) else ""
    blob = bytes(raw)
    if encoding:
        try:
            return blob.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            pass
    for bom, name in (
        (b"\xef\xbb\xbf", "utf-8-sig"),
        (b"\xff\xfe\x00\x00", "utf-32"),
        (b"\x00\x00\xfe\xff", "utf-32"),
        (b"\xff\xfe", "utf-16"),
        (b"\xfe\xff", "utf-16"),
    ):
        if blob.startswith(bom):
            try:
                return blob.decode(name)
            except UnicodeDecodeError:
                break
    for name in ("utf-8", "gb18030", "big5"):
        try:
            return blob.decode(name)
        except UnicodeDecodeError:
            continue
    return blob.decode("utf-8", "replace")


# --------------------------------------------------------------------------
# 时间解析
# --------------------------------------------------------------------------

_DATE_TIME_PATTERNS = (
    re.compile(r"^(?P<y>\d{4})[-/.](?P<m>\d{1,2})[-/.](?P<d>\d{1,2})[ T]+(?P<H>\d{1,2}):(?P<M>\d{2})(?::(?P<S>\d{2}))?"),
    re.compile(r"^(?P<y>\d{4})[-/.](?P<m>\d{1,2})[-/.](?P<d>\d{1,2})$"),
    re.compile(r"^(?P<m>\d{1,2})月(?P<d>\d{1,2})日?\s*(?P<H>\d{1,2}):(?P<M>\d{2})(?::(?P<S>\d{2}))?"),
    re.compile(r"^(?P<m>\d{1,2})月(?P<d>\d{1,2})日?$"),
    re.compile(r"^(?P<H>\d{1,2}):(?P<M>\d{2})(?::(?P<S>\d{2}))?$"),
)


def _build_datetime(
    year: int, month: int, day: int, hour: int, minute: int, second: int
) -> dt.datetime | None:
    """hour=24 表示次日 0 点（课程群常见写法），其余越界一律返回 None。"""
    if hour == 24:
        if minute or second:
            return None
        try:
            return dt.datetime(year, month, day) + dt.timedelta(days=1)
        except ValueError:
            return None
    try:
        return dt.datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


def parse_timestamp(value: Any, *, now: dt.datetime | None = None) -> dt.datetime | None:
    """把各种时间写法归一成朴素本地时间；认不出返回 None。"""
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None, microsecond=0) if value.tzinfo is None else value.astimezone().replace(tzinfo=None, microsecond=0)
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _from_epoch(float(value))
    text = clean_text(value)
    if not text:
        return None
    if re.fullmatch(r"\d{9,19}", text):
        return _from_epoch(float(text))
    anchor = now or dt.datetime.now()
    head = text[:32]
    for pattern in _DATE_TIME_PATTERNS:
        found = pattern.match(head)
        if not found:
            continue
        parts = found.groupdict()
        year = int(parts["y"]) if parts.get("y") else anchor.year
        month = int(parts["m"])
        day = int(parts["d"])
        hour = int(parts["H"]) if parts.get("H") else 0
        minute = int(parts["M"]) if parts.get("M") else 0
        second = int(parts["S"]) if parts.get("S") else 0
        built = _build_datetime(year, month, day, hour, minute, second)
        if built is not None:
            return built
        break
    try:
        parsed = dt.datetime.fromisoformat(text.replace("/", "-").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None, microsecond=0)
    return parsed.replace(microsecond=0)


def _from_epoch(seconds: float) -> dt.datetime | None:
    """秒级 / 毫秒级时间戳都接（>1e11 当毫秒）。"""
    if seconds <= 0:
        return None
    if seconds > 1e12:
        seconds = seconds / 1000.0
    try:
        return dt.datetime.fromtimestamp(seconds).replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        return None


def format_deadline(value: dt.datetime | None) -> str:
    """截止时间的展示写法：有具体时分带上，没有就只给日期。"""
    if not isinstance(value, dt.datetime):
        return ""
    if value.hour or value.minute:
        return value.strftime("%Y-%m-%d %H:%M")
    return value.strftime("%Y-%m-%d")


def clean_child_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """给子进程用的环境变量：去掉 IDE/代理/解释器注入的键，其余原样保留。"""
    env = dict(os.environ if base is None else base)
    for key in list(env):
        upper = key.upper()
        if upper.endswith("_PROXY") or upper in {"ALL_PROXY", "NO_PROXY"}:
            env.pop(key, None)
        elif upper.startswith("CLIENT_") or upper.startswith("CODEBUDDY_") or upper.startswith("IDE_"):
            env.pop(key, None)
        elif upper in {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "VIRTUAL_ENV", "CONDA_PREFIX"}:
            env.pop(key, None)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


# --------------------------------------------------------------------------
# 导出的 HTML / JSON / MHTML 解包
# --------------------------------------------------------------------------


class HTMLTextExtractor(html.parser.HTMLParser):
    """把 HTML 压成纯文本：丢掉脚本样式，块级标签换行，保留文字与换行结构。"""

    _SKIP = {"script", "style", "noscript", "template", "svg"}
    _BLOCK = {
        "p", "div", "br", "li", "tr", "td", "th", "section", "article",
        "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "table",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        if name in self._SKIP:
            self._skip_depth += 1
            return
        if name in self._BLOCK:
            self._chunks.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self._BLOCK:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if name in self._BLOCK:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if data:
            self._chunks.append(data)

    def text(self) -> str:
        joined = "".join(self._chunks)
        lines = [html.unescape(line).strip() for line in joined.splitlines()]
        return "\n".join(line for line in lines if line)


def html_to_text(markup: str) -> str:
    """HTML → 纯文本（保留分段）。解析失败时退回粗暴去标签。"""
    if not markup:
        return ""
    parser = HTMLTextExtractor()
    try:
        parser.feed(str(markup))
        parser.close()
        return parser.text()
    except Exception:  # noqa: BLE001 - 解析器对脏 HTML 不该让整批导出失败
        return clean_text(re.sub(r"<[^>]+>", " ", str(markup)))


def looks_like_message(node: Mapping[str, Any]) -> bool:
    """结构上像不像一条消息：有正文，且至少有发送者或时间之一。"""
    if not isinstance(node, Mapping):
        return False
    has_text = any(isinstance(node.get(key), str) and node.get(key).strip() for key in TEXT_KEYS)
    if not has_text:
        return False
    has_sender = any(node.get(key) for key in SENDER_KEYS)
    has_time = any(node.get(key) for key in TIME_KEYS)
    return bool(has_sender or has_time)


def walk_json(node: Any) -> Iterator[Mapping[str, Any]]:
    """深度优先遍历任意 JSON 结构，产出所有 dict 节点。"""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            yield current
            for value in current.values():
                if isinstance(value, (Mapping, list)):
                    stack.append(value)
        elif isinstance(current, list):
            for value in current:
                if isinstance(value, (Mapping, list)):
                    stack.append(value)


def extract_text_from_object(node: Mapping[str, Any]) -> str:
    """从消息对象里取正文：优先已知正文键，其次看嵌套的内容对象。"""
    for key in TEXT_KEYS:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return clean_text(value)
    for key in ("content", "elements", "payload"):
        value = node.get(key)
        if isinstance(value, Mapping):
            nested = extract_text_from_object(value)
            if nested:
                return nested
        elif isinstance(value, list):
            pieces = []
            for item in value:
                if isinstance(item, Mapping):
                    piece = extract_text_from_object(item)
                    if piece:
                        pieces.append(piece)
                elif isinstance(item, str):
                    pieces.append(clean_text(item))
            if pieces:
                return clean_text(" ".join(pieces))
    return ""


def find_sender(node: Mapping[str, Any]) -> str:
    for key in SENDER_KEYS:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return clean_text(value)
        if isinstance(value, Mapping):
            for inner in ("name", "nickname", "displayName", "uid", "uin"):
                candidate = value.get(inner)
                if isinstance(candidate, str) and candidate.strip():
                    return clean_text(candidate)
    return ""


def find_timestamp(node: Mapping[str, Any]) -> dt.datetime | None:
    for key in TIME_KEYS:
        if key in node:
            stamp = parse_timestamp(node.get(key))
            if stamp is not None:
                return stamp
    return None


def find_group_name(node: Mapping[str, Any]) -> str:
    for key in GROUP_NAME_KEYS:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return clean_text(value)
    return ""


def parse_text_messages(raw: str, source: str = "") -> list[Message]:
    """解析「时间 发送者: 正文」这类纯文本聊天导出。"""
    messages: list[Message] = []
    pending: list[str] = []
    head = re.compile(
        r"^\s*(?P<time>\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?)\s+"
        r"(?P<sender>[^:：]{1,32})[:：]\s*(?P<text>.*)$"
    )
    for line in str(raw).splitlines():
        found = head.match(line)
        if found:
            if pending:
                messages.append(Message(None, "", clean_text(" ".join(pending)), source))
                pending = []
            messages.append(
                Message(
                    parse_timestamp(found.group("time")),
                    found.group("sender").strip(),
                    clean_text(found.group("text")),
                    source,
                )
            )
            continue
        if line.strip():
            pending.append(line.strip())
    if pending:
        messages.append(Message(None, "", clean_text(" ".join(pending)), source))
    return [m for m in messages if m.text]


def parse_mhtml_messages(raw: bytes, source: str = "") -> list[Message]:
    """解析 MHTML/EML：取出 text/html 或 text/plain 部分再按文本消息解析。"""
    messages: list[Message] = []
    try:
        parsed = BytesParser(policy=email_policy.default).parsebytes(raw)
    except Exception:  # noqa: BLE001
        return messages
    for part in parsed.walk():
        ctype = (part.get_content_type() or "").lower()
        if ctype not in {"text/html", "text/plain"}:
            continue
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:  # noqa: BLE001
            continue
        body = decode_bytes(payload, part.get_content_charset() or "")
        if ctype == "text/html":
            body = html_to_text(body)
        messages.extend(parse_text_messages(body, source))
    return messages


def _messages_from_json(payload: Any, source: str) -> list[Message]:
    found: list[Message] = []
    for node in walk_json(payload):
        if not looks_like_message(node):
            continue
        text = extract_text_from_object(node)
        if not text:
            continue
        found.append(
            Message(
                find_timestamp(node),
                find_sender(node),
                text,
                find_group_name(node) or source,
            )
        )
    return found


def load_messages_from_bytes(raw: bytes, name: str = "", source: str = "") -> list[Message]:
    """按扩展名猜格式并解析；认不出就按纯文本处理。"""
    label = (name or source or "").lower()
    if label.endswith((".mhtml", ".mht", ".eml")):
        return parse_mhtml_messages(raw, source)
    text = decode_bytes(raw)
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            return _messages_from_json(json.loads(stripped), source)
        except json.JSONDecodeError:
            pass
    if "<html" in stripped[:2048].lower() or "<body" in stripped[:2048].lower():
        return parse_text_messages(html_to_text(stripped), source)
    return parse_text_messages(text, source)


def load_messages_from_file(path: str | os.PathLike[str], source: str = "") -> list[Message]:
    target = Path(path)
    if not target.is_file():
        return []
    return load_messages_from_bytes(target.read_bytes(), target.name, source or target.stem)


def collect_input_files(path: str | os.PathLike[str]) -> list[Path]:
    """收集输入：单文件直接收；目录递归收；zip 里的成员解到临时目录不收，交由调用方解。"""
    root = Path(path)
    if root.is_file():
        return [root] if root.suffix.lower() in SUPPORTED_EXTS or root.suffix else [root]
    if not root.is_dir():
        return []
    found: list[Path] = []
    for candidate in sorted(root.rglob("*")):
        if not candidate.is_file():
            continue
        if candidate.name.startswith("."):
            continue
        found.append(candidate)
    return found


# --------------------------------------------------------------------------
# 过滤、去重、相似度
# --------------------------------------------------------------------------


def dedupe_messages(messages: Iterable[Message]) -> list[Message]:
    """同一条消息被多处导出时只留一份：正文 + 发送者 + 分钟级时间相同即视为重复。"""
    seen: set[tuple[str, str, str]] = set()
    kept: list[Message] = []
    for message in messages:
        stamp = message.timestamp.strftime("%Y%m%d%H%M") if message.timestamp else ""
        key = (normalize_for_match(message.text), message.sender.strip(), stamp)
        if key in seen:
            continue
        seen.add(key)
        kept.append(message)
    return kept


def filter_by_time(
    messages: Iterable[Message],
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
) -> list[Message]:
    kept = []
    for message in messages:
        stamp = message.timestamp
        if stamp is None:
            kept.append(message)
            continue
        if since is not None and stamp < since:
            continue
        if until is not None and stamp > until:
            continue
        kept.append(message)
    return kept


def filter_by_groups(messages: Iterable[Message], groups: Iterable[str]) -> list[Message]:
    wanted = {str(g).strip() for g in groups if str(g).strip()}
    if not wanted:
        return list(messages)
    return [m for m in messages if str(m.source or "").strip() in wanted]


def _similarity(left: str, right: str) -> float:
    import difflib

    return difflib.SequenceMatcher(None, left, right).ratio()


def similar_text(left: str, right: str, threshold: float = 0.88) -> bool:
    """两段文本是不是「同一件事」：归一化后比对，短文本要求完全一致。"""
    a = normalize_for_match(left)
    b = normalize_for_match(right)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) < 12 or len(b) < 12:
        return False
    if a in b or b in a:
        shorter = min(len(a), len(b))
        if shorter >= 0.7 * max(len(a), len(b)):
            return True
    return _similarity(a, b) >= threshold


def dedupe_items(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """跨群同文案合并：保留第一条，把各群名收进 ``duplicate_groups``。"""
    merged: list[dict[str, Any]] = []
    for raw in items:
        item = dict(raw)
        message = item.get("message")
        text = ""
        if isinstance(message, Message):
            text = message.text
        elif isinstance(message, Mapping):
            text = str(message.get("text") or "")
        text = text or str(item.get("summary") or "")
        group = str(item.get("group_name") or item.get("group") or item.get("source") or "")
        hits = None
        for kept in merged:
            kept_message = kept.get("message")
            kept_text = ""
            if isinstance(kept_message, Message):
                kept_text = kept_message.text
            elif isinstance(kept_message, Mapping):
                kept_text = str(kept_message.get("text") or "")
            kept_text = kept_text or str(kept.get("summary") or "")
            if text and kept_text and similar_text(text, kept_text):
                hits = kept
                break
        if hits is None:
            if group:
                item.setdefault("duplicate_groups", [group])
            merged.append(item)
            continue
        names = list(hits.get("duplicate_groups") or [])
        for value in (group, str(item.get("group_name") or item.get("group") or "")):
            if value and value not in names:
                names.append(value)
        if names:
            hits["duplicate_groups"] = names
    return merged


_LINK_RE = re.compile(r"https?://[^\s<>\"'）)】\]]+", re.I)


def extract_links(text: str) -> list[str]:
    """抽出正文里的链接（去掉句末标点，保留顺序去重）。"""
    found: list[str] = []
    for match in _LINK_RE.finditer(str(text or "")):
        url = match.group(0).rstrip(".,;:!?").strip()
        if url and url not in found:
            found.append(url)
    return found


def item_group(item: Mapping[str, Any]) -> str:
    for key in ("group_name", "group", "source"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return clean_text(value)
    message = item.get("message")
    if isinstance(message, Message) and message.source:
        return clean_text(message.source)
    if isinstance(message, Mapping):
        return clean_text(message.get("source") or "")
    return ""


# --------------------------------------------------------------------------
# 语气判断
# --------------------------------------------------------------------------

_QUESTION_TAIL = re.compile(r"[?？]\s*$")
_QUESTION_MARKS = ("吗", "呢", "吧", "么")


def has_directive_notice(text: str) -> bool:
    """这句里有没有「命令别人做事」的味道（请/务必/需要/必须/大家…）。"""
    body = clean_text(text)
    if not body:
        return False
    for word in DIRECTIVE_WORDS:
        if word in body:
            return True
    for word in ACTION_WORDS:
        if word in body:
            return True
    return False


def is_colloquial_question(text: str) -> bool:
    """随口一问（闲聊），不是通知。有指令词的设问不算闲聊。"""
    body = clean_text(text)
    if not body or len(body) < 6:
        return False
    if has_directive_notice(body):
        return False
    if any(word in body for word in ("截止", "报名", "提交", "填写", "务必", "统一")):
        return False
    if _QUESTION_TAIL.search(body):
        return True
    if any(mark in body for mark in _QUESTION_MARKS):
        return len(body) >= 12
    return False


# --------------------------------------------------------------------------
# 截止时间抽取
# --------------------------------------------------------------------------

_WEEKDAY_INDEX = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

_WEEKDAY_RE = re.compile(
    r"(?P<prefix>下周|下星期|下礼拜|本周|这周|本星期|这星期|周|星期|礼拜)"
    r"(?:周|星期|礼拜)?\s*(?P<day>[一二三四五六日天])"
)

_RANGE_RE = re.compile(
    r"(?P<y1>\d{4})年(?P<m1>\d{1,2})月(?P<d1>\d{1,2})日?\s*[—\-~～至到]{1,2}\s*"
    r"(?:(?P<y2>\d{4})年)?(?P<m2>\d{1,2})月(?P<d2>\d{1,2})日?"
)

_YMD_RE = re.compile(r"(?:(?P<y>\d{4})年)?(?P<m>\d{1,2})月(?P<d>\d{1,2})日?")
_ISO_DATE_RE = re.compile(r"(?P<y>\d{4})[-/](?P<m>\d{1,2})[-/](?P<d>\d{1,2})")

_CN_NUMERALS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6,
    "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12,
    "十三": 13, "十四": 14, "十五": 15, "十六": 16, "十七": 17,
    "十八": 18, "十九": 19, "二十": 20, "半": 30,
}

_HOUR_TOKEN = r"(?:\d{1,2}|十[一二三四五六七八九]?|[一二两三四五六七八九])"

_CLOCK_CN_RE = re.compile(
    r"(?P<merid>凌晨|早晨|早上|上午|中午|下午|傍晚|晚上|今晚|夜里|夜间)?\s*"
    r"(?P<h>" + _HOUR_TOKEN + r")\s*(?:[点时時])\s*"
    r"(?:(?P<half>半)|(?P<m>\d{1,2}|[一二三四五六七八九十]{1,3})\s*分?)?"
)


def _to_int(token: str | None, default: int = 0) -> int:
    """把「5」「五」「十一」这类数字写法转成整数。"""
    if not token:
        return default
    token = token.strip()
    if token.isdigit():
        return int(token)
    if token in _CN_NUMERALS:
        return _CN_NUMERALS[token]
    if token.startswith("十") and len(token) > 1 and token[1:] in _CN_NUMERALS:
        return 10 + _CN_NUMERALS[token[1:]]
    return default

_CLOCK_COLON_RE = re.compile(r"(?P<h>\d{1,2})\s*[:：]\s*(?P<m>\d{2})")


def _day_shift(body: str) -> int | None:
    """相对日（明天/后天/今天…）；月份日期另算。"""
    for word, delta in (
        ("大后天", 3),
        ("后天", 2),
        ("明早", 1),
        ("明晚", 1),
        ("明天", 1),
        ("明日", 1),
        ("明儿", 1),
        ("今晚", 0),
        ("今夜", 0),
        ("今天", 0),
        ("今日", 0),
        ("今儿", 0),
    ):
        if word in body:
            return delta
    return None


def _weekday_date(base: dt.datetime, prefix: str, day_char: str) -> dt.datetime | None:
    target = _WEEKDAY_INDEX.get(day_char)
    if target is None:
        return None
    monday = base.date() - dt.timedelta(days=base.weekday())
    if prefix in ("下周", "下星期", "下礼拜"):
        return dt.datetime.combine(monday + dt.timedelta(days=7 + target), dt.time())
    candidate = monday + dt.timedelta(days=target)
    if candidate < base.date():
        candidate += dt.timedelta(days=7)
    return dt.datetime.combine(candidate, dt.time())


def _clock_in(body: str) -> tuple[int, int, bool] | None:
    """返回 (时, 分, 是否需要顺延一天)；认不出返回 None。"""
    found = _CLOCK_COLON_RE.search(body)
    if found:
        hour = int(found.group("h"))
        minute = int(found.group("m"))
        if hour == 24:
            return (0, 0, True) if minute == 0 else None
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return (hour, minute, False)
        return None
    found = _CLOCK_CN_RE.search(body)
    if not found:
        return None
    hour = _to_int(found.group("h"), -1)
    minute = 30 if found.group("half") else _to_int(found.group("m"), 0)
    merid = found.group("merid") or ""
    if merid in ("下午", "傍晚", "晚上", "今晚", "夜里", "夜间") and hour < 12:
        hour += 12
    elif merid == "中午" and hour < 12:
        hour += 12
    elif merid in ("凌晨", "早晨", "早上", "上午") and hour == 12:
        hour = 0
    if hour == 24:
        return (0, 0, True) if minute == 0 else None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return (hour, minute, False)


def extract_deadline(message: Message) -> dt.datetime | None:
    """从正文里抽截止时间；纯闲聊、没有时间线索的一律 None。"""
    body = clean_text(getattr(message, "text", "") or "")
    if not body:
        return None
    if is_colloquial_question(body):
        return None
    if _QUESTION_TAIL.search(body) and not has_directive_notice(body):
        return None
    if ATTACHMENT_PLACEHOLDER.match(body) and not has_directive_notice(body):
        # 「[文件]截屏2026-10-07 16.33.44.png」里的日期是文件名，不是截止时间
        return None

    base = getattr(message, "timestamp", None) or dt.datetime.now()
    if not isinstance(base, dt.datetime):
        base = dt.datetime.now()

    day: dt.datetime | None = None
    range_hit = _RANGE_RE.search(body)
    if range_hit:
        year = int(range_hit.group("y2") or range_hit.group("y1"))
        day = _build_date(year, int(range_hit.group("m2")), int(range_hit.group("d2")))
    if day is None:
        ymd = _YMD_RE.search(body) or _ISO_DATE_RE.search(body)
        if ymd:
            year = int(ymd.group("y") or base.year)
            day = _build_date(year, int(ymd.group("m")), int(ymd.group("d")))
    if day is None:
        weekday_hit = _WEEKDAY_RE.search(body)
        if weekday_hit:
            day = _weekday_date(base, weekday_hit.group("prefix"), weekday_hit.group("day"))
    if day is None:
        shift = _day_shift(body)
        if shift is not None:
            day = base + dt.timedelta(days=shift)
    if day is None:
        return None

    clock = _clock_in(body)
    if clock is None:
        return day.replace(hour=23, minute=59, second=0, microsecond=0)
    hour, minute, roll = clock
    result = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if roll:
        result += dt.timedelta(days=1)
    return result


def _build_date(year: int, month: int, day: int) -> dt.datetime | None:
    try:
        return dt.datetime(year, month, day)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# 打分与分类
# --------------------------------------------------------------------------

CATEGORIES = ("urgent", "action", "academic", "info")


def _hits(body: str, words: Iterable[str]) -> list[str]:
    return [word for word in words if word in body]


def analyze_message(message: Message) -> dict[str, Any]:
    """给一条消息打分、给类别、找截止时间。返回值是摘要与待办的公共输入。"""
    body = clean_text(getattr(message, "text", "") or "")
    sender = clean_text(getattr(message, "sender", "") or "")
    score = 0

    urgent_hits = _hits(body, URGENT_WORDS)
    action_hits = _hits(body, ACTION_WORDS)
    academic_hits = _hits(body, ACADEMIC_WORDS)
    admin_hits = _hits(body, ADMIN_WORDS)
    authority_hits = _hits(sender, AUTHORITY_WORDS)
    directive_hits = _hits(body, DIRECTIVE_WORDS)

    colloquial = is_colloquial_question(body)

    if urgent_hits:
        score += 3
    if action_hits:
        score += 2
    if academic_hits:
        score += 2
    if admin_hits:
        score += 1
    if authority_hits:
        score += 2
    if any(word in body for word in ("全体成员", "大家", "各位同学", "全体同学", "所有同学")):
        score += 1
    if extract_links(body):
        score += 1
    if len(body) >= 80:
        score += 1

    placeholder = bool(ATTACHMENT_PLACEHOLDER.match(body))
    if placeholder and not action_hits and not directive_hits:
        score -= 6
    if len(body) <= 5:
        score -= 3
    if body in ACK_ONLY:
        score -= 4
    if any(marker in body for marker in CHATTER_MARKERS):
        score -= 6
    if colloquial:
        score -= 6

    deadline = extract_deadline(message)
    if deadline is not None:
        score += 1
        stamp = getattr(message, "timestamp", None)
        if isinstance(stamp, dt.datetime) and deadline - stamp <= dt.timedelta(days=1):
            score += 1
            if "紧急" not in urgent_hits:
                urgent_hits = list(urgent_hits) + ["一天内"]

    tags: set[str] = set()
    if urgent_hits:
        tags.add("urgent")
    if action_hits:
        tags.add("action")
    if academic_hits:
        tags.add("academic")
    if admin_hits:
        tags.add("notice")
    if deadline is not None:
        tags.add("deadline")
    if authority_hits:
        tags.add("authority")
    if any(word in body for word in ("全体成员", "大家", "各位同学", "全体同学", "所有同学")):
        tags.add("broadcast")

    if colloquial:
        tags.discard("urgent")
        tags.discard("deadline")

    if colloquial:
        category = "info"
    elif urgent_hits or (deadline is not None and score >= 6):
        category = "urgent"
    elif action_hits:
        category = "action"
    elif academic_hits:
        category = "academic"
    else:
        category = "info"

    return {
        "message": message,
        "score": score,
        "category": category,
        "tags": tags,
        "deadline": deadline,
        "action_words": list(action_hits),
        "academic_words": list(academic_hits),
        "admin_words": list(admin_hits),
        "authority_words": list(authority_hits),
        "directive_words": list(directive_hits),
        "urgent": bool(urgent_hits) and not colloquial,
        "colloquial_question": colloquial,
        "has_attachment": placeholder,
    }


# --------------------------------------------------------------------------
# 本地文案
# --------------------------------------------------------------------------

_SENTENCE_SPLIT = re.compile(r"[。！？!?；;\n]+")


def _trim(text: str, limit: int | None) -> str:
    """压到 limit 以内并补省略号；limit=None 表示不截断，原样返回清洗后的全文。"""
    body = clean_text(text)
    if limit is None:
        return body
    if limit <= 0:
        return ""
    if len(body) <= limit:
        return body
    return body[: max(0, limit - 1)].rstrip() + "…"


def short_summary(text: str, limit: int = 60) -> str:
    """压到 limit 以内并补省略号。"""
    return _trim(text, limit)


def local_summary(text: str, limit: int | None = 130) -> str:
    """本地摘要：取第一句有信息量的话，再压到 limit。

    limit=None 时返回整条清洗后的正文（不截断、不补省略号）——给「完整内容」那条路径用。
    """
    body = clean_text(text)
    if not body:
        return ""
    if limit is None:
        return body
    for piece in _SENTENCE_SPLIT.split(body):
        candidate = piece.strip(" ，、 ")
        if len(candidate) >= 6:
            return _trim(candidate, limit)
    return _trim(body, limit)


def local_action(text: str, limit: int | None = 110) -> str:
    """本地「要做什么」：取包含动作词的那一小句。

    limit=None 时返回整条清洗后的正文（不截断、不补省略号）。
    """
    body = clean_text(text)
    if not body:
        return ""
    if limit is None:
        return body
    for piece in _SENTENCE_SPLIT.split(body):
        candidate = piece.strip(" ，、 ")
        if any(word in candidate for word in ACTION_WORDS):
            return _trim(candidate, limit)
    return _trim(body, limit)


def sort_items(items: Sequence[Mapping[str, Any]], now: dt.datetime | None = None) -> list[dict[str, Any]]:
    """排序：有截止时间的靠前（近的优先），其次分数高的。"""
    anchor = now or dt.datetime.now()

    def key(item: Mapping[str, Any]) -> tuple:
        deadline = item.get("deadline") or item.get("deadline_dt")
        if isinstance(deadline, dt.datetime):
            distance = abs((deadline - anchor).total_seconds())
        else:
            distance = float("inf")
        try:
            score = -int(item.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        return (distance, score, str(item.get("msg_id") or ""))

    return [dict(item) for item in sorted(items, key=key)]


# --------------------------------------------------------------------------
# 模型精炼（可选；只有调用方给了 api_key / CLI 才会联网）
# --------------------------------------------------------------------------

_DEFAULT_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
_OLLAMA_ENDPOINT = "http://127.0.0.1:11434/v1/chat/completions"
_LLM_ITEM_LIMIT = 50
_LLM_TEXT_LIMIT = 1200


def _codebuddy_candidates() -> list[str]:
    """CLI 候选路径：npm 包里的 bin 优先，其次是 headless 脚本。"""
    roots: list[str] = []
    for key in ("APPDATA", "LOCALAPPDATA"):
        base = os.environ.get(key, "")
        if base:
            roots.append(os.path.join(base, "npm"))
            roots.append(os.path.join(base, "Programs"))
    program_files = os.environ.get("ProgramFiles", "")
    if program_files:
        roots.append(program_files)
    # 环境变量缺失也要能找到：退回用户目录与常见安装根
    home = os.path.expanduser("~")
    roots.append(home)
    roots.append(os.path.join(home, "AppData", "Roaming", "npm"))
    roots.append(os.path.join(home, "AppData", "Local", "Programs"))
    roots.append("C:\\Program Files")
    roots.append("C:\\Program Files (x86)")
    found: list[str] = []
    for root in roots:
        found.append(os.path.join(root, "codebuddy-code", "bin", "codebuddy"))
        found.append(os.path.join(root, "codebuddy-code", "bin", "codebuddy.cmd"))
        found.append(os.path.join(root, "codebuddy", "cli", "dist", "codebuddy-headless.js"))
        found.append(os.path.join(root, "codebuddy-code", "cli", "dist", "codebuddy-headless.js"))
    return found


def detect_codebuddy_cli() -> str:
    """找一个能用的 CodeBuddy CLI；环境变量 ``QQ_DIGEST_CODEBUDDY_CLI`` 优先且不回落。"""
    override = os.environ.get("QQ_DIGEST_CODEBUDDY_CLI", "").strip()
    if override:
        return override if os.path.isfile(override) else ""
    for candidate in _codebuddy_candidates():
        if os.path.isfile(candidate):
            return candidate
    return ""


def extract_json_object(payload: str) -> Any:
    """从模型输出里掏出第一个 JSON 对象（容忍 ``` 围栏和前后废话）。"""
    text = (payload or "").strip()
    if not text:
        raise RuntimeError("模型返回空输出")
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError("模型输出 JSON 解析失败：没有找到 JSON 对象")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as error:
        raise RuntimeError(f"模型输出 JSON 解析失败：{error}") from error


def run_codebuddy(
    prompt: str,
    schema: Mapping[str, Any] | None = None,
    *,
    cli: str = "",
    timeout: int = 60,
) -> Any:
    """把 prompt 交给 CodeBuddy CLI，返回它输出的 JSON。"""
    target = cli or detect_codebuddy_cli()
    if not target:
        raise RuntimeError("没找到 CodeBuddy CLI，请检查安装路径或设置 QQ_DIGEST_CODEBUDDY_CLI")
    if not os.path.isfile(target):
        raise RuntimeError(f"CodeBuddy CLI 未找到：{target}")
    body = json.dumps({"prompt": prompt, "schema": schema or _CODEBUDDY_SCHEMA}, ensure_ascii=False)
    if target.lower().endswith((".js", ".mjs", ".cjs")):
        command = ["node", target]
    else:
        command = [target]
    try:
        completed = subprocess.run(
            command,
            input=body,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=clean_child_env(),
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"CodeBuddy CLI 超时（{timeout} 秒）") from error
    except OSError as error:
        raise RuntimeError(f"CodeBuddy CLI 无法启动：{error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"CodeBuddy CLI 退出码 {completed.returncode}：{detail[:400]}")
    stdout = (completed.stdout or "").strip()
    if not stdout:
        raise RuntimeError("CodeBuddy CLI 返回空输出")
    return extract_json_object(stdout)


def build_llm_prompt(items: Sequence[Mapping[str, Any]]) -> str:
    """把待精炼条目拼成提示词：只让它改写文案、判类别、纠截止时间。"""
    lines = [
        "你是校园群通知整理助手。下面是群里若干条消息的本地粗分析结果。",
        "请为每条输出：summary（30 字以内，说清谁要做什么）、action（要执行的动作，没有就空串）、"
        "category（urgent/action/academic/info）、deadline（YYYY-MM-DD HH:MM，没有就空串）。",
        "summary 用词严谨、书面语，不要口语和网络用语，不用感叹号；沿用群里的称呼，尽量用原话里的事实，"
        "别替人下判断，不写「请注意」「温馨提示」「建议大家」这类提示语，也不写总结句和感想；"
        "action 用动词开头的短句，只说要做的事。",
        '只输出 JSON，格式 {"items": [{"summary": ..., "action": ..., "category": ..., "deadline": ...}]}，'
        "顺序与输入一致，条数必须相同，不要解释。",
        "",
    ]
    for index, item in enumerate(items, 1):
        message = item.get("message")
        text = ""
        sender = ""
        if isinstance(message, Message):
            text, sender = message.text, message.sender
        elif isinstance(message, Mapping):
            text, sender = str(message.get("text") or ""), str(message.get("sender") or "")
        lines.append(f"[{index}] 发送者：{sender or '未知'}")
        lines.append(f"    原文：{text[:_LLM_TEXT_LIMIT]}")
    return "\n".join(lines)


def _post_chat(endpoint: str, payload: Mapping[str, Any], api_key: str, timeout: int) -> str:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        raw = response.read()
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"模型接口返回的不是 JSON：{error}") from error
    try:
        return parsed["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError("模型接口返回结构不认识（缺 choices[0].message.content）") from error


def _merge_refined(items: Sequence[Mapping[str, Any]], refined: Any) -> list[dict[str, Any]]:
    """把模型结果并回原条目：只覆盖文案类字段，消息本体不动。"""
    if isinstance(refined, Mapping):
        refined = refined.get("items")
    if not isinstance(refined, list):
        raise RuntimeError("模型输出 JSON 解析失败：缺少 items 数组")
    merged: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        updated = dict(item)
        patch = refined[index] if index < len(refined) and isinstance(refined[index], Mapping) else None
        if patch:
            for key in ("summary", "action", "category", "audience", "condition", "details"):
                value = patch.get(key)
                if isinstance(value, str) and value.strip():
                    updated[key] = clean_text(value)
            raw_deadline = patch.get("deadline")
            if isinstance(raw_deadline, str) and raw_deadline.strip():
                stamp = parse_timestamp(raw_deadline)
                if stamp is not None:
                    updated["deadline"] = stamp
                    updated["deadline_dt"] = stamp
        merged.append(updated)
    return merged


def _refine_via_http(
    items: Sequence[Mapping[str, Any]],
    api_key: str,
    model: str,
    endpoint: str,
    timeout: int,
    *,
    retries: int,
    backoff: float,
    backend: str,
) -> list[dict[str, Any]]:
    from noticehub.retry import LLMError, LLMRequestError, call_with_retries  # 延迟导入避免环

    target = endpoint or (_OLLAMA_ENDPOINT if backend == "ollama" else _DEFAULT_ENDPOINT)
    payload: dict[str, Any] = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": 6000,
        "messages": [{"role": "user", "content": build_llm_prompt(items)}],
    }
    if backend == "ollama" or "11434" in target or "localhost" in target:
        payload["think"] = False
        payload["reasoning_effort"] = "none"

    def once() -> str:
        try:
            return _post_chat(target, payload, api_key, timeout)
        except urllib.error.HTTPError as error:
            raise LLMRequestError(f"LLM 请求失败: HTTP {error.code}", status=error.code) from error
        except urllib.error.URLError as error:
            raise LLMRequestError(f"LLM 请求失败: {error}") from error
        except (TimeoutError, OSError) as error:
            raise LLMRequestError(f"LLM 请求失败: {error}") from error

    def judge(error: BaseException) -> bool:
        if isinstance(error, LLMError):
            return True
        return isinstance(error, (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError))

    content = call_with_retries(once, retries=retries, backoff=backoff, should_retry=judge, label="文本模型精炼")
    return _merge_refined(items, extract_json_object(content))


def refine_with_dashscope(
    items: Sequence[Mapping[str, Any]],
    api_key: str,
    model: str = "qwen-plus",
    endpoint: str = "",
    timeout: int = 60,
    *,
    retries: int = 2,
    backoff: float = 1.5,
    raw_response: Any = None,
    backend: str = "",
) -> list[dict[str, Any]]:
    """用模型改写摘要/动作/类别/截止时间。失败一律抛 RuntimeError，由调用方决定是否回退。"""
    selected = (backend or os.environ.get("QQ_DIGEST_LLM_BACKEND", "") or "auto").strip().lower()
    batch = list(items)[:_LLM_ITEM_LIMIT]
    if not batch:
        return []

    def via_http() -> list[dict[str, Any]]:
        return _refine_via_http(
            batch, api_key, model, endpoint, timeout,
            retries=retries, backoff=backoff, backend=selected,
        )

    if selected in ("codebuddy", "cli"):
        cli = detect_codebuddy_cli()
        content = run_codebuddy(build_llm_prompt(batch), _CODEBUDDY_SCHEMA, cli=cli, timeout=timeout)
        return _merge_refined(batch, content)
    if selected == "ollama":
        return via_http()
    if selected in ("http", "dashscope"):
        return via_http()
    if selected in ("auto", ""):
        cli = detect_codebuddy_cli()
        if cli:
            try:
                content = run_codebuddy(build_llm_prompt(batch), _CODEBUDDY_SCHEMA, cli=cli, timeout=timeout)
                return _merge_refined(batch, content)
            except RuntimeError:
                return via_http()
        if not api_key:
            raise RuntimeError("未配置 DASHSCOPE_API_KEY，也没有可用的 CodeBuddy CLI")
        return via_http()
    # openai / 其它兼容 OpenAI 协议的写法一律走 HTTP
    return via_http()


# --------------------------------------------------------------------------
# 兼容用的命令行入口（真正的 CLI 在 main.py）
# --------------------------------------------------------------------------


def parse_cli_datetime(value: str | None) -> dt.datetime | None:
    return parse_timestamp(value) if value else None


def default_output_path() -> Path:
    return Path.cwd() / "digest.md"


def render_markdown(messages: Sequence[Message], *, title: str = "群消息摘要") -> str:
    lines = [f"# {title}", ""]
    for message in messages:
        stamp = format_deadline(message.timestamp) or "时间未知"
        lines.append(f"- **{stamp} · {message.sender or '未知'}**：{local_summary(message.text, 120)}")
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="群消息本地分析（兼容入口）")
    parser.add_argument("path", nargs="?", help="导出文件或目录")
    parser.add_argument("--output", "-o", default="", help="输出 Markdown 的路径")
    parser.add_argument("--since", default="", help="起始时间")
    parser.add_argument("--until", default="", help="结束时间")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if not args.path:
        build_parser().print_help()
        return 2
    messages: list[Message] = []
    for path in collect_input_files(args.path):
        messages.extend(load_messages_from_file(path))
    messages = dedupe_messages(messages)
    messages = filter_by_time(messages, parse_cli_datetime(args.since), parse_cli_datetime(args.until))
    target = Path(args.output) if args.output else default_output_path()
    target.write_text(render_markdown(messages), encoding="utf-8")
    print(f"已写出 {target}（{len(messages)} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


