from __future__ import annotations

import datetime as dt
import json
import http.server
import sys
import unittest
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest.config import Settings  # noqa: E402
from qq_live_digest.summarizer import (  # noqa: E402
    Digest,
    analyse_records,
    build_digest,
    evidence_sentence,
    format_push_text,
    html_push_text,
    payload_to_item,
    preview_text,
    push_summary,
    urgent_analyses,
)
from qq_digest import Message, extract_deadline  # noqa: E402
from qq_digest import analyze_message  # noqa: E402
from qq_live_digest.timeutil import iso, now_local  # noqa: E402

NOW = dt.datetime(2026, 9, 28, 18, 0, 0)


def make_record(
    msg_id: str,
    content: str,
    sender: str = "辅导员",
    minutes_ago: int = 0,
    group_name: str = "学院通知群",
) -> dict:
    stamp = NOW - dt.timedelta(minutes=minutes_ago)
    return {
        "msg_id": msg_id,
        "source": "qqbot",
        "event": "GROUP_MESSAGE_CREATE",
        "group_id": "g1",
        "group_name": group_name,
        "sender_id": "u1",
        "sender_name": sender,
        "ts": iso(stamp),
        "received_at": iso(stamp),
        "content": content,
    }


class SummarizerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(group_whitelist=("g1",), group_aliases={"g1": "学院通知群"})

    def test_notice_kept_and_chatter_dropped(self) -> None:
        records = [
            make_record("m1", "请各班班长今天18:00前提交材料"),
            make_record("m2", "哈哈哈哈", sender="同学A"),
            make_record("m3", "有人一起去图书馆吗", sender="同学B"),
        ]
        digest = build_digest(self.settings, records, now=NOW)
        self.assertEqual([item["msg_id"] for item in digest.items], ["m1"])
        self.assertIn("截止", digest.body)
        self.assertTrue(digest.items[0].get("action"))
        self.assertIn("截止", digest.body)
        self.assertIn("学院通知群", digest.body)

    def test_deadline_and_category_in_payload(self) -> None:
        digest = build_digest(self.settings, [make_record("m1", "四六级报名截止时间是9月30日")], now=NOW)
        payload = digest.payload()
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["category"], "urgent")
        self.assertIn("2026-09-30", payload[0]["deadline"])
        json.dumps(payload, ensure_ascii=False)  # 必须可序列化

    def test_plain_notice_without_deadline_is_kept(self) -> None:
        digest = build_digest(self.settings, [make_record("m1", "【学院通知】关于2026年国庆节放假安排的通知")], now=NOW)
        self.assertEqual(len(digest.items), 1)

    def test_urgent_requires_substance(self) -> None:
        chatter = analyse_records([make_record("m1", "今天天气不错", sender="同学A")])
        self.assertEqual(chatter[0]["category"], "urgent")
        self.assertEqual(urgent_analyses(chatter, self.settings, now=NOW), [])

        real = analyse_records([make_record("m2", "请各班班长今天18:00前提交材料")])
        self.assertEqual(len(urgent_analyses(real, self.settings, now=NOW)), 1)

    def test_urgent_disabled(self) -> None:
        settings = Settings(group_whitelist=("g1",), urgent_immediate=False)
        analyses = analyse_records([make_record("m1", "紧急通知：请务必马上提交材料")])
        self.assertEqual(urgent_analyses(analyses, settings, now=NOW), [])

    def test_no_focus_message_gives_empty_body(self) -> None:
        digest = build_digest(self.settings, [make_record("m1", "收到")], now=NOW)
        self.assertFalse(digest.items)
        self.assertEqual(digest.body, "")
        self.assertIn("不会推送", preview_text(self.settings, [make_record("m1", "收到")]))

    def test_preview_reports_pending(self) -> None:
        text = preview_text(self.settings, [make_record("m1", "请各班班长今天18:00前提交材料")])
        self.assertIn("[仅预览]", text)
        self.assertIn("18:00", text)

    def test_missing_llm_key_reports_local_fallback(self) -> None:
        settings = Settings(group_whitelist=("g1",), dashscope_api_key="")
        digest = build_digest(settings, [make_record("m1", "请各班班长今天18:00前提交材料")], now=NOW)
        self.assertFalse(digest.llm_used)
        self.assertIn("未配置", digest.llm_error)
        # 配置缺失不应当被当成可重试故障，否则消息会被无限推迟。
        self.assertFalse(digest.llm_retryable)
        self.assertIn("本地规则筛选", digest.body)

    def test_llm_failure_falls_back(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            dashscope_api_key="sk-invalid",
            dashscope_endpoint="http://127.0.0.1:9/does-not-exist",
            http_timeout=5,
            llm_max_retries=0,
            llm_retry_backoff=0.0,
        )
        digest = build_digest(settings, [make_record("m1", "请各班班长今天18:00前提交材料")], now=NOW)
        self.assertEqual(len(digest.items), 1)
        self.assertFalse(digest.llm_used)
        self.assertTrue(digest.llm_error)
        self.assertTrue(digest.llm_retryable)
        self.assertIn("规则筛选", digest.body)

    def test_dedupe_similar_messages(self) -> None:
        records = [
            make_record("m1", "请各班班长今天18:00前提交材料"),
            make_record("m2", "请各班班长今天18:00前提交材料。"),
        ]
        digest = build_digest(self.settings, records, now=NOW)
        self.assertEqual(len(digest.items), 1)
        self.assertEqual(digest.message_count, 2)

    def test_real_notice_deadlines_keep_time_of_day(self) -> None:
        records = [
            make_record("m1", "并在明天9月29日下午五点以前填写完以下上传的附件2表格。", sender="学委"),
            make_record("m2", "这是明天入场的25人名单，相关人员请提前到达，在18:30完成入场", sender="欧学武"),
            make_record("m3", "明天下午三点半在教室集合", sender="班长"),
        ]
        payload = build_digest(self.settings, records, now=NOW).payload()
        deadlines = {item["msg_id"]: item["deadline"] for item in payload}
        self.assertIn("2026-09-29 17:00", deadlines["m1"])
        self.assertIn("2026-09-29 18:30", deadlines["m2"])
        self.assertIn("2026-09-29 15:30", deadlines["m3"])

    def test_push_uses_short_summary_and_hides_raw_text(self) -> None:
        message = Message(
            timestamp=NOW,
            sender="辅导员",
            text="请各班班长今天18:00前提交材料，这是一段绝对不应该出现在推送正文里的原文内容。",
            source="学院通知群",
        )
        item = {
            "message": message,
            "category": "urgent",
            "score": 5,
            "importance": 5,
            "summary": "今晚18:00前班长提交材料",
            "action": "班长提交材料",
            "deadline_dt": NOW.replace(hour=18, minute=0),
            "group_name": "学院通知群",
        }
        digest = Digest(
            kind="window",
            window_start=NOW,
            window_end=NOW,
            items=[item],
            message_count=1,
            groups=["学院通知群"],
            llm_used=True,
        )
        body = format_push_text(digest, self.settings)
        self.assertIn("今晚18:00前班长提交材料", body)
        self.assertNotIn("原文：", body)
        self.assertNotIn("绝对不应该出现", body)

        raw_body = format_push_text(
            digest,
            Settings(group_whitelist=("g1",), include_raw=True),
        )
        self.assertIn("原文：", raw_body)

    def test_llm_summary_is_used_and_raw_text_is_hidden(self) -> None:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                content = json.dumps(
                    {
                        "items": [
                            {
                                "id": 1,
                                "category": "action",
                                "importance": 5,
                                "summary": "班长今晚18:00前提交材料",
                                "action": "班长提交材料",
                                "deadline": "2026-09-28 18:00",
                                "keep": True,
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
                body = json.dumps(
                    {"choices": [{"message": {"content": content}}]},
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)

        port = server.server_address[1]
        settings = Settings(
            group_whitelist=("g1",),
            dashscope_api_key="sk-test",
            dashscope_endpoint=f"http://127.0.0.1:{port}/v1/chat/completions",
            http_timeout=5,
        )
        digest = build_digest(
            settings,
            [make_record("m1", "请各班班长今天18:00前提交材料，这段原文不应该出现在推送中。")],
            now=NOW,
        )
        self.assertTrue(digest.llm_used)
        self.assertIn("班长今晚18:00前提交材料", digest.body)
        self.assertNotIn("这段原文不应该出现在推送中", digest.body)

    def test_evening_word_implies_pm(self) -> None:
        payload = build_digest(self.settings, [make_record("m1", "今晚8点开班会")], now=NOW).payload()
        self.assertIn("2026-09-28 20:00", payload[0]["deadline"])

    def test_push_summary_shows_deadline_and_extra_count(self) -> None:
        records = [
            make_record("m1", "请各班班长今天18:00前提交材料"),
            make_record("m2", "【通知】图书馆周末闭馆"),
        ]
        digest = build_digest(self.settings, records, now=NOW)
        self.assertEqual(digest.summary, push_summary(digest))
        self.assertIn("今天18:00", digest.summary)

    def test_html_push_escapes_text_and_marks_actions(self) -> None:
        digest = build_digest(
            self.settings,
            [make_record("m1", "请各班班长今天18:00前提交<script>材料")],
            now=NOW,
        )
        html = html_push_text(digest, self.settings)
        self.assertIn("#fff8ec", html)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertEqual(digest.html, html)

    def test_evidence_sentence_prefers_deadline_line(self) -> None:
        text = "大家好。请各班班长今天18:00前把材料交到学工办。谢谢。"
        evidence = evidence_sentence(text)
        self.assertIn("18:00", evidence)
        self.assertNotIn("谢谢", evidence)

    def test_payload_to_item_keeps_deadline_and_evidence(self) -> None:
        digest = build_digest(self.settings, [make_record("m1", "请班长今天18:00前提交材料")], now=NOW)
        entry = dict(digest.payload()[0], created_at=iso(NOW))
        item = payload_to_item(entry)
        self.assertEqual(item["summary"], digest.items[0]["summary"])
        self.assertEqual(item["deadline_dt"], digest.items[0]["deadline_dt"])
        self.assertTrue(item["evidence"])

    def test_payload_keeps_structured_audience_and_details(self) -> None:
        item = {
            "message": Message(timestamp=NOW, sender="辅导员", text="原文", source="测试群"),
            "category": "action",
            "importance": 4,
            "summary": "两类同学都要处理",
            "audience": "刚转专业同学；其他同学",
            "condition": "没有成绩时",
            "details": ["转专业同学先看成绩", "其他同学也要看成绩"],
            "action": "查看并预约",
            "evidence": "原文",
            "deadline_dt": NOW,
            "group_name": "测试群",
            "msg_id": "m1",
        }
        digest = Digest(kind="window", window_start=NOW, window_end=NOW, items=[item])
        payload = digest.payload()[0]
        self.assertEqual(payload["audience"], "刚转专业同学；其他同学")
        self.assertEqual(payload["condition"], "没有成绩时")
        self.assertEqual(payload["details"], ["转专业同学先看成绩", "其他同学也要看成绩"])

    def test_question_sentence_has_no_deadline(self) -> None:
        message = Message(timestamp=NOW, sender="同学", text="@同学 明天几点正式表演？", source="闲聊群")
        self.assertIsNone(extract_deadline(message))

    def test_colloquial_question_is_not_urgent_or_deadline(self) -> None:
        message = Message(
            timestamp=NOW,
            sender="26测仪",
            text="不会的吧，我今天问4栋那个快递站明天可以寄快递吗他还说可以的",
            source="电气工程学院2026级新生交流群",
        )
        analysis = analyze_message(message)
        self.assertTrue(analysis["colloquial_question"])
        self.assertNotEqual(analysis["category"], "urgent")
        self.assertIsNone(extract_deadline(message))

    def test_directive_question_still_keeps_deadline(self) -> None:
        message = Message(
            timestamp=NOW,
            sender="辅导员",
            text="请大家明天下午5点前交材料，好吗？",
            source="学院通知群",
        )
        self.assertEqual(extract_deadline(message), dt.datetime(2026, 9, 29, 17, 0))

    def test_quiet_group_chatter_is_dropped_but_real_notice_is_kept(self) -> None:
        settings = Settings(
            group_whitelist=("g1",),
            group_aliases={"g1": "电气工程学院2026级新生交流群"},
            quiet_groups=("g1",),
        )
        chatter = [
            make_record("m1", "国庆节快递站会放假吗", sender="26电气", group_name="电气工程学院2026级新生交流群"),
            make_record("m2", "不会的吧，我今天问4栋那个快递站明天可以寄快递吗他还说可以的", sender="26测仪", group_name="电气工程学院2026级新生交流群"),
            make_record("m3", "明天还没开始放假昂👀", sender="26电气", group_name="电气工程学院2026级新生交流群"),
        ]
        self.assertEqual(build_digest(settings, chatter, now=NOW).items, [])
        self.assertEqual(urgent_analyses(analyse_records(chatter), settings, now=NOW), [])

        notice = [make_record(
            "m4",
            "请各班班长明天18:00前提交材料",
            sender="辅导员",
            group_name="电气工程学院2026级新生交流群",
        )]
        digest = build_digest(settings, notice, now=NOW)
        self.assertEqual(len(digest.items), 1)
        self.assertEqual(urgent_analyses(analyse_records(notice), settings, now=NOW), [])

    def test_date_range_uses_end_date(self) -> None:
        message = Message(
            timestamp=NOW,
            sender="辅导员",
            text="培训考试时间：2026年9月29日—2026年10月31日，请按时完成。",
            source="学院群",
        )
        self.assertEqual(extract_deadline(message), dt.datetime(2026, 10, 31, 23, 59))


if __name__ == "__main__":
    unittest.main()
