# 批次 D 行为规格（clean-room 重写输入）

本文件只描述**行为**。实现者不需要、也不应该去看旧代码；照这里写出来的东西必须能通过现有测试，
并且与现有对外行为一致。凡是现有测试没锁死、又会影响兼容性的地方，集中列在 §10「需要拍板」，
实现前先按那里的结论做；没有结论的按本文正文写。

覆盖模块（1 个文件，包名 `noticehub`）：

| 模块 | 职责一句话 |
|---|---|
| `noticehub/attachments.py` | 群文件/群图片：事件解析成附件对象、下载、文本与 PDF 解析、视觉识别、按群限速的后台队列、原图留存与清理 |

模块还有一层编排实现住在别处（`noticehub/application/` 下的同名模块），
本文件把两边都当成本模块的公开面来写——因为对外 import 路径是 `noticehub.attachments`，
调用方和测试都从这里取名字。实现可以放在任意内部位置，但**下面 §2 列的每个名字都必须能从
`noticehub.attachments` 导入到同样的对象**。

---

## 0. 通用约定

**0.1 时间。** 记录里的时间是本地 naive、秒精度 ISO 字符串（形如 `2026-09-29T01:00:00`）。
**0.2 异常。** 模块只自定义一个异常 `AttachmentError`，继承 `RuntimeError`；`str(异常)` 就是给用户/日志看的完整中文句子。内部另有从重试模块借用的三类错误（请求错误、响应错误、基类），重试耗尽后必须统一包成 `AttachmentError` 再抛出。
**0.3 日志。** 标准库 `logging`，频道名见 §7。日志文本属于运维契约（有人按文本 grep 排障）。
**0.4 配置。** 配置对象由外部注入，模块自己不读环境变量；键与默认值见 §5。
**0.5 不做的事。** 本模块不写数据库（只产出记录交给回调）、不弹窗、不启动子进程、不执行附件内容。
**0.6 线程。** 除「后台队列」那一节明确说的一个线程外，其余函数都是可重入的同步函数，不加锁、不留全局状态（那张「最近见过哪些附件」的表属于队列实例自己）。

---

## 1. 公开面

### 1.1 数据对象与异常

- `Attachment`：数据类（可变），字段与默认值：

| 字段 | 类型 | 默认 |
|---|---|---|
| `kind` | `str`，取值 `"file"` 或 `"image"` | 无默认（必填） |
| `name` | `str` | 无默认（必填） |
| `group_id` / `group_name` / `sender_id` / `sender_name` / `url` / `file_id` / `busid` / `ts` / `msg_id` / `hint` | `str` | `""` |
| `size` | `int` | `0` |

- 三个只读派生属性：
  - `suffix -> str`：`name` 的小写后缀（带点）；`name` 为空时为空串。
  - `key -> str`：`sha1("<群号>|<kind>|<name>|<size>")` 的**前 16 位十六进制小写**。只由这四项决定。
  - `size_text -> str`：`size<=0` 返回空串；`<1024` 返回 `"<n>B"`；`<1MiB` 返回四舍五入到整数的 `"<n>KB"`；否则返回一位小数的 `"<n.n>MB"`。

- `AttachmentError(RuntimeError)`：附件处理失败（文案见各契约条目）。
- `AttachmentStats`：数据类，字段 `queued`、`processed`、`skipped`、`failed`、`last_error`、`last_at`（前四个 `int` 默认 0、后两个 `str` 默认空串）加 `counters: dict[str,int]`；方法 `as_dict()` 返回六个固定键，再把 `counters` 的键平铺合并进去。

### 1.2 事件解析

- `attachments_from_message(payload: Any, settings) -> list[Attachment]`
- `attachment_from_notice(payload: Any, settings) -> Attachment | None`
- `attachments_from_history(message: dict, settings) -> list[Attachment]`

### 1.3 文本与文档解析

- `decode_text_bytes(data: bytes) -> str`
- `extract_text(path: Path, name: str = "") -> str`
- `extract_bytes(data: bytes, suffix: str) -> str`
- `extract_docx(path) / extract_pptx(path) / extract_pdf(path) / extract_xlsx(path) / extract_zip(path, depth: int = 0)`，均返回 `str`

### 1.4 下载

- `sanitize_name(name: str, fallback: str = "attachment") -> str`
- `download(url, dest: Path, max_bytes: int, timeout: int = 30, *, _resolver=None, _transport=None) -> Path`
  - 两个下划线参数**只给测试注入**，生产调用一律用默认值；调用方永远不该传它们。

### 1.5 模型调用

- `chat_completion(settings, messages: list[dict], *, model: str = "", timeout: int = 60, max_tokens: int = 600) -> str`
- `summarize_document(text: str, settings, name: str) -> str`
- `image_text(path: Path, settings) -> str`
- `image_bytes_text(raw: bytes, suffix: str, settings) -> str`
- `pdf_ocr_text(path: Path, settings) -> str`
- `is_meaningful_text(text: str) -> bool`

### 1.6 后台队列

- `AttachmentWorker(settings, on_record, *, logger=None, napcat_call=None)`
  - `on_record`：`Callable[[dict], Any]`，产出一条记录就调一次。
  - `napcat_call`：`Callable[[str, dict], dict]`，用于「附件只有 file_id、没有直链」时换下载地址；不注入就跳过这类附件。
  - 公开成员：`is_alive`（属性）、`start()`、`stop(timeout: float = 5)`、`submit(attachment) -> bool`、`process(attachment) -> dict | None`、`process_local(attachment, path) -> dict | None`、`describe() -> dict`、`stats`。

### 1.7 原图留存（再导出清单，缺一不可）

`IMAGE_MAX_BYTES`、`IMAGE_TYPES`、`image_dir`、`image_key`、`image_suffix`、`save_image`、`images_of`、`pick_image`、`has_image`、`delete_images`、`AttachmentWorker`。

签名：

- `image_dir(data_dir: Path) -> Path`
- `image_key(msg_id: str) -> str`
- `image_suffix(attachment, head: bytes) -> str`
- `save_image(data_dir, attachment, path, *, logger=None, prefix: str | None = None) -> Path | None`
- `images_of(data_dir, msg_id: str) -> list[Path]`
- `pick_image(data_dir, msg_id: str, *, prefix: str | None = None, unique: bool = False) -> Path | None`
- `has_image(data_dir, msg_id: str) -> bool`
- `delete_images(data_dir, msg_ids) -> int`

### 1.8 清理

- `cleanup_files(directory: Path, *, days: int) -> int`

---

