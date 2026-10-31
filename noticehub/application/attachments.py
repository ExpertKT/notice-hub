"""附件队列编排与原图留存。下载、解析和视觉识别仍在 noticehub.attachments。"""

from __future__ import annotations

import hashlib
import logging
import os
import queue
import re
import stat
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any, Callable

from noticehub.domain.time import iso
from noticehub.infrastructure.clock import SystemClock

LOGGER = logging.getLogger("noticehub.attachments")


def _parsers():
    import noticehub.attachments as attachments

    return attachments


def _now():
    return SystemClock().now()


class AttachmentWorker:
    """单线程后台队列：下载 → 解析 → 摘要 → 交给 on_record。"""

    def __init__(
        self,
        settings: Any,
        on_record: Callable[[dict[str, Any]], Any],
        *,
        logger: logging.Logger | None = None,
        napcat_call: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.settings = settings
        self.on_record = on_record
        self.logger = logger or LOGGER
        self.napcat_call = napcat_call
        self.queue: queue.Queue[Any] = queue.Queue()
        self.stats = _parsers().AttachmentStats()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._recent: deque[float] = deque(maxlen=256)
        self._lock = threading.Lock()
        self._warned_at = 0.0

    @property
    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.is_alive:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="attachments", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def submit(self, attachment: Any) -> bool:
        if not self.settings.attachments_enabled or attachment is None:
            return False
        with self._lock:
            if attachment.key in self._seen:
                return False
            self._seen[attachment.key] = time.time()
            while len(self._seen) > 500:
                self._seen.popitem(last=False)
        self.queue.put(attachment)
        self.stats.queued += 1
        self.logger.info(
            "收到群附件 group=%s kind=%s name=%s size=%s sender=%s",
            attachment.group_name or attachment.group_id,
            attachment.kind,
            attachment.name,
            attachment.size_text or "?",
            attachment.sender_name or attachment.sender_id or "?",
        )
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                attachment = self.queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self.process(attachment)
            except Exception as error:  # noqa: BLE001 - 单条失败不能拖垮线程
                self.stats.failed += 1
                self.stats.last_error = str(error)[:200]
                self.logger.exception("附件处理失败：%s", attachment.name)
            finally:
                self.stats.last_at = iso(_now())
                self.queue.task_done()

    def _rate_limited(self) -> bool:
        limit = max(1, int(self.settings.attachment_max_per_hour))
        now = time.time()
        with self._lock:
            while self._recent and now - self._recent[0] > 3600:
                self._recent.popleft()
            if len(self._recent) >= limit:
                if now - self._warned_at > 1800:
                    self._warned_at = now
                    self.logger.warning("附件处理已达每小时上限 %d，暂停解析新附件。", limit)
                return True
            self._recent.append(now)
        return False

    def process(self, attachment: Any) -> dict[str, Any] | None:
        if self._rate_limited():
            self.stats.skipped += 1
            return None

        max_bytes = max(1, int(self.settings.attachment_max_mb)) * 1024 * 1024
        if attachment.kind == "file" and attachment.size and attachment.size > max_bytes:
            self.stats.skipped += 1
            self.logger.info("附件超过 %dMB，仅记录名字：%s", self.settings.attachment_max_mb, attachment.name)
            return self._emit(attachment, body="", reason="too_large")

        path = self._fetch(attachment, max_bytes)
        if path is None:
            self.stats.skipped += 1
            return None
        if attachment.kind == "image":
            kept = self._keep_image(attachment, path)
        else:
            kept = None
        record = None
        try:
            record = self.process_local(attachment, path)
            return record
        finally:
            if attachment.kind == "image" and (kept is not None or record is None):
                # 原图已留好就删掉下载临时件：它在 files/ 里同样匹配 <key>_*，
                # 留着会让读侧「附件事件必须唯一」的判定变成歧义，也等于留下原始截图。
                self._discard(path)

    def _note_vision_unavailable(self) -> None:
        """读图不可用时只提示一次，免得每张图片刷一行日志。"""
        if getattr(self, "_vision_skip_logged", False):
            return
        self._vision_skip_logged = True
        self.logger.info(
            "当前 LLM 端点（%s）没有可用的视觉模型，图片只记录不识别；需要读图请设置 QQ_DIGEST_VL_MODEL 或换回 DashScope 端点。",
            self.settings.endpoint_host or "未知",
        )

    def process_local(self, attachment: Any, path: Path) -> dict[str, Any] | None:
        """解析已经在本地的附件，生成待入库的消息记录。"""
        parsers = _parsers()
        if attachment.kind == "image":
            if not self.settings.vision_active:
                self._note_vision_unavailable()
                self.stats.skipped += 1
                return None
            text = parsers.image_text(path, self.settings)
            if not parsers.is_meaningful_text(text):
                self.stats.skipped += 1
                self.logger.info("图片没有可读通知内容，已忽略：%s", attachment.name)
                return None
            body = f"【图片】\n{text}"
            return self._emit(attachment, body=body, source_text=text)

        text = parsers.extract_text(path, attachment.name)
        import re

        plain_text = re.sub(r"\s+", "", text)
        if attachment.suffix == ".pdf" and len(plain_text) < parsers.MIN_DOC_TEXT_CHARS:
            try:
                ocr_text = parsers.pdf_ocr_text(path, self.settings)
            except parsers.AttachmentError as error:
                self.logger.info("扫描 PDF OCR 失败，回退文本层：%s（%s）", attachment.name, error)
            else:
                if ocr_text.strip():
                    text = ocr_text
                    self.logger.info("扫描 PDF OCR 完成：%s", attachment.name)
        if not text.strip():
            self.stats.skipped += 1
            self.logger.info("附件没有可提取文本（可能是扫描件或缺依赖）：%s", attachment.name)
            return self._emit(attachment, body="", reason="no_text")

        source_text = text[: max(1000, int(self.settings.document_max_chars))]
        if len(text) > len(source_text):
            source_text += "\n\n……（文件较长，仅保留并分析前 %d 字）" % len(source_text)
        try:
            summary = parsers.summarize_document(source_text, self.settings, attachment.name)
        except parsers.AttachmentError as error:
            self.logger.info("附件结构化提取失败，改用正文截断：%s（%s）", attachment.name, error)
            summary = source_text[:400]
        body = f"【文件】{attachment.name}\n{summary}".strip()
        return self._emit(attachment, body=body, source_text=source_text)

    def _emit(
        self,
        attachment: Any,
        *,
        body: str,
        reason: str = "",
        source_text: str = "",
    ) -> dict[str, Any] | None:
        size = attachment.size_text
        if not body:
            note = "超过大小上限，未解析" if reason == "too_large" else "未能解析出文字"
            body = f"【文件】{attachment.name}（{size or '大小未知'}，{note}）"
        content = body[: _parsers().MAX_RECORD_CHARS]
        record = {
            "msg_id": f"onebot:{attachment.group_id}:attach:{attachment.key}",
            "source": "onebot-attachment",
            "event": "GROUP_ATTACHMENT",
            "group_id": attachment.group_id,
            "group_name": attachment.group_name,
            "sender_id": attachment.sender_id,
            "sender_name": attachment.sender_name,
            "ts": attachment.ts or iso(_now()),
            "received_at": iso(_now()),
            "content": content,
            "source_text": source_text or "",
        }
        self.stats.processed += 1
        self.stats.counters[f"processed_{attachment.kind}"] = (
            self.stats.counters.get(f"processed_{attachment.kind}", 0) + 1
        )
        self.logger.info(
            "附件解析完成 group=%s kind=%s name=%s 摘要=%s",
            attachment.group_name or attachment.group_id,
            attachment.kind,
            attachment.name,
            content.replace("\n", " ")[:80],
        )
        try:
            self.on_record(record)
        except Exception:  # noqa: BLE001
            self.logger.exception("附件记录入库失败：%s", attachment.name)
        return record

    def _keep_image(self, attachment: Any, path: Path) -> Path | None:
        """下载到的图片先留一份原图：没开视觉模型时，收件箱也要能看到图。

        写盘前缀必须是**读的时候算出来的前缀**：附件记录的 msg_id 是
        ``onebot:<群>:attach:<key>``，读侧按 ``<key>_*`` 找，所以这里用 ``key``。
        以前按 ``image_key(消息行 id)`` 写，两边不同源，附件记录的图永远 404。
        """
        saved = save_image(
            self.settings.data_dir, attachment, path, logger=self.logger, prefix=attachment.key
        )
        if saved is None:
            return None
        # 普通消息行（msg_id 是 onebot:<群>:<消息id>）仍按 sha1(msg_id)[:16] 找自己的图。
        # 硬链接同一个 inode：两个前缀都在，磁盘上只占一份。序号按消息内已用的最大值往后排，
        # 同一条消息的第二张图才会得到 _1 而不是覆盖 _0。
        if attachment.msg_id and ":upload:" not in attachment.msg_id:
            base = image_key(attachment.msg_id)
            for index in range(20):
                alias = saved.with_name(f"{base}_{index}{saved.suffix}")
                if alias.exists():
                    continue
                try:
                    os.link(saved, alias)
                except OSError:
                    pass
                break
        return saved

    def _discard(self, path: Path) -> None:
        """图片读完即删，不在本地留下原始截图。"""
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            self.logger.warning("图片临时文件删除失败 %s：%s", path.name, error)

    def _fetch(self, attachment: Any, max_bytes: int) -> Path | None:
        parsers = _parsers()
        target_dir = self.settings.data_dir / "files"
        dest = target_dir / f"{attachment.key}_{parsers.sanitize_name(attachment.name)}"
        if dest.is_file() and dest.stat().st_size > 0:
            return dest
        url = attachment.url
        if not url and self.napcat_call and attachment.file_id:
            payload: dict[str, Any] = {"group_id": attachment.group_id, "file_id": attachment.file_id}
            if attachment.busid:
                try:
                    payload["busid"] = int(attachment.busid)
                except ValueError:
                    payload["busid"] = attachment.busid
            try:
                result = self.napcat_call("get_group_file_url", payload)
                url = str((result.get("data") or {}).get("url") or "").strip()
            except Exception as error:  # noqa: BLE001
                self.logger.warning("取附件下载地址失败 %s：%s", attachment.name, error)
        if not url:
            self.logger.info("附件没有可下载地址，跳过：%s", attachment.name)
            return None
        try:
            return parsers.download(url, dest, max_bytes, timeout=max(20, self.settings.http_timeout * 2))
        except parsers.AttachmentError as error:
            self.logger.warning("附件下载失败 %s：%s", attachment.name, error)
            return None

    def describe(self) -> dict[str, Any]:
        return self.stats.as_dict()


# ------------------------------------------------------- 原图留存（收件箱直显）
# 只存图片本体，供 /api/inbox/image 直接回原图；命名由 msg_id 推导，不建索引文件。
IMAGE_MAX_BYTES = 8 * 1024 * 1024
IMAGE_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)


