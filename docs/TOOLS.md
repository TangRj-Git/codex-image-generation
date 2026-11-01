# 工具与配置参考

服务使用 stdio，唯一公开 MCP 工具是 `generate_image`。接入方式见 [uvx 安装说明](INSTALL.md)，本地配置诊断使用 `uvx codex-image-generation@latest --check`，不暴露额外的凭据读取工具。

## 调用原则

1. Codex 从当前任务上下文取得绝对 `cwd`，不能使用 MCP 安装目录代替。
2. 用户未指定位置时，省略 `output_dir`、`filename`，直接保存到 `cwd`。
3. 用户给出完整目标路径时，拆为 `output_dir` 和仅含文件名的 `filename`。
4. 只有可靠的运行时上下文明确给出当前对话模型的 API ID 时，才传 `current_model`；否则省略并使用已有 Codex 配置的 `model`。
5. 返回原图路径、预览及重要警告。后续编辑需要保留同一 provider 的 response/image call ID。

工具不提供独立的 URL、API Key 或专用图像模型参数。向 Responses 发送的顶层 `model` 是所选对话模型，`tools` 包含 `type: image_generation`，不包含 `tools[].model`。专用图像模型由中转站/上游默认路由决定。

## 参数

| 参数 | 默认值 / 可选值 | 用途 |
| --- | --- | --- |
| `prompt` | 必填非空字符串 | 图片描述或编辑要求 |
| `cwd` | 必填绝对路径 | 当前任务工作目录 |
| `current_model` | 省略 | 可靠上下文提供的对话模型 API ID |
| `output_dir` | `cwd` | 相对于 `cwd` 的目录，或用户指定的绝对目录 |
| `filename` | 自动生成唯一文件名 | 仅文件名，已有文件不覆盖 |
| `output_format` | 从文件名推断，否则 `png` | `png` / `jpeg` / `jpg` / `webp` |
| `size` | 省略 | `auto` 或 `WIDTHxHEIGHT`；上游决定支持范围 |
| `quality` | 省略 | `auto` / `low` / `medium` / `high` / `xhigh` / `max`；依上游能力 |
| `background` | 省略 | `auto` / `opaque` / `transparent` |
| `output_compression` | 省略 | JPEG/WebP 的严格整数 `0–100`；PNG 不接受 |
| `action` | 省略 | `auto` / `generate` / `edit` |
| `input_images` | 省略 | 参考图列表，结构见下文 |
| `mask` | 省略 | 第一张图的蒙版，结构见下文 |
| `input_fidelity` | 省略 | `low` / `high`；仅适用于支持的上游 |
| `previous_response_id` | 省略 | 同一 provider 保留的历史响应 ID |
| `previous_image_ids` | 省略 | 同一 provider 的 `image_generation_call` ID 列表 |
| `moderation` | 省略 | `auto` / `low`；上游内容策略仍生效 |
| `partial_images` | `0` | 严格整数 `1–3` 启用上游流式生成及中间图 |
| `safety_identifier` | 省略 | 最长 128 字符的隐私保护用户标识 |
| `include_preview` | `true` | 返回受大小限制的内联预览 |

除文件格式所需的 `output_format` 外，未指定的高级图像参数不会主动发送。接受一个参数值不代表任意中转站或模型都支持它。

最小调用示例（路径仅为示例，实际由 Codex 从当前任务取得）：

```json
{
  "prompt": "生成一只阳光、动漫风格、可爱的小狗",
  "cwd": "/home/user/projects/my-project"
}
```

指定输出：

```json
{
  "prompt": "透明背景的茶杯图标，简洁、柔和的绿色",
  "cwd": "C:/Users/YourName/Projects/my-site",
  "output_dir": "assets",
  "filename": "tea.webp",
  "background": "transparent"
}
```

`current_model` 不是使用者需要填写的安装参数。普通 MCP 没有可靠的通用接口读取界面临时选择；工具会明确报告 `model_source=caller` 或 `config`。`caller` 只说明参数来自调用方，不代表验证了会话身份。两处都没有模型时，请求前报错，不硬编码兜底型号。

## 参考图、蒙版与连续编辑

`input_images` 每项恰有 `path`、`image_url`、`file_id` 之一，可附加 `detail`：

```json
[
  {"path": "assets/source.png"},
  {"image_url": "https://example.com/reference.png", "detail": "high"},
  {"image_url": "data:image/png;base64,..."},
  {"file_id": "file_example"}
]
```

`path` 支持绝对路径或相对 `cwd` 的路径；`detail` 可为 `auto`、`low`、`high`、`original`。HTTP(S) URL 直接交给上游，不在本地额外下载。

