# Codex Image Generation MCP

为使用中转站的 Codex 提供本地图像生成和编辑工具。安装后，直接告诉 Codex：

> 生成一只阳光、动漫风格的可爱小狗，保存在当前项目中。

Codex 调用 `generate_image`，工具复用用户已有的 Codex URL、API Key 和模型设置，保存原图并返回文件路径和预览。默认直接保存到当前任务目录，也可以指定文件夹、文件名和 PNG / JPEG / WebP 格式。

无需另配图像模型；请求通过 Responses API 的 `image_generation` 工具完成，不发送 `tools[].model`。中转站必须支持这一接口，并允许所选对话模型调用图像工具。

本项目由 TangRuoJie 维护，采用 [MIT 许可证](LICENSE)，是独立社区项目，与 OpenAI 无隶属关系。

## 生成效果展示

示例：阳光下、动漫风格的可爱小狗。

![阳光下坐在草地上的动漫风格可爱小狗](sunny-anime-puppy.png)

## 下载包后安装

当前版本：**0.3.0**。分发包为 `codex-image-generation-0.3.0.zip`，内含 wheel、源码包、安装说明、许可证、配置示例和 SHA-256 校验清单。这是 Python 安装包，需要 [uv](https://docs.astral.sh/uv/getting-started/installation/)；首次安装需要联网下载 Python（如果缺少）和依赖。

分享给别人时，发送 `dist/codex-image-generation-0.3.0.zip` 即可，对方按照包内 `INSTALL.md` 操作。以后公开 Release 后，下载其中的同名附件；GitHub 自动提供的 `Source code.zip` 是源码归档，不是这个安装分发包。

**第 1 步：准备自己的环境。** 对方需要已安装 Codex、已有可用的中转站配置，并安装 uv。执行 `uv --version` 确认命令可用。URL、API Key 和模型复用对方自己的 Codex 配置，不需要复制作者的配置。

**第 2 步：安装并检查。** 解压 ZIP，在包含 `.whl` 文件的解压目录打开终端。下面的命令适用于 Windows PowerShell、macOS 和 Linux：

~~~sh
uv tool install --python 3.11 ./codex_image_generation-0.3.0-py3-none-any.whl
codex-image-generation --version
codex-image-generation --check
~~~

如果系统找不到 `codex-image-generation`，执行 `uv tool update-shell` 后重新打开终端。版本应显示 `0.3.0`，检查结果应包含 `configured: true`；`connection_verified: false` 是正常的，因为此命令只检查本地配置，不请求中转站。检查失败时，先按 [安装说明中的故障排查](docs/INSTALL.md#常见问题) 修正现有配置。

**第 3 步：生成接入配置。** 执行：

~~~sh
codex-image-generation --print-config
~~~

**第 4 步：保存配置。** 将上一步输出的整个 `[mcp_servers.image_generation]` TOML 配置块加入**自己电脑**的 Codex 用户配置文件并保存。这条命令只打印内容，不会自动完成接入。

| 系统 | 默认配置文件 |
| --- | --- |
| Windows | `%USERPROFILE%\.codex\config.toml` |
| macOS / Linux | `~/.codex/config.toml` |

设置过 `CODEX_HOME` 时使用该目录下的 `config.toml`。已有同名 `[mcp_servers.image_generation]` 时替换该节，避免重复。保留已有的模型和 provider 设置。若认证依赖环境变量，启动 Codex 的环境也必须能读取这些变量。

**第 5 步：重启 Codex 并验证。** 保存配置后重启 Codex，打开需要保存图片的项目，确认 MCP 已加载且包含 `generate_image` 工具。首次测试可以明确说：

> 请使用 image_generation MCP，生成一只阳光、动漫风格的可爱小狗，保存在当前项目中。

成功时应收到原图路径，并能在当前项目目录找到图片；仅安装成功或 `--check` 通过不代表已经验证上游出图能力。之后日常直接描述图片和保存位置即可，Codex 会按配置启动服务，无需每次安装或手动常驻一个终端。

安装过程不会自动修改 Codex 配置。生成的接入配置使用安装环境中 Python 的绝对路径，不依赖项目源码目录或下载目录；常规安装完成后可以删除解压文件。包内不包含发布者的 URL、API Key 或 Codex 配置。

完整的安装、升级、卸载、离线限制和故障排查见 [安装说明](docs/INSTALL.md)。也提供 [本地 wheel 的 uv 启动配置](examples/codex-uvx.toml)，采用该方式时需要保留 wheel。

## 日常使用

| 你对 Codex 说 | 工具行为 |
| --- | --- |
| 生成一张安静的书桌插画 | 在当前任务目录保存唯一命名的 PNG |
| 生成透明背景的茶杯图标，存到 assets/tea.webp | 保存到指定目录，使用 WebP 和透明背景 |
| 把这张图片中的沙发改成蓝色 | 将参考图传给上游进行编辑 |
| 按上一张继续调整，光线暖一些 | 在同一 provider 支持历史引用时继续编辑 |

风格可以是写实、动漫、插画、像素画、海报等，由提示词和上游图像模型决定。本项目封装官方 Responses 图像工具的调用方式；不保证与 ChatGPT 网页或 Codex 内置工具的模型路由、全部功能、界面和效果完全相同。

支持的主要能力：

- PNG、JPEG（含 `jpg` 别名）、WebP 静态图片；透明背景使用 PNG / WebP。
- 尺寸、质量、背景、JPEG / WebP 压缩率等选项，具体可用值由上游模型决定。
- 参考图、蒙版，以及同一 provider 的历史响应 / 图像 ID。
- 可选流式中间图与 MCP 进度；客户端是否实时显示取决于其支持。
- 原图落盘、受大小限制的预览、绝对路径和结构化结果。
- 已有文件不覆盖；部分图片失败时保留成功结果；不会自动重试付费生成。

不输出 SVG、GIF 动画或视频。所有参数、返回字段和限制见 [工具参考](docs/TOOLS.md)。

## 模型、凭据与当前目录

工具公开参数只有一个必需的业务输入 `prompt`，以及由 Codex 从任务上下文提供的绝对工作目录 `cwd`。用户不必在每次对话中手动填写路径；没有指定输出位置时，直接保存到 `cwd`。

模型选择顺序：

1. Codex 能可靠获知当前会话的精确 API 模型 ID 时，调用时传入 `current_model`。
2. 未传入时，读取所选 Codex 配置中的 `model`。
3. 两处都没有模型时明确报错，不硬编码兜底模型。

普通 MCP 协议不保证提供当前会话模型。因此，界面临时切换模型后能否立即同步，取决于 Codex 是否传入可靠上下文；不能仅凭读取配置文件就承诺跟随所有界面切换。`current_model` 是调用参数，无需用户另建模型配置。

连接设置只读取所选用户级 Codex provider：

- 复用 `base_url`、环境变量 API Key、配置中的 bearer token、请求头和查询参数。
- 在所选 provider 要求 API Key 认证时，支持读取 Codex 文件存储的 API Key。
- 支持已有 profile；使用 `--profile` 选择。
- 不扫描其他 provider 寻找密钥，不读取项目 `.env`，不实现 ChatGPT OAuth 或系统钥匙串登录。

`--print-config` 会生成所需环境变量**名称**的白名单，不复制密钥值。若 provider 使用环境变量认证，启动 Codex 的环境也必须能访问这些变量。

| 可选启动设置 | 用途 |
| --- | --- |
| `--codex-home` / `CODEX_HOME` | 定位已有 Codex 配置目录 |
| `--profile` / `CODEX_IMAGE_PROFILE` | 选择已有 profile |
| `CODEX_IMAGE_TIMEOUT` | 上游超时秒数，默认 300，范围大于 0 且不超过 3600 |

## 系统兼容性

项目使用 Python 3.11+，wheel 为 `py3-none-any`，不写死开发者目录。依赖中包含本机二进制组件，因此这一标签不表示任意旧系统都能直接安装。

| 平台 | 验证情况 |
| --- | --- |
| Windows x64 | 本地测试、安装包独立环境启动和 stdio 验证 |
| Linux x64 | 容器内安装、测试和 stdio 验证 |
| macOS Apple Silicon | 已检查依赖 wheel 可获取；尚无 macOS 实机运行验证 |
| macOS Intel | 当前 cryptography 50 系列缺少 Intel wheel，可能需要本机构建；尚未验证完整安装 |

建议使用仍受支持的现代系统。旧版 Linux（例如 glibc 2.17）可能缺少当前 Pillow 的预编译包。首次安装需要联网；不将此包描述为免依赖、完全离线的可执行文件。

## 源码开发与构建分发包

仅使用工具时无需下载源码。维护项目时，在源码目录执行：

~~~sh
uv sync --locked
uv run --locked python -B -m unittest discover -s tests -v
uv run --locked python scripts/build_release.py
uv run --locked python scripts/smoke_wheel.py --wheel dist/codex_image_generation-0.3.0-py3-none-any.whl
~~~

构建输出：

~~~text
dist/
  codex_image_generation-0.3.0-py3-none-any.whl
  codex_image_generation-0.3.0.tar.gz
  codex-image-generation-0.3.0.zip
  SHA256SUMS-0.3.0.txt
~~~

构建脚本检查包内容，不纳入本地配置、凭据或虚拟环境。源码包包含 README 使用的展示图 `sunny-anime-puppy.png`，其他生成图片不纳入；wheel 不包含展示图。安装冒烟测试使用临时安装目录、假凭据和无网络图像请求，从其他工作目录验证 CLI 与 MCP；不会替换你的全局安装。

仓库包含 Windows、Linux、macOS 的 CI 工作流，执行测试、打包和安装验证，并保存构建产物。维护者可手动运行发布工作流，为已存在的版本标签创建 GitHub Release 草稿；只有全部检查通过才会创建草稿。工作流是否通过以实际运行结果为准。当前产物在本地构建，尚未上传 PyPI 或其他公开下载服务；不要将裸命令 `uvx codex-image-generation` 视为已验证的安装入口。

详见 [发布维护说明](docs/RELEASING.md) 和 [更新记录](CHANGELOG.md)。

## 贡献、反馈与许可

- 作者：TangRuoJie；邮箱：[3177065378@qq.com](mailto:3177065378@qq.com)。
- 贡献代码或改进文档，请先阅读 [贡献指南](CONTRIBUTING.md)。
- 普通问题可通过仓库 Issues 反馈；漏洞或凭据泄漏请按 [安全报告说明](SECURITY.md) 私下联系。
- 本项目采用 [MIT 许可证](LICENSE)，允许使用、修改和商业分发，分发时需保留版权及许可声明。第三方依赖遵循各自的许可证。

## 官方参考

- [Responses 图像生成工具](https://developers.openai.com/api/docs/guides/tools-image-generation)
- [Responses Create API](https://developers.openai.com/api/reference/resources/responses/methods/create)
- [Codex MCP 配置](https://developers.openai.com/codex/mcp)
- [Codex 配置参考](https://developers.openai.com/codex/config-reference)
