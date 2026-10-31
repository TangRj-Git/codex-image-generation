"""One image tool using the current task context and existing relay settings."""

from __future__ import annotations

import asyncio
import io
from typing import Annotated, Any, Literal, Self

from fastmcp import Context, FastMCP
from fastmcp.tools import ToolResult
from fastmcp.utilities.types import Image
from PIL import Image as PILImage
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import __version__
from .config import resolve_config
from .errors import ImageToolError
from .generator import generate

INSTRUCTIONS = """Generate or edit raster images when the user requests them.
Reuse the existing Codex relay configuration; never ask for another URL, API key or image model.
Supply cwd as the absolute working directory of the CURRENT task, not the MCP installation.
Without a requested destination, omit output_dir and filename to save directly under cwd.
Split a requested file path into output_dir and filename.
Pass current_model only when its exact API ID is supplied by reliable session/runtime context.
Never guess your model identity. Otherwise omit it: the server may use the configured model and
reports model_source=config. This does not prove that it matches a temporary UI selection.
Keep the selected provider/profile context consistent with the session.
Use filename/output_format for PNG, JPEG or WebP. Transparency requires PNG/WebP.
Forward image options requested by the user; upstream decides model-specific capabilities.
Use input_images for local paths, URLs/data URLs or provider file IDs, and mask for editing.
previous_response_id and previous_image_ids must belong to the same provider that created them.
Return saved original paths, previews, warnings and response/image IDs needed for later edits.
Do not automatically repeat a failed or timed-out generation: it may already have incurred charges.
Previews are not originals. partial_images enables upstream streaming and progress, but real-time
image display depends on the host. Intermediate previews are drafts, not saved final files.
"""

mcp = FastMCP(
    "codex-image-generation", version=__version__, instructions=INSTRUCTIONS,
    mask_error_details=True, strict_input_validation=True,
)