## 2. 对外常量

全部在模块顶层，名字不能改（有外部直接读）：

| 常量 | 值 |
|---|---|
| `TEXT_EXTS` | `.txt .md .csv .json .log .htm .html` |
| `DOC_EXTS` | `.docx .pdf .xlsx .xlsm .pptx` |
| `ARCHIVE_EXTS` | `.zip` |
| `IMAGE_EXTS` | `.jpg .jpeg .png .webp .bmp .gif` |
| `SUPPORTED_EXTS` | 上面四者的并集 |
| `LEGACY_EXTS` | `.doc .xls .ppt .wps .et .dps`（**永不解析**） |
| `MAX_ZIP_ENTRIES` | 20 |
| `MAX_ZIP_TOTAL_BYTES` | 5 MiB |
| `MAX_PDF_PAGES` | 40 |
| `MIN_IMAGE_BYTES` | 10 KiB |
| `MIN_DOC_TEXT_CHARS` | 40 |
| `MAX_RECORD_CHARS` | 1500 |
| `DOC_CHUNK_CHARS` / `DOC_MAX_CHUNKS` | 4000 / 8（压缩包累计上限 = 两者相乘） |
| `NO_TEXT_MARKERS` | 四个：`无有效通知`、`无文字内容`、`没有文字`、`无文字` |
| `IMAGE_MAX_BYTES` | 8 MiB |
| `IMAGE_TYPES` | `.jpg→image/jpeg`、`.jpeg→image/jpeg`、`.png→image/png`、`.gif→image/gif`、`.webp→image/webp` |
| `ALLOWED_DOWNLOAD_SCHEMES` | `("http", "https")` |
| `MAX_DOWNLOAD_REDIRECTS` | 5 |
| `SUMMARY_SYSTEM_PROMPT` / `DOC_PROMPT` / `IMAGE_PROMPT` | 见 A-49…A-52 |
| `USER_AGENT` | 固定 UA，末尾带产品标识；标识内容见 §10 拍板 P-01 |

---

## 3. 契约条目

### 3.1 事件 → 附件对象（A-01 … A-17）

- **A-01** 只接受 `post_type == "message"` 且 `message_type == "group"` 的负载；其它返回空列表。`payload` 不是字典直接返回空列表。
- **A-02** 群号为空、或群号不在白名单 → 空列表。白名单是 **fail-closed**：空白名单表示一个群都不收，不是「全收」。
- **A-03** `user_id` 与 `self_id` 都非空且相等（自己发的消息）→ 空列表。
- **A-04** `message` 不是列表 → 空列表；列表里不是字典的元素跳过。
- **A-05** `file` 段的名称：取 `file`，退回 `file_name`，`strip()` 后为空则跳过该段。
- **A-06** `file` 段字段映射：`url←url`、`file_id←file_id`、`busid←busid`（都 `strip()`）；`size←file_size` 退回 `size`，转整数，转不动按 0。`kind="file"`。
- **A-07** `image` 段三种情况必须跳过：① `sub_type` 按字符串等于 `"1"`（缺省按 `"0"`）；② `summary` `strip()` 后整串匹配「一个方括号 + 1～12 个非方括号字符 + 一个方括号」（表情包摘要）；③ 声明大小满足 `0 < size < 10240`（但声明大小缺失或为 0 时不因此跳过）。
- **A-08** `image` 段 `url` 与 `file`（或 `file_id`）**都**为空 → 跳过。名称取 `file`，退回 `filename`，再退回 `image.jpg`。`kind="image"`，`busid` 留空。
- **A-09** 每个附件的 `group_name` 取「群号别名优先，否则群号，群号也为空则『未知群』」。
- **A-10** 发送者名：`sender.card` 优先，退 `nickname`，退 `user_id`，退 `"群成员"`；`sender` 不是字典按空处理。`sender_id` 是 `str(user_id)`。
- **A-11** `ts`：`payload["time"]` 走 ISO 转换，转换失败取当前本地时间。同一条消息里所有附件共用同一个 `ts`。
- **A-12** `msg_id`：有 `message_id` 时是 `onebot:<群号>:<message_id>`，没有时为空串。`hint` 是所有 `text` 段文本拼接后 `strip()`（没有 text 段则为空串）。
- **A-13** 上传通知路径：`post_type == "notice"`、`notice_type == "group_upload"`；群号与白名单判定同 A-02；`file` 必须字典；名称取 `name` 退回 `file`，空则返回 `None`；`file_id←id` 退回 `file_id`；`busid`、`size` 同 A-06；`sender_id = user_id`，`sender_name = user_id 或 "群成员"`；`ts` 规则同 A-11；`msg_id = onebot:<群号>:upload:<file_id 或 name>`；`hint` 为空；`kind="file"`。
- **A-14** 历史补采适配：入参缺 `post_type` / `message_type` 时补成 `"message"` / `"group"`，然后走 A-01 起同一条路径。不修改调用方传入的字典。
- **A-15** `key` 只包含「群号 + kind + 名称 + 大小」四项且顺序固定，因此**同一个群文件的上传通知与随后的文件消息会得到同一个 key**，同一次上传不会被下载、解析两次。这是对外语义，不是实现细节。
- **A-16** `suffix` 只看 `name`，不看 `url`。
- **A-17** `size_text` 的取整规则：KB 用「保留 0 位小数」的格式化（即 Python `:.0f` 的舍入，.5 走银行家舍入）、MB 保留一位小数；`size` 为 0 或负数返回空串（调用方据此写「大小未知」）。

### 3.2 下载与下载安全（A-18 … A-30）

附件 URL 来自外部事件，必须按不可信输入处理。以下顺序即检查顺序。

