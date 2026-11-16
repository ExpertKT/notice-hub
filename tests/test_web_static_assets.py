"""web_static/js/*.js 整文件语法检查。

为什么要有：settings.js 曾因一行行尾注释吞掉后面整段代码，设置页整块挂掉，
而当时没有任何用例真正解析过这些文件。这里用 ``node --check`` 做语法兜底；
node 不在 PATH 时 skip（不 fail），免得没装 Node 的机器全量门禁变红。

3D 地图带进来的 map3d.js 与 three.module.min.js 是 ES 模块：``node --check``
按文件扩展名判模块类型，.js 一律当 CommonJS，见到 ``import`` 直接报错。
模块脚本走 stdin 加 ``--input-type=module``，普通脚本仍然按文件检查（报错里留着路径）。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

JS_DIR = Path(__file__).resolve().parent.parent / "noticehub" / "web_static" / "js"
_MODULE_RE = re.compile(r"(?m)^[ \t]*(?:import|export)\b")


class WebStaticScriptSyntaxTests(unittest.TestCase):
    def test_every_js_file_parses(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node 不在 PATH，跳过 JS 语法检查")

        files = sorted(JS_DIR.glob("*.js"))
        self.assertTrue(files, f"没找到要检查的 JS 文件：{JS_DIR}")

        failures: list[str] = []
        modules = 0
        for path in files:
            text = path.read_text(encoding="utf-8", errors="replace")
            if _MODULE_RE.search(text):
                modules += 1
                proc = subprocess.run(
                    [node, "--input-type=module", "--check", "-"],
                    input=text,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
            else:
                proc = subprocess.run(
                    [node, "--check", str(path)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
            if proc.returncode != 0:
                failures.append(f"{path.name}:\n{(proc.stderr or '').strip()}")

        self.assertEqual(failures, [], "JS 语法检查失败：\n" + "\n\n".join(failures))
        self.assertGreater(modules, 0, "3D 地图的模块脚本没被当成模块检查，这条分支等于没跑")


if __name__ == "__main__":
    unittest.main()
