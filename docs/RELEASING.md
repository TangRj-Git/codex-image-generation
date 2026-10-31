# 本地构建与分发

本流程生成可下载的 Python 包和 ZIP，不会上传 PyPI/GitHub，也不会修改使用者的 Codex 配置。

## 版本与检查

同步更新 `pyproject.toml` 的 `project.version`、`src/gpt_image_generation/__init__.py` 的 `__version__`、`CHANGELOG.md` 及安装示例中的文件名。然后在项目根目录执行：

```sh
uv lock
uv sync --locked
uv run --frozen python -B -m unittest discover -s tests -v
git diff --check
```

测试使用假凭据、临时图片和模拟上游，不产生实际图片生成费用。单元测试通过不等于真实中转站支持所有能力。

## 生成发布文件

```sh
uv run --frozen python scripts/build_release.py
```

脚本调用 `uv build`，在临时目录构建并检查 wheel、源码包内容，随后写入 `dist/`。只更新当前版本的同名产物，不清空整个目录。

0.3.0 的主要产物：

- `gpt_image_generation-0.3.0-py3-none-any.whl`：供使用者安装。
- `gpt_image_generation-0.3.0.tar.gz`：源码发行包。
- `gpt-image-generation-0.3.0.zip`：便于分享的下载包，含 wheel、源码包、安装说明、MCP 配置示例和内部校验和。
- `SHA256SUMS-0.3.0.txt`：外部 SHA-256 校验文件，校验 wheel、源码包和 ZIP。

`py3-none-any` 表示本项目 wheel 没有平台专用二进制扩展，不表示全部依赖支持任意操作系统。uv 会为使用者的平台安装相应依赖。

包内容应只包含项目代码、必要元数据、分发文档和明确列入的展示图片。README 使用的 `sunny-anime-puppy.png` 随源码包分发，wheel 不包含展示图。构建验证会拒绝不在预期范围内的文件。不要将 `.venv`、`.git`、`.env`、真实 Codex 配置、密钥、其他本地生成图片或作者电脑的启动配置加入发布包。对生成包做检查，不要仅检查 Git 的跟踪文件。

## 安装后验证

```sh
uv run --frozen python scripts/smoke_wheel.py --wheel dist/gpt_image_generation-0.3.0-py3-none-any.whl
```

该脚本在临时隔离的 uv 工具目录安装 wheel，使用假 Codex 配置，从与源码无关的工作目录验证：

- 已安装命令和版本；
- `--check` 与 `--print-config`；
- 含空格、中文和 emoji 的配置目录；
- 生成的 TOML 及独立安装环境中的启动路径；
- MCP stdio 工具发现和无需请求上游的参数错误返回。

此安装检查可能下载依赖，但不调用真实图像 API，也不替换用户的全局工具安装。它补充源码测试，防止“只在项目目录能运行”的问题。

发布前应在每个宣称支持的平台运行安装验证，并记录操作系统、CPU、Python 版本和结果。跨平台依赖解析或 wheel 下载检查不能代替 macOS/Linux/Windows 的实际运行测试。

仓库 CI 用于测试、构建和隔离安装验证，不自动发布包。将工作流加入仓库不表示对应平台的远端 CI 已运行通过；以实际运行记录为准。

## 分享与后续公开发布

本地分发时，分享当前版本 ZIP 和对应外部校验和即可。收件人按照 ZIP 内 `INSTALL.md` 安装，再在自己的电脑上生成 Codex MCP 配置；无需取得作者的源码目录、密钥或 Codex 配置。

项目目前没有选定许可证。公开发布前应由所有者决定使用许可并补齐相应文件和包元数据；不要擅自填入 MIT 等许可证名称。若以后发布到公共包索引，还需确认包名归属、仓库地址和发布凭据，再增加按包名安装的说明。

`uv.lock` 用于源码开发和项目测试；使用者直接安装 wheel 时，安装器按 wheel 中的依赖范围解析适合本机的版本。当前 ZIP 没有提供离线依赖集合，不能承诺在无网络的新机器上完成首次安装。
