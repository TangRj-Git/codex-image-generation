# Codex Image Generation MCP

为中转站用户提供一个本地 FastMCP 图像工具：复用已有 Codex 的 URL 和凭据，使用本次调用提供的对话模型或 Codex 配置中的模型，通过 Responses 的 image_generation 工具生成/编辑图片。

用户日常只需要说：

- “生成一张安静的书桌插画。” → 当前任务目录下的唯一 PNG 文件。
- “生成透明背景的 Logo，保存为 assets/logo.webp。” → 指定目录和 WebP 格式。
- “把这张参考图中的沙发改成蓝色。” → 提供参考图并编辑。

不另外配置 URL、API Key 或专用图像模型；请求不会包含工具内的 model 字段。

## 安装与接入

要求 Python 3.11+ 和 uv。在项目目录运行：

~~~powershell
uv sync --frozen
uv run --frozen gpt-image-generation --check
uv run --frozen gpt-image-generation --print-config
~~~

将最后一条命令输出的配置加入 Codex 用户配置，再重新加载 MCP / 开启新会话。

生成的配置使用 Python 和 Codex 配置目录的绝对路径，不依赖启动工作目录；自动列出 provider 所需的环境变量名，包括暂时尚未设置的变量，不复制变量值或密钥。MCP 超时会比上游请求超时更长。

源码开发也可使用：

~~~toml
[mcp_servers.image_generation]
command = "uv"
args = ["--directory", "E:/OpenProject/gpt-image-generation", "run", "--frozen", "gpt-image-generation"]
startup_timeout_sec = 60
tool_timeout_sec = 360
~~~

将目录替换为本机实际路径。provider 使用环境变量认证时，优先采用 --print-config 自动生成的 env_vars 白名单。服务正常启动为 stdio，stdout 只用于 MCP 协议。

--check 不请求上游、不生成图片、不产生生成费用；configured=true 只表示本地连接配置可解析，不表示中转站的工具权限或实际出图已验证。

## 模型、连接与任务目录

模型来源优先级：

1. Codex 可靠获知当前会话模型时，在调用参数 current_model 中传入精确 API ID，例如 gpt-6-astra，结果标为 model_source=caller。
2. 没有传入时使用当前选定 Codex 配置中的 model，结果标为 model_source=config。
3. 两处都缺失时，在请求前报错。没有硬编码模型兜底。

普通 MCP 不保证提供会话模型元数据。caller 只说明调用方提供了模型，不代表服务验证了会话身份；配置模型也可能不同于界面的临时选择。Codex 不应猜测自己的型号。准确同步会话临时切换需要宿主实际传入对应上下文。

cwd 必须由 Codex 传入当前任务的绝对工作目录，不能用 MCP 安装目录代替。默认直接保存到 cwd，不自动增加 outputs/images 子目录。用户指定的相对 output_dir 也以 cwd 为基准。

连接仅来自已有用户级 Codex 配置：

- 自定义 provider 使用其 base_url、env_key、experimental_bearer_token、http_headers、env_http_headers、query_params。
- 内置 openai provider 使用顶层 openai_base_url；没有明确 URL 时不会静默改用其他地址。
- 当前 provider 要求 OpenAI API Key 认证时，可以复用对应环境变量或文件存储的 API Key。
- 支持新版 profile 文件及旧版内嵌 profile。--profile 只定位已有配置，不能创建另一套模型/密钥设置。
- 不读取项目 .env，不扫描其他 provider 找 Key，不实现 ChatGPT OAuth、钥匙串登录或外部 token 命令。
- 不同用途的显式请求头可共存；同一 Authorization 来源出现不一致的值时拒绝。
- URL query 与路径分开传输；重复 query 键会明确报错，避免静默丢值。

--codex-home / CODEX_HOME 用于定位配置；--profile / CODEX_IMAGE_PROFILE 用于选择已有 profile。CODEX_IMAGE_TIMEOUT 是可选运行超时，默认 300 秒，范围大于 0 且不超过 3600 秒。它不是模型或连接设置。

## 唯一公开工具：generate_image

| 参数 | 作用 |
| --- | --- |
| prompt | 必填，图片描述或编辑要求 |
| cwd | 必填，Codex 当前任务目录的绝对路径 |
| current_model | 可选，仅在可靠获知当前会话模型时自动传入 |
| output_dir | 默认 cwd；支持项目内相对目录或用户指定的绝对目录 |
| filename | 可选，仅文件名；默认唯一名，已有文件不覆盖 |
| output_format | png / jpeg / jpg / webp；省略时先按文件名推断，再默认 PNG |
| size | 可选 auto 或 WIDTHxHEIGHT；具体能力由上游判断 |
| quality | 可选 auto / low / medium / high / xhigh / max |
| background | 可选 auto / opaque / transparent |
| output_compression | JPEG/WebP 可选严格整数 0–100，PNG 不接受 |
| action | 可选 auto / generate / edit |
| input_images | 可选参考图列表，每项恰有 path、image_url、file_id 之一 |
| mask | 可选蒙版，同样提供 path、image_url 或 file_id |
| input_fidelity | 可选 low / high；仅适用于支持该参数的上游，默认不发送 |
| previous_response_id | 同一 provider 保留的上次响应 ID |
| previous_image_ids | 同一 provider 的 image_generation_call ID 列表 |
| moderation | 可选官方 auto / low 设置；上游策略仍生效 |
| partial_images | 默认 0；1–3 启用上游 SSE 和中间图 |
| safety_identifier | 可选隐私保护后的用户标识，适用于多用户部署 |
| include_preview | 默认 true，返回最多四张受大小限制的预览 |

