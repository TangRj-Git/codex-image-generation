# 构建与开源发布

本文供维护者构建和发布版本。用户接入统一使用 [uvx 配置](INSTALL.md)，不需要下载源码或手动安装 wheel。GitHub Release 与 PyPI 发布由各自的手动工作流完成。

## 版本与检查

发布新版本时，同步更新 `pyproject.toml` 的 `project.version`、`src/codex_image_generation/__init__.py` 的 `__version__` 和 `CHANGELOG.md`。下文校验命令中的版本应替换为待发布版本；面向用户的 `@latest` 配置无需修改。然后在项目根目录执行：

```sh
uv lock
uv sync --locked
uv run --frozen python -B -m unittest discover -s tests -v
git diff --check
```

测试使用假凭据、临时图片和模拟上游，不产生实际图片生成费用。单元测试通过不等于真实中转站支持所有能力。

## GitHub 与 PyPI 的说明文件

- `README.md`：GitHub 项目首页，包含使用说明、效果展示和开发入口。
- `README_PYPI.md`：PyPI 详细描述，只介绍用途、配置和使用，不展示图片。
- `pyproject.toml` 的 `description`：PyPI 页面顶部的一句话简介。

`pyproject.toml` 中的 `readme` 指向 `README_PYPI.md`，构建时将它的内容写入 wheel 和源码包元数据。两份说明可以分别维护，公共的配置片段仍需保持一致。源码包保留两份文件，确保从源码重新构建时继续使用 PyPI 专用描述。

## 生成发布文件

```sh
uv run --frozen python scripts/build_release.py
```

脚本调用 `uv build`，在临时目录构建并检查 wheel、源码包内容，随后写入 `dist/`。只更新当前版本的同名产物，不清空整个目录。

0.3.0 的主要产物：

- `codex_image_generation-0.3.0-py3-none-any.whl`：供 PyPI 分发的安装包。
- `codex_image_generation-0.3.0.tar.gz`：源码发行包。
- `codex-image-generation-0.3.0.zip`：GitHub Release 归档，含 wheel、源码包、安装说明、MIT 许可证、MCP 配置示例和内部校验和。
- `SHA256SUMS-0.3.0.txt`：外部 SHA-256 校验文件，校验 wheel、源码包和 ZIP。

`py3-none-any` 表示本项目 wheel 没有平台专用二进制扩展，不表示全部依赖支持任意操作系统。uv 会为使用者的平台安装相应依赖。

包内容应只包含项目代码、必要元数据、分发文档和明确列入的展示图片。GitHub README 使用的 `sunny-anime-puppy.png` 随源码包保留，wheel 不包含展示图；PyPI 长描述仅使用不含图片的 `README_PYPI.md`。构建验证会拒绝不在预期范围内的文件。不要将 `.venv`、`.git`、`.env`、真实 Codex 配置、密钥、其他本地生成图片或作者电脑的启动配置加入发布包。对生成包做检查，不要仅检查 Git 的跟踪文件。

根目录 `LICENSE` 会进入源码包、wheel 的 `.dist-info/licenses/` 和 ZIP。构建会验证包元数据声明 `MIT`，并逐字节核对许可证与源码一致。更新许可文件或分发文档时，需要同时维护构建脚本的文件白名单。

## 安装后验证

```sh
uv run --frozen python scripts/smoke_wheel.py --wheel dist/codex_image_generation-0.3.0-py3-none-any.whl
```

该脚本在临时隔离的 uv 工具目录安装 wheel，使用假 Codex 配置，从与源码无关的工作目录验证：

- 已安装命令和版本；
- `--check` 与 `--print-config`；
- 含空格、中文和 emoji 的配置目录；
- 生成的 TOML 及独立安装环境中的启动路径；
- MCP stdio 工具发现和无需请求上游的参数错误返回。

此安装检查可能下载依赖，但不调用真实图像 API，也不替换用户的全局工具安装。它补充源码测试，防止“只在项目目录能运行”的问题。

发布前应在每个宣称支持的平台运行安装验证，并记录操作系统、CPU、Python 版本和结果。跨平台依赖解析或 wheel 下载检查不能代替 macOS/Linux/Windows 的实际运行测试。

仓库 CI 执行测试、构建、隔离安装验证和 Git 历史凭据扫描；各平台通过后保留版本化构建产物 30 天。将工作流加入仓库不表示对应平台的远端 CI 已运行通过；以实际运行记录为准。

凭据扫描使用 Gitleaks 默认规则，输出中的候选密钥会脱敏。`.gitleaks.toml` 只放行脱敏回归测试中的一个已确认伪凭据，要求文件路径和完整值同时匹配；不要扩大为整个测试目录或任意前缀。扫描是辅助检查，不是不存在泄漏的保证。

## 发布前确认

