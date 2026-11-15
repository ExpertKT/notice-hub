# scripts

这里放发布前的独立冒烟脚本；它们在临时目录启动包，不接用户正在运行的实例。

| 路径 | 作用 |
| --- | --- |
| `smoke/cold_install_smoke.py` | 解压发布 ZIP 到 `%TEMP%`，注入端口/token，等 `/health`，检查版本和主页，再停止并删除临时实例。 |
| `smoke/__pycache__/` | Python 运行缓存，不是脚本输入。 |

常见改动：冷装验证运行 `python scripts/smoke/cold_install_smoke.py --zip packaging\QQ-Notice-Hub.zip --port 8877`；端口必须是临时空闲端口，脚本退出码 `0` 才算通过。
