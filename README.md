# Codex Image Generation MCP

使用 FastMCP 提供本地 `stdio` 图像工具，复用 Codex 的中转站地址和凭据，内部调用 OpenAI Responses API 的 `image_generation` 工具。

用户可以直接说：

- “生成一张安静的书桌插画。” → 当前项目的 `outputs/images/`，自动命名 PNG。
- “生成一个透明背景 Logo，保存到 public/images/logo.webp。” → 指定目录与 WebP 格式。
- “把 assets/room.png 中的沙发改成蓝色，保存为 room-blue.png。” → 使用参考图编辑。

Codex 应在调用工具时传入当前任务的**绝对项目路径**。MCP 不使用自己的启动目录猜测当前项目，也不能自动获知 Codex 界面临时切换的模型。

## 安装与接入

要求 Python 3.11+ 和 uv。在本项目目录执行：

```powershell
uv sync --frozen
uv run --frozen gpt-image-generation --check --responses-model gpt-6-astra
uv run --frozen gpt-image-generation --print-config --responses-model gpt-6-astra
```

`--check` 只读取本地配置，不请求上游、不产生生成费用。`configured=true` 不等于中转站的出图能力已验证。

将 `--print-config` 输出加入用户级 `$CODEX_HOME/config.toml`（默认 `~/.codex/config.toml`）。配置可解析时，它会自动包含所需的环境变量名（不包含值），并按请求超时设置更长的 MCP 超时。也可以使用下面的源码开发配置，替换为实际项目绝对路径：

```toml
[mcp_servers.image_generation]
command = "uv"
args = ["--directory", "E:/OpenProject/gpt-image-generation", "run", "--frozen", "gpt-image-generation", "--responses-model", "gpt-6-astra"]
startup_timeout_sec = 60
tool_timeout_sec = 360
env_vars = ["CODEX_HOME"]
```

通过 CLI 注册的等价方式：

```powershell
codex mcp add image_generation -- uv --directory E:/OpenProject/gpt-image-generation run --frozen gpt-image-generation --responses-model gpt-6-astra
```

注册后补充 `tool_timeout_sec = 360`，并重新加载 MCP / 启动新会话。如果 provider 使用 `env_key = "MY_PROVIDER_API_KEY"`，请将 **变量名** 加入 `env_vars`，例如 `["CODEX_HOME", "MY_PROVIDER_API_KEY"]`，让子进程继承已有环境变量；不需要复制密钥值。如果桌面应用找不到 `uv`，使用其绝对路径，或使用 `--print-config` 输出的 Python 绝对路径配置。

本仓库不会自动改写你的 Codex 配置。普通启动模式为 stdio 服务，stdout 仅用于协议，不会打印提示语或生成结果。

## 两个工具

### `generate_image`

| 参数 | 默认值 / 作用 |
| --- | --- |
| `prompt` | 必填，图片描述或编辑指令 |
| `project_dir` | 必填，由 Codex 根据当前任务传入项目绝对路径 |
| `output_dir` | 默认 `outputs/images`；相对路径按项目解析，也支持用户指定的绝对目录 |
| `filename` | 可选，仅文件名；省略时自动生成唯一名称，多图附加序号 |
| `output_format` | 省略时按文件扩展名推断，再默认 PNG；`png` / `jpeg` / `jpg` / `webp` |
| `size` | `auto` 或 `WIDTHxHEIGHT`，例如 `1536x1024` |
| `quality` | `auto` / `low` / `medium` / `high`；2.5 flare/sunburst 另支持 `xhigh` / `max` |
| `background` | `auto` / `opaque` / `transparent` |
| `output_compression` | JPEG/WebP 可选整数 0–100；PNG 不接受 |
| `action` | `auto` / `generate` / `edit` |
| `reference_images` | 可选本地 PNG/JPEG/WebP 路径列表，按项目解析相对路径 |
| `mask_image` | 可选本地蒙版，作用于第一张参考图 |
| `input_fidelity` | 可选 `low` / `high`，只用于支持该参数的图像模型 |
| `previous_response_id` | 可选上次返回的响应 ID；依赖中转站保留 Responses 状态 |
| `responses_model` | 本次调用的对话模型覆盖值，例如 `gpt-6-astra` |
| `image_model` | 本次调用的专用图像模型覆盖值，默认 `gpt-image-2.5-flare` |
| `include_preview` | 默认 `true`，返回最多四张有限尺寸的预览及全部原图路径 |

URL 和 API Key 不出现在工具参数中。原图按上游返回的字节保存；预览可能缩小和重新编码，预览不是原始交付文件。返回结构还包含实际模型、MIME、宽高、响应 ID、文字说明和 revised prompt。

示例调用：

```json
{
  "prompt": "生成一个简洁的茶杯品牌图标，透明背景",
  "project_dir": "E:/OpenProject/my-site",
  "output_dir": "public/images",
  "filename": "tea-logo.webp",
  "background": "transparent",
  "quality": "high",
  "size": "1024x1024",
  "responses_model": "gpt-6-astra"
}
```

### `get_image_generation_status`

无参数。报告本地配置是否齐全、provider、模型与来源、凭据来源、默认保存规则。不会返回密钥、Authorization、完整请求头或查询参数，也不会请求上游。

## 图像参数规则

