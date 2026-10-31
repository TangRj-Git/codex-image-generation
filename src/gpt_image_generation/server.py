"""MCP tools: generate an image, or inspect the local configuration."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from fastmcp.utilities.types import Image
from PIL import Image as PILImage
from pydantic import Field

from . import __version__
from .config import resolve_config
from .errors import ImageToolError
from .generator import generate

INSTRUCTIONS = """Generate or edit raster images using the user's configured Responses provider.
When a user asks for an image, call generate_image. Supply project_dir as the absolute
path of the user's CURRENT task project, not the MCP server's installation directory.
Do not guess a project directory if the task does not identify one. With no user-specified
destination, omit output_dir and filename to save under <project_dir>/outputs/images.
Split a requested file path into output_dir and filename. Relative paths are relative
to project_dir; an explicitly requested absolute output_dir is supported.
Infer PNG/JPEG/WebP from the requested filename or format; use PNG/WebP for transparency.
Preserve the user's requested format, dimensions, quality and edit instructions.
Pass local reference_images for edits, and mask_image only when the user supplies a mask.
The provider URL and credentials are resolved internally; never request them as tool arguments.
Only pass responses_model when the user or reliable session context specifies it; do not guess
the current model's name. The server otherwise follows its configured model priority.
Return the generated image and its absolute saved path to the user. Previews can be downscaled;
saved files are the full original images. Generation may incur provider charges. After a timeout,
do not automatically repeat the request: the provider may already have generated the image.
get_image_generation_status checks local configuration only, without contacting the provider.
"""

mcp = FastMCP(
    "codex-image-generation",
    version=__version__,
    instructions=INSTRUCTIONS,
    mask_error_details=True,
)


def configuration_status(
    responses_model: str | None = None, image_model: str | None = None
) -> dict[str, Any]:
    """Inspect local settings without a model request or secret disclosure."""
    try:
        settings = resolve_config(responses_model, image_model)
    except ImageToolError as exc:
        return {"configured": False, "connection_verified": False, "error": str(exc)}
    return {
        **settings.public_status(),
        "configured": True,
        "connection_verified": False,
        "server_version": __version__,
        "default_output_directory": "<project_dir>/outputs/images",
        "formats": ["png", "jpeg", "webp"],
        "session_model_auto_detection": False,
    }


def _preview(data: bytes) -> tuple[bytes, str]:
    """Bound inline image payloads without changing the saved original."""
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


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def get_image_generation_status() -> dict[str, Any]:
    """Check the configured provider and models locally. No API call or image charge.

    Returns credential source names, never credential values. A configured provider
    has not necessarily been tested for image generation. Temporary model switches
    in the Codex UI are not automatically visible to this server.
    """
    return configuration_status()


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def generate_image(
    prompt: Annotated[str, Field(description="What to draw or edit; include subject, style and composition.", min_length=1)],
    project_dir: Annotated[str, Field(description="Absolute path of the user's current project, supplied by Codex from task context.")],
    output_dir: Annotated[str | None, Field(description="Destination directory; relative to project_dir, or an explicitly requested absolute path. Default: outputs/images.")] = None,
    filename: Annotated[str | None, Field(description="Optional filename only, such as hero.webp. No directories. Existing files are never overwritten.")] = None,
    output_format: Annotated[Literal["png", "jpeg", "jpg", "webp"] | None, Field(description="Infer from filename when omitted; otherwise PNG by default. SVG/GIF output is not supported.")] = None,
    size: Annotated[str, Field(description="auto or WIDTHxHEIGHT, e.g. 1536x1024. GPT Image 2/2.5: multiples of 16, edges <=3840, ratio <=3:1, 655360–8294400 pixels.")] = "auto",
    quality: Annotated[Literal["auto", "low", "medium", "high", "xhigh", "max"], Field(description="Rendering quality. xhigh/max require GPT Image 2.5 flare or sunburst.")] = "auto",
    background: Annotated[Literal["auto", "opaque", "transparent"], Field(description="Transparent backgrounds require PNG or WebP.")] = "auto",
    output_compression: Annotated[int | None, Field(description="JPEG/WebP compression parameter, 0–100. Omit for PNG.", ge=0, le=100)] = None,
    action: Annotated[Literal["auto", "generate", "edit"], Field(description="Let the model choose, force a new image, or edit a reference/previous response.")] = "auto",
    reference_images: Annotated[list[str] | None, Field(description="Local PNG/JPEG/WebP reference paths, absolute or relative to project_dir. Uploaded only for this request.")] = None,
    mask_image: Annotated[str | None, Field(description="Local mask for the first reference image. Must match its format and dimensions and contain an alpha channel.")] = None,
    input_fidelity: Annotated[Literal["low", "high"] | None, Field(description="Optional preservation fidelity for supported models. Omit for gpt-image-2; gpt-image-1-mini supports low only.")] = None,
    previous_response_id: Annotated[str | None, Field(description="Optional previous response ID for multi-turn editing when the provider retains Responses state.")] = None,
    responses_model: Annotated[str | None, Field(description="Optional known dialogue model override, for example gpt-6-astra. Omit to follow configuration; do not guess the session model.")] = None,
    image_model: Annotated[str | None, Field(description="Optional image model override. Default: gpt-image-2.5-flare.")] = None,
    include_preview: Annotated[bool, Field(description="Return bounded inline previews as well as original saved paths.")] = True,
) -> ToolResult:
    """Generate or edit images via the configured provider's Responses image_generation tool.

    Saves full images in the current project (outputs/images by default) or the user's
    requested directory, and returns image previews plus absolute paths and metadata.
    URL and API key are reused from Codex configuration. Supports PNG, JPEG, WebP,
    sizes, quality, transparent backgrounds, local reference images and alpha masks.
    Provider requests can incur charges; do not automatically retry after a timeout.
    """
    try:
        settings = resolve_config(responses_model, image_model)
        result = await generate(
            settings,
            prompt=prompt,
            project_dir=project_dir,
            output_dir=output_dir,
            filename=filename,
            output_format=output_format,
            size=size,
            quality=quality,
            background=background,
            output_compression=output_compression,
            action=action,
            reference_images=reference_images,
            mask_image=mask_image,
            input_fidelity=input_fidelity,
            previous_response_id=previous_response_id,
        )
    except ImageToolError as exc:
        raise ToolError(str(exc)) from None
    except Exception:
        # In particular, never serialize a raw SDK exception, request or config object.
        raise ToolError("Image generation failed unexpectedly; inspect the local configuration and try again only after checking the provider.") from None

    metadata: dict[str, Any] = {
        "images": [],
        "text": result.text,
        "response_id": result.response_id,
        "responses_model": settings.responses_model,
        "image_model": settings.image_model,
        "model_source": settings.model_source,
        "warnings": list(result.warnings),
    }
    content: list[Any] = []
    for index, generated in enumerate(result.images):
        path = str(Path(generated.path).resolve())
        entry: dict[str, Any] = {
            "path": path,
            "mime_type": generated.mime_type,
            "width": generated.width,
            "height": generated.height,
            "bytes": len(generated.data),
            "revised_prompt": generated.revised_prompt,
            "preview_included": False,
        }
        content.append(f"Image saved: {path} ({generated.width}x{generated.height}, {generated.mime_type})")
        if include_preview and index < 4:
            try:
                preview_data, preview_format = await asyncio.to_thread(_preview, generated.data)
                content.append(Image(data=preview_data, format=preview_format))
                entry["preview_included"] = True
                entry["preview_is_original"] = False
            except Exception:
                metadata["warnings"].append(f"Preview unavailable for saved image {index + 1}; use its absolute path.")
        metadata["images"].append(entry)
    if include_preview and len(result.images) > 4:
        metadata["warnings"].append("Only the first four images have inline previews; all originals were saved.")
    if result.text:
        content.append(result.text)
    return ToolResult(content=content, structured_content=metadata)
