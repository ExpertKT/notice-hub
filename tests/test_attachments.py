from __future__ import annotations

import json

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_digest import Message, analyze_message, dedupe_items, similar_text  # noqa: E402
from qq_live_digest import attachments as att  # noqa: E402
from qq_live_digest.config import Settings  # noqa: E402
from qq_live_digest.receiver import OneBotReceiver  # noqa: E402
from qq_live_digest.summarizer import filter_recent_duplicates  # noqa: E402

DOCX_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:body><w:p><w:r><w:t>培养方案说明：本学期共 12 门课程。</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>请于 9 月 30 日前完成选课确认。</w:t></w:r></w:p></w:body></w:document>"
)


def make_settings(root: Path, **overrides) -> Settings:
    params = {
        "group_whitelist": ("123456",),
        "data_dir": root / "data",
        "log_dir": root / "logs",
        # 附件解析（含读图）默认按「已配置 DashScope key」的装机来测；网络调用都被 mock。
        "dashscope_api_key": "test-key",
    }
    params.update(overrides)
    return Settings(**params)


GROUP_MESSAGE = {
    "post_type": "message",
    "message_type": "group",
    "message_id": 2048,
    "group_id": 123456,
    "user_id": 999,
    "self_id": 111,
    "time": 1789000000,
    "sender": {"card": "学习委员", "nickname": "学委"},
    "message": [
        {"type": "text", "data": {"text": "这是今天要交的表格："}},
        {
            "type": "file",
            "data": {
                "file": "附件2：报名表.xlsx",
                "file_id": "/abc-def",
                "file_size": "17306",
                "url": "https://example.com/table.xlsx",
            },
        },
    ],
}

UPLOAD_NOTICE = {
    "post_type": "notice",
    "notice_type": "group_upload",
    "group_id": 123456,
    "user_id": 999,
    "time": 1789000000,
    "file": {"id": "file-1", "name": "通知.pdf", "size": 204800, "busid": 102},
}


class AttachmentParseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = make_settings(Path(self.temp.name))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_file_segment_becomes_attachment(self) -> None:
        found = att.attachments_from_message(GROUP_MESSAGE, self.settings)
        self.assertEqual(len(found), 1)
        attachment = found[0]
        self.assertEqual(attachment.kind, "file")
        self.assertEqual(attachment.name, "附件2：报名表.xlsx")
        self.assertEqual(attachment.size, 17306)
        self.assertEqual(attachment.url, "https://example.com/table.xlsx")
        self.assertEqual(attachment.msg_id, "onebot:123456:2048")

    def test_upload_notice_becomes_attachment(self) -> None:
        attachment = att.attachment_from_notice(UPLOAD_NOTICE, self.settings)
        self.assertIsNotNone(attachment)
        assert attachment is not None
        self.assertEqual(attachment.name, "通知.pdf")
        self.assertEqual(attachment.file_id, "file-1")
        self.assertEqual(attachment.busid, "102")
        self.assertEqual(attachment.size, 204800)

    def test_pdf_ocr_renders_pages_without_pymupdf(self) -> None:
        import pypdf

        pdf_path = Path(self.temp.name) / "scan.pdf"
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=144, height=144)
        with pdf_path.open("wb") as handle:
            writer.write(handle)

        rendered: list[bytes] = []

        def fake_vision(raw, suffix, settings):
            rendered.append(raw)
            return "扫描件测试：请于 10 月 9 日前提交材料。"

        with mock.patch.object(att, "image_bytes_text", side_effect=fake_vision):
            text = att.pdf_ocr_text(pdf_path, self.settings)

        self.assertIn("【第1页】", text)
        self.assertTrue(rendered and rendered[0].startswith(b"\x89PNG"))

    def test_sticker_and_tiny_images_are_skipped(self) -> None:
        payload = {
            "post_type": "message",
            "message_type": "group",
            "message_id": 1,
            "group_id": 123456,
            "user_id": 999,
            "message": [
                {
                    "type": "image",
                    "data": {"file": "a.jpg", "file_size": "40000", "sub_type": 1, "url": "https://x/a"},
                },
                {
                    "type": "image",
                    "data": {"file": "b.jpg", "file_size": "2048", "url": "https://x/b"},
                },
                {
                    "type": "image",
                    "data": {
                        "file": "c.jpg",
                        "file_size": "120000",
                        "url": "https://x/c",
                        "summary": "[好的]",
                    },
                },
            ],
        }
        self.assertEqual(att.attachments_from_message(payload, self.settings), [])

    def test_normal_image_is_kept(self) -> None:
        payload = {
            "post_type": "message",
            "message_type": "group",
            "message_id": 7,
            "group_id": 123456,
            "user_id": 999,
            "message": [
                {
                    "type": "image",
                    "data": {"file": "shot.jpg", "file_size": "180000", "url": "https://x/shot"},
                }
            ],
        }
        found = att.attachments_from_message(payload, self.settings)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].kind, "image")

    def test_other_groups_are_ignored(self) -> None:
        payload = dict(GROUP_MESSAGE)
        payload["group_id"] = 99999
        self.assertEqual(att.attachments_from_message(payload, self.settings), [])

    def test_same_file_via_notice_and_message_shares_key(self) -> None:
        notice_payload = {
            "post_type": "notice",
            "notice_type": "group_upload",
            "group_id": 123456,
            "user_id": 999,
            "file": {"id": "abc", "name": "名单.xlsx", "size": 17306, "busid": 102},
        }
        message_payload = dict(GROUP_MESSAGE)
        message_payload["message"] = [
            {
                "type": "file",
                "data": {
                    "file": "名单.xlsx",
                    "file_id": "/e5caf312-other",
                    "file_size": "17306",
                    "url": "https://example.com/list.xlsx",
                },
            }
        ]
        notice = att.attachment_from_notice(notice_payload, self.settings)
        message_item = att.attachments_from_message(message_payload, self.settings)[0]
        assert notice is not None
        self.assertEqual(notice.key, message_item.key)


class ExtractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_text_file(self) -> None:
        path = self.root / "a.txt"
        path.write_text("明天上午九点在教学楼 301 开班会", encoding="utf-8")
        self.assertIn("开班会", att.extract_text(path))

    def test_docx(self) -> None:
        path = self.root / "plan.docx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", DOCX_XML)
        text = att.extract_text(path)
        self.assertIn("培养方案说明", text)
        self.assertIn("选课确认", text)

    def test_xlsx(self) -> None:
        import openpyxl

        path = self.root / "list.xlsx"
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.append(["姓名", "金额"])
        sheet.append(["张三", 2000])
        book.save(path)
        text = att.extract_text(path)
        self.assertIn("张三", text)
        self.assertIn("金额", text)

    def test_zip_skips_traversal_and_unknown(self) -> None:
        path = self.root / "pack.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("../evil.txt", "不该被读取")
            archive.writestr("notes.txt", "助学金公示名单")
            archive.writestr("photo.jpg", "binary")
        text = att.extract_zip(path)
        self.assertIn("助学金公示名单", text)
        self.assertNotIn("不该被读取", text)

    def test_legacy_doc_is_unsupported(self) -> None:
        path = self.root / "old.doc"
        path.write_bytes(b"\xd0\xcf\x11\xe0")
        self.assertEqual(att.extract_text(path), "")

    def test_sanitize_name(self) -> None:
        self.assertEqual(att.sanitize_name("../../etc/通知.pdf"), "_.._etc_通知.pdf")
        self.assertEqual(att.sanitize_name("   "), "attachment")


class WorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = make_settings(self.root)
        self.records: list[dict] = []

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _worker(self) -> att.AttachmentWorker:
        return att.AttachmentWorker(self.settings, self.records.append)

    def test_file_record_uses_ai_summary(self) -> None:
        worker = self._worker()
        attachment = att.Attachment(
            kind="file",
            name="报名表.xlsx",
            group_id="123456",
            group_name="测仪2602班群",
            sender_name="学委",
            url="https://example.com/table.xlsx",
            size=20480,
            ts="2026-09-29T09:00:00+08:00",
        )

        saved: list[Path] = []

        def fake_download(url, dest, max_bytes, timeout=30):
            from openpyxl import Workbook

            dest.parent.mkdir(parents=True, exist_ok=True)
            workbook = Workbook()
            workbook.active.append(["姓名", "金额"])
            workbook.active.append(["张三", 2000])
            workbook.save(dest)
            saved.append(dest)
            return dest

        with mock.patch.object(att, "download", side_effect=fake_download), mock.patch.object(
            att, "chat_completion", return_value="班委要求今天 18:00 前提交报名表"
        ):
            record = worker.process(attachment)

        self.assertIsNotNone(record)
        assert record is not None
        self.assertIn("【文件】报名表.xlsx", record["content"])
        self.assertIn("报名表", record["content"])
        self.assertEqual(record["group_name"], "测仪2602班群")
        self.assertEqual(self.records, [record])
        self.assertEqual(worker.stats.processed, 1)
        self.assertTrue(saved and saved[0].is_file())
        self.assertIn("张三", record["source_text"])
        self.assertIn("2000", record["source_text"])

    def test_file_keeps_structured_fields_and_full_source(self) -> None:
        worker = self._worker()
        attachment = att.Attachment(
            kind="file",
            name="转专业通知.txt",
            group_id="123456",
            group_name="电气学院群",
            sender_name="辅导员",
            url="https://example.com/notice.txt",
            size=2400,
        )
        long_body = "刚转专业到学院的同学需要体测，其他同学也要核查。" + "细节" * 900
        structured = json.dumps(
            {
                "summary": "两类同学都要核查体测成绩",
                "audience": "刚转专业同学；其他同学",
                "condition": "没有2026年体测成绩",
                "details": ["刚转专业同学先查看体测系统", "其他同学也要核查成绩"],
                "action": "预约并参加体测",
                "deadline": "2026-11-21 23:59",
            },
            ensure_ascii=False,
        )

        def fake_download(url, dest, max_bytes, timeout=30):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(long_body, encoding="utf-8")
            return dest

        with mock.patch.object(att, "download", side_effect=fake_download), mock.patch.object(
            att, "chat_completion", return_value=structured
        ):
            record = worker.process(attachment)

        assert record is not None
        self.assertIn("适用对象：刚转专业同学；其他同学", record["content"])
        self.assertIn("适用条件：没有2026年体测成绩", record["content"])
        self.assertIn("截止时间：2026-11-21 23:59", record["content"])
        self.assertGreater(len(record["source_text"]), len(record["content"]))
        self.assertIn("刚转专业到学院的同学", record["source_text"])

    def test_scanned_pdf_uses_vision_ocr(self) -> None:
        worker = self._worker()
        attachment = att.Attachment(
            kind="file",
            name="扫描通知.pdf",
            group_id="123456",
            group_name="电气学院群",
            sender_name="辅导员",
            url="https://example.com/scan.pdf",
            size=2048,
        )

        def fake_download(url, dest, max_bytes, timeout=30):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"%PDF-1.4")
            return dest

        with mock.patch.object(att, "download", side_effect=fake_download), mock.patch.object(
            att, "extract_text", return_value=""
        ), mock.patch.object(
            att, "pdf_ocr_text", return_value="扫描件要求：转专业同学核查体测成绩。"
        ), mock.patch.object(
            att, "summarize_document", return_value="适用对象：转专业同学\n截止时间：11月21日"
        ):
            record = worker.process(attachment)

        assert record is not None
        self.assertIn("扫描件要求", record["source_text"])
        self.assertIn("截止时间：11月21日", record["content"])

    def test_image_record_uses_vision_text(self) -> None:
        worker = self._worker()
        attachment = att.Attachment(
            kind="image",
            name="shot.jpg",
            group_id="123456",
            group_name="电气学院2026级群",
            sender_name="辅导员",
            url="https://example.com/shot.jpg",
            size=180000,
        )

        saved: list[Path] = []

        def fake_download(url, dest, max_bytes, timeout=30):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"\xff\xd8\xff")
            saved.append(dest)
            return dest

        with mock.patch.object(att, "download", side_effect=fake_download), mock.patch.object(
            att, "image_text", return_value="关于国家助学金公示，请相关同学 9 月 30 日 17:00 前交材料"
        ):
            record = worker.process(attachment)

        assert record is not None
        self.assertIn("【图片】", record["content"])
        self.assertIn("助学金", record["content"])
        self.assertTrue(saved and not saved[0].exists())

    def test_image_without_text_is_skipped(self) -> None:
        worker = self._worker()
        attachment = att.Attachment(
            kind="image", name="meme.jpg", group_id="123456", size=90000,
            url="https://example.com/meme.jpg",
        )

        def fake_download(url, dest, max_bytes, timeout=30):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"\xff\xd8\xff")
            return dest

        with mock.patch.object(att, "download", side_effect=fake_download), mock.patch.object(
            att, "image_text", return_value="无有效通知"
        ):
            record = worker.process(attachment)
        self.assertIsNone(record)
        self.assertEqual(self.records, [])
        self.assertEqual(worker.stats.skipped, 1)

    def test_oversized_file_only_records_name(self) -> None:
        worker = self._worker()
        attachment = att.Attachment(
            kind="file", name="大文件.zip", group_id="123456", size=99 * 1024 * 1024
        )
        record = worker.process(attachment)
        assert record is not None
        self.assertIn("大文件.zip", record["content"])
        self.assertIn("超过大小上限", record["content"])

    def test_submit_dedupes_same_attachment(self) -> None:
        worker = self._worker()
        attachment = att.Attachment(kind="file", name="a.pdf", group_id="123456", file_id="x", size=1000)
        self.assertTrue(worker.submit(attachment))
        self.assertFalse(worker.submit(attachment))
        self.assertEqual(worker.stats.queued, 1)

    def test_disabled_worker_accepts_nothing(self) -> None:
        settings = make_settings(self.root, attachments_enabled=False)
        worker = att.AttachmentWorker(settings, self.records.append)
        self.assertFalse(worker.submit(att.Attachment(kind="file", name="a.pdf")))

    def test_cleanup_files_removes_old(self) -> None:
        directory = self.root / "data" / "files"
        directory.mkdir(parents=True, exist_ok=True)
        old = directory / "old.bin"
        old.write_bytes(b"x")
        import os
        import time

        past = time.time() - 10 * 86400
        os.utime(old, (past, past))
        self.assertEqual(att.cleanup_files(directory, days=7), 1)
        self.assertFalse(old.exists())


class ReceiverDispatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = make_settings(Path(self.temp.name))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_message_with_file_dispatches_both(self) -> None:
        records: list[dict] = []
        queued: list[att.Attachment] = []

        def on_message(record: dict) -> bool:
            records.append(record)
            return True

        def on_attachment(item: att.Attachment) -> bool:
            queued.append(item)
            return True

        receiver = OneBotReceiver(self.settings, on_message, on_attachment, None)
        status, payload = receiver.handle_payload(GROUP_MESSAGE)
        self.assertEqual(status, 200)
        self.assertTrue(payload["inserted"])
        self.assertEqual(payload["attachments"], 1)
        self.assertEqual(len(records), 1)
        self.assertEqual(queued[0].name, "附件2：报名表.xlsx")

    def test_notice_is_dispatched_even_without_message(self) -> None:
        records: list[dict] = []
        queued: list[att.Attachment] = []

        def on_message(record: dict) -> bool:
            records.append(record)
            return True

        def on_attachment(item: att.Attachment) -> bool:
            queued.append(item)
            return True

        receiver = OneBotReceiver(self.settings, on_message, on_attachment, None)
        status, payload = receiver.handle_payload(UPLOAD_NOTICE)
        self.assertEqual(status, 204)
        self.assertEqual(payload["attachments"], 1)
        self.assertEqual(records, [])
        self.assertEqual(queued[0].name, "通知.pdf")

    def test_disabled_attachments_ignored(self) -> None:
        settings = make_settings(Path(self.temp.name), attachments_enabled=False)
        queued: list[att.Attachment] = []
        receiver = OneBotReceiver(settings, lambda record: True, queued.append, None)
        receiver.handle_payload(UPLOAD_NOTICE)
        self.assertEqual(queued, [])


class CrossGroupDedupeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = make_settings(Path(self.temp.name), dedupe_hours=6)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _item(self, text: str, group: str) -> dict:
        message = Message(timestamp=None, sender="班长", text=text, source=group)
        analysis = analyze_message(message)
        analysis["group_name"] = group
        analysis["msg_id"] = f"{group}:{hash(text) & 0xFFFF}"
        return analysis

    def test_same_notice_in_two_groups_merges(self) -> None:
        text = "请国家助学金公示名单中的同学今天17:00前将纸质材料交到学院办公室"
        items = [self._item(text, "测仪2602班群"), self._item(text, "测仪2602班级闲聊群")]
        merged = dedupe_items(items)
        self.assertEqual(len(merged), 1)
        self.assertEqual(
            set(merged[0]["duplicate_groups"]), {"测仪2602班群", "测仪2602班级闲聊群"}
        )

    def test_different_notices_are_kept(self) -> None:
        items = [
            self._item("请各班班长今天17:00前提交助学金纸质材料", "测仪2602班群"),
            self._item("明天上午9点在教学楼301进行英语四级模拟考试", "测仪2602班群"),
        ]
        self.assertEqual(len(dedupe_items(items)), 2)

    def test_recent_history_suppresses_repeat(self) -> None:
        text = "请国家助学金公示名单中的同学今天17:00前将纸质材料交到学院办公室"
        candidates = [self._item(text, "测仪2602班级闲聊群")]
        history = [{"text": text, "summary": "助学金材料", "group": "测仪2602班群", "ts": "2026-09-29T09:00:00+08:00"}]
        kept, suppressed = filter_recent_duplicates(candidates, history, self.settings)
        self.assertEqual(kept, [])
        self.assertEqual(len(suppressed), 1)
        self.assertEqual(suppressed[0]["suppressed_duplicate"]["group"], "测仪2602班群")

    def test_history_disabled_by_zero_hours(self) -> None:
        settings = make_settings(Path(self.temp.name), dedupe_hours=0)
        text = "请大家今天17:00前提交材料"
        candidates = [self._item(text, "A群")]
        history = [{"text": text, "group": "B群", "ts": ""}]
        kept, suppressed = filter_recent_duplicates(candidates, history, settings)
        self.assertEqual(len(kept), 1)
        self.assertEqual(suppressed, [])

    def test_similar_text_helper(self) -> None:
        self.assertTrue(similar_text("abcdef", "abcdef"))
        self.assertFalse(similar_text("abcdef", "zzzzzz"))
        self.assertTrue(similar_text("a" * 30 + "通知", "a" * 30 + "通知（重复）"))


