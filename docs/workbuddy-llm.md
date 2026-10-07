# WorkBuddy 提取引擎登录

程序通过 CodeBuddy headless CLI 调用 WorkBuddy；未登录时 `auto` 后端会回退 Ollama。

## 登录（必须在 CLI 内部输入）

也可一次启动本地 Web UI（默认会生成一次性访问密码并显示在终端；不要分享该 URL/密码）：
`node "D:\Workbuddy\resources\app.asar.unpacked\cli\dist\codebuddy-headless.js" --serve --open`
终端会显示 Endpoint、Web UI URL 和 Password，`--open` 会请求浏览器打开 Web UI；在 Web UI 内完成登录。

1. 在真实终端启动 CLI（路径按本机安装位置调整）：
   `node "D:\Workbuddy\resources\app.asar.unpacked\cli\dist\codebuddy-headless.js"`
2. 等待交互提示出现后，在 **CLI 内部**输入 `/login`，按提示完成登录。
   不要在 PowerShell 直接输入 `/login`，那会得到 `CommandNotFoundException`。
3. 退出交互会话后验证：
   `node "D:\Workbuddy\resources\app.asar.unpacked\cli\dist\codebuddy-headless.js" -p --tools "" --output-format text "Reply with exactly OK"`
   预期标准输出为 `OK`；若出现 `Authentication required` 或空输出，说明尚未登录。

认证产物的确切位置尚未确认，不要复制或提交任何凭据。换机器时需要重新安装/定位 CLI，并在 CLI 内重新执行 `/login`。

## 配置

可选环境变量：`QQ_DIGEST_LLM_BACKEND=auto`（默认；优先 CodeBuddy，失败回退 Ollama）、`codebuddy`（缺 CLI 或调用失败时报错）或 `ollama`；也可设置 `QQ_DIGEST_CODEBUDDY_CLI` 指向 headless JavaScript 文件。
