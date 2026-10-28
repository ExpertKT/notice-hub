"""自动整理规则表单的纯函数用例：直接跑真实的 js/lab-rules.js。

用本机 node 执行页面实际加载的映射文件，断言「后端规则 → 卡片 → 后端规则」往返、
逗号解析、开关映射、空条件拒绝、分类→重要程度推导、顺序保持。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAB_RULES_JS = PROJECT_ROOT / "qq_live_digest" / "web_static" / "js" / "lab-rules.js"


HARNESS_JS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf-8');
const window = {};
new Function('window', src)(window);
const R = window.NhLabRules;
const report = {};

if (!R || typeof R.rulesToCards !== 'function' || typeof R.cardsToRules !== 'function') {
  process.stdout.write(JSON.stringify({ fatal: 'window.NhLabRules 没有导出两个函数' }));
  process.exit(0);
}

function check(name, fn) {
  const failed = [];
  try {
    fn(failed);
  } catch (error) {
    failed.push('场景崩溃：' + String((error && error.stack) || error));
  }
  report[name] = { pass: failed.length === 0, detail: failed.join('；') || 'ok' };
}

function same(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

check('roundtrip_preserves_hidden_fields', (failed) => {
  const backend = [{
    id: '班委通知', enabled: true, scope: ['123', '456'],
    match: { keyword: ['报名', '截止'], regex: ['^【通知】'], at_me: true, sender: ['10001'] },
    action: { action: 'promote', payload: { summary: '班委通知', category: 'urgent', importance: 5 }, template: 'tpl' },
    priority: 7, dedupe_key: '{group_id}-{msg_id}', note: '后端扩展字段',
  }];
  const cards = R.rulesToCards(backend);
  if (cards.length !== 1) { failed.push('卡片数量 ' + cards.length); return; }
  const card = cards[0];
  if (card.category !== 'urgent') failed.push('分类没读出来：' + card.category);
  if (card.scopeMode !== 'groups' || card.scopeText !== '123, 456') failed.push('范围没读出来：' + card.scopeMode + '/' + card.scopeText);
  if (!card.useRegex || card.regexText !== '^【通知】') failed.push('正则没读出来');
  if (!card.atMe) failed.push('@我 没读出来');
  const back = R.cardsToRules(cards);
  if (!back.ok) { failed.push('回写失败：' + back.error); return; }
  const rule = back.rules[0];
  if (rule.priority !== 7) failed.push('priority 丢了：' + rule.priority);
  if (rule.dedupe_key !== '{group_id}-{msg_id}') failed.push('dedupe_key 丢了：' + rule.dedupe_key);
  if (rule.note !== '后端扩展字段') failed.push('未知扩展字段丢了：' + rule.note);
  if (!same(rule.scope, ['123', '456'])) failed.push('scope 变了：' + JSON.stringify(rule.scope));
  if (!same(rule.match, backend[0].match)) failed.push('match 往返不一致：' + JSON.stringify(rule.match));
  if (rule.action.action !== 'promote') failed.push('action 名丢了：' + rule.action.action);
  if (rule.action.template !== 'tpl') failed.push('action 扩展字段丢了');
  if (rule.action.payload.category !== 'urgent') failed.push('payload.category 变了');
  if (rule.action.payload.importance !== 5) failed.push('payload.importance 变了：' + rule.action.payload.importance);
  if (rule.action.payload.summary !== '班委通知') failed.push('payload.summary 变了');
});

check('keyword_split_trims_and_drops_empty', (failed) => {
  const back = R.cardsToRules([{
    id: 'k', keywordText: ' 报名, 截止 ， 开会、、  ', scopeMode: 'all', category: 'action', enabled: true,
  }]);
  if (!back.ok) { failed.push(back.error); return; }
  const keyword = back.rules[0].match.keyword;
  if (!same(keyword, ['报名', '截止', '开会'])) failed.push('关键词解析结果：' + JSON.stringify(keyword));
});

check('at_me_and_regex_toggle', (failed) => {
  const off = R.cardsToRules([{ id: 'a', keywordText: '', atMe: true, useRegex: false, regexText: '^【通知】', category: 'action' }]);
  if (!off.ok) { failed.push(off.error); return; }
  const offMatch = off.rules[0].match;
  if (offMatch.at_me !== true) failed.push('勾了 @我 却没写进 match');
  if ('regex' in offMatch) failed.push('没勾正则却把正则写进去了：' + JSON.stringify(offMatch));
  const on = R.cardsToRules([{ id: 'b', regexText: '^【通知】, 截止', useRegex: true, category: 'action' }]);
  if (!on.ok) { failed.push(on.error); return; }
  if (!same(on.rules[0].match.regex, ['^【通知】', '截止'])) failed.push('正则解析：' + JSON.stringify(on.rules[0].match.regex));
});

check('empty_condition_rejected_with_index', (failed) => {
  const back = R.cardsToRules([
    { id: 'ok', keywordText: '报名', category: 'action' },
    { id: 'bad', keywordText: '   ', category: 'action' },
  ]);
  if (back.ok) { failed.push('空条件居然通过了'); return; }
  if (!/第\s*2\s*条/.test(back.error)) failed.push('错误没指到第 2 条：' + back.error);
  if (!/缺少匹配条件/.test(back.error)) failed.push('错误没说明缺什么：' + back.error);
});

check('category_drives_importance', (failed) => {
  const expected = { urgent: 5, action: 4, academic: 3, info: 3 };
  const back = R.cardsToRules(['urgent', 'action', 'academic', 'info'].map((category, index) => ({
    id: 'r' + index, keywordText: 'x', category: category, scopeMode: 'all', enabled: true,
  })));
  if (!back.ok) { failed.push(back.error); return; }
  back.rules.forEach((rule, index) => {
    const category = ['urgent', 'action', 'academic', 'info'][index];
    if (rule.action.payload.category !== category) failed.push('分类 ' + category + ' 没保住');
    if (rule.action.payload.importance !== expected[category]) {
      failed.push('分类 ' + category + ' 的重要程度是 ' + rule.action.payload.importance + '，应为 ' + expected[category]);
    }
    if (rule.action.action !== 'promote') failed.push('动作名应为 promote');
  });
});

check('order_and_default_name_preserved', (failed) => {
  const back = R.cardsToRules([
    { id: 'A', keywordText: 'a', category: 'action' },
    { id: '', keywordText: 'b', category: 'action' },
    { id: 'C', keywordText: 'c', category: 'action' },
  ]);
  if (!back.ok) { failed.push(back.error); return; }
  const ids = back.rules.map((rule) => rule.id);
  if (!same(ids, ['A', '规则 2', 'C'])) failed.push('顺序或默认名不对：' + JSON.stringify(ids));
});

check('scope_and_enabled_mapping', (failed) => {
  const all = R.cardsToRules([{ id: 'all', keywordText: 'x', scopeMode: 'all', enabled: false, category: 'info' }]);
  if (!all.ok) { failed.push(all.error); return; }
  if (all.rules[0].scope !== '*') failed.push('所有群应写 *：' + JSON.stringify(all.rules[0].scope));
  if (all.rules[0].enabled !== false) failed.push('启用开关丢了');
  const groups = R.cardsToRules([{ id: 'g', keywordText: 'x', scopeMode: 'groups', scopeText: '111，222', category: 'action' }]);
  if (!groups.ok) { failed.push(groups.error); return; }
  if (!same(groups.rules[0].scope, ['111', '222'])) failed.push('指定群解析：' + JSON.stringify(groups.rules[0].scope));
  const emptyScope = R.cardsToRules([{ id: 'g2', keywordText: 'x', scopeMode: 'groups', scopeText: ' ', category: 'action' }]);
  if (emptyScope.ok) failed.push('选了指定群却没填群 ID，居然通过了');
});

process.stdout.write(JSON.stringify(report));
"""