def image_dir(data_dir: Path) -> Path:
    """图片落盘目录，与 digest.sqlite3 同级的 files/。"""
    return Path(data_dir) / "files"


def image_key(msg_id: str) -> str:
    """msg_id → 文件名前缀（16 位十六进制）。只用十六进制，天然挡住路径字符。"""
    return hashlib.sha1(str(msg_id or "").encode("utf-8")).hexdigest()[:16]


def image_suffix(attachment: Any, head: bytes) -> str:
    """按文件头定后缀，认不出来再用附件名后缀——借口要对得上实际字节，否则 Content-Type 会谎报。"""
    for magic, ext in _IMAGE_MAGIC:
        if head.startswith(magic):
            return ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    suffix = attachment.suffix
    return suffix if suffix in IMAGE_TYPES else ""


def save_image(
    data_dir: Path,
    attachment: Any,
    path: Path,
    *,
    logger: logging.Logger | None = None,
    prefix: str | None = None,
) -> Path | None:
    """把下载好的图片留一份到 files/<prefix>_<n><ext>；失败只记日志，绝不抛。

    ``prefix`` 缺省按 ``image_key(msg_id)`` 推。调用方知道读侧按什么前缀找，
    就传什么——写盘前缀必须等于读盘前缀，否则收件箱那张图永远 404。
    """
    log = logger or LOGGER
    msg_id = str(attachment.msg_id or "")
    if not prefix and not msg_id:
        return None
    file_prefix = prefix or image_key(msg_id)
    try:
        size = path.stat().st_size
        if size <= 0 or size > IMAGE_MAX_BYTES:
            log.warning("图片超过 %dMB，未留原图：%s", IMAGE_MAX_BYTES // (1024 * 1024), attachment.name)
            return None
        with path.open("rb") as handle:
            head = handle.read(16)
        suffix = image_suffix(attachment, head)
        if not suffix:
            log.warning("图片格式不认识，未留原图：%s", attachment.name)
            return None
        raw = path.read_bytes()
        target = image_dir(data_dir)
        target.mkdir(parents=True, exist_ok=True)
        for index in range(20):
            dest = target / f"{file_prefix}_{index}{suffix}"
            try:
                with dest.open("xb") as handle:
                    handle.write(raw)
            except FileExistsError:
                continue
            return dest
        log.warning("同一消息的图片已满 20 张，未留原图：%s", attachment.name)
    except OSError as error:
        log.warning("图片留原图失败 %s：%s", attachment.name, error)
    return None


def images_of(data_dir: Path, msg_id: str) -> list[Path]:
    """该消息已留存的图片（按文件名排序，n 升序）。"""
    directory = image_dir(data_dir)
    if not directory.is_dir():
        return []
    return sorted(directory.glob(f"{image_key(msg_id)}_*"))


_IMAGE_UNINDEXED = 1 << 30


def _image_index(name: str, prefix: str) -> int:
    """文件名 ``<prefix>_<n><ext>`` 里的 n；认不出序号就排到最后。"""
    tail = name[len(prefix) + 1:] if name.startswith(prefix + "_") else name
    digits = re.match(r"\d+", tail)
    return int(digits.group(0)) if digits else _IMAGE_UNINDEXED


def _image_candidates(data_dir: Path, prefix: str) -> list[Path]:
    """``files/<prefix>_<n><ext>`` 里的常规图片，按序号升序（``_0`` 最前）。

    只认普通文件（不跟符号链接）和已知图片后缀——这两条正是
    ``GET /api/inbox/image`` 能回 200 的条件，判定只写在这里。
    """
    directory = image_dir(data_dir)
    if not directory.is_dir():
        return []
    found: list[Path] = []
    for candidate in directory.glob(f"{prefix}_*"):
        if candidate.suffix.lower() not in IMAGE_TYPES:
            continue
        try:
            if not stat.S_ISREG(candidate.lstat().st_mode):
                continue
        except OSError:
            continue
        found.append(candidate)
    found.sort(key=lambda path: (_image_index(path.name, prefix), path.name))
    return found


def pick_image(
    data_dir: Path,
    msg_id: str,
    *,
    prefix: str | None = None,
    unique: bool = False,
) -> Path | None:
    """收件箱原图：返回要显示的那一张，没有就 None。

    唯一判定——``has_image()`` 与 ``GET /api/inbox/image`` 都走这里，
    免得「列表标了有图、点开却是 404」。

    - 普通消息 / 历史回溯：``files/<image_key(msg_id)>_<n><ext>``。同一条消息
      多张图取序号最小的（``_0``）：它们都是这条消息的图，不是歧义。
    - 群附件事件（``prefix=<attach key>`` 且 ``unique=True``）：key 是事件级的，
      同一 key 下出现多张才按异常处理（返回 None）。
    """
    candidates = _image_candidates(data_dir, prefix or image_key(msg_id))
    if unique:
        return candidates[0] if len(candidates) == 1 else None
    return candidates[0] if candidates else None


def has_image(data_dir: Path, msg_id: str) -> bool:
    """消息 JSON 用：这条消息有没有可显示的原图（与 ``GET /api/inbox/image`` 同一判定）。"""
    if not msg_id:
        return False
    return pick_image(data_dir, msg_id) is not None


def delete_images(data_dir: Path, msg_ids: Any) -> int:
    """删收件箱条目时同步删原图，避免 files/ 里留孤儿。"""
    removed = 0
    for msg_id in msg_ids or ():
        for path in images_of(data_dir, str(msg_id or "")):
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
    return removed
