# 参与贡献

欢迎修复问题、改善文档和补充跨平台验证。较大的功能变更建议先在仓库 Issues 中说明使用场景与预期行为；每个 PR 尽量只解决一个问题。

## 准备开发环境

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。在源码根目录执行：

```sh
uv sync --locked
uv run --locked python -B -m unittest discover -s tests -v
```

依赖安装可能联网；现有测试使用假凭据、临时文件和模拟上游，不调用真实图像 API。修改依赖时更新 `pyproject.toml`，执行 `uv lock`，一并提交 `uv.lock`。

## 修改原则

- 保持唯一公开工具 `generate_image` 的使用流程：描述图片，由调用方提供当前任务的绝对 `cwd`，默认保存到该目录。
- URL 和凭据复用所选 Codex provider。不要另建 `api_key`、`base_url` 或 `image_model` 工具参数；请求不发送 `tools[].model`。
- `current_model` 仅来自可靠的调用上下文，否则使用已有配置中的模型；不要猜测会话型号或增加硬编码兜底。
- 已有文件不覆盖，部分失败保留成功原图。超时或断流后不要自动重复付费生成。
- 行为修复应有能复现问题的测试。文档、排版等改动无需为了覆盖行数添加测试。
- 涉及路径、文件保存或子进程时，考虑 Windows、macOS 和 Linux，并明确实际验证过的平台。

工具接口和限制见 [工具参考](docs/TOOLS.md)。安装与打包行为变更应同步更新相应文档。

## 打包相关改动

```sh
uv run --locked python scripts/build_release.py
uv run --locked python scripts/smoke_wheel.py --wheel dist/codex_image_generation-0.3.0-py3-none-any.whl
git diff --check
```

版本变化后将命令中的 wheel 文件名换成实际产物。安装验证使用临时工具环境和假 Codex 配置，不替换你的全局安装；首次运行可能下载依赖。完整流程见 [发布说明](docs/RELEASING.md)。

## 提交 PR

说明问题如何触发、修改后的行为，以及实际执行的验证。涉及界面或示例图片时，附上你有权公开的示例；跨平台改动请注明操作系统和 Python 版本。

提交前检查差异和新文件。不要提交真实 API Key、完整 Codex 配置、私人中转地址、日志中的认证头，或未经授权的用户图片。复现数据使用假凭据和可公开的样例；报告疑似凭据泄漏或漏洞请使用 [私密报告渠道](SECURITY.md)。

贡献将按本项目的 [MIT 许可证](LICENSE) 分发。请确保你有权提交相关代码、文档和素材。