- 只输出 PNG/JPEG/WebP，`jpg` 归一化为 `jpeg`；不输出 SVG 或动画 GIF。
- 文件名后缀与显式格式不一致时，在请求前报错；返回字节的真实格式也会被检查。
- 透明背景仅支持 PNG/WebP；JPEG + `transparent` 会在请求前报错。
- GPT Image 2/2.5 自定义尺寸：宽高均为 16 的倍数，任一边不超过 3840，长宽比不超过 3:1，总像素在 655,360–8,294,400 之间。高于 2560×1440 的分辨率在官方文档中标为实验性。
- 较早的已知图像模型仅接受其标准尺寸。未知中转模型别名保留字符串传递，实际能力由上游判断，不强行替换模型。
- 参考图小于 50 MB。蒙版需与第一张参考图格式、尺寸一致，并有 alpha 通道；透明区域表示希望编辑的区域。模型对蒙版的遵循不是像素级保证。
- `action=edit` 需要参考图或 previous response ID。纯本地参考图编辑不依赖上游会话存储。
- `gpt-image-2` 的输入保真度自动为高，不传 `input_fidelity`；`gpt-image-1-mini` 只支持 `low`。
- 请求前检查目标目录和重名文件，使用不覆盖写入；不会静默替换已有图片。相对输出路径必须留在项目内，用户显式指定的绝对目录可以位于项目外。

第一版采用一次请求返回最终图片；不提供流式中间预览，不另外实现 Images API 的 `n` 参数。会处理 Responses 返回的所有图像 Item，但提示词要求的图片数量不代表上游一定返回该数量。

## 自动配置与模型选择

每次调用读取用户级 `config.toml`。通过 `--profile NAME` 或 `CODEX_IMAGE_PROFILE` 选择 `$CODEX_HOME/NAME.config.toml`；兼容旧的内嵌 profile 配置。项目内部的 `.codex/config.toml` 不作为凭据来源。

支持 provider 的 `base_url`、`env_key`、`experimental_bearer_token`、`http_headers`、`env_http_headers` 和 `query_params`。地址中的查询参数会单独传给 SDK，避免破坏 `/responses` 路径。只有当前 provider 使用 OpenAI 认证时，才回退到 `OPENAI_API_KEY` 或 `auth.json` 的 `OPENAI_API_KEY`；不会借用别的 provider 的凭据。ChatGPT OAuth、系统钥匙串、仅内存登录和外部 token 命令不在第一版支持范围内，会给出诊断。对于 `cli_auth_credentials_store="auto"`，无法判断钥匙串还是文件为当前来源，因此也不自动使用残留 `auth.json`；显式配置的 API Key / provider 认证头仍可使用。

对话模型优先级：本次 `responses_model` → `--responses-model` / `CODEX_IMAGE_RESPONSES_MODEL` → 所选 profile / 用户配置 → 缺省 `gpt-6-astra`。

图像模型优先级：本次 `image_model` → `--image-model` / `CODEX_IMAGE_MODEL` → `gpt-image-2.5-flare`。

上面的接入示例明确使用你指定的 `gpt-6-astra`。删掉 `--responses-model` 及其值即可跟随配置文件中的模型；这不代表自动获知界面里的临时会话选择。

| 可选环境变量 | 作用 |
| --- | --- |
| `CODEX_HOME` | 用户配置目录 |
| `CODEX_IMAGE_PROFILE` | 显式选择配置 profile |
| `CODEX_IMAGE_PROVIDER` | 显式选择用户配置中已有的 provider |
| `CODEX_IMAGE_RESPONSES_MODEL` | 对话模型覆盖 |
| `CODEX_IMAGE_MODEL` | 图像模型覆盖 |
| `CODEX_IMAGE_TIMEOUT` | 上游请求超时秒数，默认 300；MCP 超时应更长 |
| `CODEX_IMAGE_BASE_URL` + `CODEX_IMAGE_API_KEY` | 可选完整独立连接配置，必须同时提供，不混用原 provider 的认证头 |

不自动搜索/加载项目 `.env`。常规使用直接复用 Codex 的用户级配置即可。

## 验证与故障排查

```powershell
uv run --frozen python -B -m unittest discover -s tests -v
uv run --frozen gpt-image-generation --check --responses-model gpt-6-astra
```

测试使用假配置、模拟 Responses 和本地小图片，覆盖格式/路径校验、错误处理、图像与元数据返回、真实 SDK 的离线 HTTP 请求构造以及真实 stdio 握手，不请求付费生成接口。

中转站必须支持 `/responses` 以及 `image_generation` 工具；仅提供普通对话能力不够。模型名称存在也不代表该中转已开放图像工具权限。

401/403、限流、超时、模型不支持、空结果和无效图片会作为工具错误返回，异常响应正文不会直接暴露给 Codex。SDK 自动重试关闭；遇到超时请先查看上游请求情况，避免重复生成和计费。默认不会修改用户所选模型进行回退。

本地文件写入遵循运行 MCP 进程的系统权限。生成结果落在用户指定的路径；工具的调用仍由宿主 Codex 的权限策略管理。

## 官方依据

参数按 2026-10-31 读取的官方文档及本项目锁定的 SDK 核对：

- [Responses 图像生成工具](https://developers.openai.com/api/docs/guides/tools-image-generation)
- [图像生成、尺寸、格式与蒙版要求](https://developers.openai.com/api/docs/guides/image-generation)
- [Codex 配置参考](https://developers.openai.com/codex/config-reference)
- [Codex 认证与凭据存储](https://developers.openai.com/codex/auth)
