import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import qq_digest

FIXTURE = str(Path(__file__).parent / 'fixtures' / 'fake_codebuddy.js')

class _Response:
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self):
        return json.dumps({'choices': [{'message': {'content': '{"items": []}'}}]}).encode()

class CodeBuddyTests(unittest.TestCase):
    def run_fake_cli(self, stdout):
        result = type('Result', (), {'returncode': 0, 'stdout': stdout, 'stderr': ''})()
        with patch('qq_digest.detect_codebuddy_cli', return_value=FIXTURE), patch('qq_digest.subprocess.run', return_value=result):
            return qq_digest.run_codebuddy('x')

    def test_run_codebuddy_accepts_json_fence(self):
        self.assertEqual(self.run_fake_cli('```json\n{"items": []}\n```\n'), {'items': []})

    def test_run_codebuddy_accepts_bare_json(self):
        self.assertEqual(self.run_fake_cli('{"items": []}'), {'items': []})

    def test_run_codebuddy_rejects_invalid_json(self):
        with self.assertRaisesRegex(RuntimeError, 'JSON 解析失败'):
            self.run_fake_cli('not json')

    def test_run_codebuddy_rejects_empty_output(self):
        with self.assertRaisesRegex(RuntimeError, '空输出'):
            self.run_fake_cli('')

    def test_child_env_removes_host_variables_preserves_path(self):
        with patch.dict(os.environ, {'CLIENT_INFO_IDE_TYPE': '1', 'HTTPS_PROXY': 'http://127.0.0.1:1'}, clear=False):
            cleaned = qq_digest.clean_child_env(os.environ)
        self.assertNotIn('CLIENT_INFO_IDE_TYPE', cleaned)
        self.assertNotIn('HTTPS_PROXY', cleaned)
        self.assertIn('PATH', cleaned)

    def test_child_env_is_pure_and_idempotent(self):
        base = {'PATH': 'keep', 'HTTPS_PROXY': 'drop', 'path': 'also-keep?'}
        before = dict(base)
        once = qq_digest.clean_child_env(base)
        twice = qq_digest.clean_child_env(once)
        self.assertEqual(base, before)
        self.assertEqual(once, twice)

    def test_missing_cli_explicit_backend_error(self):
        with self.assertRaisesRegex(RuntimeError, 'CLI 未找到'):
            qq_digest.run_codebuddy('x', cli=str(Path(FIXTURE).with_name('missing.js')), timeout=2)

    def test_nonzero_exit_reaches_run_codebuddy(self):
        with patch.dict(os.environ, {'FAKE_CODEBUDDY_MODE': 'exit'}):
            with self.assertRaisesRegex(RuntimeError, '退出码 7'):
                qq_digest.run_codebuddy('x', cli=FIXTURE, timeout=5)

    def test_invalid_json_reaches_run_codebuddy(self):
        with patch.dict(os.environ, {'FAKE_CODEBUDDY_MODE': 'invalid'}):
            with self.assertRaisesRegex(RuntimeError, 'JSON 解析失败'):
                qq_digest.run_codebuddy('x', cli=FIXTURE, timeout=5)

    def test_auto_missing_cli_falls_back_to_http(self):
        items = [{'message': type('M', (), {'timestamp': None, 'sender': 'a', 'text': 'hello'})(), 'category': 'info', 'deadline': None, 'deadline_dt': None, 'score': 1}]
        with patch.dict(os.environ, {'QQ_DIGEST_LLM_BACKEND': 'auto', 'QQ_DIGEST_CODEBUDDY_CLI': 'missing.js'}):
            with patch('qq_digest.urllib.request.urlopen', return_value=_Response()) as opened:
                qq_digest.refine_with_dashscope(items, 'key', 'model', 'http://test', 5, retries=0, backoff=0)
        opened.assert_called_once()

    def test_full_npm_cli_precedes_headless(self):
        with patch.dict(os.environ, {'APPDATA': '', 'LOCALAPPDATA': '', 'ProgramFiles': ''}, clear=False), patch(
            'qq_digest.os.path.isfile',
            side_effect=lambda path: path.endswith(os.path.join('codebuddy-code', 'bin', 'codebuddy'))
            or path.endswith(os.path.join('cli', 'dist', 'codebuddy-headless.js')),
        ):
            found = qq_digest.detect_codebuddy_cli()
        self.assertTrue(found.endswith(os.path.join('codebuddy-code', 'bin', 'codebuddy')))

    def test_missing_candidates_return_empty(self):
        with patch.dict(os.environ, {'APPDATA': '', 'LOCALAPPDATA': '', 'ProgramFiles': ''}, clear=False), patch(
            'qq_digest.os.path.isfile', return_value=False
        ):
            self.assertEqual(qq_digest.detect_codebuddy_cli(), '')

    def test_auto_falls_back_to_http_after_cli_failure(self):
        items = [{'message': type('M', (), {'timestamp': None, 'sender': 'a', 'text': 'hello'})(),
                  'category': 'info', 'deadline': None, 'deadline_dt': None, 'score': 1}]
        with patch.dict(os.environ, {'QQ_DIGEST_LLM_BACKEND': 'auto', 'QQ_DIGEST_CODEBUDDY_CLI': FIXTURE, 'FAKE_CODEBUDDY_MODE': 'exit'}):
            with patch('qq_digest.urllib.request.urlopen', return_value=_Response()) as opened:
                result = qq_digest.refine_with_dashscope(items, 'key', 'model', 'http://test', 5, retries=0, backoff=0)
        self.assertEqual(result, items)
        opened.assert_called_once()

if __name__ == '__main__':
    unittest.main()