- **A-18** 协议只允许 `http` / `https`；否则 `AttachmentError("不支持的下载协议：<协议或 (空)>")`。
- **A-19** URL 带用户名或密码 → `AttachmentError("下载地址不允许包含用户名/密码")`（挡 `http://example.com@127.0.0.1/` 这种伪装）。
- **A-20** 主机名为空 → `AttachmentError("下载地址缺少主机名")`；端口非法 → `AttachmentError("下载地址端口无效：<原因>")`；缺省端口 https 443、http 80。
- **A-21** 主机名小写、去掉尾部的点之后等于 `localhost` 或以 `.localhost` 结尾 → `AttachmentError("拒绝访问本机地址：<原主机名>")`。
- **A-22** 主机名是 IP 字面量时直接用它；否则解析出该主机的**全部**地址（IPv4 与 IPv6 都算，去重保序）。解析失败 → `AttachmentError("无法解析下载地址：<原因>")`；解析结果为空 → `AttachmentError("下载地址未解析出 IP：<主机名>")`。解析器允许注入（仅测试）。
- **A-23** 解析结果里**任何一个**地址被判为内网/本机就整体拒绝：`AttachmentError("拒绝访问本机/内网地址：<主机名> -> <地址>")`。判定规则：IPv6 若是 IPv4 映射地址先还原成 IPv4 再判；然后 `private / loopback / link-local / multicast / reserved / unspecified` 任一为真即拒；另外这些网段内一律拒——IPv4：`0.0.0.0/8`、`10.0.0.0/8`、`100.64.0.0/10`、`127.0.0.0/8`、`169.254.0.0/16`（含云元数据地址）、`172.16.0.0/12`、`192.0.0.0/24`、`192.168.0.0/16`、`198.18.0.0/15`、`224.0.0.0/4`、`240.0.0.0/4`；IPv6：`::/128`、`::1/128`、`64:ff9b::/96`、`fc00::/7`、`fe80::/10`、`ff00::/8`、`2001:db8::/32`。IP 字符串解析不出来也按「拒」处理。
- **A-24** 校验通过后**只连已校验的第一个地址**，而不是再按域名连一次：HTTP 直连该 IP 且 `Host` 头保留原主机名（有端口时带端口）；HTTPS 直连该 IP 但 TLS 证书校验与 SNI 仍用原域名、信任库用系统默认。因此校验之后 DNS 再被改成本机地址也无法生效（DNS rebinding 防护）。这是安全契约，不许简化成「先 resolve 再交给常规 HTTP 客户端」。
- **A-25** 请求固定：方法 `GET`，路径取原 path（空则 `/`）+ 原 query（fragment 丢弃），头固定为 `User-Agent`（§2 常量）、`Host`（A-24）、`Accept-Encoding: identity`、`Connection: close`。
- **A-26** 重定向：`301/302/303/307/308` 跟随，`Location` 与当前 URL 做相对合并；**每一跳都重新执行 A-18…A-24**（跳去私网、跳去 `file://` 都必须拒绝）。最多跟随 `MAX_DOWNLOAD_REDIRECTS`（5）次；第 6 次仍要求重定向 → `AttachmentError("重定向次数过多")`；缺 `Location` → `AttachmentError("重定向缺少 Location")`。
- **A-27** 其余非 2xx 状态 → `AttachmentError("下载失败：HTTP <状态码>")`。
- **A-28** 大小上限双保险：响应头声明的 `Content-Length` 超过上限 → `AttachmentError("文件超过上限（<n> 字节）")`（此时还没开始写盘）；流式累计读超上限 → `AttachmentError("文件超过下载上限")`。
- **A-29** 落盘：目标父目录按需创建；先写 `<目标名>.part`，全部读完才原子替换成目标名；任何失败都删掉 `.part`（测试会检查失败后不存在残留）；读到 0 字节 → `AttachmentError("下载内容为空")`。网络/IO/超时/HTTP 协议层异常统一包成 `AttachmentError("下载失败：<原始错误文本>")`；响应对象与连接无论成败都关闭。
- **A-30** **不重试**：一次 `download()` 调用最多发起一次下载（重定向不算重试）。默认超时 30 秒；后台队列调用时传 `max(20, http_timeout × 2)`（默认 15 → 30）。

### 3.3 文本与文档解析（A-31 … A-42）

- **A-31** 支持的后缀就是 §2 的 `SUPPORTED_EXTS`；`LEGACY_EXTS`（`.doc/.xls/.ppt/.wps/.et/.dps`）以及任何未知后缀一律返回空串。**不做任何兜底猜测**（不试 olefile、不试文本强解）。
- **A-32** 纯文本文件：按 `utf-8-sig → gb18030 → utf-16 → big5` 顺序试解码，全都失败时用 `utf-8` 加「替换非法字节」收尾。除此之外不做换行归一化、不做 HTML 去标签（`.htm/.html` 按纯文本返回）。
- **A-33** docx：只读 zip 里 `word/` 下、以 `.xml` 结尾、且名字含 `document` / `header` / `footer` 之一的成员，按成员名排序；单个成员解析失败跳过、不影响其它。文本规则：`t` 与 `instrText` 取文本；`br` 与 `tab` 记一个空格；`p` 与 `tr` 记一个换行；连续 3 个以上换行压成 2 个；两端去空白；各成员文本用换行连接、空段丢弃。
- **A-34** pptx：成员名严格匹配 `ppt/slides/slide<数字>.xml`，按数字升序；每页前缀 `【第N页】`，其中 N 是**有文本页的序号**（从 1 递增），不是 slide 文件名里的数字；页内文本规则同 A-33。
- **A-35** pdf：依赖 `pypdf`，缺失时返回空串（不抛）。读前 `MAX_PDF_PAGES`（40）页；超过时追加一行 `……（文档较长，仅读前 40 页）`；单页提取抛错跳过该页；整份打不开（损坏/加密）记日志后返回空串。每页文本把连续空格与制表符压成一个空格并 `strip()`，非空的按顺序用换行连接。
- **A-36** xlsx/xlsm：依赖 `openpyxl`，缺失时返回空串。以只读（read-only）+ 只取缓存值（data-only）方式打开；逐工作表、**每表最多 200 行**，超出追加 `……（仅读前 200 行）`；每行把非空单元格转字符串 `strip()` 后用 `" | "` 连接，整行为空则跳过；有内容的表前缀 `【工作表：<表名>】`；表与表之间换行连接；工作簿无论成败都关闭。
- **A-37** zip：嵌套深度大于 1 直接返回空串（防压缩炸弹套娃）；只看非目录条目、最多前 `MAX_ZIP_ENTRIES`（20）个；条目名把反斜杠归一成 `/`；以 `/` 开头或含 `..` 路径段 → 跳过（路径穿越）；后缀不在「文本 ∪ 文档」集合 → 跳过；声明大小或实读字节超过 `MAX_ZIP_TOTAL_BYTES`（5 MiB）→ 跳过；每个成功条目前缀 `【压缩包内文件：<该条目的文件名>】`；累计已产出字符超过 `DOC_CHUNK_CHARS × DOC_MAX_CHUNKS`（32000）时提前停止；条目总数超过 20 时末尾追加 `……（压缩包共 <N> 个文件，仅读前 20 个）`；条目之间空行分隔。
- **A-38** 内存解析（`extract_bytes`）：把字节写进一个带该后缀的临时文件，走 A-31…A-37，结束后删临时文件；解析抛错也要删。
- **A-39** `extract_text` 的后缀判定：优先用传入 `name` 的后缀，没有再退回路径后缀，统一小写。
- **A-40** 解析异常（坏 zip、IO 错误、`ValueError`）→ 记一条日志、返回空串。**解析永不向调用方抛异常**。
- **A-41** 空文件与纯空白内容 → 空串。
- **A-42** 只解析不执行：压缩包内容不导出到磁盘（A-38 的临时文件除外，且用完即删）、不运行任何内容、不跟随文档里的外部引用（链接、宏、嵌入对象、远程模板一律不处理）。

