"""附件队列编排。下载、解析和视觉识别仍在 qq_live_digest.attachments。"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any, Callable

from noticehub.domain.time import iso
from noticehub.infrastructure.clock import SystemClock

LOGGER = logging.getLogger("qq_live_digest.attachments")


def _parsers():
    import qq_live_digest.attachments as attachments

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
        record = None
        try:
            record = self.process_local(attachment, path)
            return record
        finally:
            if attachment.kind == "image" and record is None:
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
