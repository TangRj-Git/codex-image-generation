# 通过 uvx 接入 Codex

安装 [uv](https://docs.astral.sh/uv/getting-started/installation/) 后，通过 `uvx` 从 [PyPI](https://pypi.org/project/codex-image-generation/) 启动 MCP。无需下载源码、保留 wheel 或在配置中指定 Python 版本。

## 准备条件

- 已安装 uv，终端中执行 `uvx --version` 能正常返回。
- 已有可用的 Codex 中转站配置，包括 URL、认证方式和模型。中转站须支持 Responses API 的 `image_generation` 工具。
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

保存后重启 Codex，确认 `image_generation` MCP 已加载并提供 `generate_image` 工具。Codex 会按配置启动 stdio 服务，不需要 `serve` 参数，也不用手动维持一个服务终端。

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
| `model_available_in_config` | 配置中是否存在可用模型；没有时，调用方需要提供可靠的 `current_model` |
| `session_model_auto_detection: false` | 普通 MCP 无法自行确认 Codex 界面的临时模型选择 |

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
| `CODEX_IMAGE_TIMEOUT` | 上游请求超时秒数，默认 300，范围大于 0 且不超过 3600 |

配置检查和 MCP 启动应使用同一份配置及 profile。请求超时超过 300 秒时，相应调大 `tool_timeout_sec`，为本地图片处理留出余量。

## 自动更新与旧配置迁移

`codex-image-generation@latest` 会在 MCP 启动时刷新包索引信息，选择兼容运行环境的最新版本。更新后重新启动 MCP 或重启 Codex 即可使用；已经运行的服务不会在生成过程中被替换。新版本须先发布到 PyPI，仅修改 GitHub 源码不会更新用户正在使用的包。

从源码路径、本地 wheel、持久工具安装或固定包版本迁移时，将原来的整个 `[mcp_servers.image_generation]` 表替换为本文配置，并保留自己确实需要的环境变量和超时设置。

当前 CLI 的 `--print-config` 输出会指向执行时的 Python 环境；通过 `uvx` 运行时，这可能是缓存目录。使用本文的 `command = "uvx"` 配置即可在缓存清理后重新准备环境，无需保存缓存 Python 的路径。

如需停用，移除对应 MCP 配置并重启 Codex。`uvx` 的环境缓存可以保留，无需另外卸载一个全局 Python 包。

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
| 切换界面模型后，返回的模型仍来自配置 | 普通 MCP 无法自动识别临时模型选择；未收到可靠的 `current_model` 时使用配置中的模型。 |

平台仍受 Python 和依赖的兼容性限制，特殊架构或较老系统需要单独验证。反馈问题时只提供脱敏后的相关错误，不要发送 API Key、完整配置或私人图片。