class ConfigTest(unittest.TestCase):
    def test_attachment_defaults(self) -> None:
        settings = Settings.from_env({})
        self.assertTrue(settings.attachments_enabled)
        self.assertEqual(settings.attachment_max_mb, 10)
        self.assertTrue(settings.vision_enabled)
        self.assertEqual(settings.vision_model, "qwen3-vl-plus")
        self.assertEqual(settings.document_max_chars, 30000)
        self.assertEqual(settings.pdf_ocr_max_pages, 20)
        self.assertEqual(settings.dedupe_hours, 6)

    def test_attachment_env_overrides(self) -> None:
        settings = Settings.from_env(
            {
                "QQ_DIGEST_ATTACHMENTS": "0",
                "QQ_DIGEST_ATTACHMENT_MAX_MB": "25",
                "QQ_DIGEST_VISION": "0",
                "QQ_DIGEST_VL_MODEL": "qwen-vl-max",
                "QQ_DIGEST_DOCUMENT_MAX_CHARS": "12000",
                "QQ_DIGEST_PDF_OCR_MAX_PAGES": "8",
                "QQ_DIGEST_DEDUPE_HOURS": "12",
            }
        )
        self.assertFalse(settings.attachments_enabled)
        self.assertEqual(settings.attachment_max_mb, 25)
        self.assertFalse(settings.vision_enabled)
        self.assertEqual(settings.vision_model, "qwen-vl-max")
        self.assertEqual(settings.document_max_chars, 12000)
        self.assertEqual(settings.pdf_ocr_max_pages, 8)
        self.assertEqual(settings.dedupe_hours, 12)
        described = settings.describe()
        self.assertIn("attachments", described)
        self.assertEqual(described["dedupe_hours"], 12)


