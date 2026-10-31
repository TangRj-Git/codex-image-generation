# 下载包安装与 Codex 接入

安装一次后，可在任意项目中使用。MCP 复用使用者自己的 Codex 中转站配置；分发包不包含作者的 URL、API Key 或模型设置。

## 准备条件

- 安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)。下文用 uv 建立独立的 Python 工具环境，不需要手动激活虚拟环境。
- 已有可用的 Codex 中转站配置，包括 URL、认证方式和模型。中转站须支持 Responses API 的 `image_generation` 工具。
- 安装时需要联网下载 Python（如果本机没有指定版本）和依赖；图片生成时需要访问中转站。

安装包是 Python 包，不是免运行时的独立可执行文件，也不是离线依赖合集。建议使用 Python 3.11；包元数据允许 3.11 及以上版本，更高版本仍受依赖兼容性约束。

## 推荐：安装到独立工具环境

下载并解压 `gpt-image-generation-0.3.0.zip`，找到 `gpt_image_generation-0.3.0-py3-none-any.whl`。也可以直接取得这个 wheel 文件。

Windows PowerShell 示例，路径替换为自己的下载位置：

```powershell
uv tool install --python 3.11 "C:\Users\YourName\Downloads\gpt_image_generation-0.3.0-py3-none-any.whl"
gpt-image-generation --version
gpt-image-generation --check
gpt-image-generation --print-config
```

macOS 示例：

```sh
uv tool install --python 3.11 "$HOME/Downloads/gpt_image_generation-0.3.0-py3-none-any.whl"
gpt-image-generation --version
gpt-image-generation --check
gpt-image-generation --print-config
```

Linux 示例：

```sh
uv tool install --python 3.11 "$HOME/Downloads/gpt_image_generation-0.3.0-py3-none-any.whl"
gpt-image-generation --version
gpt-image-generation --check
gpt-image-generation --print-config
```

若终端提示找不到 `gpt-image-generation`，执行 `uv tool update-shell`，按提示重新打开终端，再运行后三条命令。`uv tool dir --bin` 可查看命令所在目录。

安装成功后，运行时使用 uv 的工具环境，不再依赖下载目录或项目源码。可以删除下载的 wheel；请保留 uv 的工具环境及其 Python 运行时。

## 加入自己的 Codex 配置

将 `--print-config` 输出的整个 `[mcp_servers.image_generation]` 表加入 Codex 用户配置。如果已有同名表，应替换该表，不要重复添加。

默认配置文件位置：

| 系统 | 默认路径 |
| --- | --- |
| Windows | `%USERPROFILE%\.codex\config.toml` |
| macOS / Linux | `~/.codex/config.toml` |

使用自定义 `CODEX_HOME` 时，文件是该目录下的 `config.toml`。需要指定已有配置位置或 profile 时，在生成配置和诊断时使用同样的参数：

```sh
gpt-image-generation --codex-home /absolute/path/to/codex-home --profile work --check
gpt-image-generation --codex-home /absolute/path/to/codex-home --profile work --print-config
```

这里的目录和 `work` 都是示例，须对应已有配置。Windows 可以使用带引号的 Windows 绝对路径。`--profile` 选择已有 profile，不创建第二套 URL、密钥或模型设置。

生成的配置会使用当前已安装环境的 Python 绝对路径，以及本机 Codex 配置目录的绝对路径；无需把 MCP 安装目录设为项目目录。生成的配置只能用于当前电脑，其他使用者应在自己的电脑上执行 `--print-config`。

配置中的 `env_vars` 是转发给 MCP 的环境变量名，不包含变量值。如果已有 provider 通过 `env_key` 或 `env_http_headers` 引用环境变量，启动 Codex 的进程也必须能读取这些变量。仅在另一个终端临时设置变量，不一定能传给已启动的桌面应用；设置后重新启动 Codex。不要把 API Key 复制进示例文件。