### 3.4 模型调用（A-43 … A-54）

- **A-43** 没有配置模型 API key → `AttachmentError("未配置 DASHSCOPE_API_KEY")`（文案里的名字是历史遗留的配置名，见 §10 P-02）。
- **A-44** 请求体固定：`model`（入参优先，否则取配置里的默认模型）、`temperature` 0.1、`max_tokens`（默认 600）、`messages` 原样传递。当模型名小写后以 `qwen3` 开头时，额外加 `enable_thinking: false`（测试锁这一条）。
- **A-45** 请求方式：POST，JSON 体（中文不转义），头 `Authorization: Bearer <key>` 与 `Content-Type: application/json`；超时取 `max(15, 入参 timeout)` 秒。
- **A-46** 响应解析：取 `choices[0].message.content`，`strip()` 后返回。结构对不上 → `LLMResponseError("大模型返回格式异常")`；响应体不是 JSON → `LLMResponseError("大模型返回非 JSON：<原因>")`；HTTP 错误 → `LLMRequestError("大模型 HTTP <code>: <响应体前 300 字>")` 且带上状态码；连接类异常 → `LLMRequestError("大模型请求失败：<原因>")`。
- **A-47** 重试：走通用重试器，次数取 `llm_max_retries`（默认 2，即最多 3 次尝试），退避取 `llm_retry_backoff`（默认 1.5 秒，第 n 次重试前等 `1.5 × 2^(n-1)` 秒）。是否重试按瞬时错误判定：HTTP `408/409/425/429/500/502/503/504` 可重试；无状态码的请求错误可重试；响应结构错误可重试；JSON 解析错与网络/超时类异常可重试；其它（400/401/403/404 等）**立刻抛出、不等待**。每次重试记一条警告日志，日志标签固定为 `视觉/文档模型`。
- **A-48** 重试耗尽 → 统一包成 `AttachmentError`，消息就是最后那次错误文本（不许吞掉原因）。
- **A-49** 图片识别：消息体是「system = 固定摘要人格句」+「user = 两段内容（文本提示 + 图片 data URL）」。data URL 形如 `data:image/<后缀>;base64,<编码>`，后缀取文件后缀并小写、`jpg` 归一成 `jpeg`。模型取 `vision_model`，超时取 `http_timeout × 5`（默认 75 秒），`max_tokens` 2000。图片字节为空 → 直接返回空串、不发请求。
- **A-50** 图片提示词（原样）：要求把图片里的文字原样提取、保留日期/时间/人名/金额/地点/群号等关键信息；如果图片没有可读文字或不含与学习、通知、表格、名单相关的内容，只回复 `无有效通知`；不描述画面、不解释。
- **A-51** 文档摘要：system 同 A-49 的人格句；user 是固定模板，带入文件名与正文，要求**只依据正文**输出严格 JSON（不要 Markdown、不要代码块、不要解释），字段为 `summary`（≤80 字）、`audience`、`condition`、`details`（数组，按原文分条保留不同人群/例外/地点/附件/链接/注意事项）、`action`、`deadline`（`YYYY-MM-DD HH:MM` 或空串）；并要求保留适用对象、条件、例外与人群差异，不把部分同学的通知写成所有同学，没有相关信息的字段留空。超时取 `llm_timeout`（默认 60），`max_tokens` 2400。
- **A-52** 文档结果格式化：先取回复里**第一个 `{…}` 区间**做 JSON 解析；解析失败或结果不是对象 → 直接返回回复原文 `strip()`。解析成功时按下列顺序拼行，跳过空字段：`summary` 原文；`适用对象：<audience>`；`适用条件：<condition>`；`details` 每条一行 `- <条目>`（`details` 是字符串时按一条处理）；`需要行动：<action>`；`截止时间：<deadline>`。拼出来为空则回退回复原文。
- **A-53** 扫描版 PDF 识别：`vision_active` 为假 → 返回空串（不抛）。缺 `pypdfium2` 或 `Pillow` → `AttachmentError("未安装 pypdfium2/Pillow，无法 OCR 扫描版 PDF")`。页数取 `min(总页数, max(1, pdf_ocr_max_pages))`；每页按 **2 倍缩放**渲染成 PNG 后走 A-49 的图片识别；只保留「有意义文本」的页，前缀 `【第N页】`（N 是真实页序，从 1 起）；页与页之间空行分隔；文档对象必须关闭；渲染或识别抛错 → `AttachmentError("扫描 PDF OCR 失败：<原因>")`。
- **A-54** `is_meaningful_text`：去掉**全部空白字符**后长度 ≥ 12，且不含 `NO_TEXT_MARKERS` 四个标记中的任何一个。返回布尔。

### 3.5 后台队列与处理语义（A-55 … A-68）

