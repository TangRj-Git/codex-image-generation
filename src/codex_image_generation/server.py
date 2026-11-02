"""Model discovery and image generation using the existing Codex provider."""

from __future__ import annotations

import asyncio
import io
import json
from typing import Annotated, Any, Literal, Self

from fastmcp import Context, FastMCP
from fastmcp.tools import ToolResult
from fastmcp.utilities.types import Image
from PIL import Image as PILImage
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import __version__
from .config import get_model_preferences, model_cache_directory, resolve_config
from .errors import ImageToolError
from .generator import generate
from .model_selection import list_dialogue_models, list_official_image_models

INSTRUCTIONS = """Generate or edit raster images when the user requests them.
Reuse the existing Codex relay URL and credentials; never ask for a separate URL or API key.
When the user chooses this MCP for image work, reuse the installed system imagegen skill if
it is available in the host's skill catalog. Have the host load it from that catalog; do not
assume a filesystem location, copy the skill, or install a replacement skill.
Apply its shared prompt guidance, reference-image roles, edit invariants, and visual review.
Honor the user's chosen MCP backend: generate with this server, use this tool's parameters
and saved paths, and do not silently switch to the skill's built-in tool or fallback CLI.
Do not apply backend-specific CLI model defaults, parameter limits, or save-path rules to this MCP.
After generation, inspect saved originals with the host's image viewer when available.
Skill loading is controlled by the host. This server cannot activate or verify it; a tool call
alone is not evidence that the skill was used. If the skill is unavailable, this MCP still works.
Use list_models to inspect dialogue models exposed by the selected provider, and list_image_models
for the independent official image-model catalog. Image models need not appear in provider /models.
By default discover and select models with the quality policy. Use responses_model and image_model
for explicit choices, or model_policy=fast for speed. Never describe automatic choices as verified
provider capabilities. image_model=upstream_default explicitly omits tools[].model for compatibility.
Supply cwd as the absolute working directory of the CURRENT task, not the MCP installation.
Without a requested destination, omit output_dir and filename to save directly under cwd.
Split a requested file path into output_dir and filename.
Pass current_model only when its exact API ID is supplied by reliable session/runtime context.
Never guess your model identity. current_model and the Codex configured model are fallbacks when
automatic discovery cannot select a dialogue model; responses_model is a separate request override.
Keep the selected provider/profile context consistent with the session.
Use filename/output_format for PNG, JPEG or WebP. Transparency requires PNG/WebP.
Forward requested image options. Known official model constraints are checked before generation.
Use input_images for local paths, URLs/data URLs or provider file IDs, and mask for editing.
previous_response_id and previous_image_ids must belong to the same provider that created them.
Return selected dialogue/image models, their sources, saved originals, previews and edit IDs.
An explicitly requested image model is not proof of the provider's actual internal routing.
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


def _listing_result(data: dict[str, Any]) -> ToolResult:
    failed = bool(data.get("error")) or data.get("source") == "unavailable"
    status = "error" if failed else ("partial_success" if data.get("warnings") or data.get("stale") else "success")
    metadata = {**data, "status": status}
    return ToolResult(content=[json.dumps(metadata, ensure_ascii=False)],
                      structured_content=metadata, is_error=failed)


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def list_models(
    refresh: Annotated[bool, Field(strict=True, description="Refresh the provider list and official capability catalog instead of using fresh caches.")] = False,
    model_policy: Annotated[Literal["quality", "fast"] | None, Field(description="Rank dialogue models for capability or speed. Default: configured policy, normally quality.")] = None,
) -> ToolResult:
    """List provider dialogue models and recommend a documented image-tool caller.

    Does not generate images or verify paid tool execution. Image/audio/embedding models
    are excluded; unclassified provider aliases are returned separately.
    """
    try:
        overrides = {"model_policy": model_policy} if model_policy is not None else {}
        settings = resolve_config(require_model=False, include_image_preferences=False, **overrides)
        return _listing_result(await list_dialogue_models(settings, refresh=refresh, policy=model_policy))
    except ImageToolError as exc:
        return _listing_result({"models": [], "recommended_model": None, "source": "unavailable", "error": str(exc)})
    except Exception:
        return _listing_result({"models": [], "recommended_model": None, "source": "unavailable", "error": "模型列表读取失败，请检查本地配置或稍后刷新。"})


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def list_image_models(
    refresh: Annotated[bool, Field(strict=True, description="Refresh public official docs instead of using the daily cache.")] = False,
    model_policy: Annotated[Literal["quality", "fast"] | None, Field(description="Recommend for capability or speed. Default: CODEX_IMAGE_MODEL_POLICY, normally quality.")] = None,
) -> ToolResult:
    """List image models from official OpenAI docs, independently of provider /models.

    Requires no provider credentials. Results describe official support, not verified
    availability through a relay. Stale or bundled fallback catalogs are labeled.
    """
    try:
        preferences = get_model_preferences(include_responses=False, policy=model_policy)
        result = await list_official_image_models(model_cache_directory(), refresh=refresh,
                                                  policy=model_policy if model_policy is not None else preferences["model_policy"])
        return _listing_result({**result, "image_model_override": preferences["image_model"]})
    except ImageToolError as exc:
        return _listing_result({"models": [], "recommended_model": None, "source": "unavailable", "error": str(exc)})
    except Exception:
        return _listing_result({"models": [], "recommended_model": None, "source": "unavailable", "error": "官方图像目录暂时不可读取。"})


@mcp.tool(annotations={
    "readOnlyHint": False, "destructiveHint": False,
    "idempotentHint": False, "openWorldHint": True,
})
async def generate_image(
    prompt: Annotated[str, Field(min_length=1, description="Describe what to draw or edit and important visual requirements.")],
    cwd: Annotated[str, Field(min_length=1, description="Absolute CURRENT task working directory, supplied by Codex from context.")],
    ctx: Context,
    current_model: Annotated[str | None, Field(description="Exact dialogue-model API ID only when reliably known from the session. Otherwise omit; config fallback is explicit.")] = None,
    responses_model: Annotated[str | None, Field(min_length=1, max_length=200, description="Explicit top-level dialogue model, overriding automatic provider-model selection; does not change the Codex session.")] = None,
    image_model: Annotated[str | None, Field(min_length=1, max_length=200, description="Explicit tools[].model from the official image catalog. Omit for automatic selection; upstream_default omits the upstream field.")] = None,
    model_policy: Annotated[Literal["quality", "fast"] | None, Field(description="Automatic model-selection preference. Default: configured policy, normally quality. Separate from image quality.")] = None,
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

    Reuse Codex relay URL and credentials. Discover dialogue/image models independently;
    explicit responses_model/image_model values override automatic selection.
    When the user chooses this MCP, the host can reuse its installed system imagegen skill
    for prompt preparation and visual review while executing generation here. The server
    does not load skills itself; skill-specific built-in/CLI settings do not govern this tool.
    Supports PNG/JPEG/WebP, visual options, reference images and masks. Returns previews,
    absolute paths and edit IDs; successful images survive partial failures.
    """
    try:
        overrides = {name: value for name, value in (("responses_model", responses_model), ("image_model", image_model), ("model_policy", model_policy)) if value is not None}
        settings = resolve_config(current_model=current_model, require_model=False, **overrides)
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
            "discovering_models": "正在选择对话模型和图像模型",
            "models_selected": f"模型已选择：{event.get('responses_model')} / {event.get('image_model') or '上游默认'}",
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
            responses_model=responses_model, image_model=image_model, model_policy=model_policy,
            on_progress=progress,
        )
    except ImageToolError as exc:
        return _error_result("invalid_request", str(exc))
    except Exception:
        return _error_result("internal_error", "生成请求处理失败。请检查本地配置与输出目录，不要自动重复付费生成。")

    model_metadata = getattr(result, "models", {})
    requested_model = model_metadata.get("responses_model_requested", settings.model)
    model_source = model_metadata.get("responses_model_source", settings.model_source)
    metadata: dict[str, Any] = {
        "status": result.status, "images": [], "partials": [], "text": result.text,
        "response_id": result.response_id, "request_id": result.request_id,
        "model": requested_model, "model_source": model_source,
        "reported_model": getattr(result, "model", None), "usage": result.usage,
        **model_metadata, "responses_model_reported": getattr(result, "model", None),
        "tool_usage": getattr(result, "tool_usage", None),
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
    content.append(f"对话模型：{requested_model} (source: {model_source})")
    content.append(f"请求选择的图像模型：{model_metadata.get('image_model_selected') or '上游默认（型号未知）'} (source: {model_metadata.get('image_model_source', 'unknown')})")
    if metadata["reported_model"] and metadata["reported_model"] != requested_model:
        content.append(f"上游报告的对话模型：{metadata['reported_model']}")
    if model_source == "config":
        metadata["warnings"].append("模型来自 Codex 配置文件，未验证其是否等于界面临时选择的当前会话模型。")
    if result.text:
        content.append(result.text)
    content.extend(f"提示：{warning}" for warning in metadata["warnings"])
    content.extend(f"错误 [{error.get('code', 'unknown')}]：{error.get('message', '生成失败')}" for error in result.errors)
    return ToolResult(content=content, structured_content=metadata, is_error=result.status == "error")
