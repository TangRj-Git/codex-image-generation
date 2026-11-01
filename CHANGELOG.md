# 更新记录

## 未发布

- 用户接入配置统一为 `uvx codex-image-generation@latest`，不固定包版本或 Python 版本。
- 精简 README 和安装说明，移除重复的 wheel 下载、持久安装及旧路径配置步骤。
- 同步更新配置示例、问题反馈和发布说明；README 使用可供 PyPI 展示的绝对图片与文档链接。

## 0.3.0 — 2026-11-01

- 项目、发行包和启动命令统一命名为 `codex-image-generation`，Python 模块使用 `codex_image_generation`。
- 作者署名更新为 TangRuoJie，采用 MIT 许可证，并在分发包中包含许可证。
- 增加贡献指南、安全报告渠道、问题/PR 模板，以及通过检查后创建 Release 草稿的手动工作流。
- 提供 wheel、源码包及带安装说明和校验和的 ZIP 本地发布流程。
- 推荐用 `uv tool install` 安装到独立环境，使用者无需保留项目源码目录。
- 增加安装后从其他目录启动的验证，覆盖 CLI、配置输出和 MCP stdio。
- CLI 配置输出使用 UTF-8，兼容非 UTF-8 环境中的中文和 emoji 路径。
- 统一文件名锁的大小写和 Unicode 归一化，减少不同文件系统上的同名并发冲突。
- 重写安装与接入说明，拆分工具参考、发布流程及通用本地 wheel 启动示例。
- 明确模型上下文、图片格式、预览、跨平台依赖和首次联网安装的边界。

## 0.2.0

- 公开工具收敛为 `generate_image`，本地配置诊断使用 `--check`。
- `project_dir` 改为 `cwd`，默认直接保存到当前任务目录。
- 复用已有 Codex provider 的 URL 与凭据，不再使用独立 URL/Key 覆盖设置。
- 对话模型来自可靠调用上下文或已有 Codex 配置；移除独立图像模型配置和硬编码兜底。
- 支持 PNG/JPEG/WebP、参考图、蒙版、历史图像引用和可选流式中间预览。
- 保留部分失败中的成功原图，输出路径、模型来源、ID、用量及警告。

从旧版升级时，按 [安装说明](docs/INSTALL.md#自动更新与旧配置迁移) 替换 MCP 配置。旧的 `--responses-model`、`--image-model` 等启动设置不再适用。