重新加载 MCP 或重新启动 Codex，然后在项目中说：

> 给我生成一只阳光、动漫风格、可爱的小狗，保存在当前项目中。

Codex 应调用 `generate_image` 并传入当前任务的绝对 `cwd`；不指定目标目录时，原图直接保存在这个目录。需要指定位置时，可以说“保存到当前项目的 `assets/puppy.webp`”。

## 诊断命令的含义

`--check` 只检查本地配置，不请求中转站、不生成图片、不产生图片生成费用。

- `configured: true` 表示本地连接配置可解析。
- `connection_verified: false` 表示没有进行网络验证，不能据此判断中转站是否支持图片工具。
- `model_available_in_config` 表示配置里是否有可用模型；没有时，每次调用需要由 Codex 提供可靠的 `current_model`。
- `session_model_auto_detection: false` 表示普通 MCP 无法自行确认 Codex 界面的临时模型选择。

真正生成图片会调用中转站，可能产生费用。超时或断流后先检查结果和中转站记录，不要自动连续重试。

## 可选：通过本地 wheel 启动

不建立持久工具安装时，可以使用发布包中的 `codex-uvx.toml` 示例；源码中的同一文件位于 `examples/codex-uvx.toml`。它使用：

```sh
uv tool run --python 3.11 --from /absolute/path/to/gpt_image_generation-0.3.0-py3-none-any.whl gpt-image-generation
```

该模式的 wheel 必须保留在固定绝对路径。Codex 需要能找到 `uv`；桌面应用 PATH 不完整时，把 `command` 改为 uv 可执行文件的绝对路径。首次启动可能下载依赖，建议先在终端用同一命令追加 `--check` 完成准备。

此模式不要将 `--print-config` 输出作为持久安装配置：输出可能指向 uv 的临时缓存环境，清理缓存后可能失效。需要稳定配置时采用上面的 `uv tool install`。

项目尚未发布到公共包索引，因此不要使用省略本地 wheel 的 `uvx gpt-image-generation` 或 `pip install gpt-image-generation` 来代表本项目。

## 升级、卸载与源码开发

下载新版本后重新安装本地 wheel：

```sh
uv tool install --force --python 3.11 /absolute/path/to/new-version.whl
gpt-image-generation --check
gpt-image-generation --print-config
```

`new-version.whl` 是路径占位，需替换为下载文件的真实完整文件名。用新的输出更新 Codex 的同名 MCP 配置，再重新加载 MCP。升级不改写 Codex 的 provider 设置，也不清理项目中已生成的图片。

卸载时执行 `uv tool uninstall gpt-image-generation`，并从 Codex 配置中移除对应 MCP 表。

开发者在源码根目录使用：

```sh
uv sync --locked
uv run --frozen gpt-image-generation --check
uv run --frozen gpt-image-generation --print-config
```

源码方式生成的配置指向项目虚拟环境，移动/删除源码目录或重建环境后应重新生成配置。分发给其他人时优先使用 wheel。

## 平台边界

项目代码及 wheel 不含固定的作者目录，也不含系统专用可执行文件。实际运行还取决于 Python、FastMCP、Pillow 及其间接依赖。

- Windows x64 和现代 Linux x64 已进行运行测试。
- macOS 尚未进行实机运行测试。Apple Silicon 的依赖二进制解析已检查，但这不等于运行验证。
- 当前依赖链中的 `cryptography 50.0.2` 缺少 Intel macOS wheel；Intel Mac 可能需要构建依赖，尚未验证，不承诺开箱即用。
- 较老的 Linux、特殊 CPU 架构、自行编译的 Python/Pillow 需要单独验证；旧版 glibc 可能没有对应依赖 wheel。

普通 MCP 无法保证像 Codex 内置工具那样实时显示生成过程。最终返回包含原图绝对路径和受大小限制的预览；完整文件以原图路径为准。
