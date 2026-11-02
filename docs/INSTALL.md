# 通过 uvx 接入 Codex

安装 [uv](https://docs.astral.sh/uv/getting-started/installation/) 后，通过 `uvx` 从 [PyPI](https://pypi.org/project/codex-image-generation/) 启动 MCP。无需下载源码、保留 wheel 或在配置中指定 Python 版本。

## 准备条件

- 已安装 uv，终端中执行 `uvx --version` 能正常返回。
- 已有可用的 Codex 中转站配置，包括 URL 和认证方式。中转站须支持 Responses API 的 `image_generation` 工具；对话模型可以自动发现或手动指定。
- 首次启动和更新检查可以联网。uv 会管理所需 Python 和依赖；本项目要求 Python 3.11 及以上。

## 添加 MCP 配置

在自己的 Codex 用户配置中加入下面内容。已有同名表时替换它，保留其他 MCP、provider 和模型配置：

```toml
[mcp_servers.image_generation]
command = "uvx"
args = ["codex-image-generation@latest"]
startup_timeout_sec = 120
tool_timeout_sec = 360
env_vars = []
```

默认配置文件位置：

| 系统 | 配置文件 |
| --- | --- |
| Windows | `%USERPROFILE%\.codex\config.toml` |
| macOS / Linux | `~/.codex/config.toml` |

使用自定义 `CODEX_HOME` 时，文件位于该目录下；还需将该变量转发给 MCP，方法见下文。完整配置文件示例见 [codex-uvx.toml](../examples/codex-uvx.toml)。

保存后重启 Codex，确认 MCP 已提供 `generate_image`、`list_models`、`list_image_models`。Codex 会按配置启动 stdio 服务，不需要 `serve` 参数，也不用手动维持一个服务终端。

## 首次验证

先在终端运行，完成环境准备并检查本地配置：

```sh
uvx codex-image-generation@latest --version
uvx codex-image-generation@latest --check
```

`--version` 显示实际运行的包版本。`--check` 不请求中转站、不生成图片，其主要字段含义如下：

| 字段 | 含义 |
| --- | --- |
| `configured: true` | 本地连接配置可解析 |
| `connection_verified: false` | 未进行网络验证，是本地检查的正常结果 |
| `model_available_in_config` | 配置中是否存在回退对话型号；没有时仍可自动发现或显式传 `responses_model` |
| `responses_model_override` / `image_model_override` | 环境变量设置的固定型号，未设置时为空 |
| `model_selection_policy` | 自动选择偏好，默认 `quality` |
| `session_model_auto_detection: false` | 普通 MCP 无法自行确认 Codex 界面的临时模型选择 |

包含模型发现功能的版本可以先查看模型目录；这些命令在需要刷新时联网，不会生成图片：

```sh
uvx codex-image-generation@latest --list-models
uvx codex-image-generation@latest --list-image-models
uvx codex-image-generation@latest --list-models --refresh-models
uvx codex-image-generation@latest --list-image-models --model-policy fast
```

`--list-image-models` 不需要 provider 凭据，只读取图像相关偏好。两个列表命令在省略 `--model-policy` 时均继承 `CODEX_IMAGE_MODEL_POLICY`，未设置时使用 `quality`。图像列表同时显示环境中固定的 `image_model_override`。`--list-models` 使用当前中转站认证，只返回对话模型候选和单独的未分类别名。`provider_tool_support_verified: false` 表示列表查询不证明图像工具可执行。

列表不可获取时，CLI 仍输出 JSON 并返回非零状态，MCP 返回 `isError=true`；存在有效旧缓存时返回 `partial_success` 和原更新时间。生成仍可使用显式或已有配置中的对话型号。无法依据目录区分最优候选时，`recommended_model` 可以为空，用户可手动指定。

然后在 Codex 中提出生成需求，例如：

> 请使用 image_generation MCP，生成一只阳光、动漫风格的可爱小猫，保存在当前项目的 outputs 文件夹中。

实际生成会访问中转站，并可能产生费用。验证成功应以返回的保存路径存在且图片可以打开为准；`--check` 通过不等于已验证上游出图能力。超时或断流后先检查结果和中转站记录，再决定是否重试。

## 环境变量与自定义配置

MCP 复用使用者自己的 Codex 配置，不附带作者的 URL、密钥或模型设置。

如果 provider 通过 `env_key` 或 `env_http_headers` 引用环境变量，将实际变量**名称**加入 `env_vars`。例如 provider 使用 `MY_RELAY_KEY` 时，将原配置中的空数组改为：

```toml
env_vars = ["MY_RELAY_KEY"]
```

在启动 Codex 的环境中设置对应变量的值；不要把密钥值填进 `env_vars` 或示例文件。仅在另一个终端临时设置变量，不一定能传给已经启动的桌面应用。

以下设置也可以按需转发，使用哪些就把哪些变量名称加入同一个 `env_vars` 数组：

| 环境变量 | 用途 |
| --- | --- |
| `CODEX_HOME` | 自定义 Codex 配置目录 |
| `CODEX_IMAGE_PROFILE` | 选择已有 profile |
| `CODEX_IMAGE_TIMEOUT` | 单次生成的总时间预算，包含本地准备、模型发现、上游请求和图片处理；默认 300 秒，范围 `(0, 3600]` |
| `CODEX_IMAGE_RESPONSES_MODEL` | 固定顶层对话型号，覆盖自动选择 |
| `CODEX_IMAGE_MODEL` | 固定图像型号；`upstream_default` 显式使用上游默认路由 |
| `CODEX_IMAGE_MODEL_POLICY` | 自动推荐策略：`quality`（默认）或 `fast` |

配置检查和 MCP 启动应使用同一份配置及 profile。请求超时超过 300 秒时，相应调大 `tool_timeout_sec`，为本地图片处理留出余量。

`config.toml`、独立 profile 文件和 `auth.json` 的读取目标必须是普通文件，每个文件上限为 **1 MiB（1,048,576 字节）**。目录、管道、设备及超限文件会被拒绝；过深而无法解析的 TOML/JSON 会返回安全诊断。遇到读取错误时，请在本地检查文件类型、大小和格式，不要公开粘贴完整配置或认证文件。

模型偏好也可以直接放在已有 MCP 表的 `env` 子表中，无需修改整个 Codex 会话的模型：

```toml
[mcp_servers.image_generation.env]
CODEX_IMAGE_RESPONSES_MODEL = "gpt-6-astra"
CODEX_IMAGE_MODEL = "gpt-image-2.5-sunburst"
CODEX_IMAGE_MODEL_POLICY = "quality"
```

这些示例值是可选固定偏好；希望自动选择时省略对应型号。`env` 已直接提供的变量无需再放进 `env_vars`，现有 provider 凭据的转发方式保持不变。

模型缓存位于所选 Codex 配置目录的 `cache/image-generation/`，不存储认证值。单个缓存文件上限为 **1,000,000 字节**，读取目标也必须是普通文件；不满足约束的缓存按无效缓存处理。对话列表有效期 5 分钟，官方目录有效期 24 小时；刷新失败后退避 60 秒，并可使用明确标记为过期的旧缓存或标注日期的内置目录。旧缓存不代表中转站现在仍支持对应型号。`--refresh-models` 绕过退避主动刷新，不能单独作为服务启动参数。

## 自动更新与旧配置迁移

`codex-image-generation@latest` 会在 MCP 启动时刷新包索引信息，选择兼容运行环境的最新版本。更新后重新启动 MCP 或重启 Codex 即可使用；已经运行的服务不会在生成过程中被替换。新版本须先发布到 PyPI，仅修改 GitHub 源码不会更新用户正在使用的包。

从源码路径、本地 wheel、持久工具安装或固定包版本迁移时，将原来的整个 `[mcp_servers.image_generation]` 表替换为本文配置，并保留自己确实需要的环境变量和超时设置。

当前 CLI 的 `--print-config` 输出会指向执行时的 Python 环境；通过 `uvx` 运行时，这可能是缓存目录。使用本文的 `command = "uvx"` 配置即可在缓存清理后重新准备环境，无需保存缓存 Python 的路径。

如需停用，移除对应 MCP 配置并重启 Codex。`uvx` 的环境缓存可以保留，无需另外卸载一个全局 Python 包。

## 试用本地源码

README 和工具参考跟随当前源码，可能包含尚未发布到 PyPI 的功能。`@latest` 不会读取你的本地提交；出现未知 CLI 参数或缺少新工具时，先核对实际版本与 [CHANGELOG](https://github.com/TangRj-Git/codex-image-generation/blob/master/CHANGELOG.md)。

在源码目录执行 `uv sync --locked`，再检查本地命令：

```sh
uv run --locked codex-image-generation --check
uv run --locked codex-image-generation --list-models
uv run --locked codex-image-generation --list-image-models
```

试用本地 MCP 时，把已有同名表的启动命令改为下面形式，将路径替换成源码目录的绝对路径，并保留自己需要的 `env_vars`、`env` 和 profile 设置：

```toml
[mcp_servers.image_generation]
command = "uv"
args = ["--directory", "/absolute/path/to/codex-image-generation", "run", "--locked", "codex-image-generation"]
startup_timeout_sec = 120
tool_timeout_sec = 360
env_vars = []
```

Windows 路径可以写成 `E:/Projects/codex-image-generation`。重启 MCP 后生效；工具的输出目录仍由当前任务传入的 `cwd` 决定，不会因为服务从源码目录启动就改为源码目录。完成试用后可以恢复本文开头的已发布包配置。

## 常见问题

| 现象 | 处理方法 |
| --- | --- |
| 终端找不到 `uvx` | 安装 uv 并重新打开终端，确认它的可执行目录已加入 PATH。 |
| 终端能运行，Codex 找不到 `uvx` | 将 `command` 换成本机 `uvx` 的绝对路径。Windows 用 `Get-Command uvx`，macOS/Linux 用 `command -v uvx` 查看。 |
| 首次启动超时 | 先在终端运行 `uvx codex-image-generation@latest --version` 完成下载，再重启 MCP；需要时调大 `startup_timeout_sec`。 |
| 下载或检查更新失败 | 检查到 PyPI、Python 下载源的网络连接；首次安装需要联网。 |
| `--check` 返回 `configured: false` | 根据错误检查自己的 Codex 配置、provider、认证来源和 profile。 |
| 终端检查通过，MCP 却缺少凭据 | 确认所需变量名称已加入 `env_vars`，且启动 Codex 的环境中存在对应变量。 |
| Codex 中找不到工具 | 确认配置保存到了正确文件，没有重复的同名表，并重启 Codex。 |
| MCP 正常启动，但生成时报上游错误 | 核对中转站对 Responses API、`image_generation` 和所选模型的支持。能聊天不代表支持图片生成。 |
| MCP 选择的对话模型与界面不同 | 默认独立选择本次 Responses 请求模型；使用 `responses_model` 固定，不会修改 Codex 当前会话。 |
| `/models` 返回 403、404 或超时 | 列表能力不可用时仍可显式传 `responses_model` 或使用配置回退；不会自动探测付费生成。 |
| 中转站列表没有 image 型号 | 正常；专用图像型号来自官网，作为 `tools[].model` 发送。 |
| 中转站拒绝 `tools[].model` | 可明确选择 `image_model=upstream_default`；此模式下实际图像型号未知，不会自动换型号重试。 |

平台仍受 Python 和依赖的兼容性限制，特殊架构或较老系统需要单独验证。反馈问题时只提供脱敏后的相关错误，不要发送 API Key、完整配置或私人图片。
