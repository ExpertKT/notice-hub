# -*- coding: utf-8 -*-
"""用户可见文案的禁词检查：术语/机器话不许端到用户面前。

扫的是**用户能读到的字**，不是整个仓库：

- 前端：`page.html` 的文本节点与 `title` / `placeholder` / `aria-label` / `alt` 四个属性；
  `js/*.js` 与页面内联脚本里**含中文的字符串字面量**（纯英文那种是表头名、存储键、URL）。
- 后端：`PY_UI_SOURCES` 里显式列出的那几处 Python 字符串（AST 取字符串常量）。
  只收「会直接变成用户可见文案」的地方——异常原文、日志、docstring 不算 UI 文案。

不算文案的地方：`<script>` / `<style>` / `<pre>` 里的原文、HTML 与 JS 注释、
Python 的 docstring 与注释、日志调用、异常消息。

豁免只能**逐行显式标注**：在该行写 `copy-ok: 理由`（理由至少 4 个字），并且只免这一行。
不许按文件或目录整体豁免——那种豁免迟早把测试放空。
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_STATIC = ROOT / "qq_live_digest" / "web_static"

# 禁词：术语、机器话、开发者的词。大小写都不许（`api`/`API` 一样算）。
FORBIDDEN = ["OneBot", "API", "token", "webhook", "AppKey", "cookie",
             "incomplete", "未配置", "null", "undefined"]

COPY_OK = re.compile(r"copy-ok:\s*(\S.{3,})")
HTML_TEXT = re.compile(r">([^<>]+)<")
HTML_ATTR = re.compile(r"""(?:title|placeholder|aria-label|alt)\s*=\s*"([^"]*)\"""")
CJK = re.compile(r"[\u4e00-\u9fff]")
STRING_LITERAL = re.compile(
    r"'((?:[^'\\\n]|\\.)*)'"
    r'|"((?:[^"\\\n]|\\.)*)"'
)
INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)

# 后端要扫的来源清单：显式列出来，改名/挪位置就红，不许静默漏掉。
# 每条 = (相对路径, 选择器, 至少取到的字符串数, 说明)
# 选择器：
#   module                 整个模块（只用于「这个文件里的字符串全是给用户看的」）
#   function:<名字>         某个函数体（跳过它的 docstring）
#   dict_keys:a|b|c         dict 里这些键的字符串值（接口回给前端的 error/message）
PY_UI_SOURCES: tuple[tuple[str, str, int, str], ...] = (
    ("qq_live_digest/webapp.py", "function:_setup_advice", 24, "设置页体检：headline / label / hint"),
    ("qq_live_digest/webapp.py", "dict_keys:error|message|hint|next_step", 10, "/api 回给前端的 error / message 文案"),
    ("qq_live_digest/selfcheck.py", "module", 12, "自检行的状态句"),
    ("qq_live_digest/config_audit.py", "module", 18, "配置体检的 label / note"),
)

# 日志方法与异常类型：它们的参数是给开发者看的，不算 UI 文案。
LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
EXCEPTION_SUFFIXES = ("Error", "Exception", "Warning", "Exit")


def strip_markup(source: str) -> str:
    """去掉不算文案的部分，行号保持不变（用空行占位）。"""
    for pattern in (
        r"<script[^>]*>.*?</script>",
        r"<style[^>]*>.*?</style>",
        r"<pre[^>]*>.*?</pre>",
        r"<!--.*?-->",
    ):
        source = re.sub(pattern, lambda m: "\n" * m.group(0).count("\n"), source,
                        flags=re.S | re.I)
    return source


def hits_in(text: str) -> list[str]:
    lowered = text.lower()
    return [term for term in FORBIDDEN if term.lower() in lowered]


def scan_page_html() -> tuple[list[str], list[str]]:
    source = (WEB_STATIC / "page.html").read_text(encoding="utf-8")
    findings: list[str] = []
    exemptions: list[str] = []
    stripped = strip_markup(source)
    for line_no, line in enumerate(stripped.split("\n"), 1):
        if COPY_OK.search(line):
            exemptions.append("page.html:%d  %s" % (line_no, COPY_OK.search(line).group(1)))
            continue
        for match in HTML_TEXT.finditer(line):
            for term in hits_in(match.group(1)):
                findings.append("page.html:%d  正文含「%s」：%s"
                                % (line_no, term, match.group(1).strip()[:80]))
        for match in HTML_ATTR.finditer(line):
            for term in hits_in(match.group(1)):
                findings.append("page.html:%d  属性含「%s」：%s"
                                % (line_no, term, match.group(1).strip()[:80]))
    return findings, exemptions


def scan_script(source: str, label: str) -> tuple[list[str], list[str]]:
    findings: list[str] = []
    exemptions: list[str] = []
    for line_no, line in enumerate(source.split("\n"), 1):
        if COPY_OK.search(line):
            exemptions.append("%s:%d  %s" % (label, line_no, COPY_OK.search(line).group(1)))
            continue
        for match in STRING_LITERAL.finditer(line):
            literal = match.group(1) or match.group(2) or ""
            if not CJK.search(literal):
                continue  # 纯英文的是表头名 / 存储键 / URL，不是给用户看的
            for term in hits_in(literal):
                findings.append("%s:%d  文案含「%s」：%s"
                                % (label, line_no, term, literal.strip()[:80]))
    return findings, exemptions


# --------------------------------------------------------------- 后端（AST）
def _function_node(tree: ast.AST, name: str) -> ast.AST | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _is_docstring(node: ast.AST) -> bool:
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def _skip_statement(node: ast.AST) -> bool:
    """日志调用与 raise：参数是给开发者看的。"""
    if isinstance(node, ast.Raise):
        return True
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr in LOG_METHODS
    if isinstance(func, ast.Name):
        return func.id.endswith(EXCEPTION_SUFFIXES)
    return False


def collect_strings(node: ast.AST, *, dict_keys: set[str] | None = None) -> list[tuple[int, str]]:
    """收集这个节点里「会显示给用户」的字符串常量（行号, 文本）。"""
    found: list[tuple[int, str]] = []

    def add(value_node: ast.AST) -> None:
        if isinstance(value_node, ast.Constant) and isinstance(value_node.value, str):
            text = value_node.value
            if CJK.search(text):
                found.append((value_node.lineno, text))

    if dict_keys is None:
        def walk(current: ast.AST) -> None:
            if _skip_statement(current):
                return
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
                body = getattr(current, "body", [])
                for index, child in enumerate(body):
                    if index == 0 and _is_docstring(child):
                        continue
                    walk(child)
                return
            if isinstance(current, ast.Constant) and isinstance(current.value, str):
                add(current)
                return
            for child in ast.iter_child_nodes(current):
                walk(child)

        walk(node)
        return found

    # dict_keys 模式：只取这些键的字符串值（接口回给前端的那种）
    for child in ast.walk(node):
        if not isinstance(child, ast.Dict):
            continue
        for key_node, value_node in zip(child.keys, child.values):
            if not (isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)):
                continue
            if key_node.value not in dict_keys:
                continue
            for part in ast.walk(value_node):
                add(part)
    return found


def _dict_has_key_with_cjk(node: ast.AST, key: str) -> bool:
    """这棵树里有没有 dict 字面量用 key 这个键，且值里带中文。"""
    for child in ast.walk(node):
        if not isinstance(child, ast.Dict):
            continue
        for key_node, value_node in zip(child.keys, child.values):
            if not (isinstance(key_node, ast.Constant) and key_node.value == key):
                continue
            for part in ast.walk(value_node):
                if isinstance(part, ast.Constant) and isinstance(part.value, str) and CJK.search(part.value):
                    return True
    return False


def scan_python_copy(path: str, selector: str, minimum: int, note: str) -> tuple[list[str], list[str]]:
    """扫一处后端文案来源。取不到东西就报错——防线失效必须显性红。"""
    file_path = ROOT / path
    if not file_path.is_file():
        raise AssertionError("文案来源失效：%s 不存在（%s）" % (path, note))
    source = file_path.read_text(encoding="utf-8")
    lines = source.split("\n")
    tree = ast.parse(source, filename=str(file_path))

    if selector == "module":
        target: ast.AST | None = tree
        keys: set[str] | None = None
    elif selector.startswith("function:"):
        name = selector.split(":", 1)[1]
        target = _function_node(tree, name)
        keys = None
        if target is None:
            raise AssertionError("文案来源失效：%s 里找不到函数 %s（%s）" % (path, name, note))
    elif selector.startswith("dict_keys:"):
        target = tree
        keys = {item for item in selector.split(":", 1)[1].split("|") if item}
    else:
        raise AssertionError("未知选择器 %r（%s）" % (selector, note))

    strings = collect_strings(target, dict_keys=keys) if target is not None else []
    if keys is not None:
        # dict_keys 模式要逐个键验：只数总量的话，某个字段被改名后总数照样够，防线就空了。
        for key in sorted(keys):
            if not any(_dict_has_key_with_cjk(target, key) for _ in (0,)):
                raise AssertionError(
                    "文案来源失效：%s 里没有 %r 这个键的中文字符串（%s）——字段改名了" % (path, key, note))
    if len(strings) < minimum:
        raise AssertionError(
            "文案来源失效：%s（%s）只取到 %d 条字符串，少于约定的 %d 条——"
            "函数/字段改名了，或者这条防线已经扫空了" % (path, note, len(strings), minimum))

    findings: list[str] = []
    exemptions: list[str] = []
    for line_no, text in strings:
        line = lines[line_no - 1] if 0 < line_no <= len(lines) else ""
        exempt = COPY_OK.search(line)
        if exempt:
            exemptions.append("%s:%d  %s" % (path, line_no, exempt.group(1)))
            continue
        for term in hits_in(text):
            findings.append("%s:%d  文案含「%s」：%s" % (path, line_no, term, text.strip()[:80]))
    return findings, exemptions


class UserVisibleCopyTermsTest(unittest.TestCase):
    def test_python_side_copy_has_no_jargon(self) -> None:
        """Python 里拼给用户看的文案也要过同一份禁词表。

        `page.html` / `js/*.js` 那半边只罩住前端；设置页体检、自检行、配置体检、
        几个按钮的回话都是 Python 里的字符串。一条防线只罩住半个产品，
        和 CI 静默跳过是同一类问题。
        """
        findings: list[str] = []
        exemptions: list[str] = []
        for path, selector, minimum, note in PY_UI_SOURCES:
            found, exempt = scan_python_copy(path, selector, minimum, note)
            findings += found
            exemptions += exempt

        if findings:
            self.fail(
                "Python 侧用户可见文案里有 %d 处禁词（术语/机器话）。改文案，别改测试；"
                "确实必须用（比如提示里要指着屏幕上的按钮名）就在那一行写 `copy-ok: 理由`：\n%s"
                % (len(findings), "\n".join(findings)))
        print("Python 侧文案禁词检查：0 命中，%d 处显式豁免" % len(exemptions))
        for item in exemptions:
            print("  豁免 %s" % item)

    def test_no_jargon_in_user_visible_copy(self) -> None:
        findings: list[str] = []
        exemptions: list[str] = []

        page_findings, page_exemptions = scan_page_html()
        findings += page_findings
        exemptions += page_exemptions

        for path in sorted((WEB_STATIC / "js").glob("*.js")):
            found, exempt = scan_script(path.read_text(encoding="utf-8"), "js/" + path.name)
            findings += found
            exemptions += exempt

        source = (WEB_STATIC / "page.html").read_text(encoding="utf-8")
        for index, block in enumerate(INLINE_SCRIPT.findall(source)):
            found, exempt = scan_script(block, "page.html 内联脚本#%d" % index)
            findings += found
            exemptions += exempt

        if findings:
            self.fail(
                "用户可见文案里有 %d 处禁词（术语/机器话）。改文案，别改测试；"
                "确实必须要用（比如照着做的提示原文）就在那一行写 `copy-ok: 理由`：\n%s"
                % (len(findings), "\n".join(findings)))
        # 豁免必须看得见：把当前豁免条数钉在输出里，防止悄悄膨胀
        print("用户可见文案禁词检查：0 命中，%d 处显式豁免" % len(exemptions))
        for item in exemptions:
            print("  豁免 %s" % item)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