class AiPayloadTest(unittest.TestCase):
    def test_qwen3_vision_disables_thinking(self) -> None:
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"choices":[{"message":{"content":"ok"}}]}'

        def fake_urlopen(request, timeout=0):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return Response()

        settings = Settings(dashscope_api_key="key")
        with mock.patch.object(att.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = att.chat_completion(
                settings, [{"role": "user", "content": "x"}], model="qwen3-vl-plus"
            )
        self.assertEqual(result, "ok")
        self.assertFalse(captured["payload"]["enable_thinking"])


class _FakeResponse:
    """download() 测试用的假响应，不发起真实网络请求。"""

    def __init__(self, status: int, headers: dict[str, str] | None = None, body: bytes = b"") -> None:
        self.status = status
        self._headers = {key.lower(): value for key, value in (headers or {}).items()}
        self._body = body
        self.closed = False

    def getheader(self, name: str) -> str | None:
        return self._headers.get(str(name).lower())

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = len(self._body)
        chunk, self._body = self._body[:size], self._body[size:]
        return chunk

    def close(self) -> None:
        self.closed = True


class DownloadSecurityTest(unittest.TestCase):
    """附件下载只允许公网 http(s)，拒绝 file:// 与本机/内网/元数据目标。"""

    PUBLIC_IP = "93.184.216.34"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.dest = Path(self.temp.name) / "attachment.bin"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def resolver(self, host: str, port: int) -> list[str]:
        return [self.PUBLIC_IP]

    def assert_rejected(self, url: str) -> None:
        with self.assertRaises(att.AttachmentError, msg=url):
            att.download(url, self.dest, 1024)
        self.assertFalse(self.dest.with_suffix(self.dest.suffix + ".part").exists())

    def test_rejects_non_http_schemes(self) -> None:
        for url in (
            "file:///C:/test.txt",
            "file:///etc/passwd",
            "ftp://example.com/cat.jpg",
            "data:text/plain,hello",
            "javascript:alert(1)",
        ):
            self.assert_rejected(url)

    def test_rejects_loopback_private_and_metadata_literals(self) -> None:
        for url in (
            "http://127.0.0.1:1234/",
            "http://127.1.2.3/",
            "http://localhost:1234/",
            "http://0.0.0.0:1234/",
            "http://10.0.0.1/",
            "http://172.16.0.1/",
            "http://192.168.1.1/",
            "http://169.254.169.254/latest/meta-data/",
            "http://100.64.0.1/",
            "http://[::1]:1234/",
            "http://[fe80::1]/",
            "http://[::ffff:127.0.0.1]/",
        ):
            self.assert_rejected(url)

    def test_rejects_hostname_that_resolves_to_private_ip(self) -> None:
        with self.assertRaises(att.AttachmentError):
            att.download(
                "http://internal.example/file",
                self.dest,
                1024,
                _resolver=lambda host, port: ["10.0.0.5"],
            )

    def test_rejects_userinfo_disguise(self) -> None:
        for url in ("http://example.com@127.0.0.1/", "http://127.0.0.1@example.com/"):
            self.assert_rejected(url)

    def test_allows_public_http_target(self) -> None:
        seen = []

        def transport(target, timeout):
            seen.append(target)
            return _FakeResponse(200, {"Content-Length": "5"}, b"hello")

        result = att.download(
            "http://safe.example/path/file.bin?x=1",
            self.dest,
            1024,
            _resolver=self.resolver,
            _transport=transport,
        )
        self.assertEqual(result, self.dest)
        self.assertEqual(self.dest.read_bytes(), b"hello")
        self.assertEqual(seen[0].ip, self.PUBLIC_IP)
        self.assertEqual(seen[0].host_header, "safe.example")

    def test_follows_redirect_to_public_target(self) -> None:
        def transport(target, timeout):
            if target.path.startswith("/start"):
                return _FakeResponse(302, {"Location": "/final.bin"})
            return _FakeResponse(200, {"Content-Length": "5"}, b"final")

        att.download(
            "http://safe.example/start",
            self.dest,
            1024,
            _resolver=self.resolver,
            _transport=transport,
        )
        self.assertEqual(self.dest.read_bytes(), b"final")

    def test_rejects_redirect_to_private_host(self) -> None:
        calls: list[str] = []

        def transport(target, timeout):
            calls.append(target.ip)
            return _FakeResponse(302, {"Location": "http://127.0.0.1:1234/steal"})

        with self.assertRaises(att.AttachmentError):
            att.download(
                "http://safe.example/start",
                self.dest,
                1024,
                _resolver=self.resolver,
                _transport=transport,
            )
        self.assertEqual(calls, [self.PUBLIC_IP])

    def test_rejects_redirect_to_file_scheme(self) -> None:
        def transport(target, timeout):
            return _FakeResponse(302, {"Location": "file:///C:/secret.txt"})

        with self.assertRaises(att.AttachmentError):
            att.download(
                "http://safe.example/start",
                self.dest,
                1024,
                _resolver=self.resolver,
                _transport=transport,
            )

    def test_rejects_redirect_loop(self) -> None:
        def transport(target, timeout):
            return _FakeResponse(302, {"Location": "/next"})

        with self.assertRaises(att.AttachmentError):
            att.download(
                "http://safe.example/start",
                self.dest,
                1024,
                _resolver=self.resolver,
                _transport=transport,
            )

    def test_enforces_declared_and_streaming_size_limits(self) -> None:
        def declared(target, timeout):
            return _FakeResponse(200, {"Content-Length": "999"}, b"x" * 10)

        with self.assertRaises(att.AttachmentError):
            att.download(
                "http://safe.example/big", self.dest, 100,
                _resolver=self.resolver, _transport=declared,
            )

        def streaming(target, timeout):
            return _FakeResponse(200, {}, b"x" * 200)

        with self.assertRaises(att.AttachmentError):
            att.download(
                "http://safe.example/big", self.dest, 100,
                _resolver=self.resolver, _transport=streaming,
            )

    def test_rejects_http_error_status(self) -> None:
        def transport(target, timeout):
            return _FakeResponse(500, {}, b"boom")

        with self.assertRaises(att.AttachmentError):
            att.download(
                "http://safe.example/err",
                self.dest,
                1024,
                _resolver=self.resolver,
                _transport=transport,
            )



if __name__ == "__main__":
    unittest.main()
