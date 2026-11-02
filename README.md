# Codex Image Generation MCP

为 Codex 提供图片生成与编辑工具，复用已有中转站的 URL 和认证。从中转站发现对话模型，从 OpenAI 官方文档发现图像模型，默认按能力优先策略自动选择。图片默认保存在当前项目，也可以指定目录、文件名和 PNG / JPEG / WebP 格式。

[PyPI](https://pypi.org/project/codex-image-generation/) · [安装与故障排查](https://github.com/TangRj-Git/codex-image-generation/blob/master/docs/INSTALL.md) · [工具参考](https://github.com/TangRj-Git/codex-image-generation/blob/master/docs/TOOLS.md)

## 快速接入

先安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，并准备好可用的 Codex 中转站配置。中转站需要支持 Responses API 的 `image_generation` 工具。

将下面配置加入自己的 Codex `config.toml`，已有同名 MCP 表时替换该表：

```toml
[mcp_servers.image_generation]
command = "uvx"
args = ["codex-image-generation@latest"]
startup_timeout_sec = 120
tool_timeout_sec = 360
env_vars = []
```

默认配置文件位置：Windows 为 `%USERPROFILE%\.codex\config.toml`，macOS / Linux 为 `~/.codex/config.toml`。使用自定义 `CODEX_HOME` 时，修改该目录下的 `config.toml`，并按[安装说明](https://github.com/TangRj-Git/codex-image-generation/blob/master/docs/INSTALL.md#环境变量与自定义配置)转发这个变量。

保存后重启 Codex，确认 `image_generation` MCP 已加载，就可以直接说：

> 请使用 image_generation MCP，生成一只阳光、动漫风格的可爱小猫，保存在当前项目的 outputs 文件夹中。

`uvx` 自动管理运行环境和依赖，无需下载源码、手动安装 wheel 或固定 Python 版本。`@latest` 会在 MCP 启动时检查 PyPI 上的最新兼容版本；发布新版本后，重新启动 MCP 或重启 Codex 即可使用。服务由 Codex 启动，不需要 `serve` 参数，也不用单独保持终端运行。

如果 provider 通过环境变量认证，把对应的变量**名称**加入 `env_vars`，并确保启动 Codex 的进程能读取它们。Codex 找不到 `uvx` 时，将 `command` 改为本机 `uvx` 的绝对路径。可直接复制的配置文件见 [codex-uvx.toml](https://github.com/TangRj-Git/codex-image-generation/blob/master/examples/codex-uvx.toml)。

## 首次检查

先在终端运行以下命令，可以完成依赖准备并检查本地配置：

```sh
uvx codex-image-generation@latest --version
uvx codex-image-generation@latest --check
```

`--check` 只检查本地配置，不请求中转站。`configured: true` 表示配置可解析；`connection_verified: false` 表示尚未验证上游连接，是正常结果。实际生成图片时会访问中转站，并可能产生费用。

查看模型目录（只查询列表和文档，不生成图片）：

```sh
uvx codex-image-generation@latest --list-models
uvx codex-image-generation@latest --list-image-models
uvx codex-image-generation@latest --list-models --refresh-models
```

在 Codex 中也可以直接说“用 image_generation 的 list_models 查看中转站的对话模型”或“用 list_image_models 查看图像模型”。目录会标明推荐型号、来源、更新时间和未确认的支持情况。

## 使用示例

| 你对 Codex 说 | 工具行为 |
| --- | --- |
| 生成一张安静的书桌插画 | 在当前项目保存图片 |
| 生成透明背景的茶杯图标，存到 assets/tea.webp | 按指定路径和格式保存 |
| 把这张图片中的沙发改成蓝色 | 使用参考图进行编辑 |
| 按上一张继续调整，光线暖一些 | 在上游支持历史引用时继续编辑 |
| 使用 gpt-6-astra 调用 gpt-image-2.5-sunburst 生成图片 | 分别设置对话模型和图像模型 |

支持参考图、蒙版、历史图片引用和可选流式中间预览。已有文件不会覆盖，部分生成失败时会保留成功图片；不会自动重复付费生成。参数、格式限制和返回字段详见[工具参考](https://github.com/TangRj-Git/codex-image-generation/blob/master/docs/TOOLS.md)。

可以复用 Codex 系统自带的 `imagegen` 技能，无需安装另一份技能。明确指定技能和执行工具，例如：

> $imagegen 请按这个技能整理提示词并检查结果，实际生成使用 image_generation MCP 的 generate_image。生成一张安静的书桌插画，保存到当前项目。

MCP 的说明会提示宿主复用已有技能的通用工作流程；技能加载仍由 Codex 决定，调用 MCP 本身不保证激活技能。技能默认的内置工具、CLI 参数及保存路径规则不直接套用到本 MCP。

## 效果展示

阳光下的动漫风格小狗：

![阳光下坐在草地上的动漫风格可爱小狗](https://raw.githubusercontent.com/TangRj-Git/codex-image-generation/master/sunny-anime-puppy.png)

## 模型与运行条件

- URL 和认证使用已有 Codex provider。对话模型写入顶层 `model`，图像模型写入 `tools[].model`；图像型号无需出现在中转站 `/models` 中。
- 默认自动选择；`responses_model`、`image_model` 可固定本次型号，`model_policy=fast` 可优先速度。`image_model=upstream_default` 显式沿用上游图像路由。
- 中转站列表不可用或没有可推荐型号时，回退到可靠的 `current_model` 或 Codex 配置中的 `model`。普通 MCP 无法自行确认界面临时选择的模型。
- 中转站列表缓存 5 分钟，官网目录缓存 24 小时；刷新失败会标明旧缓存或内置快照。推荐是有来源的选择策略，不是性能评测或中转站能力验证。
- 返回结果分别显示请求选择的图像型号和对话型号。上游没有独立报告图像执行型号时保持未知；显式指定型号不证明中转站实际路由。
- 包要求 Python 3.11 及以上，实际运行还取决于平台和依赖兼容性。uv 会管理 Python；首次安装和更新检查需要联网。
- 图片质量、参数支持和生成速度取决于中转站及其上游。预览可能经过压缩，完整图片以返回的保存路径为准。

环境变量、自定义 profile、升级和常见问题见[安装说明](https://github.com/TangRj-Git/codex-image-generation/blob/master/docs/INSTALL.md)。

## 开发与维护

在源码目录准备环境并运行测试：

```sh
uv sync --locked
uv run --locked python -B -m unittest discover -s tests -v
```

开发规范见[贡献指南](https://github.com/TangRj-Git/codex-image-generation/blob/master/CONTRIBUTING.md)，构建、GitHub Release 与 PyPI 发布流程见[发布说明](https://github.com/TangRj-Git/codex-image-generation/blob/master/docs/RELEASING.md)，版本变更见[更新记录](https://github.com/TangRj-Git/codex-image-generation/blob/master/CHANGELOG.md)。

由 TangRuoJie 维护，采用 [MIT 许可证](https://github.com/TangRj-Git/codex-image-generation/blob/master/LICENSE)。这是独立社区项目，与 OpenAI 无隶属关系。普通问题通过 [Issues](https://github.com/TangRj-Git/codex-image-generation/issues) 反馈；漏洞或凭据泄漏请按[安全报告说明](https://github.com/TangRj-Git/codex-image-generation/blob/master/SECURITY.md)私下联系。