- **A-55** 单线程串行：一个 daemon 线程（线程名 `attachments`）从 FIFO 队列逐条取（取等待 1 秒，超时继续等），一次只处理一条。队列**无上限、不丢弃**（限速导致的丢弃发生在处理阶段，见 A-61）。
- **A-56** `start()` 幂等（已经活着就不重复起）；`stop(timeout=5)` 置停止标志并 join 该线程，**不**等待队列排空（未处理的条目直接丢掉）。
- **A-57** `submit()` 前置：`attachments_enabled` 为假或附件为 `None` → 返回 `False`、不进队列、不计统计。
- **A-58** `submit()` 去重：同一个 `key` 在内存里最近出现过（保留最近 500 条，按插入顺序淘汰最老的）→ 返回 `False`，`queued` 不增加。去重表只在进程内、不落盘。
- **A-59** `submit()` 成功：入队、`queued += 1`、写一条日志（群、kind、名称、大小文本、发送者）、返回 `True`。
- **A-60** 单条处理顺序固定：① 限速检查 → ② 声明大小检查（只对 `file`）→ ③ 取到本地文件 → ④ 图片先留原图 → ⑤ 解析/识别 → ⑥ 产出记录。
- **A-61** 限速：**滑动 1 小时窗口**，只统计「进入处理」的次数，图片与文件一并计；上限 `attachment_max_per_hour`（默认 30，最小 1）。超限时该条 `skipped += 1` 并**直接丢弃**——不排队重试、不延后、不产记录。警告日志每 30 分钟最多一条（`附件处理已达每小时上限 N，暂停解析新附件。`）。没有「每秒几次」的限制。
- **A-62** 声明大小超过 `attachment_max_mb`（默认 10，向上取整为字节）→ `skipped += 1`，并且**仍然产一条记录**：正文 `【文件】<名>（<大小文本或「大小未知」>，超过大小上限，未解析）`。即「大文件至少让用户看见名字」。
- **A-63** 取文件（`_fetch` 语义）：目标路径已存在且字节数大于 0 → 直接复用，不重复下载（幂等）；没有 `url` 但注入了 `napcat_call` 且有 `file_id` → 调 `get_group_file_url`，参数 `group_id`、`file_id`，`busid` 能转成整数就传整数，取回 `data.url`；拿不到 URL → 记日志、返回 `None`；下载抛 `AttachmentError` → 记警告、返回 `None`。后两种都 `skipped += 1` 且不产记录。
- **A-64** 图片临时件清理：图片处理结束后（无论有没有留住原图、无论成功还是抛错）删除下载下来的临时文件，避免 `files/` 里同时出现「临时件」与「留存件」两个都匹配 `<key>_*` 的候选（会让读取侧的「附件事件必须唯一」变成歧义）。文件类附件的下载件**保留**，供复用。
- **A-65** 单条处理抛异常：`failed += 1`、`last_error` 记错误文本前 200 字、写异常日志，线程继续下一条。每条处理结束都把 `last_at` 更新为当前时间（成功失败都更新）。
- **A-66** 统计：`describe()` 返回 `queued / processed / skipped / failed / last_error / last_at`，外加按类型累加的 `processed_file`、`processed_image` 计数；健康负载里以 `attachments` 键暴露这份字典。`processed` 只在**真的产出一条记录**时加一。
- **A-67** 图片分支：`vision_active` 为假 → `skipped += 1`、不产记录，但原图已经留下（收件箱仍要能看图）；这种情况下「视觉不可用」的提示日志**只写一次**（同一 worker 实例内）。`vision_active` 为真但模型文本不满足 A-54 → `skipped += 1`、不产记录、记一条「没有可读通知内容」日志。`vision_active` 的判定：读图开关开、有 API key、且（视觉模型名被用户显式改过，或模型端点是 DashScope 域名）——具体定义在配置层，本模块只读这个布尔值。
- **A-68** 文档分支：`.pdf` 且去掉空白后文本长度小于 `MIN_DOC_TEXT_CHARS`（40）→ 先尝试扫描版识别；识别成功且有内容则改用识别文本（记一条「扫描 PDF OCR 完成」）；识别抛 `AttachmentError` 则记日志、继续用文本层。最终仍无文本 → `skipped += 1` 且产一条「未能解析出文字」记录。摘要调用抛 `AttachmentError` → 记日志、摘要退化为正文前 400 字符。

### 3.6 记录形状（A-69 … A-75）

- **A-69** 记录恰好 11 个键：`msg_id`、`source`、`event`、`group_id`、`group_name`、`sender_id`、`sender_name`、`ts`、`received_at`、`content`、`source_text`。键名与取值类型是对外契约（数据库、前端、收件箱都按它读）。
- **A-70** `source` 恒为 `onebot-attachment`，`event` 恒为 `GROUP_ATTACHMENT`。
- **A-71** `msg_id = onebot:<群号>:attach:<附件 key>`（key 即 A-15 的 16 位十六进制）。`received_at` 是产出时刻；`ts` 优先用附件自带的、为空才用当前时间。
- **A-72** `content` 上限 `MAX_RECORD_CHARS`（1500）——按**字符**截断，不是字节。
- **A-73** 正文三种形态：图片 `【图片】` 换行接模型文本；文件 `【文件】<名>` 换行接摘要；无文字时 `【文件】<名>（<大小文本>，未能解析出文字）`。记录整体 `strip()` 掉首尾空白。
- **A-74** `source_text` 是「送进模型的原文」：文档是截断后的正文，长度取 `max(1000, document_max_chars)`；被截断时末尾追加 `……（文件较长，仅保留并分析前 N 字）`；图片是模型返回的文本；大文件与无文本记录为空串。
- **A-75** 记录交给 `on_record`；回调抛异常只记日志，不影响返回值、不回滚统计、不重试。

### 3.7 磁盘布局、原图与清理（A-76 … A-88）

- **A-76** 根目录是 `<data_dir>/files/`（与数据库同级），不存在时按需创建。图片与下载件**同一目录**。
- **A-77** 下载件文件名：`<附件 key>_<清洗后的原名>`。
- **A-78** `sanitize_name`：把 `\ / : * ? " < > |` 以及回车、换行、制表符替换成下划线；两端去空白与句点；结果为空用 `fallback`（默认 `attachment`）；超过 60 字符时保留扩展名（扩展名最多取 4 字符）并把主名截到 55。示例：`../../etc/通知.pdf` → `_.._etc_通知.pdf`；纯空白 → `attachment`。
- **A-79** 原图文件名：`<前缀>_<序号><后缀>`；序号从 0 到 19，用「独占创建」占位（已存在就试下一个）；20 个都满 → 放弃并记警告。
- **A-80** 前缀有两套来源，**写盘前缀必须等于读盘前缀**：群附件事件用附件 `key`；普通消息与历史回溯用 `sha1(msg_id)` 前 16 位。这条历史 bug（写盘按 A、读盘按 B，收件箱永远 404）不允许重演。
- **A-81** 后缀按**文件头**认：`89 50 4E 47 0D 0A 1A 0A`→`.png`；`FF D8 FF`→`.jpg`；`GIF87a`/`GIF89a`→`.gif`；前 4 字节 `RIFF` 且第 9～12 字节 `WEBP`→`.webp`。认不出才退回附件名后缀（须在 `IMAGE_TYPES` 内）；两者都不行 → 不留原图（记警告）。理由：`Content-Type` 必须与实际字节一致。
- **A-82** 原图大小上限 `IMAGE_MAX_BYTES`（8 MiB）；字节数为 0 或超限 → 不留原图（记警告）。
- **A-83** 双前缀硬链接：当附件 `msg_id` 不含 `":upload:"` 时，留住原图后再把它硬链接成 `<sha1(msg_id) 前 16 位>_<最小空闲序号><后缀>`（序号 0…19）。这样同一张图两个前缀都能读到，磁盘上只占一份。硬链接失败**静默忽略**（不复制、不报错）。
- **A-84** 读侧判定（`pick_image`）：在 `files/` 里按 `<前缀>_*` 找，只认**普通文件**（用 lstat 判，不跟随符号链接）、后缀在 `IMAGE_TYPES` 内的；按文件名里的序号升序（认不出序号排最后，再按名字）。普通消息取序号最小的那张（同一消息多张不算歧义）；附件事件（`unique=True`）要求候选**恰好 1 个**，否则视为找不到。
- **A-85** 对外不变式：收件箱 JSON 里的 `has_image` 与「取原图」接口必须**共用同一个判定函数**，因此 `has_image` 为真等价于取图接口回 200；取不到时取图接口 404、`has_image` 为假。任何一侧单独改判定都算破坏契约。
- **A-86** 删收件箱条目时同步删该 `msg_id` 的所有原图，返回删除数量；单张删除失败跳过不计数。
- **A-87** 清理：按修改时间，早于 `now - days × 86400` 的**普通文件**删除，返回删除数量；`days <= 0` 或目录不存在 → 返回 0；单个文件失败跳过。调度方每 6 小时最多跑一次，`days` 取 `attachment_retention_days`（默认 7）；清理范围是整个 `files/`（下载件与原图都清）。**只清文件，不删目录、不碰子目录**。
- **A-88** **没有缩略图**：只存原图，不做任何缩放或转码（扫描 PDF 的 2 倍渲染只用于识别，不落盘）。收件箱直接回原图。

