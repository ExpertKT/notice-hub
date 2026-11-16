"""web_static/js/*.js 整文件语法检查。

为什么要有：settings.js 曾因一行行尾注释吞掉后面整段代码，设置页整块挂掉，
而当时没有任何用例真正解析过这些文件。这里用 ``node --check`` 做语法兜底；
node 不在 PATH 时 skip（不 fail），免得没装 Node 的机器全量门禁变红。

3D 地图带进来的 map3d.js 与 three.module.min.js 是 ES 模块，而目录里的其它脚本是
经典脚本。``node --check`` 按扩展名判语法：.js 会跟着上层的 package.json 走，
同一条命令在不同目录下结论不一样（nh-tmp 下有个 package.json 就把模块当 CommonJS 判）。
所以这里把源文本各写一份 .mjs 与 .cjs 再检查，两种语法任一通过就算过，
跟跑测试时所在目录无关；两边都失败才算语法错误。
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

JS_DIR = Path(__file__).resolve().parent.parent / "noticehub" / "web_static" / "js"


class WebStaticScriptSyntaxTests(unittest.TestCase):
    def test_every_js_file_parses(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node 不在 PATH，跳过 JS 语法检查")

        files = sorted(JS_DIR.glob("*.js"))
        self.assertTrue(files, f"没找到要检查的 JS 文件：{JS_DIR}")

        failures: list[str] = []
        esm_only = 0
        with tempfile.TemporaryDirectory(prefix="nh-js-syntax-") as tmp:
            for path in files:
                text = path.read_text(encoding="utf-8", errors="replace")
                results = {}
                for suffix in (".mjs", ".cjs"):
                    probe = Path(tmp) / f"{path.stem}{suffix}"
                    probe.write_text(text, encoding="utf-8")
                    results[suffix] = subprocess.run(
                        [node, "--check", str(probe)],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    )
                if results[".mjs"].returncode == 0:
                    if results[".cjs"].returncode != 0:
                        esm_only += 1
                    continue
                if results[".cjs"].returncode != 0:
                    failures.append(f"{path.name}:\n{(results['.mjs'].stderr or '').strip()}")

        self.assertEqual(failures, [], "JS 语法检查失败：\n" + "\n\n".join(failures))
        self.assertGreaterEqual(esm_only, 1, "3D 地图的模块脚本没走到模块语法解析，这条分支等于没跑")


if __name__ == "__main__":
    unittest.main()
