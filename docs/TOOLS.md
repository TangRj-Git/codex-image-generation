# 工具与配置参考

服务使用 stdio，公开工具为 `generate_image`、`list_models`、`list_image_models`。两个列表工具只查询目录，不生成图片。接入方式见 [uvx 安装说明](INSTALL.md)，本地配置诊断使用 `uvx codex-image-generation@latest --check`。

## 调用原则

1. Codex 从当前任务上下文取得绝对 `cwd`，不能使用 MCP 安装目录代替。
2. 用户未指定位置时，省略 `output_dir`、`filename`，直接保存到 `cwd`。
3. 用户给出完整目标路径时，拆为 `output_dir` 和仅含文件名的 `filename`。
4. 默认自动选择模型。用户指定型号时，分别传 `responses_model` 和 `image_model`；`current_model` 只用于可靠会话上下文提供的回退对话型号。
5. 返回原图路径、预览及重要警告。后续编辑需要保留同一 provider 的 response/image call ID。

URL 和认证复用所选 Codex provider。顶层 `model` 是对话模型，`tools[].model` 是专用图像模型，两者独立选择；图像型号不需要出现在中转站 `/models` 列表中。

## 复用系统 imagegen 技能

技能由 Codex 宿主选择和加载，MCP 服务没有激活宿主技能的接口。本服务的初始化说明和 `generate_image` 工具描述会提醒宿主：用户选择此 MCP 时，若系统 `imagegen` 技能可用，读取该技能并复用通用提示词指导、参考图角色、编辑不变量和结果检查流程。

推荐显式指定，避免技能的默认工具选择与用户期望不同：

```text
$imagegen 请使用系统自带的技能整理提示词、处理参考图并检查结果。
实际图片生成和编辑使用 image_generation MCP 的 generate_image。
生成一张安静的书桌插画，保存在当前项目。
```

内置技能通常默认调用内置图像工具。这里由用户明确选择 MCP 执行，图片参数、模型选择、文件路径和失败处理遵循本 MCP 的接口；不套用技能 CLI 专用的默认模型、参数限制或目录约定。生成后，宿主可使用图片查看能力检查返回的原图路径。

本项目不复制技能正文、不修改系统 `SKILL.md`、不硬编码技能安装路径，也不向图像模型发送整份技能文件。仅调用 MCP 不能证明宿主已加载技能；其他客户端没有该技能时仍可直接使用工具，但不会因此自动获得该技能的工作流程。

