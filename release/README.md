# release

这里放更新清单及离线签名工具，发布门禁和客户端更新器读取它们。

| 文件 | 作用 |
| --- | --- |
| `update-manifest.json` | 发布版本、下载地址、哈希、补丁和签名清单。 |
| `manifest-pubkey.json` | 客户端验签使用的公钥数据。 |
| `sign-manifest.py` | 用外部私钥签发或用 `--check` 验证清单；私钥不进仓库。 |
| `__pycache__/` | Python 运行缓存，不是发布输入。 |

常见改动：只验签运行 `python release/sign-manifest.py --check`；发布清单结构变化同时跑 `.\.venv\Scripts\python.exe -X utf8 -m unittest tests.test_update_signing tests.test_verify_manifest`。
