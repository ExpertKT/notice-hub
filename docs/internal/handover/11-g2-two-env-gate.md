# 11-g2-two-env-gate.md — G2 双环境门禁

用户要求：**G2 必须在「无软依赖」与「全依赖」两种环境各跑一遍，skip 不算通过。**

---

## 1. 为什么单环境不够

`tests.md` 第 2 节列过：软依赖缺失时用例是 **skip 而不是 fail**。
所以：

- 只跑**无软依赖**环境 → 绿灯，但 playwright 浏览器路径、PDF/Excel 附件路径**根本没测**。
- 只跑**全依赖**环境 → 绿灯，但看不出「外派方在干净机器上会 skip 掉多少」。

两种环境都跑，才能回答「哪些路径在哪种环境下被测到了」。

## 2. 两种环境的确切定义

### E1 无软依赖环境

```powershell
python -m venv .venv-e1
.\.venv-e1\Scripts\python.exe -m pip install -r requirements.txt
# 不装 requirements-dev.txt（无 playwright）
# 不装 pypdf / pypdfium2 / openpyxl（先在 E1 里卸掉验证）
.\.venv-e1\Scripts\python.exe -m pip uninstall -y pypdf pypdfium2 openpyxl
```

> `requirements.txt` 里有 `pypdf`/`pypdfium2`/`openpyxl`，所以「无软依赖」
> 必须显式卸。这一点容易搞错，写成步骤。

### E2 全依赖环境

```powershell
python -m venv .venv-e2
.\.venv-e2\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
.\.venv-e2\Scripts\python.exe -m playwright install msedge
# 或直接 channel="msedge" 用 runner 预装 Edge（tests/test_ui_browser.py 的约定）
```

## 3. 跑法（两种环境各一遍，串行）

```powershell
# E1
.\.venv-e1\Scripts\python.exe -m unittest discover -s tests -t .

# E2
.\.venv-e2\Scripts\python.exe -m unittest discover -s tests -t .
```

**不要并发**：测试起 HTTP 服务用随机端口，并发会互相打架（tests.md 第 4 节）。

## 4. 判据（skip 不算通过）

| 环境 | 通过判据 |
|---|---|
| E1 | `exit 0`，且 **skip 数 = 已知软依赖缺失数**（不是「比 E2 少几个」这种相对说法） |
| E2 | `exit 0`，且 **skip 数 = 0** 或仅剩平台性 skip（非 Windows） |

**关键动作：把两边的 skip 集合做差。**

```powershell
$e1 = .\.venv-e1\Scripts\python.exe -m unittest discover -s tests -t . 2>&1
$e2 = .\.venv-e2\Scripts\python.exe -m unittest discover -s tests -t . 2>&1
# 逐条比对 skipped 列表，E2 有而 E1 没有的那些 = 只有全依赖才测到的路径
```

E2 跑通而 E1 skip 的每一条，都必须在回归报告里点名（测试 id + 为什么 skip）。
**不许用「E1 也 OK」一句话带过。**

## 5. 本机现状（只有 Windows，只能给一半）

本机 `.venv` 装齐 `requirements.txt`、**未装** `requirements-dev.txt`，
所以本机是「部分软依赖」环境，既不是 E1 也不是 E2。
实测三遍 `Ran 1325 tests / OK (skipped=1)`，那 1 条是 `test_ui_browser.py:45`。

**本机无法完成 G2 双环境门禁**，缺的是：
- E1 需要一个卸掉 pypdf/pypdfium2/openpyxl 的 venv（可建，但要几分钟）
- E2 需要 playwright + Edge（本机未装）

这两步我可以在本机补 E1；E2 要装 playwright（约百 MB），
按「不往 C 盘装东西」的约束需先确认。

---

## 必须由人补的项

1. **是否允许在本机装 playwright**（E2 环境前置）。
2. **CI 上能否加一个 E1 job**：现在 `browser` job 只覆盖 E2 的浏览器路径，
   没有 job 覆盖「缺 pypdf/openpyxl」的场景。加一个 job 是永久解，
   但改 CI 要用户点头。
3. **平台性 skip 的白名单**：非 Windows 的 skip 哪些算「预期」、哪些算「漏测」，
   需要逐条过一遍才能写死判据。