官方说明：[技能的显式与隐式调用](https://developers.openai.com/codex/skills#how-codex-uses-skills)、[使用 $imagegen](https://learn.chatgpt.com/docs/image-generation#generate-or-edit-an-image)。

## 模型发现与推荐

| 工具 | 参数 | 数据来源 |
| --- | --- | --- |
| `list_models` | `refresh=false`，可选 `model_policy=quality/fast` | 当前 provider 的 `/models`；官方工具指南用于标记图像工具支持情况 |
| `list_image_models` | `refresh=false`，可选 `model_policy=quality/fast` | OpenAI 官方图像工具指南及模型页；不读取 provider 凭据 |

`list_models` 返回的结构化 `models` 包含对话模型候选，排除明显的图像、音频和嵌入型号。标准 `/models` 没有完整的能力字段，名称推断项标为 `text_capability=inferred_from_id`；无法分类的别名放在 `unclassified_models`。只有官方工具指南列出的对话模型及已知日期快照参与自动推荐，推断项不会自动选中。

两个列表工具的推荐策略均为本次 `model_policy` → `CODEX_IMAGE_MODEL_POLICY` → `quality`，显式参数覆盖环境偏好。列表只读取各自相关的型号偏好，无关的另一层型号设置不会阻断查询。图像列表通过 `image_model_override` 显示环境变量中的固定型号；推荐型号不覆盖用户已经固定的选择。

返回 `recommended_model`、`recommendation_reason`、`source`、`fetched_at`（Unix 秒）、`stale` 和 `warnings`。两个 MCP 列表工具均返回结构化结果和文本 JSON：不可获取目录时为 `status=error`、`isError=true`；旧缓存、内置回退或带警告的结果为 `status=partial_success`；完整正常结果为 `status=success`。CLI 保持 JSON 输出，不可获取目录时返回非零退出码。`provider_tool_support_verified=false` / `provider_support_verified=false` 表示没有进行付费探测，官方支持不等于中转站已支持。旧缓存会保留原更新时间，失败退避期间还会提供 `retry_after`（Unix 秒）。

官网解析分别读取正文开头的图像型号和 `Supported models` 中的顶层对话型号，过滤停用型号，并读取模型页的定位与快照。只有模型页实际列出的有效日期快照会继承对话型号的工具支持标记。明确的停用记录会保留在目录中用于校验，并在读取缓存时按日期生效；若官网确认已无可用型号，不会用旧内置型号替代这一结果。

`quality` 优先使用官方模型首页 Featured models 的当前旗舰定位、图像指南的明确推荐，再参考已核对的本地偏好；`fast` 使用有依据的速度偏好，不根据描述中偶然出现的 `fast` 字样加分，已知快照继承其别名的偏好。候选都没有速度依据时，保留完整质量排序并报告 `quality_fallback_no_speed_evidence`，不把质量得分截断后按名称选择。

推荐不是全模型的客观性能排名，不根据型号名称、列表顺序或创建时间推断谁更强。新型号没有充分定位信息时标为 `unranked`；没有有依据的候选、或不同型号同分而无法区分时，`recommended_model` 可以为空。对话模型此时可使用已有配置回退；图像模型没有已固定选择时需显式指定。用户始终可以手动选择有效型号。

目录缓存位于所选 Codex 配置目录的 `cache/image-generation/`。中转站列表缓存 5 分钟，并按地址、凭据和租户上下文隔离；官方目录缓存 24 小时。缓存只存放模型目录和时间，不存凭据。刷新失败后退避 60 秒，期间跳过重复的目录读取；`refresh=true` / `--refresh-models` 绕过退避并强制刷新。退避不会自动重试付费生成。

刷新失败时可以使用最后有效缓存，包括中转站 `/models` 返回 403、404 或网络失败的情况；旧缓存没有固定最大保留期，必须结合 `stale`、原更新时间和警告理解，不能视为当前可用性的证明。官网没有有效缓存则使用标明日期的内置快照。目录在查询或生成时按需刷新，没有后台定时轮询，也不会在每次生成时强制联网刷新。

对话模型选择顺序：本次 `responses_model` → `CODEX_IMAGE_RESPONSES_MODEL` → 中转站目录推荐 → 可靠的 `current_model` / Codex 配置 `model`。图像模型选择顺序：本次 `image_model` → `CODEX_IMAGE_MODEL` → 官网目录推荐。选择策略为本次 `model_policy` → `CODEX_IMAGE_MODEL_POLICY` → `quality`。

两个型号都已明确指定时不联网发现，但仍用内置规则和已有本地目录检查已知停用信息及模型层级。自定义别名可以显式指定，未知能力交给上游验证。自动选择应省略型号参数，不能把 `auto` 当成型号 ID；已知对话型号与图像型号放反、无效 ID 或包含已知认证值的名称会在请求前拒绝。

`image_model="upstream_default"` 是本地兼容选项：省略 `tools[].model` 并标记图像型号未知；不会把此字符串发送为 API 模型名，也不能用于 `responses_model`。上游拒绝型号后不会自动换型号重试。

## 参数

| 参数 | 默认值 / 可选值 | 用途 |
| --- | --- | --- |
| `prompt` | 必填非空字符串 | 图片描述或编辑要求 |
| `cwd` | 必填绝对路径 | 当前任务工作目录 |
| `current_model` | 省略 | 可靠上下文提供的回退对话模型 API ID |
| `responses_model` | 自动选择 | 本次请求的顶层对话模型，不改变 Codex 会话 |
| `image_model` | 自动选择 | 专用图像型号；`upstream_default` 显式使用上游默认路由 |
| `model_policy` | 环境偏好，否则 `quality` | `quality` 能力优先，`fast` 速度优先；与渲染质量参数独立 |
| `output_dir` | `cwd` | 相对于 `cwd` 的目录，或用户指定的绝对目录 |
| `filename` | 自动生成唯一文件名 | 仅文件名，已有文件不覆盖 |
| `output_format` | 从文件名推断，否则 `png` | `png` / `jpeg` / `jpg` / `webp` |
| `size` | 省略 | `auto` 或 `WIDTHxHEIGHT`；已知型号在本地校验尺寸规则 |
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

除所选图像 `model` 和文件格式所需的 `output_format` 外，未指定的高级图像参数不会主动发送。已知型号会校验质量、尺寸和 `input_fidelity`；例如 GPT Image 2 不接受 `max` 或可调 fidelity，2.5 支持 `xhigh/max`。未知型号不会套用已知型号的限制。接受一个参数值不代表任意中转站都支持它。

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

指定两个模型的示例：

```json
{
  "prompt": "生成一张安静的书桌插画",
  "cwd": "/home/user/projects/my-project",
  "responses_model": "gpt-6-astra",
  "image_model": "gpt-image-2.5-sunburst",
  "quality": "high"
}
```

`current_model` 不是使用者需要填写的安装参数。普通 MCP 无法确认界面临时选择，自动选择的对话模型可能与会话不同。自动选择、显式参数和配置回退都会标记来源；没有候选和回退型号时，请求前报错。

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

`path` 支持绝对路径或相对 `cwd` 的路径；本地参考图和蒙版必须是普通文件，目录、管道和设备不会被作为图片读取。`detail` 可为 `auto`、`low`、`high`、`original`。HTTP(S) URL 直接交给上游，不在本地额外下载。

`mask` 也是恰含 `path`、`image_url`、`file_id` 之一的对象，不接受 `detail`。优先使用本地文件、data URL 或 provider 文件 ID。可本地读取的蒙版会检查 alpha；首张参考图也可本地读取时，检查尺寸和格式是否匹配。远程内容由上游验证。

后续编辑可以传同一 provider 的 `previous_response_id` 和/或 `previous_image_ids`。ID 能否继续使用取决于上游保留策略，不能跨 provider 搬用。提供参考图时仍需在 `prompt` 中写明编辑内容。

## 保存与返回

- 生成的是 PNG、JPEG、WebP 位图；不直接输出 SVG、GIF 或动画。
- 透明背景要求 PNG/WebP。显式格式与文件后缀冲突、JPEG 透明背景、无效压缩值等会在请求前拒绝。
- 上游返回格式与请求不一致时，保留可用图片，按真实格式修正扩展名并返回警告。
- 请求前校验目标目录并预留文件名；已有文件不覆盖。显式名称给第一张成功图片，后续图片编号。
- 多张图片中某项失败时，其他成功文件仍保留；不因部分失败自动重复生成。
- SDK 自动重试关闭。超时、断流等可能发生在上游已处理请求之后，应先核实状态再决定是否重试。
- 超时或取消后，已开始的本地文件操作需要完成安全清理，因此实际返回时间可能略超出配置的时间预算。

成功或已进入生成流程的结果含：

| 字段 | 含义 |
| --- | --- |
| `status` | `success`、`partial_success` 或 `error` |
| `images` | 原图绝对路径、真实 MIME、尺寸、字节数、call ID、修订提示词和上游元数据 |
| `partials` | 中间预览的信息，不是已保存的最终原图 |
| `response_id` / `request_id` | 上游返回的响应与请求标识，可能为空 |
| `model` / `model_source` | 请求使用的对话模型及其来源 |
| `reported_model` | 上游 `response.model`，属于对话模型；未知时为空 |
| `responses_model_requested` / `responses_model_source` | 请求发送的对话型号及选择来源，旧 `model` 字段保持兼容 |
| `responses_model_reported` | 上游报告的对话型号 |
| `image_model_selected` / `image_model_source` | 请求选择的图像型号及来源；兼容模式下型号为空 |
| `image_model_reported` / `image_model_evidence` | 标准结果没有独立图像型号报告，因此保持空值；证据为 `request_only` 或 `upstream_default_unknown` |
| `model_selection_policy` / `catalog_source` / `catalog_fetched_at` | 选择偏好、目录来源和更新时间；两个型号都固定时目录字段为空 |
| `catalog_stale` | 官网目录是否过期或使用内置回退；没有读取目录时为空 |
| `provider_models_source` / `provider_models_fetched_at` / `provider_models_stale` | 中转站模型列表的来源、更新时间和过期状态；没有读取列表时为空 |
| `responses_model_reason` / `image_model_reason` | 自动选择的推荐依据；显式选择时为空 |
| `text` / `usage` | 上游文字说明（含提供的拒绝说明）及对话模型用量 |
| `tool_usage` | 单独保留上游工具用量，例如 `image_gen`；缺失表示未知，不等于零 |
| `warnings` / `errors` | 警告及按项错误 |

配置或模型选择前的参数错误会返回精简的错误结构，字段可能少于上表；选定型号后发现能力不兼容时保留模型及来源，帮助定位问题。MCP 文本内容也包含路径、型号、ID 和重要警告。显式请求型号不能证明中转站实际路由；不把对话模型、请求回显或推测填入 `image_model_reported`。

`errors[]` 中的错误项可包含 `upstream_code`、`upstream_param`、`upstream_type`，未完整完成时还可包含 `incomplete_reason`。SDK 在流式事件交付前抛出的 API 错误也会尽量保留这些标识，读取超时归类为 `timeout`。上游提供的拒绝说明保留在 `text` 中并经过已知认证值脱敏；不返回原始错误正文或已知凭据。已完成图片仍会保留，用量可能不完整，实际费用以 provider 账单为准。

最多返回四张预览，每张以 1024×1024 为尺寸上限并进行压缩；预览可能重新编码。交付原图应使用 `images[].path`，不能把预览当作完整文件。

`partial_images > 0` 会读取上游 SSE 并发送 MCP 进度通知，中间图可包含在最终工具结果中。是否实时显示图片由客户端决定，不能保证与 Codex 内置工具界面一致。

## 本地处理限制

这些限制用于控制本地资源，不代表图像模型的能力上限：

- 参考/历史图像引用最多 16 个。
- 本地/data 输入合计 100 MB，单张小于 50 MB。
- 最多 16 张最终图，合计 200 MiB，单张小于 100 MiB。
- 最多保留 12 张中间图，合计 32 MiB。
- `config.toml`、独立 profile 和 `auth.json` 每个文件最多 1 MiB（1,048,576 字节）。
- 单个模型目录或失败记录缓存文件最多 1,000,000 字节，JSON 容器嵌套最多 32 层；读写使用同一限制，不依赖 Python 版本的递归行为。

配置、认证和缓存读取均要求普通文件，打开前后都会核对文件类型和大小；打开后继续增长也受读取上限约束。POSIX 使用非阻塞打开来处理文件被替换成 FIFO 的情况，读取结束或失败时关闭描述符。过深的配置/认证数据转换为安全解析错误，profile 合并采用迭代方式，避免深层递归。

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
| `CODEX_IMAGE_TIMEOUT` | 单次生成总时间预算，包含本地准备、模型发现、上游请求和图片处理；默认 300 秒，范围 `(0, 3600]` |
| `CODEX_IMAGE_RESPONSES_MODEL` | 可选固定对话型号 |
| `CODEX_IMAGE_MODEL` | 可选固定图像型号或 `upstream_default` |
| `CODEX_IMAGE_MODEL_POLICY` | 默认选择偏好，`quality` 或 `fast` |

这些设置复用已有 URL 和 Key，仅添加可选模型偏好。选中的 provider/profile 应与当前 Codex 会话保持一致。环境变量需转发给 MCP 子进程；本次工具参数优先于环境偏好。

## 与官方能力的关系

项目包装 [Responses 的 image_generation 工具](https://developers.openai.com/api/docs/guides/tools-image-generation)，从 `image_generation_call.result` 读取图片，而不是调用旧式 Images 结果的 `data[0].b64_json`。

动漫、写实、插画、产品图等内容风格取决于提示词和上游模型。工具接口可以尽量接近官方，但无法证明中转站实际路由、模型版本、参数支持及界面体验与官网完全相同。