提交改动并推送到 [GitHub 仓库](https://github.com/TangRj-Git/codex-image-generation)，检查 **Test and package** 的实际运行结果。确认准备公开的文件、Git 历史及构建产物不包含真实凭据、私人中转配置或未经授权的素材。

`dist/` 保持 Git 忽略，分发包通过 Release 附件和 PyPI 提供。`README_PYPI.md` 中的文档链接使用绝对 URL，避免 PyPI 将它们解析到错误的位置；新增包元数据链接时也应使用真实仓库地址。

## 创建版本 Release 草稿

确认版本文件、文档和待发布提交一致后，创建并推送对应版本标签。下面以 `v0.3.0` 展示命令格式；这个版本已经发布，实际操作请使用新的版本号：

```sh
git tag -a v0.3.0 -m "Release v0.3.0"
git push origin v0.3.0
```

标签须指向已经准备好的提交；已有标签不要强制覆盖。然后在 GitHub Actions 中选择 **Prepare draft release**，从仓库默认分支手动运行，输入已存在的 `v0.3.0` 标签。

工作流会核对标签、`project.version` 和 `__version__`，解析为固定提交，对该提交运行全部平台测试及扫描。全部通过后，它会使用已测试的 Linux/Python 3.11 产物，复核校验和与远端标签，再创建附带 ZIP、wheel、源码包和校验文件的 Release 草稿。已有同名 Release 时会停止，不覆盖附件或自动重新发布。

最后检查草稿的版本说明与附件，确认后在 GitHub 上公开。该工作流不会自动发布公开 Release，也不会上传 PyPI；PyPI 采用下文独立的手动流程。远端 Actions 尚未运行前，不得把本地语法检查写成远端 CI 通过。

## 发布到 PyPI

用户配置见 `examples/codex-uvx.toml`，启动命令为 `uvx codex-image-generation@latest`，不固定包版本和 Python 版本。ZIP 保留作发行归档，其安装说明和配置示例也采用这一入口。

PyPI 发布使用 `.github/workflows/publish-pypi.yml`（GitHub Actions 中显示为 **Publish to PyPI**），只在默认分支手动运行。它下载已经公开的 GitHub Release 原始 wheel 和源码包，核对 GitHub 提供的 SHA-256 与 Release 校验清单、包名、版本和许可证，确认标签提交有成功 CI，并验证 `uvx` 的 CLI 与 MCP stdio 后上传。ZIP 和校验清单保留在 GitHub Release，不上传到 PyPI。

首次发布时，登录 [PyPI Trusted Publisher Management](https://pypi.org/manage/account/publishing/)，添加 pending publisher：

| 字段 | 值 |
| --- | --- |
| PyPI Project Name | `codex-image-generation` |
| Owner | `TangRj-Git` |
| Repository name | `codex-image-generation` |
| Workflow name | `publish-pypi.yml` |
| Environment name | `pypi` |

该授权仅允许指定仓库、工作流和环境发布这个包，无需在仓库保存 PyPI API Token。第一次成功上传会创建项目，pending publisher 随后成为该项目的 publisher。配置 pending publisher 不会提前保留包名，最终以 PyPI 接受上传为准。

准备好后，在 GitHub Actions 运行 **Publish to PyPI**，输入本次已公开的 Release 标签。工作流把验证与带有 `id-token: write` 权限的上传分为不同 job；上传 job 不运行项目代码。`pypi` 环境名称须与 PyPI 配置一致，可在 GitHub 仓库 Settings → Environments 中增加维护者审核规则。

本流程复用原始 Release 附件，不重新打包或替换已经公开的同版本文件。PyPI 不允许用新文件覆盖已上传的同名版本文件；后续代码或包元数据修改应发布新版本。推送 `README.md` 会更新 GitHub 首页；修改 `README_PYPI.md` 则需要构建并发布新版本，才会更新 PyPI 的描述。已发布版本的说明不会自动变化。

上传成功后，先检查 `@latest` 的实际版本，再对本次发布的精确版本运行安装验证。以下 `0.3.0` 仅为校验命令示例，发布其他版本时应相应替换：

```sh
uvx codex-image-generation@latest --version
uv run --locked python -B scripts/smoke_uvx.py --package codex-image-generation==0.3.0 --expected-version 0.3.0
```

第二条命令使用假凭据和临时配置，验证指定版本的 MCP 工具发现和本地参数校验，不调用真实图像 API。发布测试使用精确版本，以确保验证的是待发布产物；这不改变用户配置中的 `@latest`。

`uv.lock` 用于源码开发和项目测试；使用者直接安装 wheel 时，安装器按 wheel 中的依赖范围解析适合本机的版本。当前 ZIP 没有提供离线依赖集合，不能承诺在无网络的新机器上完成首次安装。
