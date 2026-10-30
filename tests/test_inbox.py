import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace
from qq_live_digest.llm_json import parse_json
from qq_live_digest.store import Store
from qq_live_digest.inbox import classify_messages, promote, _call
from qq_live_digest import inbox, group_suggest

class InboxTests(unittest.TestCase):
    def test_parse_fenced_object(self): self.assertEqual(parse_json('thinking...```json\n{"a":1}\n```'), {"a":1})
    def test_llm_request_budget_and_timeout(self):
        settings=SimpleNamespace(llm_backend="ollama",dashscope_endpoint="http://llm",dashscope_model="m",llm_timeout=7,dashscope_api_key="k")
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self): return json.dumps({"choices":[{"message":{"content":"[]"}}]}).encode()
        seen={}
        def fake(request, timeout): seen.update(body=json.loads(request.data),timeout=timeout,headers=dict(request.headers)); return Response()
        with patch("qq_live_digest.inbox.urllib.request.urlopen", fake): _call(None,settings,[{"msg_id":str(i),"content":"x"} for i in range(20)])
        self.assertEqual(seen["body"]["reasoning_effort"],"none"); self.assertGreaterEqual(seen["body"]["max_tokens"],900); self.assertEqual(seen["timeout"],7); self.assertIn("Authorization",seen["headers"])
    def test_parse_array(self): self.assertEqual(parse_json('prefix [{"x":1}] suffix', expect="array"), [{"x":1}])
    def test_parse_invalid(self):
        with self.assertRaises(ValueError): parse_json("no json")
    def _setup(self):
        td=tempfile.TemporaryDirectory(); s=Store(Path(td.name)/"data.db"); settings=SimpleNamespace(dashscope_endpoint="",dashscope_model="",data_dir=Path(td.name),is_quiet_group=lambda group_id: False,is_quiet_group_name=lambda name: False)
        for i,text in enumerate(["周三3-4节交作业","明天开会","哈哈"]): s.insert_message(msg_id=str(i),group_id="g",content=text,group_name="G")
        return td,s,settings
    def test_inbox_items_preserve_message_source(self):
        td, s, settings = self._setup()
        try:
            s.insert_message(
                msg_id="onebot:g:attach:0123456789abcdef",
                group_id="g",
                content="图片附件",
                source="onebot-attachment",
                event="GROUP_ATTACHMENT",
            )
            s.save_mark("onebot:g:attach:0123456789abcdef", "notice")
            item = s.inbox_items(limit=20)[0]
            self.assertEqual(item["source"], "onebot-attachment")
            self.assertEqual(item["event"], "GROUP_ATTACHMENT")
        finally:
            td.cleanup()

    def test_heuristic_has_all_three_verdicts(self):
        td,s,settings=self._setup()
        try:
            s.insert_message(msg_id="n",group_id="g",content="周三3-4节交作业，截止10月10日23:59前提交到学习通")
            s.insert_message(msg_id="q",group_id="g",content="那个事怎么样")
            s.insert_message(msg_id="z",group_id="g",content="哈哈哈")
            classify_messages(settings,s,client=lambda _: (_ for _ in ()).throw(OSError("offline")))
            got={x["msg_id"]:x["verdict"] for x in s.inbox_items(limit=20)}
            self.assertEqual(got["n"],"notice"); self.assertEqual(got["q"],"suspect"); self.assertEqual(got["z"],"noise")
        finally: td.cleanup()

    def test_heuristic_fallback_and_unmarked(self):
        td,s,settings=self._setup()
        try:
            r=classify_messages(settings,s,client=lambda _: (_ for _ in ()).throw(OSError("x"))); self.assertEqual(r["method"],"heuristic"); self.assertEqual(r["classified"],3); self.assertEqual(len(s.unmarked_messages()),0)
        finally: td.cleanup()
    def test_batching(self):
        td,s,settings=self._setup(); [s.insert_message(msg_id=f"x{i}",group_id="g",content="通知内容很长" ) for i in range(25)]
        calls=[]
        try:
            r=classify_messages(settings,s,client=lambda p: calls.append(p) or json.dumps([{"msg_id":x["msg_id"],"verdict":"suspect","reason":"r"} for x in []])); self.assertGreaterEqual(len(calls),2)
        finally: td.cleanup()
    def test_partial_llm_results_cover_batch_and_reject_forgery(self):
        td,s,settings=self._setup()
        try:
            def partial(_): return json.dumps([{"msg_id":"0","verdict":"notice","reason":"ok"},{"msg_id":"fake","verdict":"notice","reason":"bad"}])
            r=classify_messages(settings,s,client=partial)
            self.assertEqual(r["method"],"mixed")
            self.assertEqual(len(s.unmarked_messages()),0)
            self.assertEqual(s.inbox_items(limit=20,verdict="notice")[0]["msg_id"],"0")
            self.assertIsNone(s.inbox_items(limit=20,verdict="notice")[0]["task_id"])
        finally: td.cleanup()

    def test_missing_ids_are_retried_as_llm(self):
        td,s,settings=self._setup(); calls=[]
        try:
            def client(_):
                calls.append(1); return json.dumps(([{"msg_id":"0","verdict":"notice","reason":"first"}] if len(calls)==1 else [{"msg_id":str(i),"verdict":"notice","reason":"retry"} for i in ("1","2")]))
            r=classify_messages(settings,s,client=client); self.assertEqual(r["method"],"llm"); self.assertEqual(len(calls),2); self.assertEqual({x["method"] for x in s.inbox_items()}, {"llm"})
        finally: td.cleanup()

    def test_codebuddy_backend_uses_cli_without_endpoint(self):
        settings=SimpleNamespace(llm_backend="codebuddy",llm_timeout=7,codebuddy_cli="fake-cli",dashscope_endpoint="http://must-not-use",dashscope_model="m")
        rows=[{"msg_id":"1","content":"通知"}]
        with patch("qq_digest.run_codebuddy", return_value={"items":[{"msg_id":"1","verdict":"notice","reason":"CLI"}]}) as cli, patch.object(inbox.urllib.request, "urlopen", side_effect=AssertionError("endpoint used")):
            result = _call(None, settings, rows)
        self.assertEqual(json.loads(result)[0]["reason"], "CLI")
        cli.assert_called_once()

    def test_auto_cli_failure_falls_back_to_endpoint(self):
        settings=SimpleNamespace(llm_backend="auto",llm_timeout=7,codebuddy_cli="fake",dashscope_endpoint="http://llm",dashscope_model="m",dashscope_api_key="")
        class R:
            def __enter__(self): return self
            def __exit__(self,*a): pass
            def read(self): return json.dumps({"choices":[{"message":{"content":json.dumps([{"msg_id":"1","verdict":"notice","reason":"endpoint"}])}}]}).encode()
        with patch("qq_digest.run_codebuddy", side_effect=RuntimeError("CLI down")), patch.object(inbox.urllib.request, "urlopen", return_value=R()) as endpoint:
            result = _call(None, settings, [{"msg_id":"1","content":"通知"}])
        self.assertEqual(json.loads(result)[0]["reason"], "endpoint")
        endpoint.assert_called_once()

    def test_codebuddy_failure_never_falls_back(self):
        settings=SimpleNamespace(llm_backend="codebuddy",llm_timeout=7,codebuddy_cli="fake",dashscope_endpoint="http://must-not-use",dashscope_model="m")
        with patch("qq_digest.run_codebuddy", side_effect=RuntimeError("CLI down")), patch.object(inbox.urllib.request, "urlopen", side_effect=AssertionError("endpoint used")):
            with self.assertRaisesRegex(RuntimeError, "CLI down"):
                _call(None, settings, [{"msg_id":"1","content":"通知"}])

    def test_auto_cli_success_skips_endpoint(self):
        settings=SimpleNamespace(llm_enabled=True,llm_backend="auto",llm_timeout=7,codebuddy_cli="fake",dashscope_endpoint="http://must-not-use",dashscope_model="m")
        with patch("qq_digest.run_codebuddy", return_value=[{"msg_id":"1","verdict":"notice","reason":"CLI"}]) as cli, patch.object(inbox.urllib.request, "urlopen", side_effect=AssertionError("endpoint used")):
            result = _call(None, settings, [{"msg_id":"1","content":"通知"}])
        self.assertEqual(json.loads(result)[0]["reason"], "CLI")
        cli.assert_called_once()

    def test_ollama_backend_uses_endpoint(self):
        settings=SimpleNamespace(llm_backend="ollama",dashscope_endpoint="http://llm/v1/chat/completions",dashscope_model="35b",llm_timeout=7,dashscope_api_key="")
        class R:
            def __enter__(self): return self
            def __exit__(self,*a): pass
            def read(self): return json.dumps({"choices":[{"message":{"content":"[]"}}]}).encode()
        seen={}
        def fetch(req,timeout): seen["body"]=json.loads(req.data); return R()
        with patch.object(inbox, "_local_model", return_value="qwen3.5:9b"), patch.object(inbox.urllib.request, "urlopen", fetch): _call(None,settings,[{"msg_id":str(i),"content":"x"} for i in range(20)])
        self.assertEqual(seen["body"]["model"],"qwen3.5:9b")

    def test_unknown_backend_is_reported_not_silently_successful(self):
        settings=SimpleNamespace(llm_backend="mystery",dashscope_endpoint="http://must-not-use",dashscope_model="m",llm_timeout=1)
        with patch.object(inbox.urllib.request, "urlopen", side_effect=AssertionError("endpoint used")):
            with self.assertRaisesRegex(RuntimeError, "不支持的 LLM backend"):
                _call(None, settings, [{"msg_id":"1","content":"通知"}])

    def test_local_model_probe_selects_qwen9b(self):
        settings=SimpleNamespace(dashscope_endpoint="http://llm/v1/chat/completions",dashscope_model="35b",llm_timeout=1)
        class R:
            def __enter__(self): return self
            def __exit__(self,*a): pass
            def read(self): return json.dumps({"models":[{"name":"qwen3.5:9b"}]}).encode()
        group_suggest._clear_caches()
        with patch.object(group_suggest.urllib.request, "urlopen", return_value=R()): self.assertEqual(group_suggest._local_model(settings),"qwen3.5:9b")

    def test_local_model_probe_failure_falls_back(self):
        settings=SimpleNamespace(dashscope_endpoint="http://llm/v1/chat/completions",dashscope_model="35b",llm_timeout=1)
        group_suggest._clear_caches()
        with patch.object(group_suggest.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertEqual(group_suggest._local_model(settings),"35b")

    def test_promoted_filter_and_counts(self):
        td,s,settings=self._setup()
        try:
            classify_messages(settings,s,client=lambda p: json.dumps([{"msg_id":str(i),"verdict":"notice","reason":"r"} for i in range(3)]))
            self.assertEqual(len(s.inbox_items(promoted=True)),0); self.assertEqual(len(s.inbox_items(promoted=False)),3)
            promote(settings,s,"0")
            self.assertEqual(len(s.inbox_items(promoted=True)),1); self.assertEqual(len(s.inbox_items(promoted=False)),2)
            counts=s.inbox_counts(); self.assertEqual(counts["promoted"],1)
            self.assertTrue({"notice","suspect","noise"}.issubset(counts))
        finally: td.cleanup()

    def test_promoted_filter_preserves_verdict_filter(self):
        td,s,settings=self._setup()
        try:
            classify_messages(settings,s,client=lambda p: json.dumps([{"msg_id":str(i),"verdict":"notice","reason":"r"} for i in range(3)]))
            promote(settings,s,"0")
            self.assertEqual({x["msg_id"] for x in s.inbox_items(verdict="notice")}, {"0","1","2"})
            self.assertEqual([x["msg_id"] for x in s.inbox_items(verdict="notice",promoted=True)], ["0"])
        finally: td.cleanup()

    def test_promote_idempotent(self):
        td,s,settings=self._setup()
        try:
            classify_messages(settings,s,client=lambda p: json.dumps([{ "msg_id":str(i),"verdict":"notice","reason":"r"} for i in range(3)]))
            a=promote(settings,s,"0"); b=promote(settings,s,"0"); self.assertTrue(a["ok"]); self.assertEqual(a["task_id"],b["task_id"])
        finally: td.cleanup()
    def test_promote_reuses_task_auto_created_for_same_message(self):
        td,s,settings=self._setup()
        try:
            classify_messages(settings,s,client=lambda p: json.dumps([{"msg_id":"0","verdict":"notice","reason":"r"}]))
            before=len(s.list_tasks())
            s.upsert_task(task_key="0",summary="自动建的任务",source="qq_message")
            self.assertEqual(len(s.list_tasks()),before+1)
            res=promote(settings,s,"0")
            self.assertTrue(res["ok"])
            self.assertEqual(len(s.list_tasks()),before+1)
        finally: td.cleanup()
    def test_old_db_upgrade(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"old.db"; Store(p); self.assertIn("notice",Store(p).inbox_counts())

    def test_llm_disabled_skips_every_llm_call(self):
        """LLM 关掉时不能去起 codebuddy：那会卡到 llm_timeout × 每批 2 次重试。"""
        td,s,settings=self._setup()
        settings.llm_enabled=False; settings.llm_backend="auto"; settings.codebuddy_cli="fake"; settings.llm_timeout=1
        calls=[]
        try:
            with patch("qq_digest.run_codebuddy", side_effect=lambda *a,**k: calls.append("cli")), \
                 patch("qq_live_digest.inbox.urllib.request.urlopen", side_effect=lambda *a,**k: calls.append("http")):
                r=classify_messages(settings,s)
            self.assertEqual(calls,[],"LLM 关闭时一条请求都不许发")
            self.assertEqual(r["method"],"heuristic")
            self.assertEqual(r["classified"],3)
            self.assertEqual(len(s.unmarked_messages()),0,"启发式也要把结果落库")
        finally: td.cleanup()
    def test_llm_enabled_still_uses_codebuddy(self):
        td,s,settings=self._setup()
        settings.llm_enabled=True; settings.llm_backend="codebuddy"; settings.codebuddy_cli="fake"; settings.llm_timeout=1
        calls=[]
        def fake_cli(prompt, schema, **kwargs):
            calls.append(kwargs)
            return {"items":[{"msg_id":str(i),"verdict":"notice","reason":"r"} for i in range(3)]}
        try:
            with patch("qq_digest.run_codebuddy", side_effect=fake_cli):
                r=classify_messages(settings,s)
            self.assertEqual(len(calls),1,"打开时应走一次 codebuddy")
            self.assertEqual(r["method"],"llm")
        finally: td.cleanup()
    def test_auto_backend_skips_codebuddy_when_disabled(self):
        """_call 那层保险：auto + 关闭时不去碰 CLI。"""
        settings=SimpleNamespace(llm_enabled=False,llm_backend="auto",codebuddy_cli="fake",llm_timeout=1,dashscope_endpoint="http://llm",dashscope_model="m",dashscope_api_key="")
        calls=[]
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self): return json.dumps({"choices":[{"message":{"content":"[]"}}]}).encode()
        with patch("qq_digest.run_codebuddy", side_effect=lambda *a,**k: calls.append("cli")), \
             patch("qq_live_digest.inbox.urllib.request.urlopen", lambda *a,**k: Response()):
            _call(None,settings,[{"msg_id":"1","content":"x"}])
        self.assertEqual(calls,[],"auto + 关闭时不许起 CLI")

    def test_cancel_stops_before_next_batch_and_keeps_saved_rows(self):
        """第 2 批前取消：已落库的保留，后续批一条不许再发。"""
        td,s,settings=self._setup()
        [s.insert_message(msg_id=f"x{i}",group_id="g",content="通知内容很长") for i in range(25)]
        calls=[]
        try:
            def client(prompt):
                calls.append(prompt)
                import re as _re
                ids=_re.findall(r'"msg_id":\s*"([^"]+)"',prompt)
                return json.dumps([{"msg_id":i,"verdict":"notice","reason":"r"} for i in ids])
            def cancel():
                return len(calls) >= 1
            r=classify_messages(settings,s,client=client,cancel=cancel)
            self.assertTrue(r["cancelled"])
            self.assertEqual(len(calls),1,"第 2 批不许再发请求")
            self.assertEqual(r["classified"],20)
            self.assertEqual(len(s.unmarked_messages()),8,"剩下的留给下一次判定")
        finally: td.cleanup()

    def test_cancelled_result_carries_counts_and_method(self):
        td,s,settings=self._setup()
        try:
            r=classify_messages(settings,s,client=lambda p: (_ for _ in ()).throw(RuntimeError("x")),cancel=lambda: True)
            self.assertTrue(r["cancelled"])
            self.assertEqual(r["classified"],0)
            self.assertEqual(r["method"],"heuristic")
        finally: td.cleanup()

    def test_llm_failure_is_logged_and_reported(self):
        """LLM 判定全挂时不能静默：要有 warning，结果体要带 llm_error。"""
        td,s,settings=self._setup()
        def boom(_): raise RuntimeError("LLM 503")
        try:
            with self.assertLogs("qq_live_digest.inbox", level="WARNING") as logs:
                r=classify_messages(settings,s,client=boom)
            self.assertEqual(r["method"],"heuristic")
            self.assertIn("503",r["llm_error"])
            self.assertTrue(any("LLM 判定第 1 批失败" in line for line in logs.output),logs.output)
            self.assertEqual(len(s.unmarked_messages()),0,"回退启发式也要落库")
        finally: td.cleanup()
    def test_llm_success_reports_empty_llm_error(self):
        td,s,settings=self._setup()
        try:
            r=classify_messages(settings,s,client=lambda p: json.dumps([{"msg_id":str(i),"verdict":"notice","reason":"r"} for i in range(3)]))
            self.assertEqual(r["method"],"llm")
            self.assertEqual(r["llm_error"],"")
        finally: td.cleanup()

if __name__ == "__main__": unittest.main()
