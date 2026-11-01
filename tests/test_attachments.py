"""附件链路的行为契约：解析、提取、安全下载、后台处理、原图留存。

用例名是用来定位失败的坐标，断言里的期望值是契约；这里改的只是构造方式、
桩的形态和行文顺序。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from noticehub import attachments as att  # noqa: E402
from noticehub.config import Settings  # noqa: E402
from noticehub.receiver import OneBotReceiver  # noqa: E402
from noticehub.summarizer import filter_recent_duplicates  # noqa: E402
from qq_digest import Message, analyze_message, dedupe_items, similar_text  # noqa: E402

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PNG_BYTES = PNG_MAGIC + b"\x00" * 64


def docx_xml(*paragraphs: str) -> str:
    """拼一份最小的 WordprocessingML；段数由调用方给。"""
    body = "".join(
        "<w:p><w:r><w:t>%s</w:t></w:r></w:p>" % text for text in paragraphs
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>%s</w:body></w:document>" % body
    )


def part(kind: str, **data) -> dict:
    """OneBot 消息段：类型 + data 两个键。"""
    return {"type": kind, "data": data}


def group_message(message_id: int = 2048, segments: list[dict] | None = None, **changed) -> dict:
    """一条群消息信封；默认带「一段文字 + 一个文件」。"""
    payload = dict(
        post_type="message",
        message_type="group",
        message_id=message_id,
        group_id=123456,
        user_id=999,
        self_id=111,
        time=1789000000,
        sender={"card": "学习委员", "nickname": "学委"},
        message=segments
        if segments is not None
        else [
            part("text", text="这是今天要交的表格："),
            part(
                "file",
                file="附件2：报名表.xlsx",
                file_id="/abc-def",
                file_size="17306",
                url="https://example.com/table.xlsx",
            ),
        ],
    )
    payload.update(changed)
    return payload


def upload_notice(**changed) -> dict:
    """群文件上传通知：只有通知、没有消息体。"""
    payload = dict(
        post_type="notice",
        notice_type="group_upload",
        group_id=123456,
        user_id=999,
        time=1789000000,
        file={"id": "file-1", "name": "通知.pdf", "size": 204800, "busid": 102},
    )
    payload.update(changed)
    return payload


def settings_for(root: Path, **overrides) -> Settings:
    """按「装机时已经填了 DashScope key」来构造；真实网络调用全被桩替换。"""
    base = dict(
        group_whitelist=("123456",),
        data_dir=root / "data",
        log_dir=root / "logs",
        dashscope_api_key="test-key",
    )
    base.update(overrides)
    return Settings(**base)


def image_only_message(message_id: int, file: str = "shot.jpg", size: str = "180000") -> dict:
    return group_message(
        message_id,
        segments=[part("image", file=file, file_size=size, url="https://x/" + file)],
    )


class TempDirCase(unittest.TestCase):
    """每条用例独占一个临时目录，收尾自动清理。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)