class ImageSource(BaseModel):
    """Exactly one local or upstream image source."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    path: str | None = Field(default=None, min_length=1, description="Local path, absolute or relative to cwd.")
    image_url: str | None = Field(default=None, min_length=1, description="HTTP(S) or data URL; masks should use data URLs or file IDs.")
    file_id: str | None = Field(default=None, min_length=1, description="File ID from the configured provider.")

    @model_validator(mode="after")
    def exactly_one_source(self) -> Self:
        if sum(value is not None for value in (self.path, self.image_url, self.file_id)) != 1:
            raise ValueError("Provide exactly one of path, image_url or file_id.")
        return self


class ImageInput(ImageSource):
    detail: Literal["auto", "low", "high", "original"] | None = None


def configuration_status() -> dict[str, Any]:
    """CLI-only inspection; it cannot discover the active session model."""
    try:
        settings = resolve_config(require_model=False)
    except ImageToolError as exc:
        return {"configured": False, "connection_verified": False, "error": str(exc)}
    return {
        **settings.public_status(), "configured": True,
        "model_available_in_config": settings.model is not None,
        "connection_verified": False, "server_version": __version__,
        "default_output_directory": "<cwd>", "formats": ["png", "jpeg", "webp"],
        "session_model_auto_detection": False,
    }


def _preview(data: bytes) -> tuple[bytes, str]:
    """Bound inline previews without changing saved originals."""
    with PILImage.open(io.BytesIO(data)) as source:
        alpha = "A" in source.getbands() or "transparency" in source.info
        preview = source.convert("RGBA" if alpha else "RGB")
    preview.thumbnail((1024, 1024), PILImage.Resampling.LANCZOS)
    image_format = "png" if alpha else "jpeg"
    while True:
        buffer = io.BytesIO()
        options: dict[str, Any] = {"optimize": True}
        if image_format == "jpeg":
            options["quality"] = 85
        preview.save(buffer, format=image_format.upper(), **options)
        encoded = buffer.getvalue()
        if len(encoded) <= 1024 * 1024 or max(preview.size) <= 128:
            return encoded, image_format
        preview.thumbnail(
            (max(1, preview.width // 2), max(1, preview.height // 2)),
            PILImage.Resampling.LANCZOS,
        )


def _error_result(code: str, message: str) -> ToolResult:
    return ToolResult(
        content=[message], is_error=True,
        structured_content={
            "status": "error", "images": [], "response_id": None,
            "warnings": [], "errors": [{"code": code, "message": message}],
        },
    )


@mcp.tool(annotations={
    "readOnlyHint": False, "destructiveHint": False,
    "idempotentHint": False, "openWorldHint": True,
})
async def generate_image(
    prompt: Annotated[str, Field(min_length=1, description="Describe what to draw or edit and important visual requirements.")],
    cwd: Annotated[str, Field(min_length=1, description="Absolute CURRENT task working directory, supplied by Codex from context.")],
    ctx: Context,
    current_model: Annotated[str | None, Field(description="Exact dialogue-model API ID only when reliably known from the session. Otherwise omit; config fallback is explicit.")] = None,
    output_dir: Annotated[str | None, Field(description="Directory relative to cwd or an explicitly requested absolute directory. Default: cwd itself.")] = None,
    filename: Annotated[str | None, Field(description="Filename only, e.g. cover.webp. Infer format from extension; default is a unique name. Never overwrite existing files.")] = None,
    output_format: Annotated[Literal["png", "jpeg", "jpg", "webp"] | None, Field(description="Infer from filename if omitted, otherwise default PNG. No SVG or animated output.")] = None,
    size: Annotated[str | None, Field(description="auto or WIDTHxHEIGHT. Model-specific limits are decided by upstream.")] = None,
    quality: Annotated[Literal["auto", "low", "medium", "high", "xhigh", "max"] | None, Field(description="Optional quality; available levels depend on the upstream image model.")] = None,
    background: Annotated[Literal["auto", "opaque", "transparent"] | None, Field(description="Transparent backgrounds require PNG or WebP.")] = None,
    output_compression: Annotated[int | None, Field(strict=True, ge=0, le=100, description="JPEG/WebP only: strictly integer 0–100.")] = None,
    action: Annotated[Literal["auto", "generate", "edit"] | None, Field(description="Choose automatically, generate, or edit supplied/previous images.")] = None,
    input_images: Annotated[list[ImageInput] | None, Field(description="Reference images, each with exactly one path, image_url or file_id.")] = None,
    mask: Annotated[ImageSource | None, Field(description="Mask for the first image; local/data masks need alpha and must match comparable local input dimensions/format.")] = None,
    input_fidelity: Annotated[Literal["low", "high"] | None, Field(description="Optional fidelity only if supported upstream; omit by default.")] = None,
    previous_response_id: Annotated[str | None, Field(description="Prior response ID from the same provider; requires retained upstream state.")] = None,
    previous_image_ids: Annotated[list[str] | None, Field(description="Prior image_generation_call IDs from the same provider.")] = None,
    moderation: Annotated[Literal["auto", "low"] | None, Field(description="Optional official moderation setting; upstream policy still applies.")] = None,
    partial_images: Annotated[int, Field(strict=True, ge=0, le=3, description="0: final-only default. 1–3: streaming drafts and progress; host live display varies.")] = 0,
    safety_identifier: Annotated[str | None, Field(max_length=128, description="Optional privacy-preserving end-user ID for multi-user deployments.")] = None,
    include_preview: Annotated[bool, Field(strict=True, description="Return bounded inline previews and original file paths.")] = True,
) -> ToolResult:
    """Generate/edit images and save originals in the current directory or requested folder.

    Reuse Codex relay URL and credentials, without a separate image-model setting.
    Supports PNG/JPEG/WebP, visual options, reference images and masks. Returns previews,
    absolute paths and edit IDs; successful images survive partial failures.
    """
    try:
        settings = resolve_config(current_model=current_model)
    except ImageToolError as exc:
        return _error_result("configuration_error", str(exc))
    progress_count = 0

    async def progress(event: dict[str, Any]) -> None:
        nonlocal progress_count
        progress_count += 1
        messages = {
            "partial_image": "收到中间预览，正在继续生成最终图片",
            "completed": "上游生成完成，正在保存图片",
            "created": "图像请求已接收",
        }
        try:
            await ctx.report_progress(
                progress_count, message=messages.get(event.get("stage"), "正在生成图片")
            )
        except Exception:
            pass  # Unsupported host progress must not fail a paid request.

    try:
        result = await generate(
            settings, prompt=prompt, cwd=cwd, output_dir=output_dir,
            filename=filename, output_format=output_format, size=size, quality=quality,
            background=background, output_compression=output_compression, action=action,
            input_images=[item.model_dump(exclude_none=True) for item in input_images] if input_images is not None else None,
            mask=mask.model_dump(exclude_none=True) if mask is not None else None,
            input_fidelity=input_fidelity, previous_response_id=previous_response_id,
            previous_image_ids=previous_image_ids, moderation=moderation,
            partial_images=partial_images, safety_identifier=safety_identifier,
            on_progress=progress,
        )
    except ImageToolError as exc:
        return _error_result("invalid_request", str(exc))
    except Exception:
        return _error_result("internal_error", "生成请求处理失败。请检查本地配置与输出目录，不要自动重复付费生成。")

    metadata: dict[str, Any] = {
        "status": result.status, "images": [], "partials": [], "text": result.text,
        "response_id": result.response_id, "request_id": result.request_id,
        "model": settings.model, "model_source": settings.model_source,
        "reported_model": getattr(result, "model", None), "usage": result.usage,
        "warnings": list(result.warnings), "errors": list(result.errors),
        "session_model_auto_detection": False,
    }
    content: list[Any] = []
    preview_count = 0

    async def add_preview(data: bytes) -> bool:
        nonlocal preview_count
        if not include_preview or preview_count >= 4:
            return False
        try:
            preview_data, preview_format = await asyncio.to_thread(_preview, data)
            content.append(Image(data=preview_data, format=preview_format))
            preview_count += 1
            return True
        except Exception:
            metadata["warnings"].append("有一张预览无法显示；已保存的原图不受影响。")
            return False

    for image in result.images:
        path = str(image.path)
        content.append(f"图片已保存：{path} ({image.width}x{image.height}, {image.mime_type})")
        if image.call_id:
            content.append(f"Image call ID: {image.call_id}")
        metadata["images"].append({
            "path": path, "mime_type": image.mime_type, "width": image.width, "height": image.height,
            "bytes": len(image.data), "call_id": image.call_id, "revised_prompt": image.revised_prompt,
            "actual": image.metadata, "preview_included": await add_preview(image.data),
            "preview_is_original": False,
        })
    for draft in result.partials:
        entry = {
            "call_id": draft.call_id, "width": draft.info.width, "height": draft.info.height,
            "mime_type": draft.info.mime_type, "metadata": draft.metadata,
            "kind": "intermediate_preview", "preview_included": False,
        }
        if include_preview and preview_count < 4:
            content.append("以下是生成过程中的中间预览，不是最终保存的原图。")
            entry["preview_included"] = await add_preview(draft.data)
        metadata["partials"].append(entry)
    if result.response_id:
        content.append(f"Response ID: {result.response_id}")
    if result.request_id:
        content.append(f"Request ID: {result.request_id}")
    content.append(f"Model: {settings.model} (source: {settings.model_source})")
    if metadata["reported_model"] and metadata["reported_model"] != settings.model:
        content.append(f"Upstream reported model: {metadata['reported_model']}")
    if settings.model_source == "config":
        metadata["warnings"].append("模型来自 Codex 配置文件，未验证其是否等于界面临时选择的当前会话模型。")
    if result.text:
        content.append(result.text)
    content.extend(f"提示：{warning}" for warning in metadata["warnings"])
    content.extend(f"错误 [{error.get('code', 'unknown')}]：{error.get('message', '生成失败')}" for error in result.errors)
    return ToolResult(content=content, structured_content=metadata, is_error=result.status == "error")
