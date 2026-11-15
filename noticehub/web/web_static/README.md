# web_static

网页静态资源由 `webapp.py` 直接读取，开发者和浏览器都使用这里的文件。

| 路径 | 作用 |
| --- | --- |
| `page.html` | 主页面的 HTML、CSS 和大量页面交互；它是单文件巨页。 |
| `js/settings.js` | 设置表单、推送配置、更新检查和来源选择。 |
| `js/guide.js` | QQ 接入引导、二维码刷新和群选择。 |
| `js/pair.js` | 手机配对和防火墙状态。 |
| `js/announce.js` | 公告、铃铛、公告面板和弹窗。 |
| `js/notice.js` | 右上角通知栈、详情弹窗和关闭行为。 |
| `js/lab-rules.js` | 实验室规则表单和关键词控件。 |
| `manifest.webmanifest` | PWA 名称、图标和启动属性。 |
| `icon.svg` | PWA 和网页图标。 |

没有前端构建步骤，改外置脚本后刷新即可看到新文件；`page.html` 由运行中的 Python 进程导入时读入，改它要重载自己的开发实例。HTTPS 下 service worker 可能返回缓存壳，遇到旧内容先强制刷新。改 `page.html` 或脚本后跑 `.\.venv\Scripts\python.exe -X utf8 -m unittest tests.test_web_static_assets tests.test_ui_browser`，浏览器用例需 `NH_REQUIRE_BROWSER=1`。