class AttachmentParseTest(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = settings_for(self.root)

    def test_file_segment_becomes_attachment(self) -> None:
        found = att.attachments_from_message(group_message(), self.settings)
        self.assertEqual(1, len(found))
        item = found[0]
        self.assertEqual("file", item.kind)
        self.assertEqual("附件2：报名表.xlsx", item.name)
        self.assertEqual(17306, item.size)
        self.assertEqual("https://example.com/table.xlsx", item.url)
        self.assertEqual("onebot:123456:2048", item.msg_id)

    def test_upload_notice_becomes_attachment(self) -> None:
        item = att.attachment_from_notice(upload_notice(), self.settings)
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual("通知.pdf", item.name)
        self.assertEqual("file-1", item.file_id)
        self.assertEqual("102", item.busid)
        self.assertEqual(204800, item.size)

    def test_pdf_ocr_renders_pages_without_pymupdf(self) -> None:
        import io

        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(144, 144)
        buffer = io.BytesIO()
        writer.write(buffer)
        pdf_path = self.root / "scan.pdf"
        pdf_path.write_bytes(buffer.getvalue())

        seen: list[bytes] = []
        stub = lambda raw, suffix, settings: seen.append(raw) or "扫描件测试：请于 10 月 9 日前提交材料。"
        with mock.patch.object(att, "image_bytes_text", side_effect=stub):
            text = att.pdf_ocr_text(pdf_path, self.settings)

        # 送进模型的是渲染后的位图，不是 PDF 原字节。
        self.assertIn("【第1页】", text)
        self.assertTrue(seen and seen[0].startswith(PNG_MAGIC))

    def test_sticker_and_tiny_images_are_skipped(self) -> None:
        payload = group_message(
            message_id=1,
            segments=[
                part("image", file="a.jpg", file_size="40000", sub_type=1, url="https://x/a"),
                part("image", file="b.jpg", file_size="2048", url="https://x/b"),
                part("image", file="c.jpg", file_size="120000", url="https://x/c", summary="[好的]"),
            ],
        )
        self.assertEqual([], att.attachments_from_message(payload, self.settings))

    def test_normal_image_is_kept(self) -> None:
        found = att.attachments_from_message(image_only_message(7), self.settings)
        self.assertEqual(1, len(found))
        self.assertEqual("image", found[0].kind)

    def test_other_groups_are_ignored(self) -> None:
        payload = group_message(group_id=99999)
        self.assertEqual([], att.attachments_from_message(payload, self.settings))

    def test_same_file_via_notice_and_message_shares_key(self) -> None:
        notice = att.attachment_from_notice(
            upload_notice(file={"id": "abc", "name": "名单.xlsx", "size": 17306, "busid": 102}),
            self.settings,
        )
        self.assertIsNotNone(notice)
        assert notice is not None
        message_payload = group_message(
            segments=[
                part(
                    "file",
                    file="名单.xlsx",
                    file_id="/e5caf312-other",
                    file_size="17306",
                    url="https://example.com/list.xlsx",
                )
            ]
        )
        from_message = att.attachments_from_message(message_payload, self.settings)[0]

        self.assertEqual(from_message.key, notice.key)


class ExtractTest(TempDirCase):
    def test_text_file(self) -> None:
        note = self.root / "a.txt"
        note.write_text("明天上午九点在教学楼 301 开班会", encoding="utf-8")
        self.assertIn("开班会", att.extract_text(note))

    def test_docx(self) -> None:
        docx_path = self.root / "plan.docx"
        xml = docx_xml("培养方案说明：本学期共 12 门课程。", "请于 9 月 30 日前完成选课确认。")
        with zipfile.ZipFile(docx_path, "w") as archive:
            archive.writestr("word/document.xml", xml)

        body = att.extract_text(docx_path)
        self.assertIn("培养方案说明", body)
        self.assertIn("选课确认", body)

    def test_xlsx(self) -> None:
        import openpyxl

        sheet_path = self.root / "list.xlsx"
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.append(["姓名", "金额"])
        sheet.append(["张三", 2000])
        book.save(sheet_path)

        dump = att.extract_text(sheet_path)
        self.assertIn("张三", dump)
        self.assertIn("金额", dump)

    def test_zip_skips_traversal_and_unknown(self) -> None:
        pack = self.root / "pack.zip"
        entries = {
            "../evil.txt": "不该被读取",
            "notes.txt": "助学金公示名单",
            "photo.jpg": "binary",
        }
        with zipfile.ZipFile(pack, "w") as archive:
            for name, body in entries.items():
                archive.writestr(name, body)

        listed = att.extract_zip(pack)
        self.assertIn("助学金公示名单", listed)
        self.assertNotIn("不该被读取", listed)

    def test_legacy_doc_is_unsupported(self) -> None:
        path = self.root / "old.doc"
        path.write_bytes(b"\xd0\xcf\x11\xe0")
        self.assertEqual("", att.extract_text(path))

    def test_sanitize_name(self) -> None:
        self.assertEqual("_.._etc_通知.pdf", att.sanitize_name("../../etc/通知.pdf"))
        self.assertEqual("attachment", att.sanitize_name("   "))


class WorkerCase(TempDirCase):
    """处理链路的公共底座：配置、记录桶、下载桩。"""

    def setUp(self) -> None:
        super().setUp()
        self.settings = settings_for(self.root)
        self.records: list[dict] = []

    def worker(self) -> att.AttachmentWorker:
        return att.AttachmentWorker(self.settings, self.records.append)

    def model_reply(self, payload: str):
        """把文本模型固定成某个回包。"""
        return mock.patch.object(att, "chat_completion", return_value=payload)

    def download_stub(self, write, seen: list[Path] | None = None):
        """返回 (patcher, 落盘记录)：write(dest) 负责把内容写进去。"""
        written = seen if seen is not None else []

        def stub(url, dest, max_bytes, timeout=30):
            dest.parent.mkdir(parents=True, exist_ok=True)
            write(dest)
            written.append(dest)
            return dest

        return mock.patch.object(att, "download", side_effect=stub), written


def file_attachment(name: str = "报名表.xlsx", size: int = 20480, **overrides) -> att.Attachment:
    base = dict(
        kind="file",
        name=name,
        group_id="123456",
        group_name="测仪2602班群",
        sender_name="学委",
        url="https://example.com/table.xlsx",
        size=size,
    )
    base.update(overrides)
    return att.Attachment(**base)


def image_attachment(name: str = "shot.jpg", msg_id: str = "", **overrides) -> att.Attachment:
    base = dict(
        kind="image",
        name=name,
        group_id="123456",
        group_name="电气学院2026级群",
        sender_name="辅导员",
        url="https://example.com/shot.jpg",
        size=180000,
        msg_id=msg_id,
    )
    base.update(overrides)
    return att.Attachment(**base)


class WorkerTest(WorkerCase):
    def test_file_record_uses_ai_summary(self) -> None:
        worker = self.worker()
        attachment = file_attachment(ts="2026-09-29T09:00:00+08:00")

        def write_xlsx(dest: Path) -> None:
            from openpyxl import Workbook

            workbook = Workbook()
            workbook.active.append(["姓名", "金额"])
            workbook.active.append(["张三", 2000])
            workbook.save(dest)

        patcher, saved = self.download_stub(write_xlsx)
        with patcher, self.model_reply("班委要求今天 18:00 前提交报名表"):
            record = worker.process(attachment)
            self.assertIsNotNone(record)
            assert record is not None

        self.assertIn("【文件】报名表.xlsx", record["content"])
        self.assertIn("报名表", record["content"])
        self.assertEqual("测仪2602班群", record["group_name"])
        self.assertEqual([record], self.records)
        self.assertEqual(1, worker.stats.processed)
        self.assertTrue(saved and saved[0].is_file())
        self.assertIn("张三", record["source_text"])
        self.assertIn("2000", record["source_text"])

    def test_file_keeps_structured_fields_and_full_source(self) -> None:
        attachment = file_attachment(
            "转专业通知.txt", 2400, group_name="电气学院群", sender_name="辅导员"
        )
        long_body = "刚转专业到学院的同学需要体测，其他同学也要核查。" + "细节" * 900
        structured = json.dumps(
            dict(
                summary="两类同学都要核查体测成绩",
                audience="刚转专业同学；其他同学",
                condition="没有2026年体测成绩",
                details=["刚转专业同学先查看体测系统", "其他同学也要核查成绩"],
                action="预约并参加体测",
                deadline="2026-11-21 23:59",
            ),
            ensure_ascii=False,
        )

        patcher, _ = self.download_stub(lambda dest: dest.write_text(long_body, encoding="utf-8"))
        with patcher, self.model_reply(structured):
            record = self.worker().process(attachment)
            self.assertIsNotNone(record)
            assert record is not None

        rendered, original = record["content"], record["source_text"]
        self.assertIn("适用对象：刚转专业同学；其他同学", rendered)
        self.assertIn("适用条件：没有2026年体测成绩", rendered)
        self.assertIn("截止时间：2026-11-21 23:59", rendered)
        self.assertGreater(len(original), len(rendered))
        self.assertIn("刚转专业到学院的同学", original)

    def test_scanned_pdf_uses_vision_ocr(self) -> None:
        attachment = file_attachment(
            "扫描通知.pdf", 2048, group_name="电气学院群", sender_name="辅导员"
        )
        patcher, _ = self.download_stub(lambda dest: dest.write_bytes(b"%PDF-1.4"))

        flat_text = mock.patch.object(att, "extract_text", return_value="")
        ocr_text = mock.patch.object(
            att, "pdf_ocr_text", return_value="扫描件要求：转专业同学核查体测成绩。"
        )
        summarized = mock.patch.object(
            att, "summarize_document", return_value="适用对象：转专业同学\n截止时间：11月21日"
        )
        with patcher, flat_text, ocr_text, summarized:
            record = self.worker().process(attachment)
            self.assertIsNotNone(record)
            assert record is not None

        self.assertIn("扫描件要求", record["source_text"])
        self.assertIn("截止时间：11月21日", record["content"])

    def test_image_record_uses_vision_text(self) -> None:
        attachment = image_attachment(group_name="电气学院2026级群")
        patcher, saved = self.download_stub(lambda dest: dest.write_bytes(b"\xff\xd8\xff"))

        with patcher, mock.patch.object(
            att, "image_text", return_value="关于国家助学金公示，请相关同学 9 月 30 日 17:00 前交材料"
        ):
            record = self.worker().process(attachment)
            self.assertIsNotNone(record)
            assert record is not None

        self.assertIn("【图片】", record["content"])
        self.assertIn("助学金", record["content"])
        # 留存件与临时件都能匹配 <key>_*，临时件不删会让读侧「必须唯一」变成歧义。
        self.assertFalse(saved[0].exists(), "原图留好后下载临时件不该留在 files/ 里")
        kept = sorted((self.settings.data_dir / "files").glob(f"{attachment.key}_*"))
        self.assertEqual([f"{attachment.key}_0.jpg"], [path.name for path in kept])
        self.assertEqual(b"\xff\xd8\xff", kept[0].read_bytes())

    def test_image_without_text_is_skipped(self) -> None:
        worker = self.worker()
        attachment = image_attachment("meme.jpg", size=90000, group_name="", sender_name="")
        patcher, saved = self.download_stub(lambda dest: dest.write_bytes(b"\xff\xd8\xff"))

        with patcher, mock.patch.object(att, "image_text", return_value="无有效通知"):
            record = worker.process(attachment)

        self.assertIsNone(record)
        self.assertEqual([], self.records)
        self.assertEqual(1, worker.stats.skipped)
        self.assertTrue(saved and not saved[0].exists())

    def test_image_without_vision_discards_download(self) -> None:
        attachment = image_attachment("photo.jpg", group_name="", sender_name="")
        patcher, saved = self.download_stub(lambda dest: dest.write_bytes(b"image"))

        with mock.patch.object(
            Settings, "vision_active", new_callable=mock.PropertyMock, return_value=False
        ), patcher:
            record = self.worker().process(attachment)

        self.assertIsNone(record)
        self.assertEqual([], self.records)
        self.assertTrue(saved and not saved[0].exists())

    def test_image_processing_failure_discards_download(self) -> None:
        attachment = image_attachment("broken.jpg", group_name="", sender_name="")
        patcher, saved = self.download_stub(lambda dest: dest.write_bytes(b"invalid image"))

        with patcher, mock.patch.object(
            att.AttachmentWorker, "process_local", side_effect=RuntimeError("decode failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "decode failed"):
                self.worker().process(attachment)

        self.assertTrue(saved and not saved[0].exists())

    def test_oversized_file_only_records_name(self) -> None:
        record = self.worker().process(file_attachment("大文件.zip", 99 * 1024 * 1024))
        self.assertIsNotNone(record)
        assert record is not None
        self.assertIn("大文件.zip", record["content"])
        self.assertIn("超过大小上限", record["content"])

    def test_submit_dedupes_same_attachment(self) -> None:
        worker = self.worker()
        attachment = file_attachment(
            "a.pdf", 1000, file_id="x", group_name="", sender_name="", url=""
        )
        self.assertTrue(worker.submit(attachment))
        self.assertFalse(worker.submit(attachment))
        self.assertEqual(1, worker.stats.queued)

    def test_disabled_worker_accepts_nothing(self) -> None:
        settings = settings_for(self.root, attachments_enabled=False)
        worker = att.AttachmentWorker(settings, self.records.append)
        self.assertFalse(worker.submit(att.Attachment(kind="file", name="a.pdf")))

    def test_cleanup_files_removes_old(self) -> None:
        import os
        import time

        directory = self.root / "data" / "files"
        directory.mkdir(parents=True, exist_ok=True)
        stale = directory / "old.bin"
        stale.write_bytes(b"x")
        aged = time.time() - 10 * 86400
        os.utime(stale, (aged, aged))

        self.assertEqual(1, att.cleanup_files(directory, days=7))
        self.assertFalse(stale.exists())


class ReceiverDispatchTest(TempDirCase):
    """接收端分发：一条消息可能同时产生「消息记录」和「附件任务」。"""

    def setUp(self) -> None:
        super().setUp()
        self.settings = settings_for(self.root)

    def receiver(self) -> tuple[OneBotReceiver, list[dict], list[att.Attachment]]:
        records: list[dict] = []
        queued: list[att.Attachment] = []
        instance = OneBotReceiver(
            self.settings,
            lambda record: records.append(record) or True,
            lambda item: queued.append(item) or True,
            None,
        )
        return instance, records, queued

    def test_message_with_file_dispatches_both(self) -> None:
        receiver, records, queued = self.receiver()
        status, payload = receiver.handle_payload(group_message())

        self.assertEqual(200, status)
        self.assertTrue(payload["inserted"])
        self.assertEqual(1, payload["attachments"])
        self.assertEqual(1, len(records))
        self.assertEqual("附件2：报名表.xlsx", queued[0].name)

    def test_notice_is_dispatched_even_without_message(self) -> None:
        receiver, records, queued = self.receiver()
        status, payload = receiver.handle_payload(upload_notice())

        self.assertEqual(204, status)
        self.assertEqual(1, payload["attachments"])
        self.assertEqual([], records)
        self.assertEqual("通知.pdf", queued[0].name)

    def test_disabled_attachments_ignored(self) -> None:
        settings = settings_for(self.root, attachments_enabled=False)
        queued: list[att.Attachment] = []
        receiver = OneBotReceiver(settings, lambda record: True, queued.append, None)
        receiver.handle_payload(upload_notice())
        self.assertEqual([], queued)


class CrossGroupDedupeTest(TempDirCase):
    """跨群去重：同一条通知在两个群出现时只留一条。"""

    def setUp(self) -> None:
        super().setUp()
        self.settings = settings_for(self.root, dedupe_hours=6)

    def item(self, text: str, group: str) -> dict:
        analysis = analyze_message(Message(timestamp=None, sender="班长", text=text, source=group))
        analysis["group_name"] = group
        analysis["msg_id"] = f"{group}:{hash(text) & 0xFFFF}"
        return analysis

    def test_same_notice_in_two_groups_merges(self) -> None:
        text = "请国家助学金公示名单中的同学今天17:00前将纸质材料交到学院办公室"
        merged = dedupe_items([self.item(text, "测仪2602班群"), self.item(text, "测仪2602班级闲聊群")])
        self.assertEqual(1, len(merged))
        self.assertEqual({"测仪2602班群", "测仪2602班级闲聊群"}, set(merged[0]["duplicate_groups"]))

    def test_different_notices_are_kept(self) -> None:
        items = [
            self.item("请各班班长今天17:00前提交助学金纸质材料", "测仪2602班群"),
            self.item("明天上午9点在教学楼301进行英语四级模拟考试", "测仪2602班群"),
        ]
        self.assertEqual(2, len(dedupe_items(items)))

    def test_recent_history_suppresses_repeat(self) -> None:
        text = "请国家助学金公示名单中的同学今天17:00前将纸质材料交到学院办公室"
        candidates = [self.item(text, "测仪2602班级闲聊群")]
        history = [
            dict(
                text=text,
                summary="助学金材料",
                group="测仪2602班群",
                ts="2026-09-29T09:00:00+08:00",
            )
        ]
        kept, suppressed = filter_recent_duplicates(candidates, history, self.settings)
        self.assertEqual([], kept)
        self.assertEqual(1, len(suppressed))
        self.assertEqual("测仪2602班群", suppressed[0]["suppressed_duplicate"]["group"])

    def test_history_disabled_by_zero_hours(self) -> None:
        settings = settings_for(self.root, dedupe_hours=0)
        text = "请大家今天17:00前提交材料"
        history = [dict(text=text, group="B群", ts="")]
        kept, suppressed = filter_recent_duplicates([self.item(text, "A群")], history, settings)
        self.assertEqual(1, len(kept))
        self.assertEqual([], suppressed)

    def test_similar_text_helper(self) -> None:
        self.assertIs(True, similar_text("abcdef", "abcdef"))
        self.assertIs(False, similar_text("abcdef", "zzzzzz"))
        self.assertIs(True, similar_text("a" * 30 + "通知", "a" * 30 + "通知（重复）"))


class ConfigTest(unittest.TestCase):
    def test_attachment_defaults(self) -> None:
        settings = Settings.from_env({})
        self.assertTrue(settings.attachments_enabled)
        self.assertEqual(10, settings.attachment_max_mb)
        self.assertTrue(settings.vision_enabled)
        self.assertEqual("qwen3-vl-plus", settings.vision_model)
        self.assertEqual(30000, settings.document_max_chars)
        self.assertEqual(20, settings.pdf_ocr_max_pages)
        self.assertEqual(6, settings.dedupe_hours)

    def test_attachment_env_overrides(self) -> None:
        settings = Settings.from_env(
            dict(
                QQ_DIGEST_ATTACHMENTS="0",
                QQ_DIGEST_ATTACHMENT_MAX_MB="25",
                QQ_DIGEST_VISION="0",
                QQ_DIGEST_VL_MODEL="qwen-vl-max",
                QQ_DIGEST_DOCUMENT_MAX_CHARS="12000",
                QQ_DIGEST_PDF_OCR_MAX_PAGES="8",
                QQ_DIGEST_DEDUPE_HOURS="12",
            )
        )
        self.assertFalse(settings.attachments_enabled)
        self.assertEqual(25, settings.attachment_max_mb)
        self.assertFalse(settings.vision_enabled)
        self.assertEqual("qwen-vl-max", settings.vision_model)
        self.assertEqual(12000, settings.document_max_chars)
        self.assertEqual(8, settings.pdf_ocr_max_pages)
        self.assertEqual(12, settings.dedupe_hours)

        described = settings.describe()
        self.assertIn("attachments", described)
        self.assertEqual(12, described["dedupe_hours"])


class AiPayloadTest(unittest.TestCase):
    def test_qwen3_vision_disables_thinking(self) -> None:
        captured: dict = {}

        reply = mock.MagicMock()
        reply.__enter__.return_value = reply
        reply.read.return_value = b'{"choices":[{"message":{"content":"ok"}}]}'

        def fake_urlopen(request, timeout=0):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return reply

        settings = Settings(dashscope_api_key="key")
        with mock.patch.object(att.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = att.chat_completion(settings, [user_turn()], model="qwen3-vl-plus")

        self.assertEqual("ok", result)
        self.assertFalse(captured["payload"]["enable_thinking"])


def user_turn() -> dict:
    return {"role": "user", "content": "x"}


class FakeResponse:
    """下载测试用的假响应：只实现 download() 真正用到的那几个方法。"""

    def __init__(self, status: int, headers: dict[str, str] | None = None, body: bytes = b"") -> None:
        self.status = status
        self.fields = {key.lower(): value for key, value in (headers or {}).items()}
        self.body = body
        self.closed = False

    def getheader(self, name: str):
        return self.fields.get(str(name).lower())

    def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            amount = len(self.body)
        chunk, self.body = self.body[:amount], self.body[amount:]
        return chunk

    def close(self) -> None:
        self.closed = True


class DownloadSecurityTest(TempDirCase):
    """附件地址来自外部事件：只放行公网 http(s)，且连的是校验过的 IP。"""

    PUBLIC_IP = "93.184.216.34"

    def setUp(self) -> None:
        super().setUp()
        self.sink = self.root / "attachment.bin"

    def resolver(self, host: str, port: int) -> list[str]:
        return [self.PUBLIC_IP]

    def reply(self, status: int = 200, body: bytes = b"", **headers: str) -> FakeResponse:
        return FakeResponse(status, dict(headers), body)

    def fetch(self, url: str, transport=None, *, limit: int = 1024):
        return att.download(
            url,
            self.sink,
            limit,
            _resolver=self.resolver,
            _transport=transport or self.reply(),
        )

    def no_network(self, target, timeout):
        raise AssertionError("校验不通过时不该建立连接：%s" % target.host)

    def must_reject(self, url: str) -> None:
        self.fetch(url, self.no_network)
        self.assertFalse(Path(str(self.sink) + ".part").exists())

    def test_rejects_non_http_schemes(self) -> None:
        schemes = (
            "file:///C:/test.txt",
            "file:///etc/passwd",
            "ftp://example.com/cat.jpg",
            "data:text/plain,hello",
            "javascript:alert(1)",
        )
        for url in schemes:
            with self.subTest(url=url):
                with self.assertRaises(att.AttachmentError, msg=url):
                    self.must_reject(url)

    def test_rejects_loopback_private_and_metadata_literals(self) -> None:
        IPv4_LOCAL = (
            "http://127.0.0.1:1234/",
            "http://127.1.2.3/",
            "http://localhost:1234/",
            "http://0.0.0.0:1234/",
        )
        IPv4_LAN = (
            "http://10.0.0.1/",
            "http://172.16.0.1/",
            "http://192.168.1.1/",
            "http://169.254.169.254/latest/meta-data/",
            "http://100.64.0.1/",
        )
        IPV6_LOCAL = (
            "http://[::1]:1234/",
            "http://[fe80::1]/",
            "http://[::ffff:127.0.0.1]/",
        )
        for url in IPv4_LOCAL + IPv4_LAN + IPV6_LOCAL:
            with self.subTest(url=url):
                with self.assertRaises(att.AttachmentError, msg=url):
                    self.must_reject(url)

    def test_rejects_hostname_that_resolves_to_private_ip(self) -> None:
        with self.assertRaises(att.AttachmentError):
            att.download(
                "http://internal.example/file",
                self.sink,
                1024,
                _resolver=lambda host, port: ["10.0.0.5"],
            )

    def test_rejects_userinfo_disguise(self) -> None:
        disguises = ("http://example.com@127.0.0.1/", "http://127.0.0.1@example.com/")
        for url in disguises:
            with self.subTest(url=url):
                with self.assertRaises(att.AttachmentError, msg=url):
                    self.must_reject(url)

    def test_allows_public_http_target(self) -> None:
        seen: list = []

        def transport(target, timeout):
            seen.append(target)
            return self.reply(body=b"hello", **{"Content-Length": "5"})

        result = self.fetch("http://safe.example/path/file.bin?x=1", transport)

        self.assertEqual(self.sink, result)
        self.assertEqual(b"hello", self.sink.read_bytes())
        # 连的是校验过的 IP，Host 仍是原域名（证书与虚拟主机才对得上）。
        self.assertEqual(self.PUBLIC_IP, seen[0].ip)
        self.assertEqual("safe.example", seen[0].host_header)

    def test_follows_redirect_to_public_target(self) -> None:
        def transport(target, timeout):
            if target.path.startswith("/start"):
                return self.reply(302, Location="/final.bin")
            return self.reply(body=b"final", **{"Content-Length": "5"})

        self.fetch("http://safe.example/start", transport)
        self.assertEqual(b"final", self.sink.read_bytes())

    def test_rejects_redirect_to_private_host(self) -> None:
        calls: list[str] = []

        def transport(target, timeout):
            calls.append(target.ip)
            return self.reply(302, Location="http://127.0.0.1:1234/steal")

        with self.assertRaises(att.AttachmentError):
            self.fetch("http://safe.example/start", transport)
        # 跳转目标在连接前就被拦下，所以传输层只被调用一次。
        self.assertEqual([self.PUBLIC_IP], calls)

    def test_rejects_redirect_to_file_scheme(self) -> None:
        def transport(target, timeout):
            return self.reply(302, Location="file:///C:/secret.txt")

        with self.assertRaises(att.AttachmentError):
            self.fetch("http://safe.example/start", transport)

    def test_rejects_redirect_loop(self) -> None:
        def transport(target, timeout):
            return self.reply(302, Location="/next")

        with self.assertRaises(att.AttachmentError):
            self.fetch("http://safe.example/start", transport)

    def test_enforces_declared_and_streaming_size_limits(self) -> None:
        declared = lambda target, timeout: self.reply(body=b"x" * 10, **{"Content-Length": "999"})
        streaming = lambda target, timeout: self.reply(body=b"x" * 200)

        with self.subTest(kind="declared"):
            with self.assertRaises(att.AttachmentError):
                self.fetch("http://safe.example/big", declared, limit=100)

        with self.subTest(kind="streaming"):
            with self.assertRaises(att.AttachmentError):
                self.fetch("http://safe.example/big", streaming, limit=100)

    def test_rejects_http_error_status(self) -> None:
        def transport(target, timeout):
            return self.reply(500, body=b"boom")

        with self.assertRaises(att.AttachmentError):
            self.fetch("http://safe.example/err", transport)


class ImageKeepTest(WorkerCase):
    """没有视觉模型也要留住原图，否则收件箱里只剩一句「图片」。"""

    def setUp(self) -> None:
        super().setUp()
        self.settings = settings_for(self.root, dashscope_api_key="")

    def kept_image(self, msg_id: str = "onebot:123456:2048", name: str = "photo.jpg") -> att.Attachment:
        return att.Attachment(
            kind="image",
            name=name,
            group_id="123456",
            group_name="测试群",
            url="https://example.com/photo.jpg",
            size=len(PNG_BYTES),
            msg_id=msg_id,
        )

    def write_png(self, dest: Path) -> None:
        dest.write_bytes(PNG_BYTES)

    def test_image_is_kept_even_without_vision(self) -> None:
        attachment = self.kept_image()
        patcher, _ = self.download_stub(self.write_png)
        with patcher:
            record = self.worker().process(attachment)

        self.assertIsNone(record)
        self.assertEqual([], self.records)
        kept = att.images_of(self.settings.data_dir, attachment.msg_id)
        self.assertEqual(1, len(kept))
        self.assertEqual(f"{att.image_key(attachment.msg_id)}_0.png", kept[0].name)
        self.assertEqual(PNG_BYTES, kept[0].read_bytes())
        self.assertTrue(att.has_image(self.settings.data_dir, attachment.msg_id))
        self.assertTrue(kept[0].is_file())

    def test_two_images_of_same_message_use_indexes(self) -> None:
        first, second = self.kept_image(), self.kept_image(name="photo2.png")
        patcher, _ = self.download_stub(self.write_png)
        with patcher:
            worker = self.worker()
            worker.process(first)
            worker.process(second)

        kept = att.images_of(self.settings.data_dir, first.msg_id)
        self.assertEqual(
            [f"{att.image_key(first.msg_id)}_0.png", f"{att.image_key(first.msg_id)}_1.png"],
            [path.name for path in kept],
        )

    def test_file_attachment_is_not_kept_as_image(self) -> None:
        attachment = file_attachment(
            "表格.xlsx",
            100,
            msg_id="onebot:123456:9",
            url="https://example.com/table.xlsx",
            group_name="",
            sender_name="",
        )
        patcher, _ = self.download_stub(self.write_png)
        with patcher, mock.patch.object(
            att, "extract_text", return_value="正文"
        ), mock.patch.object(att, "summarize_document", return_value="摘要"):
            self.worker().process(attachment)

        self.assertFalse(att.has_image(self.settings.data_dir, attachment.msg_id))
        self.assertEqual([], att.images_of(self.settings.data_dir, attachment.msg_id))

    def test_oversized_image_is_not_kept(self) -> None:
        attachment = self.kept_image()
        oversized = att.IMAGE_MAX_BYTES + 1

        def write_huge(dest: Path) -> None:
            with dest.open("wb") as handle:
                handle.write(PNG_MAGIC)
                handle.truncate(oversized)

        patcher, _ = self.download_stub(write_huge)
        with patcher:
            self.worker().process(attachment)

        self.assertFalse(att.has_image(self.settings.data_dir, attachment.msg_id))

    def test_plain_message_image_lands_under_its_message_id(self) -> None:
        """普通消息里的图片：附件 msg_id 必须等于消息行 msg_id，接口才找得到。"""
        payload = group_message(
            segments=[
                part("text", text="看这张"),
                part("image", file="shot.jpg", file_size="180000", url="https://x/shot"),
            ]
        )
        found = att.attachments_from_message(payload, self.settings)
        self.assertEqual(1, len(found))
        self.assertEqual("onebot:123456:2048", found[0].msg_id)

        patcher, _ = self.download_stub(self.write_png)
        with patcher:
            self.worker().process(found[0])

        self.assertTrue(att.has_image(self.settings.data_dir, "onebot:123456:2048"))
        self.assertEqual(
            f"{att.image_key('onebot:123456:2048')}_0.png",
            att.images_of(self.settings.data_dir, "onebot:123456:2048")[0].name,
        )

    def test_delete_images_removes_saved_copies(self) -> None:
        attachment = self.kept_image()
        patcher, _ = self.download_stub(self.write_png)
        with patcher:
            self.worker().process(attachment)

        self.assertEqual(1, att.delete_images(self.settings.data_dir, [attachment.msg_id]))
        self.assertFalse(att.has_image(self.settings.data_dir, attachment.msg_id))

    def test_unknown_image_bytes_are_not_kept(self) -> None:
        attachment = self.kept_image(name="photo")
        patcher, _ = self.download_stub(lambda dest: dest.write_bytes(b"not an image"))
        with patcher:
            self.worker().process(attachment)

        self.assertFalse(att.has_image(self.settings.data_dir, attachment.msg_id))


if __name__ == "__main__":
    unittest.main()
