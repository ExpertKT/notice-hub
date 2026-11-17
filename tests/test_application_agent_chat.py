"""待办长对话：多轮、确认、对不上不改。不打真模型。"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from noticehub.application.agent_chat import (
    AGENT_CHAT_PATH,
    _SYSTEM,
    _coerce_deadline,
    chat_history,
    chat_ready,
    chat_turn,
    optional_get,
    optional_post,
    reset_sessions,
)
from noticehub.infrastructure.sqlite.store import Store
from noticehub.routes import POST_ROUTES
from noticehub.timeutil import now_local

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _Settings:
    def __init__(self, **extra):
        self.llm_enabled = True
        self.llm_backend = "auto"
        self.dashscope_api_key = ""
        self.dashscope_model = "unit"
        self.dashscope_endpoint = "http://127.0.0.1/v1/chat/completions"
        self.llm_timeout = 10
        self.deadline_reminders_enabled = False
        for key, value in extra.items():
            setattr(self, key, value)


def _reply(say, calls=None):
    return json.dumps({"say": say, "calls": calls or []}, ensure_ascii=False)


class AgentChatTurnTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_sessions()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "chat.sqlite3")
        self.settings = _Settings(dashscope_api_key="sk-test")

    def test_second_turn_sees_the_first(self) -> None:
        seen = []

        def poster(messages):
            seen.append(messages)
            return _reply("看到了")

        first = chat_turn(self.store, self.settings, "session1234", "明天那件是什么", poster=poster)
        chat_turn(self.store, self.settings, first["session_id"], "再说具体点", poster=poster)
        self.assertEqual([item["content"] for item in seen[1] if item["role"] == "user"], ["明天那件是什么", "再说具体点"])

    def test_trace_shows_what_it_read_and_called(self) -> None:
        # 界面上要能看见它读了什么、调了哪个工具：第一跳调 create，第二跳收尾
        replies = [
            _reply("先记一条", [{"name": "create", "summary": "交实验报告 基础实验楼B203", "deadline": "2026-11-19 15:00"}]),
            _reply("记好了"),
        ]
        result = chat_turn(
            self.store, self.settings, "session1234", "周四下午去基础实验楼交实验报告",
            poster=lambda messages: replies.pop(0),
        )
        kinds = [item["kind"] for item in result["trace"]]
        self.assertEqual(kinds[0], "read")
        self.assertIn("读了", result["trace"][0]["text"])
        calls = [item for item in result["trace"] if item["kind"] == "call"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["tool"], "create")
        self.assertTrue(calls[0]["ok"])
        self.assertIn("基础实验楼", calls[0]["args"].get("summary", ""))

    def test_route_tools_return_numbers_to_model_and_trace(self) -> None:
        from tests.test_route_tools import POIS

        self.settings.data_dir = self.tmp.name
        (Path(self.tmp.name) / 'campus_poi.json').write_text(json.dumps(POIS), encoding='utf-8')
        task_id = self.store.upsert_task(task_key='manual:route', summary='中间楼交表', source='manual', status='open')
        calls = [
            {'name': 'poi_near', 'place': '起点楼', 'radius_m': 300},
            {'name': 'plan_route', 'points': ['起点楼', '远处楼', '中间楼']},
        ]
        def poster(messages):
            if messages[-1]['content'].startswith('工具结果：'):
                outcomes = json.loads(messages[-1]['content'].split('：', 1)[1])
                self.assertEqual(outcomes[0]['result']['tasks'][0]['task_id'], task_id)
                self.assertEqual(outcomes[1]['result']['saved_m'], 300)
                return _reply('')  # 模型没总结时仍保留工具结论
            self.assertIn('起点楼', messages[1]['content'])
            self.assertIn('plan_route', messages[0]['content'])
            return _reply('算一下', calls)
        result = chat_turn(self.store, self.settings, 'session1234', '第三件事顺路吗', poster=poster)
        trace = [row for row in result['trace'] if row['kind'] == 'call']
        self.assertEqual([row['tool'] for row in trace], ['poi_near', 'plan_route'])
        self.assertEqual(trace[1]['args']['points'], calls[1]['points'])
        self.assertEqual(trace[1]['result']['distance_m'], 600)
        self.assertIn('300', trace[1]['detail'])
        self.assertIn('300', result['say'])
        self.assertFalse(result['changed'])
        self.assertEqual(self.store.get_task(task_id)['status'], 'open')

    def test_route_no_saving(self) -> None:
        from tests.test_route_tools import POIS

        self.settings.data_dir = self.tmp.name
        (Path(self.tmp.name) / 'campus_poi.json').write_text(json.dumps(POIS), encoding='utf-8')
        replies = [_reply('规划路线', [dict(name='plan_route', points=['起点楼', '中间楼', '远处楼'])]), _reply('')]
        result = chat_turn(self.store, self.settings, 'session1234', '算一下路线', poster=lambda messages: replies.pop(0))
        call = next(row for row in result['trace'] if row['kind'] == 'call')
        self.assertEqual(call['result']['saved_m'], 0)
        self.assertIn('和输入顺序一致，没有更顺的顺序', call['detail'])
        self.assertNotIn('比输入顺序少', call['detail'])
        self.assertEqual(result['say'], call['detail'])

    def test_nearby_then_route_second_round_returns_computed_conclusion(self) -> None:
        from tests.test_route_tools import POIS

        self.settings.data_dir = self.tmp.name
        (Path(self.tmp.name) / 'campus_poi.json').write_text(json.dumps(POIS), encoding='utf-8')
        self.store.upsert_task(task_key='manual:third', summary='中间楼交表', source='manual', status='open')
        def poster(messages):
            if not messages[-1]['content'].startswith('工具结果：'):
                return _reply('先找附近的事', [dict(name='poi_near', place='起点楼', radius_m=300)])
            nearby = json.loads(messages[-1]['content'].split('：', 1)[1])[0]['result']
            return _reply('', [dict(name='plan_route', points=['起点楼', '远处楼', nearby['tasks'][0]['place']])])
        result = chat_turn(self.store, self.settings, 'session1234', '第三件事顺路办', poster=poster)
        self.assertIn('600', result['say'])
        self.assertIn('300', result['say'])
        self.assertEqual([row['tool'] for row in result['trace'] if row['kind'] == 'call'], ['poi_near', 'plan_route'])
        self.assertFalse(result['changed'])

    def test_route_failure_is_readable_and_returned_to_model(self) -> None:
        for call in [dict(name='plan_route', points=[]),
                     dict(name='plan_route', points=['不存在楼']),
                     dict(name='poi_near', place='图书馆', radius_m=-1)]:
            with self.subTest(call=call):
                def poster(messages):
                    if messages[-1]['content'].startswith('工具结果：'):
                        outcome = json.loads(messages[-1]['content'].split('：', 1)[1])[0]
                        self.assertFalse(outcome['ok'])
                        self.assertTrue(outcome['detail'])
                        return _reply('')
                    return _reply('', [call])
                result = chat_turn(self.store, self.settings, 'session1234', '算一下路线', poster=poster)
                trace = [row for row in result['trace'] if row['kind'] == 'call'][0]
                self.assertFalse(trace['ok'])
                self.assertFalse(result['changed'])
                self.assertEqual(result['say'], trace['detail'])
                expected = 'radius_m' if call['name'] == 'poi_near' else ('points' if not call['points'] else '找不到')
                self.assertIn(expected, trace['detail'])

    def test_day_plan_reuses_today_plan_and_returns_complete_result(self) -> None:
        from unittest.mock import patch
        from demo.seed import TASKS, LESSONS
        from noticehub.application.agent_plan import today_plan, save_schedule, save_routines

        self.settings.data_dir = self.tmp.name
        clock = now_local().replace(hour=12, minute=0, second=0, microsecond=0)
        for index, summary in enumerate(TASKS):
            self.store.upsert_task(task_key=f'manual:day{index}', summary=summary, source='manual', status='open')
        weekday = '一二三四五六日'[clock.weekday()]
        save_schedule(self.settings, {'text': '\n'.join(f'周{weekday} {lesson}' for lesson in LESSONS)})
        save_routines(self.settings, {'action': 'add', 'title': '一食堂 吃饭', 'minutes': 30})
        before = self.store.list_tasks()
        seen = []
        def poster(messages):
            if messages[-1]['content'].startswith('工具结果：'):
                seen.extend(json.loads(messages[-1]['content'].split('：', 1)[1]))
                return _reply('')
            return _reply('', [{'name': 'day_plan'}])
        with patch('noticehub.application.agent_plan.now_local', return_value=clock):
            expected = today_plan(self.store, self.settings)
            result = chat_turn(self.store, self.settings, 'session1234', '下午上完课，把今天要办的事排一下行程，别让我来回跑', poster=poster)
        self.assertEqual(len(expected['trips']), 2)
        self.assertTrue(seen[0]['ok'])
        self.assertEqual(seen[0]['result'], expected)
        calls = [item for item in result['trace'] if item['kind'] == 'call']
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['tool'], 'day_plan')
        self.assertEqual(calls[0]['result'], expected)
        counts = expected['counts']
        self.assertEqual(calls[0]['detail'], '排了今天的行程：%s 趟、%s 站、%s 米' % (counts['trips'], counts['stops'], counts['distance_m']))
        self.assertEqual(result['say'], calls[0]['detail'])
        self.assertFalse(result['changed'])
        self.assertEqual(self.store.list_tasks(), before)

    def test_post_tool_reply_survives_later_tool_summary_and_empty_reply(self) -> None:
        from unittest.mock import patch

        self.settings.data_dir = self.tmp.name
        task_id = self.store.upsert_task(task_key='manual:move', summary='取生活用品 学生公寓A', source='manual', status='open')
        spoken = '已将取生活用品改到明天下午 15:00。'
        for rounds in (2, 3):
            with self.subTest(rounds=rounds):
                replies = [
                    _reply('准备改期', [dict(name='reschedule', task_id=task_id, deadline='2026-11-17T15:00')]),
                    _reply(spoken, [dict(name='day_plan')]),
                    _reply(''),
                ]
                with patch('noticehub.application.agent_chat._ROUNDS', rounds):
                    result = chat_turn(self.store, self.settings, f'sessionround{rounds}', '把取生活用品改到明天下午', poster=lambda messages: replies.pop(0))
                self.assertEqual(result['say'], spoken)
                self.assertTrue(result['changed'])
                self.assertTrue(self.store.get_task(task_id)['deadline'].startswith('2026-11-17T15:00'))
                self.assertEqual([item['tool'] for item in result['trace'] if item['kind'] == 'call'], ['reschedule', 'day_plan'])

    def test_day_plan_without_lessons_or_tasks_explains_empty_plan(self) -> None:
        self.settings.data_dir = self.tmp.name
        replies = [_reply('', [{'name': 'day_plan'}]), _reply('')]
        result = chat_turn(self.store, self.settings, 'session1234', '今天怎么安排', poster=lambda messages: replies.pop(0))
        call = next(item for item in result['trace'] if item['kind'] == 'call')
        self.assertTrue(call['ok'])
        self.assertEqual(call['result']['trips'], [])
        self.assertEqual(call['result']['counts'], {'trips': 0, 'stops': 0, 'distance_m': 0})
        self.assertTrue(call['result']['checks'])
        self.assertIn('没有可排的行程', result['say'])
        self.assertFalse(result['changed'])
        self.assertEqual(self.store.list_tasks(), [])

    def test_unknown_tool_is_rejected_and_failure_reaches_model(self) -> None:
        seen = []
        def poster(messages):
            if messages[-1]['content'].startswith('工具结果：'):
                seen.extend(json.loads(messages[-1]['content'].split('：', 1)[1]))
                return _reply('')
            return _reply('', [{'name': 'unknown_plan'}])
        result = chat_turn(self.store, self.settings, 'session1234', '今天怎么安排', poster=poster)
        self.assertEqual(seen, [{'ok': False, 'changed': False, 'detail': '不能做这个'}])
        self.assertEqual(result['say'], '不能做这个')
        self.assertFalse(result['changed'])
        self.assertFalse(next(item for item in result['trace'] if item['kind'] == 'call')['ok'])
        self.assertEqual(self.store.list_tasks(), [])

    def test_fallback_has_empty_trace(self) -> None:
        """没有模型也不再是空回复：走本地规则，trace 第一步自报「按本地规则」。"""
        settings = _Settings(llm_enabled=False, dashscope_api_key="")
        result = chat_turn(self.store, settings, "session1234", "记一件事", poster=lambda m: "")
        self.assertTrue(result["ok"])
        self.assertTrue(result["fallback"])
        self.assertEqual(result["trace"][0]["kind"], "rule")
        self.assertIn("本地规则", result["trace"][0]["text"])
        self.assertTrue(result["changed"])
        self.assertEqual([task["summary"] for task in self.store.list_tasks()], ["记一件事"])

    def test_no_key_query_counts_locally(self) -> None:
        """没钥匙也能查：件数与最近截止都从 store 出来。"""
        settings = _Settings(llm_enabled=False, dashscope_api_key="")
        self.store.upsert_task(task_key="manual:a", summary="交实验报告", source="manual", status="open", deadline="2026-11-17T18:00")
        self.store.upsert_task(task_key="manual:b", summary="取快递", source="manual", status="open")
        result = chat_turn(self.store, settings, "sessionquery", "我今天还有哪些事", poster=lambda m: "")
        self.assertTrue(result["fallback"])
        self.assertFalse(result["changed"])
        self.assertIn("2 件", result["say"])
        self.assertIn("交实验报告", result["say"])
        self.assertIn("2026-11-17T18:00", result["say"])

    def test_no_key_reschedule_refuses_to_guess(self) -> None:
        """两条都对得上就不猜：反问，谁都不改。"""
        settings = _Settings(llm_enabled=False, dashscope_api_key="")
        self.store.upsert_task(task_key="manual:left", summary="劳动课上午", source="manual", status="open")
        self.store.upsert_task(task_key="manual:right", summary="劳动课下午", source="manual", status="open")
        result = chat_turn(self.store, settings, "sessionambig", "把劳动课改到明天下午", poster=lambda m: "")
        self.assertTrue(result["fallback"])
        self.assertFalse(result["changed"])
        self.assertIn("劳动课", result["say"])
        self.assertIn("说清是哪一件", result["say"])
        self.assertFalse(any(task.get("deadline") for task in self.store.list_tasks()))

    def test_no_key_unknown_sentence_says_where_the_key_goes(self) -> None:
        settings = _Settings(llm_enabled=False, dashscope_api_key="")
        result = chat_turn(self.store, settings, "sessiohelp", "帮助", poster=lambda m: "")
        self.assertTrue(result["fallback"])
        self.assertFalse(result["changed"])
        self.assertIn("llm.key", result["say"])
        self.assertEqual(self.store.list_tasks(), [])

    def test_create_says_how_it_fits_the_route(self) -> None:
        """新建的这件进了今天的行程：回一句带前一个地点和到达时刻的顺路话。"""
        with tempfile.TemporaryDirectory() as folder:
            data_dir = Path(folder)
            (data_dir / "campus_poi.json").write_text(
                json.dumps(
                    [
                        {"id": "lib", "name": "图书馆", "x": 0, "y": 0, "zone": "教学"},
                        {"id": "tw", "name": "图文信息中心", "x": 97, "y": 0, "zone": "教学"},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            settings = _Settings(dashscope_api_key="sk-test", data_dir=str(data_dir))
            self.store.upsert_task(task_key="manual:lib", summary="去图书馆还书", source="manual", status="open")
            fixed = dt.datetime(2026, 11, 17, 15, 0)
            with patch("noticehub.application.agent_plan.now_local", return_value=fixed), patch(
                "noticehub.application.agent_chat.now_local", return_value=fixed
            ):
                result = chat_turn(
                    self.store, settings, "sessionroute", "记一件：去图文信息中心还书",
                    poster=lambda messages: _reply("已记下", [{"name": "create", "summary": "去图文信息中心还书"}]),
                )
        self.assertTrue(result["changed"])
        self.assertIn("排进", result["say"])
        self.assertRegex(result["say"], r"\d{2}:\d{2}")
        self.assertTrue(any(item["kind"] == "plan" and "排程" in item["text"] for item in result["trace"]))

    def test_create_names_the_building_that_cannot_be_planned(self) -> None:
        """地点不在 POI 表：说清排不进去，并点名是哪栋楼。"""
        with tempfile.TemporaryDirectory() as folder:
            data_dir = Path(folder)
            (data_dir / "campus_poi.json").write_text(
                json.dumps([{"id": "lib", "name": "图书馆", "x": 0, "y": 0, "zone": "教学"}], ensure_ascii=False),
                encoding="utf-8",
            )
            settings = _Settings(dashscope_api_key="sk-test", data_dir=str(data_dir))
            fixed = dt.datetime(2026, 11, 17, 15, 0)
            with patch("noticehub.application.agent_plan.now_local", return_value=fixed), patch(
                "noticehub.application.agent_chat.now_local", return_value=fixed
            ):
                result = chat_turn(
                    self.store, settings, "sessionnomap", "记一件：去求是楼B203交报告",
                    poster=lambda messages: _reply("已记下", [{"name": "create", "summary": "去求是楼B203交报告"}]),
                )
        self.assertTrue(result["changed"])
        self.assertIn("排不进去", result["say"])
        self.assertIn("求是楼", result["say"])

    def test_history_can_be_read_back(self) -> None:
        missing = chat_history("missing-session-id")
        self.assertEqual(missing["messages"], [])
        first = chat_turn(
            self.store, self.settings, "session1234", "明天那件是什么",
            poster=lambda messages: _reply("看到了"),
        )
        loaded = chat_history(first["session_id"])
        self.assertEqual(
            [(item["role"], item["content"]) for item in loaded["messages"]],
            [("user", "明天那件是什么"), ("assistant", "看到了")],
        )
        self.assertEqual(chat_history("missing-session-id")["messages"], [])

    def test_plain_text_does_not_write(self) -> None:
        task_id = self.store.upsert_task(task_key="manual:aaa", summary="交实验报告", source="manual", status="open")
        result = chat_turn(
            self.store, self.settings, "session1234", "帮我看看",
            poster=lambda messages: "先看实验报告",
        )
        self.assertFalse(result["changed"])
        self.assertEqual(result["say"], "先看实验报告")
        self.assertEqual(self.store.get_task(task_id)["status"], "open")

    def test_ambiguous_title_does_not_write(self) -> None:
        left = self.store.upsert_task(task_key="manual:left", summary="劳动课上午", source="manual", status="open")
        right = self.store.upsert_task(task_key="manual:right", summary="劳动课下午", source="manual", status="open")

        def poster(messages):
            if any(str(item.get("content") or "").startswith("工具结果") for item in messages):
                return _reply("有两条")
            return _reply("我来", [{"name": "act", "task_id": "劳动课", "action": "done"}])

        result = chat_turn(self.store, self.settings, "session1234", "完成劳动课", poster=poster)
        self.assertFalse(result["changed"])
        self.assertEqual(self.store.get_task(left)["status"], "open")
        self.assertEqual(self.store.get_task(right)["status"], "open")

    def test_delete_waits_until_the_next_turn(self) -> None:
        task_id = self.store.upsert_task(task_key="manual:gone", summary="要删的待办", source="manual", status="open")

        def first(messages):
            if any(str(item.get("content") or "").startswith("工具结果") for item in messages):
                return _reply("删掉了", [{"name": "act", "task_id": str(task_id), "action": "delete", "confirm": True}])
            return _reply("要删吗", [{"name": "act", "task_id": str(task_id), "action": "delete"}])

        held = chat_turn(self.store, self.settings, "session1234", "删掉要删的待办", poster=first)
        self.assertFalse(held["changed"])
        self.assertIsNotNone(self.store.get_task(task_id))

        def second(messages):
            return _reply("已删", [{"name": "act", "task_id": str(task_id), "action": "delete", "confirm": True}])

        done = chat_turn(self.store, self.settings, held["session_id"], "删", poster=second)
        self.assertTrue(done["changed"])
        self.assertIsNone(self.store.get_task(task_id))

    def test_a_personal_plan_is_created_after_the_model_refuses(self) -> None:
        def poster(messages):
            if any(str(item.get("content") or "").startswith("上一句没有调用") for item in messages):
                return _reply("已新建", [{"name": "create", "summary": "去立德楼见学长", "deadline": "明天上午"}])
            return "好的，记下了。这不在我的待办范围内，我这边只管群待办的处理。"

        result = chat_turn(self.store, self.settings, "session1234", "我明天上午要去立德楼见一个学长", poster=poster)
        self.assertTrue(result["changed"])
        self.assertNotIn("只管群", result["say"])
        tasks = [task for task in self.store.list_tasks() if "立德楼" in str(task.get("summary") or "")]
        self.assertEqual(len(tasks), 1)
        tomorrow = (now_local() + dt.timedelta(days=1)).strftime("%Y-%m-%dT09:00")
        self.assertTrue(str(tasks[0]["deadline"]).startswith(tomorrow))

    def test_read_step_reports_todays_lessons(self) -> None:
        # 排程靠课表算空档，读的那一行就得把课表也说出来，否则用户看不出它读过
        weekday = now_local().isoweekday()
        data_dir = Path(self.tmp.name) / "data"
        data_dir.mkdir()
        (data_dir / "campus_schedule.json").write_text(
            json.dumps(
                {"lessons": [{"weekday": weekday, "start": "00:00", "end": "23:59", "name": "高等数学", "place": "立德楼302"}]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        settings = _Settings(dashscope_api_key="sk-test", data_dir=str(data_dir))
        result = chat_turn(
            self.store, settings, "session1234", "今天还有什么",
            poster=lambda messages: _reply("看完了"),
        )
        self.assertIn("今天还有 1 节课", result["trace"][0]["text"])
        self.assertIn("23:59", result["trace"][0]["text"])

    def test_read_step_says_so_when_there_is_no_lesson(self) -> None:
        result = chat_turn(
            self.store, self.settings, "session1234", "今天还有什么",
            poster=lambda messages: _reply("看完了"),
        )
        self.assertIn("今天没课", result["trace"][0]["text"])

    def test_itinerary_numbers_require_replanning_after_edits(self) -> None:
        self.assertTrue(all(word in _SYSTEM for word in ('改完', 'day_plan', '行程数字')))

    def test_create_keeps_the_place_words_from_the_user(self) -> None:
        # 模型自己缩写标题会把「基础实验楼」弄丢，那一件事就排不进行程了
        self.assertIn("summary 保留用户原话里的地点和钟点", _SYSTEM)
        self.assertIn("能不能排进行程，靠的就是这几个字", _SYSTEM)

    def test_the_same_call_is_not_run_twice_in_one_turn(self) -> None:        # 本机模型会把上一跳那件事又说一遍；照单执行就是重复建单
        same = _reply("好的，已添加待办。", [{"name": "create", "summary": "去西门打印店取材料", "deadline": "明天上午"}])
        replies = [same, same]
        result = chat_turn(
            self.store, self.settings, "session1234", "帮我记一件：去西门打印店取材料",
            poster=lambda messages: replies.pop(0) if replies else _reply("好了"),
        )
        rows = [task for task in self.store.list_tasks() if "打印店" in str(task.get("summary") or "")]
        self.assertEqual(len(rows), 1)
        calls = [item for item in result["trace"] if item["kind"] == "call"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["tool"], "create")

    def test_saying_noted_without_a_write_is_not_reported_as_saved(self) -> None:
        result = chat_turn(
            self.store, self.settings, "session1234", "我明天上午要去立德楼见一个学长",
            poster=lambda messages: "好的，记下了。只管群待办。",
        )
        self.assertFalse(result["changed"])
        self.assertEqual(result["say"], "还没写进待办。直接说要记的标题和时间。")
        self.assertEqual(self.store.list_tasks(), [])

    def test_bare_create_falls_back_to_the_user_words(self) -> None:
        """模型发裸 {"name":"create"}：标题退回用户原话，待办必须真的建出来。"""
        raw = "3号楼405交实验报告"
        result = chat_turn(
            self.store, self.settings, "session1234", raw,
            poster=lambda messages: _reply("已记：" + raw, [{"name": "create"}]),
        )
        self.assertTrue(result["changed"])
        self.assertEqual([task["summary"] for task in self.store.list_tasks()], [raw])
        self.assertNotIn("请填写待办内容", result["say"])

    def test_bare_create_without_user_words_fails_readably(self) -> None:
        """连原话都没有时不许假装记下：失败要说清楚，不能出现「已记」。"""
        from noticehub.application.agent_chat import _run_call, _say_from

        outcome = _run_call(self.store, self.settings, {}, {"name": "create"}, allow_confirm=False)
        self.assertFalse(outcome["ok"])
        self.assertFalse(outcome["changed"])
        self.assertIn("请填写待办内容", outcome["detail"])
        self.assertNotIn("已记", _say_from([outcome]))
        self.assertEqual(self.store.list_tasks(), [])

    def test_noted_claim_is_replaced_by_the_tool_detail(self) -> None:
        """没改动就说了「已记」：这句话必须被工具结果替换掉。"""
        result = chat_turn(
            self.store, self.settings, "session1234", "把那条改到明天下午",
            poster=lambda messages: _reply(
                "已记：3号楼405交实验报告",
                [{"name": "reschedule", "task_id": "没有这条", "deadline": "明天下午"}],
            ),
        )
        self.assertFalse(result["changed"])
        self.assertNotIn("已记", result["say"])
        self.assertIn("没有标题里含", result["say"])

    def test_model_failure_falls_back_to_local_rules(self) -> None:
        """模型通道挂了（401／超时／返回乱码）：说人话、真落待办，不端厂商原文。"""
        failures = {
            "401": RuntimeError(
                "模型请求失败：HTTP 401：Incorrect API key provided. For details, see: "
                "https://help.aliyun.com/zh/model-studio/error-code#apikey-error"
            ),
            "timeout": RuntimeError("模型太慢，这一轮没等到回应：timed out"),
            "garbage": RuntimeError("模型接口返回结构不认识"),
        }
        for name, error in failures.items():
            with self.subTest(kind=name):
                def boom(messages, error=error):
                    raise error

                result = chat_turn(
                    self.store, self.settings, "session" + name, "3号楼405交实验报告", poster=boom
                )
                self.assertTrue(result["ok"])
                self.assertTrue(result["fallback"])
                self.assertTrue(result["changed"], "兜底也得真落一条待办")
                for word in ("HTTP", "401", "http", "Incorrect", "aliyun"):
                    self.assertNotIn(word, result["say"])
                self.assertEqual(
                    [task["summary"] for task in self.store.list_tasks()][-1], "3号楼405交实验报告"
                )
                self.assertTrue(
                    any("本地兜底" in str(item.get("text") or "") for item in result["trace"])
                )

    def test_model_failure_answers_counts_from_local_data(self) -> None:
        """问句走「数件数」，不硬造一条待办。"""
        self.store.upsert_task(
            task_key="manual:today",
            summary="交实验报告",
            source="manual",
            status="open",
            deadline="2026-11-17T18:00",
        )

        def boom(messages):
            raise RuntimeError("模型请求失败：HTTP 401：Incorrect API key provided.")

        with patch("noticehub.application.agent_chat.now_local", return_value=dt.datetime(2026, 11, 17, 9, 0)):
            result = chat_turn(self.store, self.settings, "sessionread1", "今天还有几件事？", poster=boom)
        self.assertTrue(result["fallback"])
        self.assertFalse(result["changed"])
        self.assertIn("1", result["say"])
        for word in ("HTTP", "401", "Incorrect"):
            self.assertNotIn(word, result["say"])
        self.assertEqual(len(self.store.list_tasks()), 1)

    def test_coerce_deadline_reads_chinese_clock(self) -> None:
        """「下午四点」得算成 16:00：本地规则和模型都会把这种话直接给我。"""
        with patch("noticehub.application.agent_chat.now_local", return_value=dt.datetime(2026, 11, 17, 9, 0)):
            self.assertEqual(_coerce_deadline("下午四点"), "2026-11-17T16:00")
            self.assertEqual(_coerce_deadline("明天下午3点"), "2026-11-18T15:00")
            self.assertEqual(_coerce_deadline("晚上十点半"), "2026-11-17T22:30")
            self.assertEqual(_coerce_deadline("后天上午十点"), "2026-11-19T10:00")
            self.assertEqual(_coerce_deadline("今天"), "2026-11-17T23:59")
            # 真有钟点以外的说法：原样交回去，别自己编一个时间。
            self.assertEqual(_coerce_deadline("有空的时候"), "有空的时候")

    def _no_model_settings(self) -> _Settings:
        return _Settings(dashscope_api_key="", llm_enabled=False)

    def test_no_model_reschedule_understands_the_clock(self) -> None:
        """没有钥匙时改期：「下午四点」要真改成 16:00，不能回一句「无效的截止时间」。"""
        task_id = self.store.upsert_task(
            task_key="manual:parcel", summary="去博雅居拿快递", source="manual", status="open",
            deadline="2026-11-17T18:00",
        )
        with patch("noticehub.application.agent_chat.now_local", return_value=dt.datetime(2026, 11, 17, 9, 0)):
            result = chat_turn(
                self.store, self._no_model_settings(), "sessionnone1", "把博雅居拿快递改到下午四点"
            )
        self.assertTrue(result["changed"], result["say"])
        self.assertNotIn("无效", result["say"])
        self.assertEqual(self.store.get_task(task_id)["deadline"][:16], "2026-11-17T16:00")

    def test_no_model_reschedule_asks_back_when_the_time_is_unreadable(self) -> None:
        """听不懂时间就问清楚，别把内部的校验短语端给用户。"""
        self.store.upsert_task(
            task_key="manual:parcel", summary="去博雅居拿快递", source="manual", status="open",
            deadline="2026-11-17T18:00",
        )
        result = chat_turn(
            self.store, self._no_model_settings(), "sessionnone2", "把博雅居拿快递改到有空的时候"
        )
        self.assertFalse(result["changed"])
        self.assertIn("说个钟点", result["say"])
        self.assertNotIn("无效", result["say"])

    def test_no_model_create_appends_the_route_advice(self) -> None:
        """建完件事得回一句顺不顺路：这句话是用户的核心卖点，不能只在面板里。"""
        advice = "已经排进今天第 1 趟：接着图书馆走，16:30 到博雅居。"
        step = {"kind": "plan", "text": "排程：排进第 1 趟第 2 站，16:30 到（接着走 283 米）"}
        with patch("noticehub.application.agent_chat._route_advice", return_value=(advice, step)):
            result = chat_turn(
                self.store, self._no_model_settings(), "sessionnone3", "记一件：去博雅居拿快递"
            )
        self.assertTrue(result["changed"])
        self.assertTrue(result["say"].endswith(advice), result["say"])
        self.assertIn(step, result["trace"])

    def test_route_advice_speaks_from_the_plan_values(self) -> None:
        """顺路那句里的地点与时刻一律取 today_plan 的返回值，不另算一套。"""
        from noticehub.application.agent_chat import _route_advice

        plan = {
            "trips": [
                {"stops": [
                    {"task_id": "7", "place": "图书馆", "arrive": "16:00", "why": "就在下课的这栋楼里"},
                    {"task_id": "9", "place": "博雅居", "arrive": "16:20", "why": "接着走 283 米"},
                ]},
            ],
            "unscheduled": [],
        }
        with patch("noticehub.application.agent_chat.today_plan", return_value=plan):
            text, step = _route_advice(self.store, self.settings, ["9"])
        self.assertEqual(text, "已经排进今天第 1 趟：接着图书馆走，16:20 到博雅居。")
        self.assertEqual(step["kind"], "plan")
        self.assertIn("16:20", step["text"])

    def test_route_advice_names_the_place_it_could_not_schedule(self) -> None:
        """排不进去也要点名，别静默丢进 unscheduled。"""
        from noticehub.application.agent_chat import _route_advice

        plan = {
            "trips": [],
            "unscheduled": [{"task_id": "9", "title": "交大学物理实验报告", "reason": "没认出地点"}],
        }
        with patch("noticehub.application.agent_chat.today_plan", return_value=plan):
            text, step = _route_advice(self.store, self.settings, ["9"])
        self.assertIn("交大学物理实验报告", text)
        self.assertIn("不在校园地点表里", text)
        self.assertEqual(step["kind"], "plan")

    def test_rename_changes_the_title(self) -> None:
        task_id = self.store.upsert_task(task_key="manual:rename", summary="见学长", source="manual", status="open")
        result = chat_turn(
            self.store, self.settings, "session1234", "把见学长改成去立德楼",
            poster=lambda messages: _reply("改好了", [{"name": "rename", "task_id": "见学长", "summary": "去立德楼见学长"}]),
        )
        self.assertTrue(result["changed"])
        self.assertEqual(self.store.get_task(task_id)["summary"], "去立德楼见学长")

    def test_open_group_task_is_not_deleted(self) -> None:
        task_id = self.store.upsert_task(task_key="digest:group", summary="劳动课", source="qq", status="open")
        seen = []

        def poster(messages):
            seen.append(messages)
            if any(str(item.get("content") or "").startswith("工具结果") for item in messages):
                return _reply("只能忽略")
            return _reply("删掉", [{"name": "act", "task_id": str(task_id), "action": "delete", "confirm": True}])

        chat_turn(self.store, self.settings, "session1234", "删掉劳动课", poster=poster)
        self.assertEqual(self.store.get_task(task_id)["status"], "open")
        self.assertIn("只能忽略", json.dumps(seen, ensure_ascii=False))

    def test_http_error_detail(self) -> None:
        import io
        from unittest.mock import patch
        from urllib.parse import quote

        from noticehub.application.agent_chat import _post

        key = 'sk-unit/secret+value'
        self.settings.dashscope_api_key = key
        message = 'Incorrect API key provided: ' + key + ' encoded=' + quote(key, safe='') + ' Bearer other-secret'
        cases = [
            (json.dumps({'error': {'message': message}}).encode(), 'Incorrect API key provided:'),
            (json.dumps({'error': {'message': 'Quota exceeded'}}).encode(), 'Quota exceeded'),
            (b'<html>private proxy error</html>', ''),
            (b'{bad json', ''), (b'[]', ''),
            (b'{"error":{"message":{"token":"hidden"}}}', ''),
            (json.dumps({'error': {'message': 'x' * 230 + key + ' trailing'}}).encode(), 'x' * 230 + '***'),
        ]
        for raw, detail in cases:
            with self.subTest(detail=detail):
                error = urllib.error.HTTPError(self.settings.dashscope_endpoint, 401, 'Unauthorized', {}, io.BytesIO(raw))
                with patch('urllib.request.urlopen', side_effect=error), self.assertLogs(
                    'noticehub.application.agent_chat', level='WARNING'
                ) as logs:
                    with self.assertRaises(RuntimeError) as caught:
                        _post(self.settings, [{"role": "user", "content": "今天有什么"}])
                # 异常文本只留状态码；厂商正文进日志，且已经脱敏。
                self.assertEqual(str(caught.exception), '模型请求失败：HTTP 401')
                logged = ' '.join(record.getMessage() for record in logs.records)
                if detail:
                    self.assertIn(detail, logged)
                for secret in (key, quote(key, safe=''), 'other-secret', 'hidden', '<html>'):
                    self.assertNotIn(secret, logged)
                self.assertLessEqual(len(logged), len('模型请求失败：HTTP 401 ') + 240)
        # 上面那串只给日志；对话这层不许把厂商原文端给用户，要落到本地兜底。
        error = urllib.error.HTTPError(self.settings.dashscope_endpoint, 401, 'Unauthorized', {}, io.BytesIO(cases[0][0]))
        with patch('urllib.request.urlopen', side_effect=error):
            result = chat_turn(self.store, self.settings, 'errorcase456', '今天有什么')
        self.assertTrue(result['fallback'])
        for word in ('HTTP', '401', 'Incorrect', 'help.aliyun.com', key):
            self.assertNotIn(word, result['say'])

    def test_without_a_key_the_page_keeps_the_old_parser(self) -> None:
        self.assertFalse(chat_ready(_Settings()))
        result = chat_turn(self.store, _Settings(), "session1234", "完成实验")
        self.assertTrue(result["fallback"])
        self.assertFalse(result["changed"])


class AgentChatRouteTest(unittest.TestCase):
    def test_static_table_does_not_grow(self) -> None:
        text = (PROJECT_ROOT / "noticehub" / "routes.py").read_text(encoding="utf-8")
        self.assertNotIn(AGENT_CHAT_PATH, text)
        self.assertNotIn(AGENT_CHAT_PATH, POST_ROUTES)
        previous = os.environ.get("NOTICEHUB_AGENT_SHELL")
        os.environ["NOTICEHUB_AGENT_SHELL"] = "0"
        try:
            self.assertIsNone(optional_post(AGENT_CHAT_PATH))
            self.assertIsNone(optional_get(AGENT_CHAT_PATH))
        finally:
            if previous is None:
                os.environ.pop("NOTICEHUB_AGENT_SHELL", None)
            else:
                os.environ["NOTICEHUB_AGENT_SHELL"] = previous

    def test_http_returns_route_trace_and_failure_without_stack(self) -> None:
        from unittest.mock import patch
        from noticehub.config import Settings
        from noticehub.webapp import TaskWebServer
        from tests.test_route_tools import POIS

        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'NOTICEHUB_AGENT_SHELL': '1'}):
            (Path(folder) / 'campus_poi.json').write_text(json.dumps(POIS), encoding='utf-8')
            store = Store(Path(folder) / 'api.sqlite3')
            settings = Settings(web_host='127.0.0.1', web_token='secret', web_port=0, data_dir=Path(folder))
            settings.llm_enabled = True
            settings.llm_backend = 'auto'
            settings.dashscope_api_key = 'unit-key'
            server = TaskWebServer(settings, store)
            self.assertTrue(server.start())
            try:
                url = 'http://127.0.0.1:%s%s' % (server.server.server_address[1], AGENT_CHAT_PATH)
                for points, ok in [(['起点楼', '远处楼', '中间楼'], True), (['不存在楼'], False)]:
                    with self.subTest(points=points), patch('noticehub.application.agent_chat._post', side_effect=[
                        _reply('', [dict(name='plan_route', points=points)]), _reply(''),
                    ]):
                        request = urllib.request.Request(url, method='POST',
                            data=json.dumps(dict(text='规划路线', session_id='httproute123')).encode(),
                            headers={'Content-Type': 'application/json', 'X-Token': 'secret'})
                        with urllib.request.urlopen(request, timeout=5) as response:
                            self.assertEqual(response.status, 200)
                            body = json.loads(response.read())
                        call = next(row for row in body['trace'] if row['kind'] == 'call')
                        self.assertEqual(call['ok'], ok)
                        self.assertFalse(body['changed'])
                        if ok:
                            self.assertEqual(call['result']['saved_m'], 300)
                        else:
                            self.assertIn('找不到', body['say'])
            finally:
                server.stop()

    def test_http_falls_back_until_a_key_exists(self) -> None:
        previous = os.environ.get("NOTICEHUB_AGENT_SHELL")
        os.environ["NOTICEHUB_AGENT_SHELL"] = "0"
        if previous is None:
            self.addCleanup(os.environ.pop, "NOTICEHUB_AGENT_SHELL", None)
        else:
            self.addCleanup(os.environ.__setitem__, "NOTICEHUB_AGENT_SHELL", previous)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        from noticehub.config import Settings
        from noticehub.webapp import TaskWebServer

        store = Store(Path(folder.name) / "api.sqlite3")
        settings = Settings(web_host="127.0.0.1", web_token="secret", web_port=0, data_dir=Path(folder.name) / "data")
        settings.dashscope_api_key = ""
        settings.llm_backend = "auto"
        server = TaskWebServer(settings, store)
        self.assertTrue(server.start())
        self.addCleanup(server.stop)
        base = "http://127.0.0.1:%s" % server.server.server_address[1]

        def post() -> tuple[int, dict]:
            request = urllib.request.Request(
                base + AGENT_CHAT_PATH,
                data=json.dumps({"text": "你好", "session_id": "session1234"}).encode(),
                headers={"Content-Type": "application/json", "X-Token": "secret"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, json.loads(response.read().decode())
            except urllib.error.HTTPError as error:
                body = json.loads(error.read().decode() or "{}")
                error.close()
                return error.code, body

        status, body = post()
        self.assertEqual(status, 404)
        os.environ["NOTICEHUB_AGENT_SHELL"] = "1"
        status, body = post()
        self.assertEqual(status, 200)
        self.assertTrue(body["fallback"])


class AgentPostTest(unittest.TestCase):
    """真正发请求那一步：本机 Ollama 要关掉思考，读超时不能断在 socket 上。"""

    def test_local_ollama_turns_reasoning_off(self) -> None:
        from noticehub.application.agent_chat import _payload

        payload = _payload(_Settings(llm_backend="ollama"), [{"role": "user", "content": "今天有什么"}])
        self.assertEqual(payload["reasoning_effort"], "none")

    def test_cloud_backend_keeps_the_request_plain(self) -> None:
        from noticehub.application.agent_chat import _payload

        payload = _payload(_Settings(llm_backend="auto"), [{"role": "user", "content": "今天有什么"}])
        self.assertNotIn("reasoning_effort", payload)

    def test_read_timeout_becomes_one_sentence(self) -> None:
        from unittest import mock

        from noticehub.application.agent_chat import _post

        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaises(RuntimeError) as caught:
                _post(_Settings(llm_backend="ollama"), [{"role": "user", "content": "今天有什么"}])
        self.assertIn("模型太慢", str(caught.exception))

    def test_local_model_gets_a_longer_read_timeout(self) -> None:
        # 35B 本机模型冷启动（装进显存）实测一分钟上下，云端那 25 秒上限会把第一轮直接掐掉。
        # 本机模型也不许被通用的 llm_timeout（默认 60）压回去：实测第一句 60.6 秒被判超时，
        # 用户第一句话就失败。所以 ollama 一律用足 90 秒，与 llm_timeout 设成多少无关。
        from unittest import mock

        from noticehub.application.agent_chat import _post

        seen = []

        def fake_urlopen(request, timeout=None):
            seen.append(timeout)
            raise TimeoutError("timed out")

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            for backend, budget in (("ollama", 120), ("ollama", 60), ("auto", 120)):
                with self.assertRaises(RuntimeError):
                    _post(
                        _Settings(llm_backend=backend, llm_timeout=budget),
                        [{"role": "user", "content": "在吗"}],
                    )
        self.assertEqual(seen[0], 90)
        self.assertEqual(seen[1], 90)
        self.assertEqual(seen[2], 25)

    def test_vision_gets_its_own_longer_timeout_and_sends_the_image(self) -> None:
        # 读图比读一句话重：本地 35B 认一张课表实测 94.8 秒，跟着对话的 90 秒会被掐掉，
        # 而读图又是「选一张截图」这种一步到位的动作，不能折成「再试一次」。
        from unittest import mock

        from noticehub.application.agent_chat import ask_vision

        seen = []

        def fake_urlopen(request, timeout=None):
            seen.append((timeout, json.loads(request.data.decode("utf-8"))))
            raise TimeoutError("timed out")

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            # 读不成返回 None，不抛栈：这条路径上的异常都给上层换成一句人话。
            self.assertIsNone(ask_vision(_Settings(llm_backend="ollama"), "抄课表", "data:image/png;base64,AAAA"))
        budget, payload = seen[0]
        self.assertEqual(budget, 180)
        parts = payload["messages"][1]["content"]
        self.assertEqual(parts[0]["type"], "text")
        self.assertEqual(parts[1]["type"], "image_url")
        self.assertEqual(parts[1]["image_url"]["url"], "data:image/png;base64,AAAA")

    def test_vision_failures_never_hand_back_vendor_text(self) -> None:
        """没钥匙／钥匙错／超时：返回 None，日志里有，返回值里一个字都不留。"""
        import io
        from unittest import mock

        from noticehub.application.agent_chat import ask_vision

        key = "sk-unit/secret"
        bodies = {
            "no key": b'{"error":{"message":"You didn\'t provide an API key. You need to provide your API key '
                      b'in the Authorization header, using Bearer auth."}}',
            "wrong key": json.dumps({"error": {"message": "Incorrect API key provided: " + key}}).encode(),
        }
        for name, body in bodies.items():
            with self.subTest(name):
                error = urllib.error.HTTPError("https://example.invalid/v1/chat/completions", 401, "Unauthorized", {}, io.BytesIO(body))
                settings = _Settings(dashscope_api_key=key)
                with mock.patch("urllib.request.urlopen", side_effect=error), self.assertLogs(
                    "noticehub.application.agent_chat", level="WARNING"
                ) as logs:
                    self.assertIsNone(ask_vision(settings, "抄课表", "data:image/png;base64,AAAA"))
                self.assertTrue(any("HTTP 401" in line for line in logs.output))
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")), self.assertLogs(
            "noticehub.application.agent_chat", level="WARNING"
        ) as logs:
            self.assertIsNone(ask_vision(_Settings(), "抄课表", "data:image/png;base64,AAAA"))
        self.assertTrue(any("读图没成功" in line for line in logs.output))