`mask` 也是恰含 `path`、`image_url`、`file_id` 之一的对象，不接受 `detail`。优先使用本地文件、data URL 或 provider 文件 ID。可本地读取的蒙版会检查 alpha；首张参考图也可本地读取时，检查尺寸和格式是否匹配。远程内容由上游验证。

后续编辑可以传同一 provider 的 `previous_response_id` 和/或 `previous_image_ids`。ID 能否继续使用取决于上游保留策略，不能跨 provider 搬用。提供参考图时仍需在 `prompt` 中写明编辑内容。

## 保存与返回

- 生成的是 PNG、JPEG、WebP 位图；不直接输出 SVG、GIF 或动画。
- 透明背景要求 PNG/WebP。显式格式与文件后缀冲突、JPEG 透明背景、无效压缩值等会在请求前拒绝。
- 上游返回格式与请求不一致时，保留可用图片，按真实格式修正扩展名并返回警告。
- 请求前校验目标目录并预留文件名；已有文件不覆盖。显式名称给第一张成功图片，后续图片编号。
- 多张图片中某项失败时，其他成功文件仍保留；不因部分失败自动重复生成。
- SDK 自动重试关闭。超时、断流等可能发生在上游已处理请求之后，应先核实状态再决定是否重试。

成功或已进入生成流程的结果含：

| 字段 | 含义 |
| --- | --- |
| `status` | `success`、`partial_success` 或 `error` |
| `images` | 原图绝对路径、真实 MIME、尺寸、字节数、call ID、修订提示词和上游元数据 |
| `partials` | 中间预览的信息，不是已保存的最终原图 |
| `response_id` / `request_id` | 上游返回的响应与请求标识，可能为空 |
| `model` / `model_source` | 请求使用的对话模型及其来源 |
| `reported_model` | 上游明确报告的模型名称；未知时为空 |
| `text` / `usage` | 上游文字说明及用量信息 |
| `warnings` / `errors` | 警告及按项错误 |

配置或参数错误会返回精简的错误结构，字段可能少于上表。MCP 文本内容也包含路径、ID 和重要警告，便于不支持结构化结果的客户端使用。`reported_model` 不保证是专用图像模型，更不等于验证了 Codex 当前会话模型。

最多返回四张预览，每张以 1024×1024 为尺寸上限并进行压缩；预览可能重新编码。交付原图应使用 `images[].path`，不能把预览当作完整文件。

`partial_images > 0` 会读取上游 SSE 并发送 MCP 进度通知，中间图可包含在最终工具结果中。是否实时显示图片由客户端决定，不能保证与 Codex 内置工具界面一致。

## 本地处理限制

这些限制用于控制本地资源，不代表图像模型的能力上限：

- 参考/历史图像引用最多 16 个。
- 本地/data 输入合计 100 MB，单张小于 50 MB。
- 最多 16 张最终图，合计 200 MiB，单张小于 100 MiB。
- 最多保留 12 张中间图，合计 32 MiB。

目录中可能出现请求期间的隐藏锁或临时文件。不要删除仍在使用的锁；进程意外终止留下的锁，应确认没有相关任务后再处理。

## 连接来源

工具只读取选中的已有用户级 Codex 配置：

- 自定义 provider 的 `base_url`、`env_key`、`experimental_bearer_token`、`http_headers`、`env_http_headers`、`query_params`。
- 内置 `openai` provider 的顶层 `openai_base_url`；没有明确 URL 时不静默替换地址。
- 当前 provider 要求 OpenAI API Key 认证时，对应环境变量或文件存储的 API Key。
- 新版 profile 文件和旧版内嵌 profile。

不读取项目 `.env`，不扫描其他 provider 的 Key，不实现 ChatGPT OAuth、系统钥匙串登录或外部 token 命令。同一 Authorization 来源出现不一致值时拒绝；URL 中重复的 query 键也会报错。

| 启动设置 | 用途 |
| --- | --- |
| `--codex-home` / `CODEX_HOME` | 定位已有 Codex 配置目录 |
| `--profile` / `CODEX_IMAGE_PROFILE` | 选择已有 profile |
| `CODEX_IMAGE_TIMEOUT` | 可选请求超时，默认 300 秒，范围 `(0, 3600]` |

这些设置不建立第二套 URL、Key、模型配置。选中的 provider/profile 应与当前 Codex 会话保持一致。

## 与官方能力的关系

项目包装 [Responses 的 image_generation 工具](https://developers.openai.com/api/docs/guides/tools-image-generation)，从 `image_generation_call.result` 读取图片，而不是调用旧式 Images 结果的 `data[0].b64_json`。

动漫、写实、插画、产品图等内容风格取决于提示词和上游模型。工具接口可以尽量接近官方，但无法证明中转站实际路由、模型版本、参数支持及界面体验与官网完全相同。