---

## 4. 磁盘布局一览

| 路径 | 内容 | 谁写 |
|---|---|---|
| `<data_dir>/files/<key>_<清洗名>` | 下载的附件原件 | 取文件流程（A-77） |
| `<data_dir>/files/<key>_<n><ext>` | 群附件事件的留存原图（前缀 = 附件 key） | 图片留原图（A-79） |
| `<data_dir>/files/<sha1(msg_id)[:16]>_<n><ext>` | 普通消息/回溯消息的留存原图；不含 `:upload:` 的附件事件另有一条硬链接指向同一内容 | 同上（A-80、A-83） |
| `<data_dir>/files/<目标名>.part` | 下载中的临时件，成功即改名、失败即删除 | 下载（A-29） |
| 系统临时目录下的 `*.{后缀}` | 压缩包内文件的内存解析副本，用完即删 | `extract_bytes`（A-38） |

不建索引文件、不建子目录、不用数据库记录「哪个文件对应哪条消息」——映射完全由文件名里的前缀与序号推导。

---

## 5. 配置键与默认值

模块只读配置对象，不读环境变量。环境键统一前缀 `NOTICEHUB_`，旧前缀仍兼容（新名优先，旧名命中时写一次迁移警告）。

| 配置属性 | 默认 | 环境键后缀 | 被谁用 |
|---|---|---|---|
| `attachments_enabled` | `True` | `ATTACHMENTS` | 提交前置、接收端是否解析附件 |
| `attachment_max_mb` | `10`（最小 1） | `ATTACHMENT_MAX_MB` | 下载上限、声明大小检查 |
| `attachment_max_per_hour` | `30`（最小 1） | `ATTACHMENT_MAX_PER_HOUR` | 限速 |
| `attachment_retention_days` | `7`（最小 1） | `ATTACHMENT_RETENTION_DAYS` | 清理 |
| `vision_enabled` | `True` | `VISION` | 组成 `vision_active` |
| `vision_model` | `qwen3-vl-plus` | `VL_MODEL` | 图片识别 |
| `document_max_chars` | `30000`（最小 1000） | `DOCUMENT_MAX_CHARS` | 送模型的正文长度 |
| `pdf_ocr_max_pages` | `20`（最小 1） | `PDF_OCR_MAX_PAGES` | 扫描 PDF 页数上限 |
| `dashscope_api_key` | 空 | `LLM_API_KEY`（兼容 `DASHSCOPE_API_KEY`） | 是否具备模型能力 |
| `dashscope_model` | `qwen-plus` | `LLM_MODEL` | 文档摘要默认模型 |
| `dashscope_endpoint` | DashScope 兼容端点 | `LLM_ENDPOINT` | 模型请求地址 |
| `llm_max_retries` | `2`（最小 0） | `LLM_MAX_RETRIES` | 重试次数 |
| `llm_retry_backoff` | `1.5`（最小 0） | `LLM_RETRY_BACKOFF` | 重试退避基数 |
| `http_timeout` | `15`（最小 5） | `HTTP_TIMEOUT` | 下载超时、图片识别超时基数 |
| `llm_timeout` | `60`（最小 10） | `LLM_TIMEOUT` | 文档摘要超时 |
| `data_dir` | 运行根下的 `data` | 无 | `files/` 根 |
| `dedupe_hours` | `6` | `DEDUPE_HOURS` | **不归本模块**，只出现在配置描述里 |

`vision_active`（配置层属性，本模块只读）：读图开关开 **且** 有 API key **且**（视觉模型名与默认值不同，**或** 端点主机属于 DashScope 域名）。含义是「别对着不认识视觉模型的端点每张图撞一次 404」。

---

## 6. 外部依赖

| 依赖 | 用途 | 缺失时 |
|---|---|---|
| 标准库 HTTP 客户端 + `ssl` | 下载与模型请求 | 无（必需） |
| 标准库 `zipfile` + XML 解析 | docx / pptx / zip | 无（必需） |
| `pypdf` | PDF 文本层 | 返回空串（可降级） |
| `openpyxl` | xlsx / xlsm | 返回空串（可降级） |
| `pypdfium2` + `Pillow` | 扫描 PDF 渲染成位图 | 抛 `AttachmentError`（OCR 路径不可用） |
| 视觉/文本模型服务 | 图片识别、文档摘要 | 抛 `AttachmentError`（无 key 时） |
| NapCat 的「取群文件地址」接口 | 附件只有 `file_id` 时换直链 | 走不通（不注入调用能力时就跳过这类附件） |
| 系统时钟、文件系统、`time.sleep`（重试退避） | — | 无 |

不写数据库、不监听网络端口、不起子进程、不弹窗。

---

## 7. 日志频道与日志文本

- 频道名：`logging.getLogger(__name__)`，即包内本模块名（形如 `<包名>.attachments`）；编排层那个 worker 的默认 logger 用**同一个频道名**，外部可注入自己的 logger。
- 日志级别约定：正常流水 `info`，可恢复的失败 `warning`，处理线程内未预期异常 `exception`。

