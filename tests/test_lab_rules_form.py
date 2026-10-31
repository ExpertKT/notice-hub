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
LAB_RULES_JS = PROJECT_ROOT / "noticehub" / "web_static" / "js" / "lab-rules.js"


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
if (!R.chips || typeof R.chips.merge !== 'function' || typeof R.chips.drop !== 'function') {
  process.stdout.write(JSON.stringify({ fatal: 'window.NhLabRules.chips 没有导出标签输入的核心函数' }));
  process.exit(0);
}
const C = R.chips;

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

check('chip_enter_commits_one_word', (failed) => {
  // 回车走的就是这一个函数：把输入框里那一段收成一个标签。
  const one = C.merge([], '报名', false);
  if (!same(one, ['报名'])) { failed.push('回车收一个词：' + JSON.stringify(one)); return; }
  const kept = C.merge(one, '', false);
  if (!same(kept, ['报名'])) failed.push('空提交动了已有标签：' + JSON.stringify(kept));
  const trimmed = C.merge([], '  截止  ', false);
  if (!same(trimmed, ['截止'])) failed.push('首尾空白没去掉：' + JSON.stringify(trimmed));
});

check('chip_comma_and_paste_split', (failed) => {
  const commas = C.merge([], '报名, 截止，开会、生日;值班；', false);
  if (!same(commas, ['报名', '截止', '开会', '生日', '值班'])) { failed.push('逗号/顿号/分号没拆开：' + JSON.stringify(commas)); return; }
  const pasted = C.merge([], 'a,b\nc d', true);
  if (!same(pasted, ['a', 'b', 'c', 'd'])) failed.push('粘贴多值没拆开：' + JSON.stringify(pasted));
  const typedSpace = C.merge([], '期末 考试', false);
  if (!same(typedSpace, ['期末 考试'])) failed.push('手打空格应保留为一个词：' + JSON.stringify(typedSpace));
});

check('chip_dedupe_silently', (failed) => {
  const twice = C.merge(['报名'], '报名，截止', false);
  if (!same(twice, ['报名', '截止'])) failed.push('重复词没去重：' + JSON.stringify(twice));
  const selfDup = C.merge([], '开会,开会, 开会 ', false);
  if (!same(selfDup, ['开会'])) failed.push('同一段里的重复没去重：' + JSON.stringify(selfDup));
});

check('chip_delete_one', (failed) => {
  const dropped = C.drop(['报名', '截止', '开会'], 1);
  if (!same(dropped, ['报名', '开会'])) failed.push('× 删中间一个：' + JSON.stringify(dropped));
  const last = C.drop(['报名', '截止'], 1);
  if (!same(last, ['报名'])) failed.push('退格删最后一个：' + JSON.stringify(last));
  const outOfRange = C.drop(['报名'], 5);
  if (!same(outOfRange, ['报名'])) failed.push('越界删除改了列表：' + JSON.stringify(outOfRange));
  const untouched = ['报名'];
  C.drop(untouched, 0);
  if (!same(untouched, ['报名'])) failed.push('删除把原数组改了（界面会丢已输内容）');
});

check('chip_submit_is_string_array', (failed) => {
  const keywords = ['报名', '截止', '开会'];
  const back = R.cardsToRules([{ id: 'k', keywordText: keywords.join(', '), scopeMode: 'all', category: 'action', enabled: true }]);
  if (!back.ok) { failed.push(back.error); return; }
  const sent = back.rules[0].match.keyword;
  if (!Array.isArray(sent)) { failed.push('提交出去的 match.keyword 不是数组：' + JSON.stringify(sent)); return; }
  if (!same(sent, keywords)) failed.push('提交出去的关键词不对：' + JSON.stringify(sent));
  const empty = R.cardsToRules([{ id: 'e', keywordText: C.merge([], '', false).join(', '), atMe: true, category: 'action' }]);
  if (!empty.ok) { failed.push(empty.error); return; }
  if ('keyword' in empty.rules[0].match) failed.push('没有标签却塞了 keyword：' + JSON.stringify(empty.rules[0].match));
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

    def test_keyword_chip_enter_commits_one_word(self) -> None:
        self._check("chip_enter_commits_one_word")

    def test_keyword_chip_comma_and_paste_split(self) -> None:
        self._check("chip_comma_and_paste_split")

    def test_keyword_chip_dedupes_silently(self) -> None:
        self._check("chip_dedupe_silently")

    def test_keyword_chip_delete_one(self) -> None:
        self._check("chip_delete_one")

    def test_keyword_chip_submits_string_array(self) -> None:
        self._check("chip_submit_is_string_array")


class KeywordChipWidgetTest(unittest.TestCase):
    """标签输入的可达性等只存在于 DOM 层的要求：用源码锁住，防止被顺手删掉。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = LAB_RULES_JS.read_text(encoding="utf-8")

    def test_remove_button_is_a_real_button_with_aria_label(self) -> None:
        source = self.source
        self.assertIn("createElement('button')", source)
        self.assertIn("'aria-label', '移除' + noun", source)
        # × 必须是 button，不能是可点的 span（键盘 Tab 到不了）。
        self.assertIn("className = 'nh-chip-x'", source)

    def test_same_widget_covers_the_other_comma_fields(self) -> None:
        source = self.source
        self.assertIn("'input.lab-rule-senders'", source)
        self.assertIn("'input.lab-rule-grouptext'", source)
        # 正则字段自己就可能带逗号（a{1,2}），拆成标签会拆坏表达式。
        self.assertNotIn("input.lab-rule-regextext", source)

    def test_chip_and_remove_target_are_tappable(self) -> None:
        self.assertIn(".nh-chip{display:inline-flex;align-items:center;max-width:100%;min-height:44px", self.source)
        self.assertIn(".nh-chips .nh-chip-x{flex:none;min-width:44px;min-height:44px", self.source)

    def test_keyboard_and_paste_paths_are_wired(self) -> None:
        source = self.source
        self.assertIn("addEventListener('keydown'", source)
        self.assertIn("key === 'Enter'", source)
        self.assertIn("key === 'Backspace'", source)
        self.assertIn("addEventListener('paste'", source)
        self.assertIn("addEventListener('blur'", source)
        # 中文输入法选词的回车不能被当成「提交关键词」。
        self.assertIn("event.isComposing", source)

    def test_new_cards_are_enhanced_too(self) -> None:
        # 规则卡片每次装载都会重建，只绑定首屏那些输入框是不够的。
        self.assertIn("MutationObserver", self.source)
        self.assertIn("CHIP_FIELDS", self.source)


class LabRulesLoadedByPageTest(unittest.TestCase):
    """page.html 必须在 settings.js 之前加载 lab-rules.js，否则卡片渲染拿不到映射函数。"""

    def test_script_order(self) -> None:
        html = (PROJECT_ROOT / "noticehub" / "web_static" / "page.html").read_text(encoding="utf-8")
        self.assertIn('src="js/lab-rules.js"', html)
        self.assertLess(html.index('src="js/lab-rules.js"'), html.index('src="js/settings.js"'))

    def test_json_textarea_is_gone(self) -> None:
        settings = (PROJECT_ROOT / "noticehub" / "web_static" / "js" / "settings.js").read_text(encoding="utf-8")
        self.assertNotIn("lab-rules-json", settings)
        self.assertNotIn("规则用 JSON 写", settings)


if __name__ == "__main__":
    unittest.main()
