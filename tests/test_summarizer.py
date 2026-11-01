"""摘要模块的行为回归：选材、去重、截止时间、三种推送文本与 LLM 降级。

写之前先记住：用例名和断言里的期望值是契约。改表述可以，改期望值不行。
"""

from __future__ import annotations

import datetime as dt
import http.server
import sys
import threading
import json
import unittest
from pathlib import Path
from unittest import mock

# 让 tests/ 之外的包能被 import：本文件在 tests/ 里，仓库根在上层。
_HERE = Path(__file__).resolve()
for _root in (_HERE.parent.parent,):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

from noticehub.config import Settings  # noqa: E402
from noticehub.summarizer import Digest, analyse_records, build_digest, evidence_sentence  # noqa: E402
from noticehub.summarizer import format_push_text, html_push_text, payload_to_item  # noqa: E402
from noticehub.summarizer import preview_text, push_summary, urgent_analyses  # noqa: E402
from noticehub.timeutil import iso  # noqa: E402
from qq_digest import Message, analyze_message, extract_deadline, parse_timestamp  # noqa: E402

NOW = dt.datetime(2026, 9, 28, 18, 0, 0)
GROUP = "学院通知群"
# 模型返回体里那条「像样的」待办：多个用例都要用它，抽出来省得每次抄一遍字段。
STUB_ITEM = {
    "id": 1,
    "keep": True,
    "summary": "班长今晚18:00前提交材料",
    "action": "班长提交材料",
    "deadline": "2026-09-28 18:00",
    "category": "action",
    "importance": 5,
}


def record(
    msg_id: str,
    content: str,
    sender: str = "辅导员",
    minutes_ago: int = 0,
    group_name: str = GROUP,
) -> dict:
    """造一条形状与库里一致的群消息。时间按「相对 NOW 往前推几分钟」算。"""
    at = iso(NOW - dt.timedelta(minutes=minutes_ago))
    return dict(
        msg_id=msg_id,
        ts=at,
        received_at=at,
        content=content,
        sender_name=sender,
        group_name=group_name,
        sender_id="u1",
        group_id="g1",
        source="qqbot",
        event="GROUP_MESSAGE_CREATE",
    )


def settings_for(**overrides) -> Settings:
    """默认只认 g1 这一个群（映射成学院通知群），用例只覆盖自己关心的键。"""
    base = {"group_whitelist": ("g1",), "group_aliases": {"g1": GROUP}}
    base.update(overrides)
    return Settings(**base)


def urgent_for(records, settings, now=NOW):
    """一步算完：先分析再筛紧急项。"""
    return urgent_analyses(analyse_records(records), settings, now=now)


def msg(text: str, sender: str = "辅导员", source: str = GROUP) -> Message:
    """只关心正文与来源时用它，省得每条都把 timestamp/sender/source 写一遍。"""
    return Message(timestamp=NOW, sender=sender, text=text, source=source)


