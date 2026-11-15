# assets

这里放应用图标源文件和生成器，PyInstaller 规格与启动器会读取它们。

| 文件 | 作用 |
| --- | --- |
| `make_icon.py` | 用 Pillow 确定性生成 ICO、PNG 和 `.reports` 预览。 |
| `icon.svg`、`logo.svg` | 网页/包内矢量图标和品牌源图。 |
| `icon.ico` | Windows 启动器和服务包图标。 |
| `icon-16.png`、`icon-256.png`、`icon-512.png` | 小尺寸检查、托盘和高分辨率 PNG。 |
| `icon-256-beta.png`、`icon-beta.ico` | beta 通道图标。 |
| `donate-wechat.jpg` | 包内捐赠二维码图片。 |

常见改动：改图形后运行 `.\.venv\Scripts\python.exe assets\make_icon.py`，再跑 `tests.test_launcher_watchdog tests.test_web_static_assets`；生成的 `.reports` 只作预览，不提交。
