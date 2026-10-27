# 09-page-source-assertions.md — PAGE_SOURCE 断言口径核对

用户拍板：**按 255 条入材料**，325 作粗估不入材料；抽 10 条核对差异。

---

## 1. 三个口径的实测数字

| 口径 | 定义 | 数字 | 出处 |
|---|---|---|---|
| **A（本文采用）** | `PAGE_SOURCE`/`PAGE_HTML` 在**全 tests 目录**的出现次数 | **623** | 本机 grep 实测 |
| A′ | 同上但只数 `test_webapp.py` | 572 | 同左 |
| **B** | `assertIn(..., PAGE_SOURCE/PAGE_HTML)` **行数** | **406**（`test_webapp.py`）+ 41（`test_page_beta11.py`）= **447** | 同左 |
| C（backlog 口径） | 待清理条目 | 325 | `docs/qa/backlog-beta12.md:23,59` 自称 |

**用户说的「255」与三个口径都不等。** 最接近的是 backlog 里那句
「`PAGE_SOURCE` 内容断言仍有 325 条」。255 这个数字**在仓库里找不到来源**。

## 2. 抽样 10 条逐条核对

| # | 行 | 断言 | 判定 |
|---|---|---|---|
| 1 | `test_webapp.py:207` | `assertIn("document.documentElement.dataset.theme='light'", PAGE_SOURCE)` | 锁源码片段；该串在 `page.html` 仍在 |
| 2 | `:210` | `assertIn('<button id="history-cancel" ...>取消回溯</button>', PAGE_SOURCE)` | 锁 HTML 源码；仍在 |
| 3 | `:211` | `assertIn("/api/history/fetch/cancel", PAGE_SOURCE)` | 锁接口路径字符串；仍在 |
| 4 | `:212` | `assertIn("status.stage === 'cancelled'", PAGE_SOURCE)` | 锁 JS 源码片段；仍在 |
| 5 | `:213` | `assertIn("已取消", PAGE_SOURCE)` | 锁文案；仍在 |
| 6 | `:214` | `assertIn("if (status.running)", PAGE_SOURCE)` | 锁 JS 源码片段；仍在 |
| 7 | `:314` | `assertIn('id="pref-theme"', PAGE_SOURCE)` | **锁 `settings.js` 里 `innerHTML` 拼出来的字符串**（`js/settings.js:605`），`page.html` 里没有 |
| 8 | `:316` | `assertIn('value="' + value + '"', PAGE_SOURCE)` | 同 #7，锁 innerHTML 片段 |
| 9 | `:320` | `assertIn("function initThemePreference()", PAGE_SOURCE)` | 锁 JS 函数名；仍在 |
| 10 | `:333` | `assertIn('<fieldset id="pref-theme" class="theme-choice-list">', PAGE_SOURCE)` | 同 #7，锁 innerHTML 片段 |

**10 条全部属于「锁源码片段」类**，没有一条是行为断言。
与 backlog 的描述「字符串断言锁死实现细节，改文案就红，同时测不出真实行为」一致。

## 3. 口径差异的原因

`PAGE_SOURCE` 的定义（`tests/test_webapp.py:47-53`）：

```python
PAGE_DIR = .../web_static
PAGE_HTML   = _read_normalized(PAGE_DIR / "page.html")
PAGE_SCRIPT = "".join(... for p in sorted((PAGE_DIR / "js").glob("*.js")))
PAGE_SOURCE = PAGE_HTML + PAGE_SCRIPT
```

所以 `PAGE_SOURCE` **既含 page.html 也含四个 js**。三个口径差在：

- **A（623）** 数的是「PAGE_SOURCE/PAGE_HTML 这两个词出现多少次」——
  包含定义处、注释、`assertNotIn`、以及同一行里出现两次。
- **B（447）** 数的是「`assertIn(..., PAGE_*)` 的行数」——一条断言一行。
- **C（325）** 是 backlog 自己记的「待清理条目」，**没有给出数法**，
  且 `:23` 与 `:59` 两处都写 325，无法复现。

## 4. 结论与建议

1. **「255」这个数字无出处**，建议入材料时改用 **B = 447**（`assertIn` 行数，
   可复现、可逐条清），或 A = 623（引用次数）。
2. **backlog 的 325 应当作粗估废弃**，理由是它不可复现，且与实测两个口径都对不上。
3. **抽样 10 条 100% 是源码片段断言**，所以「这批要清理」的结论不变，
   变的是**工程量口径**：447 条断言，按 backlog 自报「两周量」反推约 22 条/人日。

---

## 必须由人补的项

1. **255 的原始出处**：用户记忆中的数字，仓库与 git log 里都找不到。
   若来自外部估算，需要确认是否要把「447」还是「623」写回给用户。
2. **哪些 `assertNotIn` 也算清理量**：A 口径含 `assertNotIn`，B 口径不含。
3. **`test_page_beta11.py` 的 41 条是否同批清理**：它锁的是 beta.11 页快照，
   可能整文件作废更省。