必须保留原文的日志文本（有人按这些字排障）：

| 文本 |
|---|
| `收到群附件 group=%s kind=%s name=%s size=%s sender=%s` |
| `附件处理失败：%s`（带异常栈） |
| `附件处理已达每小时上限 %d，暂停解析新附件。` |
| `附件超过 %dMB，仅记录名字：%s` |
| `附件解析完成 group=%s kind=%s name=%s 摘要=%s` |
| `附件记录入库失败：%s`（带异常栈） |
| `附件没有可读通知内容，已忽略：%s` |
| `附件没有可提取文本（可能是扫描件或缺依赖）：%s` |
| `附件结构化提取失败，改用正文截断：%s（%s）` |
| `扫描 PDF OCR 失败，回退文本层：%s（%s）` |
| `扫描 PDF OCR 完成：%s` |
| `附件下载失败 %s：%s` |
| `附件没有可下载地址，跳过：%s` |
| `取附件下载地址失败 %s：%s` |
| `当前 LLM 端点（%s）没有可用的视觉模型，图片只记录不识别；需要读图请设置 <视觉模型环境键> 或换回 DashScope 端点。`（`%s` 是端点主机名，取值失败时用「未知」；尖括号处填 §5 表里 `VL_MODEL` 那一行的完整环境键名） |
| `图片临时文件删除失败 %s：%s` |
| `清理 %d 个过期附件文件。`（由调度方写） |
| `图片超过 %dMB，未留原图：%s` |
| `图片格式不认识，未留原图：%s` |
| `同一消息的图片已满 20 张，未留原图：%s` |
| `图片留原图失败 %s：%s` |
| `附件解析失败 %s：%s` |
| `PDF 解析失败 %s：%s` |
| `Excel 解析失败 %s：%s` |

---

## 8. 测试锁住的事实（`tests/test_attachments.py`，本批次相关 48 条）

解析类：

1. 一条含「文本段 + 文件段」的群消息 → 恰好 1 个附件，`kind="file"`、名称保留中文冒号（`附件2：报名表.xlsx`）、`size=17306`（从字符串转整数）、`url` 透传、`msg_id == "onebot:123456:2048"`。
2. `group_upload` 通知 → 附件名 `通知.pdf`、`file_id` 取 `id`、`busid` 是字符串 `"102"`、`size=204800`。
3. 用 `pypdf` 现做一页空白 PDF 走扫描识别：返回文本含 `【第1页】`，且送到视觉模型的字节以 PNG 魔数开头（证明渲染成 PNG，而非把 PDF 原样发过去）。
4. 一条消息里放三张图：`sub_type=1`（表情）、声明大小 2048（<10 KiB）、`summary="[好的]"` → 三个全部被丢弃，返回空列表。
5. 声明大小 180000 的普通图片 → 保留，`kind="image"`。
6. 群号不在白名单 → 空列表。
7. 同一个文件分别经「文件消息」与「上传通知」进入 → 两者的 `key` 相等。

提取类：

8. `.txt`（UTF-8 中文）→ 提取出正文。
9. 手工构造的 docx（zip 里放 `word/document.xml`，两个段落）→ 文本含两段的关键词。
10. `openpyxl` 写出的 xlsx（表头「姓名/金额」+ 一行数据）→ 文本含单元格内容。
11. zip 里同时放 `../evil.txt`、`notes.txt`、`photo.jpg` → 只读到 `notes.txt`，不含穿越文件，图片被跳过。
12. `.doc`（OLE 魔数）→ 空串。
13. `sanitize_name("../../etc/通知.pdf") == "_.._etc_通知.pdf"`；`sanitize_name("   ") == "attachment"`。

队列与记录类：

14. 文件附件走完流程 → `content` 含 `【文件】报名表.xlsx`；`group_name` 透传；`on_record` 收到同一条记录；`stats.processed == 1`；下载的文件仍在磁盘上；`source_text` 含表格单元格文本。
15. 模型返回结构化 JSON → `content` 依次含 `适用对象：…`、`适用条件：…`、`截止时间：…`；`source_text` 比 `content` 长；`source_text` 含正文开头。
16. 扫描 PDF 场景（文本层为空 + 识别返回文本）→ `source_text` 含识别文本、`content` 含结构化字段。
17. 图片附件 → `content` 含 `【图片】` 与模型文本；下载临时件**已删除**；`files/` 里只剩 `<key>_0.jpg` 且内容等于写入的字节。
18. 图片模型返回「无有效通知」→ 不产记录、`on_record` 未被调用、`stats.skipped == 1`、临时件已删。
19. `vision_active` 为假 → 不产记录、回调未调用、临时件已删。
20. `process_local` 抛错 → 异常照常抛给 `process()` 的调用方（测试断言异常文本），临时件已删。
21. 声明大小 99 MB 的文件 → 仍产记录，`content` 含文件名与 `超过大小上限`。
22. `submit` 同一个附件两次 → 第一次 `True`、第二次 `False`、`stats.queued == 1`。
23. `attachments_enabled=False` → `submit` 返回 `False`。
24. `cleanup_files(days=7)` 对 10 天前的文件 → 返回 1、文件已删。

接收端分发（跨模块，但由本模块的解析函数驱动）：

25. 含文件段的群消息 → 状态 200、载荷 `inserted` 为真、`attachments == 1`、消息记录 1 条、排队附件的名称正确。
26. 只有上传通知 → 状态 204、`attachments == 1`、**不**产生消息记录。
27. `attachments_enabled=False` → 上传通知不排队。

配置类：

28. `from_env({})` 的默认值：`attachments_enabled=True`、`attachment_max_mb=10`、`vision_enabled=True`、`vision_model="qwen3-vl-plus"`、`document_max_chars=30000`、`pdf_ocr_max_pages=20`、`dedupe_hours=6`。
29. 用旧前缀设 7 个环境键 → 全部生效（关闭开关、25 MB、关读图、换视觉模型、12000 字、8 页、12 小时去重）；`describe()` 里含 `attachments` 段且 `dedupe_hours` 为 12。

模型请求类：

30. 模型名 `qwen3-vl-plus` → 请求体里 `enable_thinking` 为 `false`，返回值取 `choices[0].message.content`。

下载安全类：

