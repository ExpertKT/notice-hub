"""群文件与群图片：事件解析、安全下载、文本/文档提取、模型调用与清理。

行为契约见 `docs/rewrite-spec/batch-d.md`。本模块只做「解析与取文本」，
后台队列与原图留存住在 `noticehub.application.attachments`，这里再导出，
保证对外 import 路径只有 `noticehub.attachments` 一个。
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from xml.etree import ElementTree

from noticehub.domain.time import iso
from noticehub.infrastructure.clock import SystemClock
from noticehub.retry import (
    LLMRequestError,
    LLMResponseError,
    call_with_retries,
    llm_should_retry,
)

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------- 对外常量
TEXT_EXTS = frozenset({".txt", ".md", ".csv", ".json", ".log", ".htm", ".html"})
DOC_EXTS = frozenset({".docx", ".pdf", ".xlsx", ".xlsm", ".pptx"})
ARCHIVE_EXTS = frozenset({".zip"})
IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"})
SUPPORTED_EXTS = frozenset(TEXT_EXTS | DOC_EXTS | ARCHIVE_EXTS)
LEGACY_EXTS = frozenset({".doc", ".xls", ".ppt", ".wps", ".et", ".dps"})

MAX_ZIP_ENTRIES = 20
MAX_ZIP_TOTAL_BYTES = 5 * 1024 * 1024
MAX_PDF_PAGES = 40
MIN_IMAGE_BYTES = 10 * 1024
MIN_DOC_TEXT_CHARS = 40
MAX_RECORD_CHARS = 1500
DOC_CHUNK_CHARS = 4000
DOC_MAX_CHUNKS = 8
NO_TEXT_MARKERS = ("无有效通知", "无文字内容", "没有文字", "无文字")

IMAGE_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

ALLOWED_DOWNLOAD_SCHEMES = ("http", "https")
MAX_DOWNLOAD_REDIRECTS = 5
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) notice-hub/1.0"

SUMMARY_SYSTEM_PROMPT = (
    "你是大学生群通知整理助手。只依据用户提供的材料工作，不猜测、不补造、不描述画面。"
)
DOC_PROMPT = (
    "下面是一份群文件的内容（文件名：{name}）。请只依据正文，输出严格 JSON（不要 Markdown、"
    "不要代码块、不要解释）："
    '{{"summary":"不超过80字","audience":"适用对象","condition":"适用条件",'
    '"details":["按原文分条保留不同人群、例外、地点、附件、链接、注意事项"],'
    '"action":"需要做什么","deadline":"YYYY-MM-DD HH:MM 或空串"}}。'
    "必须保留适用对象、条件、例外和人群差异，不得把只针对部分同学的要求写成所有同学；"
    "没有相关信息的字段留空字符串。\n\n正文：\n{body}"
)
IMAGE_PROMPT = (
    "请把图片里的文字原样提取出来，保留日期、时间、人名、金额、地点、群号等关键信息；"
    "如果图片没有可读文字，或不含与学习、通知、表格、名单相关的内容，只回复“无有效通知”；"
    "不要描述画面，不要解释。"
)

_BLOCKED_NETWORKS_V4 = tuple(
    ipaddress.ip_network(item)
    for item in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)
_BLOCKED_NETWORKS_V6 = tuple(
    ipaddress.ip_network(item)
    for item in ("::/128", "::1/128", "64:ff9b::/96", "fc00::/7", "fe80::/10", "ff00::/8", "2001:db8::/32")
)

_SUMMARY_STRIP = re.compile(r"[\s\u3000]+")
_XML_TRAILING_BLANKS = re.compile(r"\n{3,}")


class AttachmentError(RuntimeError):
    """附件处理失败；异常文本就是给用户或日志看的完整句子。"""


# ------------------------------------------------------------------ 数据对象
@dataclass
class Attachment:
    kind: str
    name: str
    group_id: str = ""
    group_name: str = ""
    sender_id: str = ""
    sender_name: str = ""
    url: str = ""
    file_id: str = ""
    busid: str = ""
    ts: str = ""
    msg_id: str = ""
    hint: str = ""
    size: int = 0

    @property
    def suffix(self) -> str:
        return Path(self.name).suffix.lower() if self.name else ""

    @property
    def key(self) -> str:
        raw = "%s|%s|%s|%d" % (self.group_id, self.kind, self.name, int(self.size or 0))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    @property
    def size_text(self) -> str:
        size = int(self.size or 0)
        if size <= 0:
            return ""
        if size < 1024:
            return "%dB" % size
        if size < 1024 * 1024:
            return "%.0fKB" % (size / 1024)
        return "%.1fMB" % (size / (1024 * 1024))


@dataclass
class AttachmentStats:
    queued: int = 0
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    last_error: str = ""
    last_at: str = ""
    counters: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "queued": self.queued,
            "processed": self.processed,
            "skipped": self.skipped,
            "failed": self.failed,
            "last_error": self.last_error,
            "last_at": self.last_at,
        }
        data.update(self.counters)
        return data


def _now_timestamp() -> str:
    return iso(SystemClock().now())


# ------------------------------------------------------------- 事件 → 附件
def _whitelisted(group_id: str, settings: Any) -> bool:
    names = {str(item) for item in (getattr(settings, "group_whitelist", ()) or ())}
    return bool(group_id) and group_id in names


def _group_label(group_id: str, settings: Any) -> str:
    if not group_id:
        return "未知群"
    aliases = getattr(settings, "group_aliases", {}) or {}
    return str(aliases.get(group_id) or group_id)


def _int_of(*values: Any) -> int:
    for value in values:
        if value is None or value == "":
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def _stamp_of(payload: dict[str, Any]) -> str:
    raw = payload.get("time")
    if raw is None:
        return _now_timestamp()
    try:
        value = iso(int(raw))
    except (TypeError, ValueError, OSError):
        return _now_timestamp()
    return value or _now_timestamp()


def _sender_of(payload: dict[str, Any]) -> tuple[str, str]:
    sender = payload.get("sender")
    sender = sender if isinstance(sender, dict) else {}
    user_id = str(payload.get("user_id") or "")
    name = str(sender.get("card") or sender.get("nickname") or user_id or "群成员")
    return str(sender.get("user_id") or user_id), name


def attachments_from_message(payload: Any, settings: Any) -> list[Attachment]:
    if not isinstance(payload, dict):
        return []
    if payload.get("post_type") != "message" or payload.get("message_type") != "group":
        return []
    group_id = str(payload.get("group_id") or "")
    if not _whitelisted(group_id, settings):
        return []
    user_id = str(payload.get("user_id") or "")
    self_id = str(payload.get("self_id") or "")
    if user_id and self_id and user_id == self_id:
        return []
    segments = payload.get("message")
    if not isinstance(segments, list):
        return []

    stamp = _stamp_of(payload)
    sender_id, sender_name = _sender_of(payload)
    message_id = payload.get("message_id")
    msg_id = "onebot:%s:%s" % (group_id, message_id) if message_id not in (None, "") else ""
    hint = "".join(
        str(segment.get("data", {}).get("text") or "")
        for segment in segments
        if isinstance(segment, dict) and segment.get("type") == "text"
    ).strip()
    group_name = _group_label(group_id, settings)

    found: list[Attachment] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        kind = segment.get("type")
        data = segment.get("data")
        if not isinstance(data, dict):
            continue
        if kind == "file":
            name = str(data.get("file") or data.get("file_name") or "").strip()
            if not name:
                continue
            found.append(
                Attachment(
                    kind="file",
                    name=name,
                    group_id=group_id,
                    group_name=group_name,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    url=str(data.get("url") or "").strip(),
                    file_id=str(data.get("file_id") or "").strip(),
                    busid=str(data.get("busid") or "").strip(),
                    ts=stamp,
                    msg_id=msg_id,
                    hint=hint,
                    size=_int_of(data.get("file_size"), data.get("size")),
                )
            )
        elif kind == "image":
            if str(data.get("sub_type") or "0") == "1":
                continue
            summary = str(data.get("summary") or "").strip()
            if re.fullmatch(r"\[[^\[\]]{1,12}\]", summary):
                continue
            declared = _int_of(data.get("file_size"), data.get("size"))
            if 0 < declared < MIN_IMAGE_BYTES:
                continue
            url = str(data.get("url") or "").strip()
            file_ref = str(data.get("file") or data.get("file_id") or "").strip()
            if not url and not file_ref:
                continue
            name = str(data.get("file") or data.get("filename") or "image.jpg").strip() or "image.jpg"
            found.append(
                Attachment(
                    kind="image",
                    name=name,
                    group_id=group_id,
                    group_name=group_name,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    url=url,
                    file_id=file_ref,
                    ts=stamp,
                    msg_id=msg_id,
                    hint=hint,
                    size=declared,
                )
            )
    return found


def attachment_from_notice(payload: Any, settings: Any) -> Attachment | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("post_type") != "notice" or payload.get("notice_type") != "group_upload":
        return None
    group_id = str(payload.get("group_id") or "")
    if not _whitelisted(group_id, settings):
        return None
    data = payload.get("file")
    if not isinstance(data, dict):
        return None
    name = str(data.get("name") or data.get("file") or "").strip()
    if not name:
        return None
    file_id = str(data.get("id") or data.get("file_id") or "").strip()
    user_id = str(payload.get("user_id") or "")
    return Attachment(
        kind="file",
        name=name,
        group_id=group_id,
        group_name=_group_label(group_id, settings),
        sender_id=user_id,
        sender_name=user_id or "群成员",
        file_id=file_id,
        busid=str(data.get("busid") or "").strip(),
        ts=_stamp_of(payload),
        msg_id="onebot:%s:upload:%s" % (group_id, file_id or name),
        size=_int_of(data.get("size"), data.get("file_size")),
    )


def attachments_from_history(message: dict, settings: Any) -> list[Attachment]:
    if not isinstance(message, dict):
        return []
    payload = dict(message)
    payload.setdefault("post_type", "message")
    payload.setdefault("message_type", "group")
    return attachments_from_message(payload, settings)


# ------------------------------------------------------------ 文本与文档解析
def decode_text_bytes(data: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030", "utf-16", "big5"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _xml_text(data: bytes) -> str:
    root = ElementTree.fromstring(data)
    pieces: list[str] = []
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else ""
        if tag in ("t", "instrText"):
            pieces.append(element.text or "")
        elif tag in ("br", "tab"):
            pieces.append(" ")
        elif tag in ("p", "tr"):
            pieces.append("\n")
    return _XML_TRAILING_BLANKS.sub("\n\n", "".join(pieces)).strip()


def extract_docx(path: Path) -> str:
    chunks: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = sorted(
            info.filename
            for info in archive.infolist()
            if not info.is_dir()
            and info.filename.startswith("word/")
            and info.filename.endswith(".xml")
            and any(word in info.filename for word in ("document", "header", "footer"))
        )
        for name in names:
            try:
                text = _xml_text(archive.read(name))
            except (ElementTree.ParseError, KeyError, zipfile.BadZipFile):
                continue
            if text:
                chunks.append(text)
    return "\n".join(chunks).strip()


def extract_pptx(path: Path) -> str:
    slides: list[tuple[int, str]] = []
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            match = re.fullmatch(r"ppt/slides/slide(\d+)\.xml", info.filename)
            if not match:
                continue
            try:
                text = _xml_text(archive.read(info.filename))
            except (ElementTree.ParseError, KeyError, zipfile.BadZipFile):
                continue
            if text:
                slides.append((int(match.group(1)), text))
    slides.sort(key=lambda pair: pair[0])
    pages: list[str] = []
    for index, (_number, text) in enumerate(slides, start=1):
        pages.append("【第%d页】%s" % (index, text))
    return "\n".join(pages).strip()


def extract_pdf(path: Path) -> str:
    try:
        import pypdf  # noqa: PLC0415 - 可选依赖，缺了就降级
    except ImportError:
        return ""
    pages: list[str] = []
    truncated = False
    try:
        reader = pypdf.PdfReader(str(path))
        total = len(reader.pages)
        for index, page in enumerate(reader.pages[:MAX_PDF_PAGES], start=1):
            try:
                raw = page.extract_text() or ""
            except Exception:  # noqa: BLE001 - 单页坏了跳过该页
                continue
            text = re.sub(r"[ \t]+", " ", raw).strip()
            if text:
                pages.append(text)
        truncated = total > MAX_PDF_PAGES
    except Exception as error:  # noqa: BLE001 - 加密/损坏的 PDF 一律降级
        LOGGER.warning("PDF 解析失败 %s：%s", path, error)
        return ""
    if truncated:
        pages.append("……（文档较长，仅读前 %d 页）" % MAX_PDF_PAGES)
    return "\n".join(pages).strip()


def extract_xlsx(path: Path) -> str:
    try:
        import openpyxl  # noqa: PLC0415 - 可选依赖
    except ImportError:
        return ""
    blocks: list[str] = []
    book = None
    try:
        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
        for sheet in book.worksheets:
            lines: list[str] = []
            truncated = False
            for index, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                if index > 200:
                    truncated = True
                    break
                cells = [str(value).strip() for value in (row or ()) if value is not None and str(value).strip()]
                if cells:
                    lines.append(" | ".join(cells))
            if not lines:
                continue
            if truncated:
                lines.append("……（仅读前 200 行）")
            blocks.append("【工作表：%s】\n%s" % (sheet.title, "\n".join(lines)))
    except Exception as error:  # noqa: BLE001 - 坏表降级成空串
        LOGGER.warning("Excel 解析失败 %s：%s", path, error)
        return ""
    finally:
        if book is not None:
            try:
                book.close()
            except Exception:  # noqa: BLE001
                pass
    return "\n".join(blocks).strip()


def _safe_entry(name: str) -> bool:
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):
        return False
    return ".." not in normalized.split("/")


def extract_zip(path: Path, depth: int = 0) -> str:
    if depth > 1:
        return ""
    try:
        with zipfile.ZipFile(path) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            picked = infos[:MAX_ZIP_ENTRIES]
            blocks: list[str] = []
            produced = 0
            budget = DOC_CHUNK_CHARS * DOC_MAX_CHUNKS
            for info in picked:
                name = info.filename.replace("\\", "/")
                if not _safe_entry(name):
                    continue
                suffix = Path(name).suffix.lower()
                if suffix not in (TEXT_EXTS | DOC_EXTS):
                    continue
                if info.file_size > MAX_ZIP_TOTAL_BYTES:
                    continue
                try:
                    data = archive.read(info)
                except (zipfile.BadZipFile, KeyError, RuntimeError):
                    continue
                if len(data) > MAX_ZIP_TOTAL_BYTES:
                    continue
                text = _extract_bytes_depth(data, suffix, depth + 1)
                if not text:
                    continue
                blocks.append("【压缩包内文件：%s】\n%s" % (Path(name).name, text))
                produced += len(text)
                if produced >= budget:
                    break
            if len(infos) > MAX_ZIP_ENTRIES:
                blocks.append("……（压缩包共 %d 个文件，仅读前 %d 个）" % (len(infos), MAX_ZIP_ENTRIES))
    except (zipfile.BadZipFile, OSError) as error:
        LOGGER.warning("附件解析失败 %s：%s", path, error)
        return ""
    return "\n\n".join(blocks).strip()


def _dispatch(path: Path, suffix: str, depth: int) -> str:
    if suffix in TEXT_EXTS:
        return decode_text_bytes(path.read_bytes())
    if suffix == ".docx":
        return extract_docx(path)
    if suffix == ".pptx":
        return extract_pptx(path)
    if suffix == ".pdf":
        return extract_pdf(path)
    if suffix in (".xlsx", ".xlsm"):
        return extract_xlsx(path)
    if suffix == ".zip":
        return extract_zip(path, depth)
    return ""


def _extract_bytes_depth(data: bytes, suffix: str, depth: int = 0) -> str:
    handle = tempfile.NamedTemporaryFile(prefix="noticehub-attachment-", suffix=suffix, delete=False)
    target = Path(handle.name)
    try:
        handle.write(data)
        handle.close()
        return _dispatch(target, suffix, depth)
    finally:
        if not handle.closed:
            handle.close()
        try:
            target.unlink()
        except OSError:
            pass


def extract_text(path: Path, name: str = "") -> str:
    path = Path(path)
    suffix = (Path(name).suffix if name else "") or path.suffix
    suffix = suffix.lower()
    if suffix not in SUPPORTED_EXTS:
        return ""
    try:
        return (_dispatch(path, suffix, 0) or "").strip()
    except Exception as error:  # noqa: BLE001 - 解析永不向调用方抛异常
        LOGGER.warning("附件解析失败 %s：%s", path, error)
        return ""


def extract_bytes(data: bytes, suffix: str) -> str:
    suffix = (suffix or "").lower()
    if suffix and not suffix.startswith("."):
        suffix = "." + suffix
    if suffix not in SUPPORTED_EXTS:
        return ""
    try:
        return (_extract_bytes_depth(data, suffix, 0) or "").strip()
    except Exception as error:  # noqa: BLE001
        LOGGER.warning("附件解析失败 %s：%s", suffix, error)
        return ""


# ------------------------------------------------------------------ 下载
@dataclass
class _Target:
    scheme: str
    host: str
    port: int
    path: str
    query: str
    ip: str
    host_header: str

    @property
    def request_path(self) -> str:
        return self.path + (("?" + self.query) if self.query else "")


def _default_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in addresses:
            addresses.append(address)
    return addresses


def _blocked_address(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return True  # 解析不出来按「拒」
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    if (
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_unspecified
    ):
        return True
    networks = _BLOCKED_NETWORKS_V6 if parsed.version == 6 else _BLOCKED_NETWORKS_V4
    return any(parsed in network for network in networks)


def _prepare_target(url: str, resolver: Callable[[str, int], list[str]]) -> _Target:
    parts = urllib.parse.urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_DOWNLOAD_SCHEMES:
        raise AttachmentError("不支持的下载协议：%s" % (scheme or "(空)"))
    if parts.username is not None or parts.password is not None:
        raise AttachmentError("下载地址不允许包含用户名/密码")
    try:
        host = parts.hostname or ""
        port = parts.port
    except ValueError as error:
        raise AttachmentError("下载地址端口无效：%s" % error) from error
    if not host:
        raise AttachmentError("下载地址缺少主机名")
    port = port or (443 if scheme == "https" else 80)

    lowered = host.lower().rstrip(".")
    if lowered == "localhost" or lowered.endswith(".localhost"):
        raise AttachmentError("拒绝访问本机地址：%s" % host)

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        try:
            addresses = [str(literal)]
        except ValueError:
            raise AttachmentError("下载地址未解析出 IP：%s" % host) from None
    else:
        try:
            addresses = list(resolver(host, port))
        except (socket.gaierror, OSError, UnicodeError) as error:
            raise AttachmentError("无法解析下载地址：%s" % error) from error
        if not addresses:
            raise AttachmentError("下载地址未解析出 IP：%s" % host)

    for address in addresses:
        if _blocked_address(address):
            raise AttachmentError("拒绝访问本机/内网地址：%s -> %s" % (host, address))

    default_port = 443 if scheme == "https" else 80
    host_header = lowered if port == default_port else "%s:%d" % (lowered, port)
    return _Target(
        scheme=scheme,
        host=lowered,
        port=int(port),
        path=parts.path or "/",
        query=parts.query,
        ip=addresses[0],
        host_header=host_header,
    )


def _http_transport(target: _Target, timeout: int) -> Any:
    import http.client  # noqa: PLC0415 - 只在真正下载时需要

    connection = http.client.HTTPConnection(target.ip, target.port, timeout=timeout)
    connection.request(
        "GET",
        target.request_path,
        headers={
            "User-Agent": USER_AGENT,
            "Host": target.host_header,
            "Accept-Encoding": "identity",
            "Connection": "close",
        },
    )
    return connection.getresponse()


def _https_transport(target: _Target, timeout: int) -> Any:
    import http.client  # noqa: PLC0415

    context = ssl.create_default_context()
    raw = socket.create_connection((target.ip, target.port), timeout=timeout)
    try:
        tls = context.wrap_socket(raw, server_hostname=target.host)
    except Exception:
        raw.close()
        raise
    connection = http.client.HTTPSConnection.__new__(http.client.HTTPSConnection)
    connection.sock = tls
    response = http.client.HTTPResponse(tls)
    try:
        response.begin()
    except Exception:
        tls.close()
        raise
    return response


def _default_transport(target: _Target, timeout: int) -> Any:
    if target.scheme == "https":
        return _https_transport(target, timeout)
    return _http_transport(target, timeout)


def sanitize_name(name: str, fallback: str = "attachment") -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\r\n\t]", "_", str(name or ""))
    cleaned = cleaned.strip().strip(".")
    if not cleaned:
        return fallback
    if len(cleaned) > 60:
        stem = Path(cleaned).stem
        suffix = Path(cleaned).suffix[:4]
        keep = max(0, 55 - len(suffix))
        cleaned = (stem[:keep] + suffix) or fallback
    return cleaned


def _read_into(response: Any, dest: Path, max_bytes: int) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = Path("%s.part" % dest)
    total = 0
    try:
        with part.open("wb") as handle:
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > int(max_bytes):
                    raise AttachmentError("文件超过下载上限")
                handle.write(chunk)
        if total <= 0:
            raise AttachmentError("下载内容为空")
        os.replace(part, dest)
    except BaseException:
        try:
            if part.exists():
                part.unlink()
        except OSError:
            pass
        raise
    return dest


def download(
    url: str,
    dest: Path,
    max_bytes: int,
    timeout: int = 30,
    *,
    _resolver: Callable[[str, int], list[str]] | None = None,
    _transport: Callable[[_Target, int], Any] | None = None,
) -> Path:
    resolver = _resolver or _default_resolver
    transport = _transport or _default_transport
    dest = Path(dest)
    current = str(url)
    budget = max(1, int(timeout))

    for hop in range(MAX_DOWNLOAD_REDIRECTS + 1):
        target = _prepare_target(current, resolver)
        try:
            response = transport(target, budget)
        except AttachmentError:
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
            raise AttachmentError("下载失败：%s" % error) from error
        try:
            status = int(getattr(response, "status", 0) or 0)
            if status in REDIRECT_STATUSES:
                location = response.getheader("Location")
                if not location:
                    raise AttachmentError("重定向缺少 Location")
                if hop >= MAX_DOWNLOAD_REDIRECTS:
                    raise AttachmentError("重定向次数过多")
                current = urllib.parse.urljoin(current, location)
                continue
            if not (200 <= status < 300):
                raise AttachmentError("下载失败：HTTP %d" % status)
            declared = response.getheader("Content-Length")
            if declared:
                try:
                    declared_bytes = int(declared)
                except (TypeError, ValueError):
                    declared_bytes = 0
                if declared_bytes > int(max_bytes):
                    raise AttachmentError("文件超过上限（%d 字节）" % declared_bytes)
            return _read_into(response, dest, max_bytes)
        finally:
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass
    raise AttachmentError("重定向次数过多")


# ---------------------------------------------------------------- 模型调用
def chat_completion(
    settings: Any,
    messages: list[dict],
    *,
    model: str = "",
    timeout: int = 60,
    max_tokens: int = 600,
) -> str:
    key = str(getattr(settings, "dashscope_api_key", "") or "")
    if not key:
        raise AttachmentError("未配置 DASHSCOPE_API_KEY")
    name = str(model or getattr(settings, "dashscope_model", "") or "")
    endpoint = str(getattr(settings, "dashscope_endpoint", "") or "")
    payload: dict[str, Any] = {
        "model": name,
        "temperature": 0.1,
        "max_tokens": int(max_tokens),
        "messages": messages,
    }
    if name.lower().startswith("qwen3"):
        payload["enable_thinking"] = False
    budget = max(15, int(timeout))

    def _attempt() -> str:
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": "Bearer %s" % key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=budget) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:300]
            raise LLMRequestError(
                "大模型 HTTP %s: %s" % (error.code, detail), status=int(error.code)
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise LLMRequestError("大模型请求失败：%s" % error) from error
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as error:
            raise LLMResponseError("大模型返回非 JSON：%s" % error) from error
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise LLMResponseError("大模型返回格式异常") from error
        return str(content or "").strip()

    try:
        return call_with_retries(
            _attempt,
            retries=int(getattr(settings, "llm_max_retries", 2) or 0),
            backoff=float(getattr(settings, "llm_retry_backoff", 1.5) or 0.0),
            should_retry=llm_should_retry,
            logger=LOGGER,
            label="视觉/文档模型",
        )
    except (LLMRequestError, LLMResponseError) as error:
        raise AttachmentError(str(error)) from error


def _format_document_reply(reply: str) -> str:
    raw = str(reply or "").strip()
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return raw
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return raw
    if not isinstance(parsed, dict):
        return raw

    lines: list[str] = []
    summary = str(parsed.get("summary") or "").strip()
    if summary:
        lines.append(summary)
    for label, key in (("适用对象", "audience"), ("适用条件", "condition")):
        value = str(parsed.get(key) or "").strip()
        if value:
            lines.append("%s：%s" % (label, value))
    details = parsed.get("details")
    if isinstance(details, str):
        details = [details]
    if isinstance(details, list):
        for entry in details:
            text = str(entry or "").strip()
            if text:
                lines.append("- %s" % text)
    action = str(parsed.get("action") or "").strip()
    if action:
        lines.append("需要行动：%s" % action)
    deadline = str(parsed.get("deadline") or "").strip()
    if deadline:
        lines.append("截止时间：%s" % deadline)
    return "\n".join(lines).strip() or raw


def summarize_document(text: str, settings: Any, name: str) -> str:
    body = str(text or "")
    prompt = DOC_PROMPT.format(name=name or "未知文件", body=body)
    reply = chat_completion(
        settings,
        [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        model=str(getattr(settings, "dashscope_model", "") or ""),
        timeout=int(getattr(settings, "llm_timeout", 60) or 60),
        max_tokens=2400,
    )
    return _format_document_reply(reply)


def _image_media_type(suffix: str) -> tuple[str, str]:
    cleaned = (suffix or "").lower().lstrip(".")
    if cleaned == "jpg":
        cleaned = "jpeg"
    return cleaned, "image/%s" % cleaned


def image_bytes_text(raw: bytes, suffix: str, settings: Any) -> str:
    if not raw:
        return ""
    media, mime = _image_media_type(suffix)
    data_url = "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii"))
    messages = [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": IMAGE_PROMPT},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        },
    ]
    return chat_completion(
        settings,
        messages,
        model=str(getattr(settings, "vision_model", "") or ""),
        timeout=max(15, int(getattr(settings, "http_timeout", 15) or 15) * 5),
        max_tokens=2000,
    )


def image_text(path: Path, settings: Any) -> str:
    raw = Path(path).read_bytes()
    return image_bytes_text(raw, Path(path).suffix, settings)


def is_meaningful_text(text: str) -> bool:
    squeezed = _SUMMARY_STRIP.sub("", str(text or ""))
    if len(squeezed) < 12:
        return False
    return not any(marker in squeezed for marker in NO_TEXT_MARKERS)


def pdf_ocr_text(path: Path, settings: Any) -> str:
    if not getattr(settings, "vision_active", False):
        return ""
    try:
        import pypdfium2  # noqa: PLC0415 - 可选依赖
        from PIL import Image  # noqa: F401,PLC0415 - 只为确认 Pillow 在
    except ImportError as error:
        raise AttachmentError("未安装 pypdfium2/Pillow，无法 OCR 扫描版 PDF") from error

    import io  # noqa: PLC0415

    document = None
    try:
        document = pypdfium2.PdfDocument(str(path))
        total = len(document)
        pages = min(total, max(1, int(getattr(settings, "pdf_ocr_max_pages", 20) or 20)))
        blocks: list[str] = []
        for index in range(pages):
            page = document[index]
            try:
                image = page.render(scale=2).to_pil()
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
            finally:
                try:
                    page.close()
                except Exception:  # noqa: BLE001
                    pass
            text = image_bytes_text(buffer.getvalue(), ".png", settings)
            if is_meaningful_text(text):
                blocks.append("【第%d页】%s" % (index + 1, text.strip()))
    except AttachmentError:
        raise
    except Exception as error:  # noqa: BLE001 - 渲染/识别失败统一包装
        raise AttachmentError("扫描 PDF OCR 失败：%s" % error) from error
    finally:
        if document is not None:
            try:
                document.close()
            except Exception:  # noqa: BLE001
                pass
    return "\n\n".join(blocks).strip()


def cleanup_files(directory: Path, *, days: int) -> int:
    directory = Path(directory)
    if int(days or 0) <= 0 or not directory.is_dir():
        return 0
    cutoff = SystemClock().now().timestamp() - int(days) * 86400
    removed = 0
    for entry in directory.iterdir():
        try:
            if not entry.is_file() or entry.is_symlink():
                continue
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:
            continue
    return removed


# 队列与原图留存住在应用层；对外只在 `noticehub.attachments` 暴露。
from noticehub.application.attachments import (  # noqa: E402
    IMAGE_MAX_BYTES,
    IMAGE_TYPES,
    AttachmentWorker,
    delete_images,
    has_image,
    image_dir,
    image_key,
    image_suffix,
    images_of,
    pick_image,
    save_image,
)

__all__ = [
    "ALLOWED_DOWNLOAD_SCHEMES",
    "ARCHIVE_EXTS",
    "Attachment",
    "AttachmentError",
    "AttachmentStats",
    "AttachmentWorker",
    "DOC_CHUNK_CHARS",
    "DOC_EXTS",
    "DOC_MAX_CHUNKS",
    "DOC_PROMPT",
    "IMAGE_EXTS",
    "IMAGE_MAX_BYTES",
    "IMAGE_PROMPT",
    "IMAGE_TYPES",
    "LEGACY_EXTS",
    "MAX_DOWNLOAD_REDIRECTS",
    "MAX_PDF_PAGES",
    "MAX_RECORD_CHARS",
    "MAX_ZIP_ENTRIES",
    "MAX_ZIP_TOTAL_BYTES",
    "MIN_DOC_TEXT_CHARS",
    "MIN_IMAGE_BYTES",
    "NO_TEXT_MARKERS",
    "SUPPORTED_EXTS",
    "SUMMARY_SYSTEM_PROMPT",
    "TEXT_EXTS",
    "USER_AGENT",
    "attachment_from_notice",
    "attachments_from_history",
    "attachments_from_message",
    "chat_completion",
    "cleanup_files",
    "decode_text_bytes",
    "delete_images",
    "download",
    "extract_bytes",
    "extract_docx",
    "extract_pdf",
    "extract_pptx",
    "extract_text",
    "extract_xlsx",
    "extract_zip",
    "has_image",
    "image_bytes_text",
    "image_dir",
    "image_key",
    "image_suffix",
    "image_text",
    "images_of",
    "is_meaningful_text",
    "pdf_ocr_text",
    "pick_image",
    "sanitize_name",
    "save_image",
    "summarize_document",
]
