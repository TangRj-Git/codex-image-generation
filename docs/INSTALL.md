# PyPI、下载包安装与 Codex 接入

安装一次后，可在任意项目中使用。MCP 复用使用者自己的 Codex 中转站配置；分发包不包含作者的 URL、API Key 或模型设置。

## 推荐：通过 PyPI / uvx 接入

安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，并准备已有的 Codex 中转站配置。当前 [PyPI 版本](https://pypi.org/project/codex-image-generation/0.3.0/) 为 `0.3.0`：

```sh
uvx codex-image-generation --version
uvx --python 3.11 codex-image-generation@0.3.0 --check
```

`uvx` 会自动安装和缓存所需环境，无需下载源码、手动安装依赖或保留 wheel。首次运行需要联网；推荐 Python 3.11，缺少时由 uv 下载。`--check` 使用自己的 Codex 配置，不请求中转站；配置中的 URL、认证和模型要求见下文“准备条件”和“诊断命令的含义”。

将下面内容加入 Codex 用户配置（Windows 默认 `%USERPROFILE%\.codex\config.toml`，macOS/Linux 默认 `~/.codex/config.toml`）：

```toml
[mcp_servers.image_generation]
command = "uvx"
args = ["--python", "3.11", "codex-image-generation@0.3.0"]
startup_timeout_sec = 120
tool_timeout_sec = 360
env_vars = []
```

已有同名表时替换它，保留其他 provider 和模型配置。若 provider 通过 `env_key` / `env_http_headers` 引用环境变量，把实际的变量**名称**加入 `env_vars`（例如 `env_vars = ["MY_RELAY_KEY"]`），不要填写密钥值；确保启动 Codex 的环境中已有这些变量。

使用自定义 `CODEX_HOME`、`CODEX_IMAGE_PROFILE` 或 `CODEX_IMAGE_TIMEOUT` 时也需转发对应变量，或者在 `args` 的包名之后加入 `--codex-home` / `--profile` 参数。配置目录使用本机绝对路径。请求超时超过 300 秒时相应调大 `tool_timeout_sec`，为本地处理留出余量。

保存后重启 Codex，确认 `image_generation` MCP 加载并出现 `generate_image` 工具。Codex 会启动 stdio 服务，不需要另外保持终端运行。可以直接说：“请使用 image_generation MCP，生成一只阳光、动漫风格的可爱小狗，保存在当前项目中。”实际生成会访问中转站，并可能产生费用。

若桌面应用找不到 `uvx`，把 `command` 换为其绝对路径；Windows 可用 `Get-Command uvx`，macOS/Linux 可用 `command -v uvx` 查看。可复制的文件示例见 [codex-pypi.toml](../examples/codex-pypi.toml)。

`uvx ... --print-config` 输出的 Python 路径可能位于缓存目录，清理缓存后会失效。采用上述 `command = "uvx"` 的配置即可在需要时重建环境。若偏好持久工具安装，也可以使用：

```sh
uv tool install --python 3.11 codex-image-generation==0.3.0
codex-image-generation --print-config
```

此时 `--print-config` 指向持久工具环境，可以按下文“加入自己的 Codex 配置”保存。以下下载包安装方式仍可使用。

## 获取正确的安装包

在 [GitHub Release v0.3.0](https://github.com/TangRj-Git/codex-image-generation/releases/tag/v0.3.0) 附件中下载 `codex-image-generation-0.3.0.zip`，也可由维护者直接分享该文件。GitHub 自动生成的 `Source code.zip` / `Source code.tar.gz` 是源码归档，不是本指南推荐的安装分发包。

解压后应能看到 `codex_image_generation-0.3.0-py3-none-any.whl`、`INSTALL.md`、`LICENSE`、配置示例、源码包和 `SHA256SUMS`。正常使用只需安装 wheel 并接入 Codex，不需要解压源码包，也不用把这些文件放进每个项目。

第一次使用的顺序是：**准备 uv 和已有 Codex 中转站配置 → 安装 wheel → 检查配置 → 生成并保存 MCP 配置 → 重启 Codex → 实际生成一张图片**。

## 准备条件

- 安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)。下文用 uv 建立独立的 Python 工具环境，不需要手动激活虚拟环境。
- 已有可用的 Codex 中转站配置，包括 URL、认证方式和模型。中转站须支持 Responses API 的 `image_generation` 工具。
- 安装时需要联网下载 Python（如果本机没有指定版本）和依赖；图片生成时需要访问中转站。

安装 uv 后执行 `uv --version` 确认命令可用。无需另行安装 FastMCP、OpenAI SDK 或 Pillow，安装 wheel 时会自动处理这些依赖。

安装包是 Python 包，不是免运行时的独立可执行文件，也不是离线依赖合集。建议使用 Python 3.11；包元数据允许 3.11 及以上版本，更高版本仍受依赖兼容性约束。

## 下载包：安装到独立工具环境

下载并解压 `codex-image-generation-0.3.0.zip`，找到 `codex_image_generation-0.3.0-py3-none-any.whl`。也可以直接取得这个 wheel 文件。

最直接的方法是在包含 wheel 的解压目录打开终端：

```sh
uv tool install --python 3.11 ./codex_image_generation-0.3.0-py3-none-any.whl
codex-image-generation --version
codex-image-generation --check
```

版本应显示 `0.3.0`，检查结果应包含 `configured: true`。`connection_verified: false` 是正常结果，含义见后文。检查失败时先修正现有 Codex 配置，再继续接入。

也可以从其他目录安装，使用下面的绝对路径示例；路径应指向实际解压出来的 wheel，不能直接指向 ZIP。

Windows PowerShell 示例，路径替换为自己的下载位置：

```powershell
uv tool install --python 3.11 "C:\Users\YourName\Downloads\codex_image_generation-0.3.0-py3-none-any.whl"
codex-image-generation --version
codex-image-generation --check
codex-image-generation --print-config
```

macOS 示例：

```sh
uv tool install --python 3.11 "$HOME/Downloads/codex_image_generation-0.3.0-py3-none-any.whl"
codex-image-generation --version
codex-image-generation --check
codex-image-generation --print-config
```

Linux 示例：

```sh
uv tool install --python 3.11 "$HOME/Downloads/codex_image_generation-0.3.0-py3-none-any.whl"
codex-image-generation --version
codex-image-generation --check
codex-image-generation --print-config
```

若终端提示找不到 `codex-image-generation`，执行 `uv tool update-shell`，按提示重新打开终端，再运行后三条命令。`uv tool dir --bin` 可查看命令所在目录。

安装成功后，运行时使用 uv 的工具环境，不再依赖下载目录或项目源码。可以删除下载的 wheel；请保留 uv 的工具环境及其 Python 运行时。

## 加入自己的 Codex 配置

本地检查通过后运行 `codex-image-generation --print-config`，将输出的整个 `[mcp_servers.image_generation]` 表加入 Codex 用户配置并保存。此命令只打印配置，不自动修改文件。如果已有同名表，应替换该表，不要重复添加；保留原有的模型和 provider 设置。

默认配置文件位置：

| 系统 | 默认路径 |
| --- | --- |
| Windows | `%USERPROFILE%\.codex\config.toml` |
| macOS / Linux | `~/.codex/config.toml` |

使用自定义 `CODEX_HOME` 时，文件是该目录下的 `config.toml`。需要指定已有配置位置或 profile 时，在生成配置和诊断时使用同样的参数：

```sh
codex-image-generation --codex-home /absolute/path/to/codex-home --profile work --check
codex-image-generation --codex-home /absolute/path/to/codex-home --profile work --print-config
```

这里的目录和 `work` 都是示例，须对应已有配置。Windows 可以使用带引号的 Windows 绝对路径。`--profile` 选择已有 profile，不创建第二套 URL、密钥或模型设置。

生成的配置会使用当前已安装环境的 Python 绝对路径，以及本机 Codex 配置目录的绝对路径；无需把 MCP 安装目录设为项目目录。生成的配置只能用于当前电脑，其他使用者应在自己的电脑上执行 `--print-config`。

配置中的 `env_vars` 是转发给 MCP 的环境变量名，不包含变量值。如果已有 provider 通过 `env_key` 或 `env_http_headers` 引用环境变量，启动 Codex 的进程也必须能读取这些变量。仅在另一个终端临时设置变量，不一定能传给已启动的桌面应用；设置后重新启动 Codex。不要把 API Key 复制进示例文件。

保存配置后重启 Codex，打开需要保存图片的项目。确认 `image_generation` MCP 已加载且包含 `generate_image` 工具。首次验证建议明确指定工具：

> 请使用 image_generation MCP，生成一只阳光、动漫风格、可爱的小狗，保存在当前项目中。

Codex 应调用 `generate_image` 并传入当前任务的绝对 `cwd`；不指定目标目录时，原图直接保存在这个目录。需要指定位置时，可以说“保存到当前项目的 `assets/puppy.webp`”。

完成验证的标准是：Codex 返回保存路径，且能在该路径找到可打开的图片。此后直接自然语言提出生成需求即可，不用每次写工具参数或重新配置 URL、Key、模型。

Codex 会根据保存的 MCP 配置启动服务，不需要使用者手动保持一个服务终端。若直接运行不带参数的 `codex-image-generation`，它会进入 stdio 模式等待客户端消息，看起来没有输出是正常的；终端诊断请使用 `--version`、`--check` 或 `--print-config`。

## 诊断命令的含义

`--check` 只检查本地配置，不请求中转站、不生成图片、不产生图片生成费用。

- `configured: true` 表示本地连接配置可解析。
- `connection_verified: false` 表示没有进行网络验证，不能据此判断中转站是否支持图片工具。
- `model_available_in_config` 表示配置里是否有可用模型；没有时，每次调用需要由 Codex 提供可靠的 `current_model`。
- `session_model_auto_detection: false` 表示普通 MCP 无法自行确认 Codex 界面的临时模型选择。

真正生成图片会调用中转站，可能产生费用。超时或断流后先检查结果和中转站记录，不要自动连续重试。

## 常见问题

| 现象 | 处理方法 |
| --- | --- |
| 找不到 `uv` | 按官方安装说明安装 uv，重新打开终端，再运行 `uv --version`。 |
| 找不到 `codex-image-generation` | 确认 wheel 安装成功，执行 `uv tool update-shell`，按提示重新打开终端。 |
| 安装时提示 wheel 文件不存在 | 确认 ZIP 已解压，在包含 `.whl` 的目录运行相对路径命令，或改用真实绝对路径。 |
| `--check` 返回 `configured: false` | 根据错误检查原有 Codex 配置位置、所选 provider、认证来源；自定义位置/profile 时使用对应启动参数。 |
| 检查结果为 `connection_verified: false` | 这是本地诊断的预期结果；实际生成图片才会验证上游连接和工具支持。 |
| 终端检查通过，Codex 中却缺少认证环境变量 | `env_vars` 只转发变量名；让启动 Codex 的环境能读取这些变量，并重启 Codex。 |
| Codex 找不到这个 MCP 或工具 | 确认已把完整配置块保存到正确的 `config.toml`，没有重复同名表，配置中的 Python 路径仍存在，然后重启 Codex。 |
| MCP 能启动，但生成时上游报错 | 核对中转站对 Responses、`image_generation` 和所选对话模型的支持，以及具体参数限制；仅能聊天不代表能出图。 |
| 切换了界面模型，返回的模型仍来自配置 | 普通 MCP 无法自行确认临时模型选择；可靠调用上下文未传入 `current_model` 时会使用配置中的模型。 |

反馈问题时仅提供脱敏后的相关错误片段，不要发送 API Key、完整 Codex 配置或私人图片。

## 可选：通过本地 wheel 启动

不建立持久工具安装时，可以使用发布包中的 `codex-uvx.toml` 示例；源码中的同一文件位于 `examples/codex-uvx.toml`。它使用：

```sh
uv tool run --python 3.11 --from /absolute/path/to/codex_image_generation-0.3.0-py3-none-any.whl codex-image-generation
```

该模式的 wheel 必须保留在固定绝对路径。Codex 需要能找到 `uv`；桌面应用 PATH 不完整时，把 `command` 改为 uv 可执行文件的绝对路径。首次启动可能下载依赖，建议先在终端用同一命令追加 `--check` 完成准备。

此模式不要将 `--print-config` 输出作为持久安装配置：输出可能指向 uv 的临时缓存环境，清理缓存后可能失效。需要稳定配置时采用上面的 `uv tool install`。

包名启动方式参见本文开头的 PyPI / uvx 接入，无需保留本地 wheel。

## 升级、卸载与源码开发

从较早的安装版本迁移时，请安装当前名称的包，并重新运行 `codex-image-generation --print-config` 更新接入配置，避免继续引用旧安装环境。

使用 `uvx` 固定版本配置时，升级需将 `args` 中的 `@0.3.0` 改为准备使用的新版本，再重启 Codex。可先运行 `uvx --refresh --python 3.11 codex-image-generation@版本号 --check` 验证，其中“版本号”替换为实际已发布版本。不要仅因清理缓存就修改已有 provider 的配置。

下载新版本后重新安装本地 wheel：

```sh
uv tool install --force --python 3.11 /absolute/path/to/new-version.whl
codex-image-generation --check
codex-image-generation --print-config
```

`new-version.whl` 是路径占位，需替换为下载文件的真实完整文件名。用新的输出更新 Codex 的同名 MCP 配置，再重新加载 MCP。升级不改写 Codex 的 provider 设置，也不清理项目中已生成的图片。

卸载时执行 `uv tool uninstall codex-image-generation`，并从 Codex 配置中移除对应 MCP 表。

开发者在源码根目录使用：

```sh
uv sync --locked
uv run --frozen codex-image-generation --check
uv run --frozen codex-image-generation --print-config
```

源码方式生成的配置指向项目虚拟环境，移动/删除源码目录或重建环境后应重新生成配置。分发给其他人时优先使用 wheel。

## 平台边界

项目代码及 wheel 不含固定的作者目录，也不含系统专用可执行文件。实际运行还取决于 Python、FastMCP、Pillow 及其间接依赖。

- Windows x64 和现代 Linux x64 已进行运行测试。
- macOS 尚未进行实机运行测试。Apple Silicon 的依赖二进制解析已检查，但这不等于运行验证。
- 当前依赖链中的 `cryptography 50.0.2` 缺少 Intel macOS wheel；Intel Mac 可能需要构建依赖，尚未验证，不承诺开箱即用。
- 较老的 Linux、特殊 CPU 架构、自行编译的 Python/Pillow 需要单独验证；旧版 glibc 可能没有对应依赖 wheel。

普通 MCP 无法保证像 Codex 内置工具那样实时显示生成过程。最终返回包含原图绝对路径和受大小限制的预览；完整文件以原图路径为准。