31. 非 http(s) 协议（`file:///…`、`ftp://…`、`data:…`、`javascript:…`）→ 抛 `AttachmentError`，且不留 `.part`。
32. 直接写 IP/主机名的本机与内网目标共 12 个（`127.0.0.1`、`127.1.2.3`、`localhost`、`0.0.0.0`、`10.0.0.1`、`172.16.0.1`、`192.168.1.1`、`169.254.169.254`、`100.64.0.1`、`[::1]`、`[fe80::1]`、`[::ffff:127.0.0.1]`）→ 全部拒绝。
33. 域名解析到 `10.0.0.5`（注入解析器）→ 拒绝。
34. `http://example.com@127.0.0.1/` 与 `http://127.0.0.1@example.com/` → 拒绝。
35. 公网目标（注入解析器返回 `93.184.216.34`、注入传输层返回 200 与 5 字节）→ 返回目标路径、文件内容写入、实际连接的 `ip` 是解析出的公网地址、`Host` 头是原域名 `safe.example`。
36. 302 到相对路径的公网目标 → 跟随并成功。
37. 302 到 `http://127.0.0.1:1234/steal` → 拒绝，且传输层**只被调用过一次**（说明跳转后的地址在连接前就被拦下）。
38. 302 到 `file:///C:/secret.txt` → 拒绝。
39. 一直 302 到自己 → 抛错（重定向次数上限生效）。
40. 声明 `Content-Length: 999` 而上限 100 → 拒绝；不声明长度但流式读 200 字节而上限 100 → 拒绝。
41. 500 状态 → 拒绝。

原图留存类：

42. 无 API key（`vision_active` 为假）→ 不入库但留原图：`images_of` 返回 1 个，文件名 `<image_key(msg_id)>_0.png`，内容等于下载字节，`has_image` 为真。
43. 同一条消息的两张图 → 依次得到 `_0.png` 与 `_1.png`。
44. `kind="file"` 的附件 → 不留图：`has_image` 为假、`images_of` 为空。
45. 图片超过 `IMAGE_MAX_BYTES` → 不留原图、`has_image` 为假。
46. 普通消息里的 image 段：附件 `msg_id` 必须等于消息行 `msg_id`（`onebot:123456:2048`）；处理后 `has_image` 为真、落盘名是 `<image_key(该 msg_id)>_0.png`。
47. `delete_images(msg_ids=[…])` → 返回 1，`has_image` 变假。
48. 无后缀且内容不是已知图片格式的字节 → 不留原图、`has_image` 为假。

> 该文件里还有一组「跨群去重」用例（按文本相似度合并两条通知）。它们验证的是去重与摘要模块，**不是本模块的契约**，重写本模块时不需要看。

---

## 9. 契约条目计数

| 分组 | 条目数 | 编号区间 |
|---|---|---|
| 事件 → 附件对象 | 17 | A-01 … A-17 |
| 下载与下载安全 | 13 | A-18 … A-30 |
| 文本与文档解析 | 12 | A-31 … A-42 |
| 模型调用 | 12 | A-43 … A-54 |
| 后台队列与处理语义 | 14 | A-55 … A-68 |
| 记录形状 | 7 | A-69 … A-75 |
| 磁盘布局、原图与清理 | 13 | A-76 … A-88 |
| **合计** | **88** | |

对外常量 §2 共 **23 项**（含 3 条提示词与 1 个 UA）。日志原文 §7 共 **24 条**（见该节表格）。
测试锁住的事实 §8 共 **48 条**（`tests/test_attachments.py` 整份文件有 53 条用例，
其中 5 条是「跨群去重」，归去重/摘要模块，不在本批次范围）。

---

## 10. 需要拍板（测试没说清、实现前必须定）

- **P-01 UA 里的产品标识。** 下载请求的 `User-Agent` 末尾带着**旧产品名**（`… <旧名>/1.0`），测试没锁。它是外发字符串：会出现在对方服务器的访问日志里。建议改成发布名；要不要改、改成什么，需要定。本文正文只写「固定 UA，末尾带产品标识」。
- **P-02 一条错误文案里的配置名。** 没有 API key 时抛的是 `未配置 DASHSCOPE_API_KEY`，而用户侧的配置入口现在已经叫 `LLM_API_KEY`（新环境前缀下）。这条文案会被用户看到，建议改成用户当前能搜到的名字；但测试与日志可能有人按旧字 grep，需要一并确认。
- **P-03 没有缩略图。** 收件箱直接回原图，单张上限 8 MiB。手机流量下打开一条带大图的消息会整张下载。要不要加缩略图（尺寸、格式、存哪、谁来清）？加了就是新的磁盘与清理契约。
- **P-04 限速是「丢弃」不是「排队」。** 超过每小时上限后，附件**直接丢**（不产记录、不重试、不延后）。用户看到的是「图片/文件没进收件箱」，日志里只有一条每 30 分钟一次的警告。要不要改成延后处理（那就要给队列加持久化与重放语义）？
- **P-05 限速的计数口径。** 现在只数「进入处理」的次数，图片与文件一起算，滑动 1 小时；被限速丢掉的条目**不**计数。若产品希望「图片不算」「按群分开算」，语义要改。
- **P-06 下载不重试。** 网络抖动一次就放弃该附件（只留日志与 `skipped` 计数），而模型调用有 3 次尝试。要不要给下载也加有限重试（次数、退避、哪些状态码值得重试）？
- **P-07 清理会连原图一起删。** `attachment_retention_days`（7 天）清理的是整个 `files/`，收件箱原图 7 天后一起消失（消息还在，图变 404、`has_image` 变假）。产品期望是「附件缓存 7 天」还是「收件箱原图长期保留、只清下载件」？这条会直接影响用户翻旧消息的体验。
- **P-08 单线程串行。** 一次只处理一个附件；一个 20 页扫描 PDF 会把后面所有附件压住（每页一次视觉调用）。要不要并发（几路、要不要按群限并发）？
- **P-09 硬链接双前缀的降级。** 不支持硬链接的文件系统（部分网络盘/FAT）上只会有 `attach:<key>` 一个前缀，普通消息那条路径会读不到图（`has_image` 为假）。是接受降级，还是改成复制一份（占双倍空间）？
- **P-10 遗留 Office 格式永不解析。** `.doc/.xls/.ppt/.wps/.et/.dps` 只记录文件名与大小。要不要给它们一个明确的用户可见说明（现在正文只写「未能解析出文字」）？
- **P-11 视觉不可用时图片只记名字、仍留原图。** 也就是「收件箱能看图、但图片内容不进摘要」。这是当前行为；若产品希望「没有视觉模型时连记录都不产」，要改（会影响收件箱里图片消息的可发现性）。
