# Codex Image Generation MCP

为 Codex 提供图片生成与编辑能力的本地 MCP 服务。复用已有 Codex 中转站的 URL 和认证，独立发现并选择对话模型与图像模型，将图片保存到当前项目或指定目录。

## 功能

- 根据文字描述生成图片，或使用参考图、蒙版编辑图片。
- 支持 PNG、JPEG 和 WebP；可指定保存目录和文件名。
- 返回原图路径和预览，已有文件不会覆盖。
- 支持上游提供的历史图片引用和可选流式中间预览。
- `list_models` 查询中转站对话模型，`list_image_models` 查询官方图像目录；默认按能力优先策略选择，也支持手动指定。

## 准备条件

安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，并准备好自己的 Codex 中转站配置。中转站须支持 Responses API 的 `image_generation` 工具；仅支持聊天的接口无法生成图片。

通过 `uvx` 启动时，uv 会管理 Python 和依赖，无需下载源码、手动安装 wheel 或固定 Python 版本。本项目要求 Python 3.11 及以上，首次准备环境需要联网。

## 配置到 Codex

在 Codex 用户配置文件中加入：

```toml
[mcp_servers.image_generation]
command = "uvx"
args = ["codex-image-generation@latest"]
startup_timeout_sec = 120
tool_timeout_sec = 360
env_vars = []
```

默认配置文件位置：Windows 为 `%USERPROFILE%\.codex\config.toml`，macOS / Linux 为 `~/.codex/config.toml`。已有同名 MCP 表时替换它，保存后重启 Codex。

如果 provider 通过环境变量认证，把实际的变量名称加入 `env_vars`，例如 `env_vars = ["MY_RELAY_KEY"]`，并确保启动 Codex 的环境中存在对应变量；不要在数组里填写密钥值。使用自定义 `CODEX_HOME`、`CODEX_IMAGE_PROFILE` 或 `CODEX_IMAGE_TIMEOUT` 时，也按需转发这些变量。自定义配置文件位于 `CODEX_HOME` 指定目录中。

`@latest` 会在每次启动 MCP 时检查 PyPI 上的最新兼容版本。发布新版本后，重新启动 MCP 或重启 Codex 即可更新；正在运行的服务不会自动替换。

## 检查与使用

可以先在终端准备运行环境并检查本地配置：

```sh
uvx codex-image-generation@latest --version
uvx codex-image-generation@latest --check
```

`--check` 不请求中转站。`configured: true` 表示本地配置可解析，`connection_verified: false` 表示尚未验证上游连接，是正常结果。

查看模型目录，不进行图像生成：

```sh
uvx codex-image-generation@latest --list-models
uvx codex-image-generation@latest --list-image-models
uvx codex-image-generation@latest --list-models --refresh-models
```

接入后，在 Codex 中直接提出需求，例如：

> 请使用 image_generation MCP，生成一只阳光、动漫风格的可爱小猫，保存在当前项目的 outputs 文件夹中。

如需复用系统自带的 `imagegen` 技能，可明确指定：

> $imagegen 请按技能整理提示词并检查图片，实际生成使用 image_generation MCP 的 generate_image。生成一只阳光、动漫风格的小猫。

项目提示宿主复用已有技能，不安装或修改技能。技能加载由 Codex 决定，MCP 不能强制激活；实际执行参数和保存路径遵循本工具。

Codex 会启动 MCP 并调用工具，不需要 `serve` 参数，也不用单独保持一个终端运行。若直接运行 `uvx codex-image-generation@latest`，程序会进入 stdio 服务并等待客户端消息。

## 使用说明

- Codex 找不到 `uvx` 时，将 `command` 改为本机 `uvx` 的绝对路径。Windows 可用 `Get-Command uvx` 查看，macOS / Linux 可用 `command -v uvx` 查看。
- 通过 `uvx` 接入时，使用上面的配置；`--print-config` 输出的 Python 路径可能位于缓存目录，清理缓存后可能失效。
- 顶层对话模型从中转站列表选择，`tools[].model` 从官网图像目录选择。图像型号无需出现在中转站列表中，列表也不证明上游已支持图像工具。
- `responses_model`、`image_model` 可覆盖自动选择，`model_policy=fast` 优先速度；`image_model=upstream_default` 显式省略图像型号。自动选择失败时，对话模型可以回退到 `current_model` 或配置中的 `model`。
- 可通过 `CODEX_IMAGE_RESPONSES_MODEL`、`CODEX_IMAGE_MODEL`、`CODEX_IMAGE_MODEL_POLICY` 保存偏好；使用进程环境变量时也须加入 `env_vars`。
- 对话模型列表缓存 5 分钟，官网目录缓存 24 小时。刷新失败标明旧缓存或内置快照；结果区分请求选择与上游报告的型号。
- 实际生成图片会访问中转站，并可能产生费用；图片效果和参数支持取决于上游。超时或断流后先核对结果，再决定是否重试。

完整说明：[安装与故障排查](https://github.com/TangRj-Git/codex-image-generation/blob/master/docs/INSTALL.md) · [工具参数](https://github.com/TangRj-Git/codex-image-generation/blob/master/docs/TOOLS.md) · [GitHub 仓库](https://github.com/TangRj-Git/codex-image-generation)

由 TangRuoJie 维护，采用 [MIT 许可证](https://github.com/TangRj-Git/codex-image-generation/blob/master/LICENSE)。独立社区项目，与 OpenAI 无隶属关系。