除确定文件格式所需的 output_format 外，未指定的高级图像参数不主动发送。专用图像模型由中转站/上游默认机制决定，服务不会假装知道实际选择了哪个模型。

基本示例：

~~~json
{
  "prompt": "生成一个简洁的茶杯图标，透明背景",
  "cwd": "E:/OpenProject/my-site",
  "current_model": "gpt-6-astra",
  "output_dir": "assets",
  "filename": "tea.webp",
  "background": "transparent"
}
~~~

current_model 不是安装配置项；示例值只有在会话确实提供该模型信息时才应传入。

参考图支持：

~~~json
[
  {"path": "assets/source.png"},
  {"image_url": "https://example.com/reference.png"},
  {"image_url": "data:image/png;base64,..."},
  {"file_id": "file_example", "detail": "high"}
]
~~~

detail 可为 auto / low / high / original。URL 直接交给上游，不在本地额外下载。文件 ID 和历史响应 ID 不可跨 provider 复用。蒙版优先使用本地文件、data URL 或 file ID；本地可读取的蒙版会检查 alpha，且在首张图片可本地比较时检查尺寸和格式。远程资源的内容限制由上游验证。

## 文件与结果规则

- PNG/JPEG/WebP 原图按真实格式保存；不支持 SVG 和动画输出。
- 显式格式与文件名后缀冲突、JPEG 透明背景、布尔/字符串压缩参数等，在请求前拒绝。
- 上游没有遵守请求格式时，保留可用图片，以真实格式修正扩展名并返回警告；不会把 JPEG 字节冒充 PNG。
- 请求前校验文件名和目标目录，并排他预留名称，避免同名并发重复生成。Windows 设备名、UTF-16 名称长度和文件系统限制会被检查。
- 显式文件名用于第一张成功图片，后续图片编号；已有文件不覆盖。
- 任一图像 Item 失败不会删除其他已成功生成的图片。保存失败按项报告，保留成功文件。
- 结果包含 status、images、response_id、request_id、model/source、usage、warnings、errors，以及中间图信息。文本内容也包含路径、编辑 ID 和重要警告。
- model 是实际请求的对话模型；reported_model 仅记录上游明确返回的模型名称，未知时为 null，不冒充已验证的会话模型或专用图像模型。
- 预览可能缩小、重新编码；交付原图应使用返回的绝对路径。
- 上游流式事件可以触发 MCP 进度通知；中间图在最终结果中可用，但 Codex 是否实时显示图片取决于客户端，不能等同于内置工具界面。
- SDK 自动重试关闭。超时、断流或部分失败后，不自动换模型或重新生成，避免重复计费。
- 图像处理和文件操作移出事件循环。取消会关闭上游连接、等待本地临界操作收尾并释放预留；不承诺硬杀正在运行的解码线程。已保存原图不会因取消被删除。
- 本地处理预算：最多 16 个参考/历史图像引用，本地/data 输入合计 100 MB、单张小于 50 MB；最多 16 张最终图、合计 200 MiB、单张小于 100 MiB；最多保留 12 张中间图、合计 32 MiB。这些是应用资源限制，不是模型能力声明。

输出目录可能出现请求期间的隐藏锁/临时文件。不要删除正在使用的锁；进程意外终止留下的锁需要在确认没有相关任务后处理。

## 验证与升级

~~~powershell
uv run --frozen python -B -m unittest discover -s tests -v
uv run --frozen gpt-image-generation --check
~~~

离线测试使用假凭据、MockTransport、临时图片和真实 stdio 子进程。它们验证请求结构与工具行为，不代表任意中转站都支持相同能力。

0.2 版本调整：

- 公开工具收敛为 generate_image；配置诊断使用 --check。
- project_dir 改为 cwd；默认直接当前目录。
- 不再使用旧的 --responses-model / --image-model 等独立模型启动设置；旧接入配置应重新用 --print-config 生成。
- 旧的独立 URL / Key / 模型覆盖环境设置不再生效。连接来自 Codex 原配置，模型来自调用上下文或配置文件。

本项目不会自动改写 Codex 的用户配置。安装和接入只需做一次，正常生成不需要重新输入凭据。

## 官方依据

- [Responses 图像生成工具](https://developers.openai.com/api/docs/guides/tools-image-generation)
- [Responses Create API](https://developers.openai.com/api/reference/resources/responses/methods/create)
- [图像生成指南](https://developers.openai.com/api/docs/guides/image-generation)
- [Codex 高级配置](https://developers.openai.com/codex/config-advanced)
- [Codex 配置参考](https://developers.openai.com/codex/config-reference)

中转站必须支持 Responses 和 image_generation，且允许所选对话模型调用图像工具。省略工具内模型字段意味着使用上游默认路由，并不保证任意中转实现都能成功出图。
