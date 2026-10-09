# 接入大模型 API

没有本地模型也能用。复制下面对应路线到 `.env`，改好 key 后重启服务或在网页设置中保存。

## A. 免费层（适合先试用）

可从这些厂商或聚合清单寻找免费档：智谱 GLM flash、硅基流动免费模型、Google Gemini 免费档、Groq、OpenRouter 免费模型；也可参考 [free-llm-intel](https://github.com/rockbenben/free-llm-intel) 和 [awesome-free-llm-api](https://github.com/peter123023/awesome-free-llm-api)。额度以厂商页面为准，免费档通常有速率或每日上限。

把厂商提供的 OpenAI 兼容地址和模型名填入：

```dotenv
QQ_DIGEST_LLM_API_KEY=你的key
QQ_DIGEST_LLM_ENDPOINT=厂商提供的/v1/chat/completions
QQ_DIGEST_LLM_MODEL=厂商提供的模型名
```

去哪拿 key：对应厂商控制台。验证：运行 `python main.py doctor`，再发一条会触发摘要的群消息，查看网页最近推送或日志。成本：免费档通常为 0，但额度、速率和可用性以厂商页面为准；免费档不一定支持 Vision，不能读图时可将 `QQ_DIGEST_VISION=0`。

## B. 便宜官方 API（推荐）

DeepSeek 的 OpenAI 兼容 API 支持 Vision 的 `deepseek-flash`，适合同时做摘要和读图。去 [platform.deepseek.com](https://platform.deepseek.com/) 创建 key，照抄三行：

```dotenv
QQ_DIGEST_LLM_API_KEY=你的key
QQ_DIGEST_LLM_ENDPOINT=https://api.deepseek.com/v1/chat/completions
QQ_DIGEST_LLM_MODEL=deepseek-flash
QQ_DIGEST_VL_MODEL=deepseek-flash
```

`QQ_DIGEST_LLM_API_KEY` 是通用变量名，任何 OpenAI 兼容厂商的 key 都能填；旧版 `DASHSCOPE_API_KEY` 仍可用。验证：运行 `python main.py doctor`，随后触发一条摘要并检查网页是否显示 AI 摘要。

读图：`deepseek-flash` 支持图片输入（见 [官方图像理解文档](https://api-docs.deepseek.com/zh-cn/guides/vision/)），每张图无论多大都最多折算 **1024 token**，所以「待办 + 截图」这种量级依旧很便宜。本项目只在 `QQ_DIGEST_VL_MODEL` 被显式设置时才读图（上面第 4 行），没设就跟别的文本端点一样只记录图片名，不会去撞 404、也不花 token。

成本估算依据本项目实测 4 天 29 条摘要（其中 16 条调用 LLM），按每月约 120 次、输入约 18 万 token、输出约 2.4 万 token，DeepSeek flash 低谷价约 $0.04/月；流量翻 10 倍预计仍低于 $0.5/月。价格为 2026-10-09 抓取的当天信息，具体以厂商页面为准：未命中缓存输入每 1M token $0.15（低谷）/$0.30（高峰），输出 $0.60/$1.20，命中缓存输入 $0.003/$0.006。高峰为北京时间周一至周五 09:00–12:00、14:00–18:00，其余时间、周末和中国法定节假日按低谷价计。

## C. 本地 Ollama（零 API 费用）

适合已有模型和显卡的电脑，不需要 key；下载并启动 Ollama 后使用：

```dotenv
QQ_DIGEST_LLM_API_KEY=
QQ_DIGEST_LLM_ENDPOINT=http://127.0.0.1:11434/v1/chat/completions
QQ_DIGEST_LLM_MODEL=qwen3.6:35b-a3b-q4_k_m-gpu20
```

去哪找模型：Ollama 模型库和本机 Ollama 管理器。验证：打开 `http://127.0.0.1:11434/v1/models`，确认模型存在，再运行 `python main.py doctor`。成本：API 费用为 0，但需要本机磁盘、内存和电费；没有多模态模型时把 `QQ_DIGEST_VISION=0`。

## Key 变量兼容说明

新配置使用 `QQ_DIGEST_LLM_API_KEY`。旧版 `DASHSCOPE_API_KEY` 仍然支持；两个都填写时优先使用新变量。
