# 构建与开源发布

本地构建生成可下载的 Python 包和 ZIP，不会上传或修改使用者的 Codex 配置。GitHub 发布工作流需要维护者手动触发，通过检查后创建 Release 草稿，由维护者检查后公开。

## 版本与检查

同步更新 `pyproject.toml` 的 `project.version`、`src/codex_image_generation/__init__.py` 的 `__version__`、`CHANGELOG.md` 及安装示例中的文件名。然后在项目根目录执行：

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

- `codex_image_generation-0.3.0-py3-none-any.whl`：供使用者安装。
- `codex_image_generation-0.3.0.tar.gz`：源码发行包。
- `codex-image-generation-0.3.0.zip`：便于分享的下载包，含 wheel、源码包、安装说明、MIT 许可证、MCP 配置示例和内部校验和。
- `SHA256SUMS-0.3.0.txt`：外部 SHA-256 校验文件，校验 wheel、源码包和 ZIP。

`py3-none-any` 表示本项目 wheel 没有平台专用二进制扩展，不表示全部依赖支持任意操作系统。uv 会为使用者的平台安装相应依赖。

包内容应只包含项目代码、必要元数据、分发文档和明确列入的展示图片。README 使用的 `sunny-anime-puppy.png` 随源码包分发，wheel 不包含展示图。构建验证会拒绝不在预期范围内的文件。不要将 `.venv`、`.git`、`.env`、真实 Codex 配置、密钥、其他本地生成图片或作者电脑的启动配置加入发布包。对生成包做检查，不要仅检查 Git 的跟踪文件。

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

## 首次关联公开仓库

项目维护者是 TangRuoJie，公开联系邮箱为 `3177065378@qq.com`，采用 [MIT 许可证](../LICENSE)。GitHub 账号与显示署名不一定相同，不应据此猜测仓库地址。

1. 在自己的托管账号下创建仓库，确认实际仓库地址，再配置 Git 的 `origin`。
2. 在 `pyproject.toml` 增加真实的 `[project.urls]`（Repository、Issues、Documentation），并更新 README 的 Release 下载入口。尚未确定地址时不填写虚构链接。
3. 检查准备公开的文件及 Git 历史，确认无真实凭据、私人中转配置或未经授权的素材。提交完整改动后推送到仓库默认分支。
4. 在 GitHub Actions 中检查 `Test and package` 的实际运行结果，确认支持的平台与文档一致。

`dist/` 保持 Git 忽略，二进制分发包通过 Release 附件提供。项目使用 `.mailmap` 将此前 `Administrator` 与同一邮箱映射为 TangRuoJie，不需要重写历史提交。

## 创建版本 Release 草稿

确认版本文件、文档和待发布提交一致后，创建并推送版本标签。例如首次发布：

```sh
git tag -a v0.3.0 -m "Release v0.3.0"
git push origin v0.3.0
```

标签须指向已经准备好的提交；已有标签不要强制覆盖。然后在 GitHub Actions 中选择 **Prepare draft release**，从仓库默认分支手动运行，输入已存在的 `v0.3.0` 标签。

工作流会核对标签、`project.version` 和 `__version__`，解析为固定提交，对该提交运行全部平台测试及扫描。全部通过后，它会使用已测试的 Linux/Python 3.11 产物，复核校验和与远端标签，再创建附带 ZIP、wheel、源码包和校验文件的 Release 草稿。已有同名 Release 时会停止，不覆盖附件或自动重新发布。

最后检查草稿的版本说明与附件，确认后在 GitHub 上公开。工作流不会自动发布公开 Release，也不会上传 PyPI。远端 Actions 尚未运行前，不得把本地语法检查写成远端 CI 通过。

## 分享与可选 PyPI 发布

本地分发时，分享当前版本 ZIP 和对应外部校验和即可。收件人按照 ZIP 内 `INSTALL.md` 安装，再在自己的电脑上生成 Codex MCP 配置；无需取得作者的源码目录、密钥或 Codex 配置。

GitHub 开源不依赖 PyPI。若希望用户按包名安装，先确认 PyPI 账号和包名归属，再为已验证的发布工作流配置 Trusted Publishing；不要把长期上传令牌写入仓库。发布前检查长描述中的图片、文档链接，使用实际仓库的绝对 URL 或专用发布描述，避免 PyPI 无法解析相对链接。正式发布并验证安装成功后，再添加不带本地 wheel 的安装命令。

`uv.lock` 用于源码开发和项目测试；使用者直接安装 wheel 时，安装器按 wheel 中的依赖范围解析适合本机的版本。当前 ZIP 没有提供离线依赖集合，不能承诺在无网络的新机器上完成首次安装。