class StubLlm:
    """假的模型服务：收到任何 POST 都回一段固定 JSON，省掉真联网。

    关的时候顺序不能反——shutdown 让 serve_forever 先返回，再 join 线程，最后
    才 server_close。先关套接字的话，accept 线程会拿着废句柄卡在 select()，
    Windows 上就是 WinError 10038 那种未捕获异常。
    """

    def __init__(self, items: dict) -> None:
        self._payload = json.dumps(items, ensure_ascii=False)
        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._worker = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def _make_handler(self):
        payload = self._payload

        class _Reply(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - 标准库约定
                self._drain_request()
                self._send_json({"choices": [{"message": {"content": payload}}]})

            def _drain_request(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def _send_json(self, data: dict) -> None:
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                headers = {
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                }
                self.send_response(200)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        return _Reply

    @property
    def endpoint(self) -> str:
        port = self._httpd.server_address[1]
        return "http://127.0.0.1:%d/v1/chat/completions" % port

    def start(self) -> "StubLlm":
        self._worker.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        try:
            self._worker.join(timeout=5)
        finally:
            self._httpd.server_close()


def push_item(**overrides) -> dict:
    """推送用例用的单条待办；只覆盖需要变的字段。"""
    base = {
        "message": Message(
            timestamp=NOW,
            sender="辅导员",
            text="请各班班长今天18:00前提交材料，这是一段绝对不应该出现在推送正文里的原文内容。",
            source=GROUP,
        ),
        "summary": "今晚18:00前班长提交材料",
        "action": "班长提交材料",
        "deadline_dt": NOW.replace(hour=18, minute=0),
        "group_name": GROUP,
        "category": "urgent",
        "score": 5,
        "importance": 5,
    }
    base.update(overrides)
    return base


class SummarizerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = settings_for()

    def test_notice_kept_and_chatter_dropped(self) -> None:
        records = [
            record("m1", "请各班班长今天18:00前提交材料"),
            record("m2", "哈哈哈哈", sender="同学A"),
            record("m3", "有人一起去图书馆吗", sender="同学B"),
        ]
        digest = build_digest(self.settings, records, now=NOW)
        self.assertEqual([item["msg_id"] for item in digest.items], ["m1"])
        self.assertIn("截止", digest.body)
        self.assertTrue(digest.items[0].get("action"))
        self.assertIn("截止", digest.body)
        self.assertIn(GROUP, digest.body)

    def test_deadline_and_category_in_payload(self) -> None:
        records = [record("m1", "四六级报名截止时间是9月30日")]
        payload = build_digest(self.settings, records, now=NOW).payload()
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["category"], "urgent")
        self.assertIn("2026-09-30", payload[0]["deadline"])
        json.dumps(payload, ensure_ascii=False)

    def test_plain_notice_without_deadline_is_kept(self) -> None:
        records = [record("m1", "【学院通知】关于2026年国庆节放假安排的通知")]
        digest = build_digest(self.settings, records, now=NOW)
        self.assertEqual(len(digest.items), 1)

    def test_urgent_requires_substance(self) -> None:
        # 先把「只分析不过滤」的结果拿出来看一眼，再谈紧急筛选。
        chatter = analyse_records([record("m1", "今天天气不错", sender="同学A")])
        self.assertEqual(chatter[0]["category"], "urgent")
        self.assertEqual(urgent_analyses(chatter, self.settings, now=NOW), [])

        real = analyse_records([record("m2", "请各班班长今天18:00前提交材料")])
        self.assertEqual(len(urgent_analyses(real, self.settings, now=NOW)), 1)

    def test_urgent_disabled(self) -> None:
        settings = settings_for(urgent_immediate=False)
        analyses = analyse_records([record("m1", "紧急通知：请务必马上提交材料")])
        self.assertEqual(urgent_analyses(analyses, settings, now=NOW), [])

    def test_no_focus_message_gives_empty_body(self) -> None:
        digest = build_digest(self.settings, [record("m1", "收到")], now=NOW)
        self.assertFalse(digest.items)
        self.assertEqual(digest.body, "")
        self.assertIn("不会推送", preview_text(self.settings, [record("m1", "收到")]))

    def test_preview_reports_pending(self) -> None:
        text = preview_text(self.settings, [record("m1", "请各班班长今天18:00前提交材料")])
        self.assertIn("[仅预览]", text)
        self.assertIn("18:00", text)

    def test_missing_llm_key_reports_local_fallback(self) -> None:
        settings = settings_for(dashscope_api_key="", llm_backend="ollama")
        digest = build_digest(settings, [record("m1", "请各班班长今天18:00前提交材料")], now=NOW)
        self.assertFalse(digest.llm_used)
        self.assertIn("未配置", digest.llm_error)
        # 配置缺失不是可重试故障：标成可重试会把消息无限往后推。
        self.assertFalse(digest.llm_retryable)
        self.assertIn("本地规则筛选", digest.body)

    def test_codebuddy_runs_without_dashscope_key(self) -> None:
        settings = settings_for(dashscope_api_key="", llm_backend="codebuddy")
        refine_path = "noticehub.summarizer.qq_digest.refine_with_dashscope"
        patched = mock.patch(refine_path, side_effect=lambda items, *a, **k: items)
        with patched as refine:
            digest = build_digest(settings, [record("m1", "请各班班长今天18:00前提交材料")], now=NOW)
        refine.assert_called_once()
        self.assertTrue(digest.llm_used)

    def test_ollama_without_dashscope_key_skips_llm(self) -> None:
        settings = settings_for(dashscope_api_key="", llm_backend="ollama")
        with mock.patch("noticehub.summarizer.qq_digest.refine_with_dashscope") as refine:
            digest = build_digest(settings, [record("m1", "请各班班长今天18:00前提交材料")], now=NOW)
        refine.assert_not_called()
        self.assertEqual(digest.llm_error, "未配置 DASHSCOPE_API_KEY")

    def test_llm_failure_falls_back(self) -> None:
        settings = settings_for(
            dashscope_api_key="sk-invalid",
            dashscope_endpoint="http://127.0.0.1:9/does-not-exist",
            http_timeout=5,
            llm_max_retries=0,
            llm_retry_backoff=0.0,
            llm_backend="openai",
        )
        digest = build_digest(settings, [record("m1", "请各班班长今天18:00前提交材料")], now=NOW)
        self.assertEqual(len(digest.items), 1)
        self.assertFalse(digest.llm_used)
        self.assertTrue(digest.llm_error)
        self.assertTrue(digest.llm_retryable)
        self.assertIn("规则筛选", digest.body)

    def test_ollama_backend_does_not_call_codebuddy(self) -> None:
        settings = settings_for(
            dashscope_api_key="sk-test",
            dashscope_endpoint="http://127.0.0.1:9/does-not-exist",
            llm_backend="ollama",
            llm_max_retries=0,
            llm_retry_backoff=0.0,
        )
        with mock.patch("qq_digest.run_codebuddy") as codebuddy:
            build_digest(settings, [record("m1", "请各班班长今天18:00前提交材料")], now=NOW)
        codebuddy.assert_not_called()

    def test_llm_active_depends_on_backend(self) -> None:
        cases = (("codebuddy", True), ("ollama", False))
        for backend, expected in cases:
            with self.subTest(backend=backend):
                settings = Settings(llm_backend=backend, dashscope_api_key="")
                if expected:
                    self.assertTrue(settings.llm_active)
                else:
                    self.assertFalse(settings.llm_active)

    def test_llm_summary_is_used_and_raw_text_is_hidden(self) -> None:
        stub = StubLlm({"items": [STUB_ITEM]}).start()
        self.addCleanup(stub.stop)
        settings = settings_for(
            dashscope_api_key="sk-test",
            dashscope_endpoint=stub.endpoint,
            http_timeout=5,
            llm_backend="openai",
        )
        records = [record("m1", "请各班班长今天18:00前提交材料，这段原文不应该出现在推送中。")]
        digest = build_digest(settings, records, now=NOW)
        self.assertTrue(digest.llm_used)
        self.assertIn("班长今晚18:00前提交材料", digest.body)
        self.assertNotIn("这段原文不应该出现在推送中", digest.body)

    def test_dedupe_similar_messages(self) -> None:
        # 只差一个句号的同文消息算同一条。
        same_twice = [
            record("m1", "请各班班长今天18:00前提交材料"),
            record("m2", "请各班班长今天18:00前提交材料。"),
        ]
        digest = build_digest(self.settings, same_twice, now=NOW)
        self.assertEqual(len(digest.items), 1)
        self.assertEqual(digest.message_count, 2)

    def test_real_notice_deadlines_keep_time_of_day(self) -> None:
        records = [
            record("m1", "并在明天9月29日下午五点以前填写完以下上传的附件2表格。", sender="学委"),
            record("m2", "这是明天入场的25人名单，相关人员请提前到达，在18:30完成入场", sender="欧学武"),
            record("m3", "明天下午三点半在教室集合", sender="班长"),
        ]
        payload = build_digest(self.settings, records, now=NOW).payload()
        deadlines = {item["msg_id"]: item["deadline"] for item in payload}
        expected = (("m1", "2026-09-29 17:00"), ("m2", "2026-09-29 18:30"), ("m3", "2026-09-29 15:30"))
        for msg_id, want in expected:
            with self.subTest(msg_id=msg_id):
                self.assertIn(want, deadlines[msg_id])

    def test_evening_word_implies_pm(self) -> None:
        payload = build_digest(self.settings, [record("m1", "今晚8点开班会")], now=NOW).payload()
        self.assertIn("2026-09-28 20:00", payload[0]["deadline"])

    def test_push_uses_short_summary_and_hides_raw_text(self) -> None:
        digest = Digest(
            kind="window",
            window_start=NOW,
            window_end=NOW,
            message_count=1,
            groups=[GROUP],
            llm_used=True,
            items=[push_item()],
        )
        body = format_push_text(digest, self.settings)
        self.assertIn("今晚18:00前班长提交材料", body)
        self.assertNotIn("原文：", body)
        self.assertNotIn("绝对不应该出现", body)

        raw_body = format_push_text(digest, settings_for(include_raw=True))
        self.assertIn("原文：", raw_body)

    def test_push_summary_shows_deadline_and_extra_count(self) -> None:
        records = [
            record("m1", "请各班班长今天18:00前提交材料"),
            record("m2", "【通知】图书馆周末闭馆"),
        ]
        digest = build_digest(self.settings, records, now=NOW)
        self.assertEqual(digest.summary, push_summary(digest))
        self.assertIn("今天18:00", digest.summary)

    def test_html_push_escapes_text_and_marks_actions(self) -> None:
        records = [record("m1", "请各班班长今天18:00前提交<script>材料")]
        digest = build_digest(self.settings, records, now=NOW)
        html = html_push_text(digest, self.settings)
        self.assertIn("#fff8ec", html)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertEqual(digest.html, html)

    def test_evidence_sentence_prefers_deadline_line(self) -> None:
        evidence = evidence_sentence("大家好。请各班班长今天18:00前把材料交到学工办。谢谢。")
        self.assertIn("18:00", evidence)
        self.assertNotIn("谢谢", evidence)

    def test_payload_to_item_keeps_deadline_and_evidence(self) -> None:
        records = [record("m1", "请班长今天18:00前提交材料")]
        digest = build_digest(self.settings, records, now=NOW)
        entry = dict(digest.payload()[0], created_at=iso(NOW))
        item = payload_to_item(entry)
        self.assertEqual(item["summary"], digest.items[0]["summary"])
        self.assertEqual(item["deadline_dt"], digest.items[0]["deadline_dt"])
        self.assertTrue(item["evidence"])

    def test_payload_keeps_structured_audience_and_details(self) -> None:
        details = ["转专业同学先看成绩", "其他同学也要看成绩"]
        item = {
            "message": Message(timestamp=NOW, sender="辅导员", text="原文", source="测试群"),
            "msg_id": "m1",
            "group_name": "测试群",
            "category": "action",
            "importance": 4,
            "summary": "两类同学都要处理",
            "audience": "刚转专业同学；其他同学",
            "condition": "没有成绩时",
            "details": details,
            "action": "查看并预约",
            "evidence": "原文",
            "deadline_dt": NOW,
        }
        digest = Digest(kind="window", window_start=NOW, window_end=NOW, items=[item])
        payload = digest.payload()[0]
        self.assertEqual(payload["audience"], "刚转专业同学；其他同学")
        self.assertEqual(payload["condition"], "没有成绩时")
        self.assertEqual(payload["details"], details)

    def test_question_sentence_has_no_deadline(self) -> None:
        message = msg("@同学 明天几点正式表演？", sender="同学", source="闲聊群")
        self.assertIsNone(extract_deadline(message))

    def test_colloquial_question_is_not_urgent_or_deadline(self) -> None:
        message = msg(
            text="不会的吧，我今天问4栋那个快递站明天可以寄快递吗他还说可以的",
            source="电气工程学院2026级新生交流群",
            sender="26测仪",
        )
        analysis = analyze_message(message)
        self.assertTrue(analysis["colloquial_question"])
        self.assertNotEqual(analysis["category"], "urgent")
        self.assertIsNone(extract_deadline(message))

    def test_directive_question_still_keeps_deadline(self) -> None:
        message = msg("请大家明天下午5点前交材料，好吗？")
        self.assertEqual(extract_deadline(message), dt.datetime(2026, 9, 29, 17, 0))

    def test_quiet_group_chatter_is_dropped_but_real_notice_is_kept(self) -> None:
        group = "电气工程学院2026级新生交流群"
        settings = settings_for(group_aliases={"g1": group}, quiet_groups=("g1",))
        chatter = [
            record("m1", "国庆节快递站会放假吗", sender="26电气", group_name=group),
            record(
                "m2",
                "不会的吧，我今天问4栋那个快递站明天可以寄快递吗他还说可以的",
                sender="26测仪",
                group_name=group,
            ),
            record("m3", "明天还没开始放假昂👀", sender="26电气", group_name=group),
        ]
        self.assertEqual(build_digest(settings, chatter, now=NOW).items, [])
        self.assertEqual(urgent_for(chatter, settings), [])

        notice = [record("m4", "请各班班长明天18:00前提交材料", sender="辅导员", group_name=group)]
        self.assertEqual(len(build_digest(settings, notice, now=NOW).items), 1)
        self.assertEqual(urgent_for(notice, settings), [])

    def test_items_over_limit_are_counted_not_silently_dropped(self) -> None:
        """被 max_items 截掉的部分要写进正文，用户才知道还有内容没展开。"""
        settings = settings_for(max_items=2)
        records = [
            record("m1", "请各班今天18:00前提交材料"),
            record("m2", "明天9点开会，请准时参加"),
            record("m3", "四六级报名截止9月30日"),
            record("m4", "周五前交实验报告"),
            record("m5", "请填写问卷，今晚截止"),
        ]
        digest = build_digest(settings, records, now=NOW)
        total_focus = len(analyse_records(records))
        self.assertGreater(total_focus, 2)
        self.assertEqual(len(digest.items), 2)
        self.assertEqual(digest.omitted_count, total_focus - 2)
        omitted_hint = f"另有 {digest.omitted_count} 条超出单次上限未展开"
        for text in (digest.body, format_push_text(digest, settings)):
            with self.subTest(text=text[:12]):
                self.assertIn(omitted_hint, text)

    def test_digest_without_truncation_reports_zero_omitted(self) -> None:
        records = [record("m1", "请各班今天18:00前提交材料")]
        digest = build_digest(settings_for(), records, now=NOW)
        self.assertEqual(digest.omitted_count, 0)
        self.assertNotIn("未展开", digest.body)

    def test_date_range_uses_end_date(self) -> None:
        message = msg("培训考试时间：2026年9月29日—2026年10月31日，请按时完成。", source="学院群")
        self.assertEqual(extract_deadline(message), dt.datetime(2026, 10, 31, 23, 59))


class ParseTimestampTest(unittest.TestCase):
    def test_end_of_day_24_hour_normalizes_to_next_midnight(self) -> None:
        """群里的「24:00 截止」不是 24 点，而是第二天零点；按原样解析会整条丢掉。"""
        self.assertEqual(parse_timestamp("2026-10-09 24:00"), dt.datetime(2026, 10, 10, 0, 0))
        self.assertEqual(parse_timestamp("2026-10-09T24:00:00"), dt.datetime(2026, 10, 10, 0, 0))
        year = dt.datetime.now().year
        self.assertEqual(parse_timestamp("10月9日 24:00"), dt.datetime(year, 10, 10, 0, 0))

    def test_invalid_hours_still_rejected(self) -> None:
        for text in ("2026-10-09 24:30", "2026-13-40 10:00"):
            with self.subTest(text=text):
                self.assertIsNone(parse_timestamp(text))


if __name__ == "__main__":
    unittest.main()