class LabRulesMappingTest(unittest.TestCase):
    report: dict = {}

    @classmethod
    def setUpClass(cls) -> None:
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("本机没有 node，跳过规则映射测试")
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "harness.js"
            script.write_text(HARNESS_JS, encoding="utf-8")
            result = subprocess.run(
                [node, str(script), str(LAB_RULES_JS)],
                capture_output=True,
                timeout=60,
            )
        if result.returncode != 0:
            raise AssertionError(
                "harness 崩溃，退出码 %d\nstderr:\n%s"
                % (result.returncode, result.stderr.decode("utf-8", "replace"))
            )
        try:
            cls.report = json.loads(result.stdout.decode("utf-8"))
        except json.JSONDecodeError as error:
            raise AssertionError(
                "harness 输出不是 JSON：%s\nstdout:\n%s" % (error, result.stdout.decode("utf-8", "replace"))
            ) from error
        if "fatal" in cls.report:
            raise AssertionError(str(cls.report["fatal"]))

    def _check(self, name: str) -> None:
        row = self.report.get(name)
        self.assertIsNotNone(row, "没有拿到场景 %s 的结果" % name)
        self.assertTrue(row.get("pass"), "%s: %s" % (name, row.get("detail")))

    def test_roundtrip_keeps_fields_the_form_does_not_show(self) -> None:
        self._check("roundtrip_preserves_hidden_fields")

    def test_keywords_split_on_comma_and_drop_blanks(self) -> None:
        self._check("keyword_split_trims_and_drops_empty")

    def test_at_me_and_regex_toggles_map_both_ways(self) -> None:
        self._check("at_me_and_regex_toggle")

    def test_empty_condition_is_rejected_with_rule_index(self) -> None:
        self._check("empty_condition_rejected_with_index")

    def test_category_drives_importance_for_all_four(self) -> None:
        self._check("category_drives_importance")

    def test_rule_order_and_default_names(self) -> None:
        self._check("order_and_default_name_preserved")

    def test_scope_and_enabled_mapping(self) -> None:
        self._check("scope_and_enabled_mapping")


class LabRulesLoadedByPageTest(unittest.TestCase):
    """page.html 必须在 settings.js 之前加载 lab-rules.js，否则卡片渲染拿不到映射函数。"""

    def test_script_order(self) -> None:
        html = (PROJECT_ROOT / "qq_live_digest" / "web_static" / "page.html").read_text(encoding="utf-8")
        self.assertIn('src="js/lab-rules.js"', html)
        self.assertLess(html.index('src="js/lab-rules.js"'), html.index('src="js/settings.js"'))

    def test_json_textarea_is_gone(self) -> None:
        settings = (PROJECT_ROOT / "qq_live_digest" / "web_static" / "js" / "settings.js").read_text(encoding="utf-8")
        self.assertNotIn("lab-rules-json", settings)
        self.assertNotIn("规则用 JSON 写", settings)


if __name__ == "__main__":
    unittest.main()
