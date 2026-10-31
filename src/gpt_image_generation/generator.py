"""Responses image-generation requests and validated local output."""

from __future__ import annotations

import asyncio
import base64
import binascii
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, OpenAIError, omit

from .errors import ImageToolError
from .storage import (MAX_OUTPUT_BYTES, GeneratedImage, PreparedImage, inspect_image,
                      prepare_save, read_input_image, resolve_format, resolve_project, save_images)

if TYPE_CHECKING:
    from .config import ResolvedConfig


@dataclass(frozen=True)
class GenerationResult:
    images: list[GeneratedImage]
    text: str
    response_id: str | None
    warnings: list[str] = field(default_factory=list)


def _get(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def _model_family(model: str) -> str:
    if model.startswith(("gpt-image-2.5-flare", "gpt-image-2.5-sunburst")):
        return "2.5"
    if model == "gpt-image-2" or model.startswith("gpt-image-2-"):
        return "2"
    if model.startswith("gpt-image-1-mini"):
        return "mini"
    if model == "gpt-image-1" or model.startswith(("gpt-image-1-", "gpt-image-1.5")):
        return "1"
    return "unknown"


def _choice(value: str, allowed: set[str], name: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ImageToolError(f"{name} 必须是以下值之一：{', '.join(sorted(allowed))}。")
    return value


def validate_options(
    *, image_model: str, output_format: str, size: str, quality: str,
    background: str, output_compression: int | None, action: str,
    input_fidelity: str | None,
) -> dict[str, Any]:
    family = _model_family(image_model)
    quality = _choice(quality, {"auto", "low", "medium", "high", "xhigh", "max"}, "quality")
    if quality in {"xhigh", "max"} and family in {"1", "mini", "2"}:
        raise ImageToolError("所选图像模型不支持 xhigh/max；这两个质量档位适用于 GPT Image 2.5。")
    background = _choice(background, {"auto", "opaque", "transparent"}, "background")
    action = _choice(action, {"auto", "generate", "edit"}, "action")
    if background == "transparent" and output_format == "jpeg":
        raise ImageToolError("JPEG 不支持透明背景，请改用 PNG 或 WebP。")
    if output_compression is not None:
        if isinstance(output_compression, bool) or not isinstance(output_compression, int) or not 0 <= output_compression <= 100:
            raise ImageToolError("output_compression 必须是 0 到 100 的整数。")
        if output_format not in {"jpeg", "webp"}:
            raise ImageToolError("output_compression 仅用于 JPEG 或 WebP，PNG 请省略此参数。")
    if not isinstance(size, str):
        raise ImageToolError("size 必须是 auto 或 WIDTHxHEIGHT，例如 1536x1024。")
    size = size.strip().lower().replace("×", "x")
    if size != "auto":
        match = re.fullmatch(r"([1-9][0-9]{0,5})x([1-9][0-9]{0,5})", size)
        if not match:
            raise ImageToolError("size 必须是 auto 或 WIDTHxHEIGHT，例如 1536x1024。")
        width, height = map(int, match.groups())
        if family in {"2", "2.5"}:
            if width % 16 or height % 16:
                raise ImageToolError("GPT Image 2/2.5 的宽高必须都是 16 的倍数。")
            if max(width, height) > 3840 or max(width, height) > 3 * min(width, height):
                raise ImageToolError("GPT Image 2/2.5 的最长边不能超过 3840，长宽比不能超过 3:1。")
            if not 655_360 <= width * height <= 8_294_400:
                raise ImageToolError("GPT Image 2/2.5 的总像素必须在 655360 到 8294400 之间。")
        elif family in {"1", "mini"} and size not in {"1024x1024", "1024x1536", "1536x1024"}:
            raise ImageToolError("所选图像模型仅支持 auto、1024x1024、1024x1536 或 1536x1024。")
    if input_fidelity is not None:
        input_fidelity = _choice(input_fidelity, {"low", "high"}, "input_fidelity")
        if family == "2":
            raise ImageToolError("gpt-image-2 自动使用高保真输入，请省略 input_fidelity。")
        if family == "mini" and input_fidelity != "low":
            raise ImageToolError("gpt-image-1-mini 的 input_fidelity 仅支持 low。")
    tool: dict[str, Any] = {
        "type": "image_generation", "model": image_model, "output_format": output_format,
        "size": size, "quality": quality, "background": background, "action": action,
    }
    if output_compression is not None:
        tool["output_compression"] = output_compression
    if input_fidelity is not None:
        tool["input_fidelity"] = input_fidelity
    return tool


def _data_url(data: bytes, mime_type: str) -> str:
    return f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"


async def _header_auth_key() -> str:
    """Satisfy the public SDK key-provider contract for explicit header auth."""
    return ""


def _request_headers(config: ResolvedConfig) -> dict[str, Any]:
    """Keep authentication and custom headers bound to the selected provider."""
    headers: dict[str, Any] = {}
    # Ignore ambient SDK custom headers unless the selected provider declares them.
    for line in os.environ.get("OPENAI_CUSTOM_HEADERS", "").splitlines():
        name, separator, _ = line.partition(":")
        name = name.strip().lower()
        if separator and re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-z-]+", name):
            headers[name] = omit
    if config.provider != "openai":
        headers.update({"openai-organization": omit, "openai-project": omit})
    headers.update({name.lower(): value for name, value in config.headers.items()})
    if config.api_key:
        headers["authorization"] = f"Bearer {config.api_key}"
    elif "authorization" not in headers or not isinstance(headers["authorization"], str):
        headers["authorization"] = omit
    return headers


def _prepare_input(
    prompt: str, project_dir: str, reference_images: list[str] | None,
    mask_image: str | None, tool: dict[str, Any], previous_response_id: str | None,
) -> str | list[dict[str, Any]]:
    project = resolve_project(project_dir)
    if reference_images is not None and not isinstance(reference_images, list):
        raise ImageToolError("reference_images 必须是本地图片路径列表。")
    references = reference_images or []
    if any(not isinstance(item, str) for item in references):
        raise ImageToolError("reference_images 必须是本地图片路径列表。")
    if mask_image is not None and not references:
        raise ImageToolError("使用 mask_image 时必须提供至少一张参考图片。")
    if mask_image is not None and tool["action"] == "generate":
        raise ImageToolError("蒙版编辑请使用 action='edit' 或 'auto'。")
    if tool["action"] == "edit" and not references and not previous_response_id:
        raise ImageToolError("action='edit' 需要参考图片或 previous_response_id。")
    if tool.get("input_fidelity") and not references and not previous_response_id:
        raise ImageToolError("input_fidelity 需要参考图片或 previous_response_id。")
    content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    first_info = None
    for image_path in references:
        data, info = read_input_image(project, image_path)
        if first_info is None:
            first_info = info
        content.append({"type": "input_image", "image_url": _data_url(data, info.mime_type)})
    if mask_image is not None:
        mask_data, mask_info = read_input_image(project, mask_image)
        if not mask_info.has_alpha:
            raise ImageToolError("蒙版图片必须包含 alpha 透明通道。")
        if first_info is None or (mask_info.format, mask_info.width, mask_info.height) != (first_info.format, first_info.width, first_info.height):
            raise ImageToolError("蒙版必须与第一张参考图片具有相同格式和像素尺寸。")
        tool["input_image_mask"] = {"image_url": _data_url(mask_data, mask_info.mime_type)}
    return [{"role": "user", "content": content}] if references else prompt


def _decode_results(response: Any, output_format: str) -> list[PreparedImage]:
    if _get(response, "status") in {"failed", "cancelled", "incomplete", "queued", "in_progress"}:
        raise ImageToolError("上游响应未成功完成，没有保存图片；请检查模型能力或稍后重新请求。")
    images: list[PreparedImage] = []
    for item in _get(response, "output", []) or []:
        if _get(item, "type") != "image_generation_call":
            continue
        if _get(item, "status") not in {None, "completed"} or _get(item, "error") is not None:
            raise ImageToolError("上游图像工具调用失败或未完成，本次未保存任何图片。")
        encoded = _get(item, "result")
        if not isinstance(encoded, str) or not encoded:
            raise ImageToolError("上游图像工具没有返回有效图片内容。")
        if len(encoded) > (MAX_OUTPUT_BYTES * 4 // 3 + 4):
            raise ImageToolError("上游返回的图片内容过大，未保存文件。")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise ImageToolError("上游图片不是有效的 Base64 数据，未保存文件。") from None
        info = inspect_image(data)
        if info.format != output_format:
            raise ImageToolError("上游返回图片的实际格式与请求格式不符，未保存文件。")
        revised_prompt = _get(item, "revised_prompt")
        images.append(PreparedImage(data, info, revised_prompt if isinstance(revised_prompt, str) else None))
    if not images:
        raise ImageToolError("上游未返回 image_generation_call 图片。请确认中转站和对话模型支持 Responses 图像工具。")
    return images


async def generate(
    config: ResolvedConfig,
    *, prompt: str, project_dir: str, output_dir: str | None = None,
    filename: str | None = None, output_format: str | None = None,
    size: str = "auto", quality: str = "auto", background: str = "auto",
    output_compression: int | None = None, action: str = "auto",
    reference_images: list[str] | None = None, mask_image: str | None = None,
    input_fidelity: str | None = None, previous_response_id: str | None = None,
) -> GenerationResult:
    """Generate or edit images, validating all options before sending a request."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ImageToolError("prompt 不能为空，请描述需要生成或修改的图片。")
    if previous_response_id is not None and (not isinstance(previous_response_id, str) or not previous_response_id.strip()):
        raise ImageToolError("previous_response_id 必须是非空的上一次响应 ID。")
    selected_format = resolve_format(output_format, filename)
    tool = validate_options(image_model=config.image_model, output_format=selected_format,
                            size=size, quality=quality, background=background,
                            output_compression=output_compression, action=action, input_fidelity=input_fidelity)
    request_input = _prepare_input(prompt, project_dir, reference_images, mask_image, tool, previous_response_id)
    plan = prepare_save(project_dir, output_dir, filename, selected_format)
    request: dict[str, Any] = {"model": config.responses_model, "input": request_input,
                               "tools": [tool], "tool_choice": "required"}
    if previous_response_id is not None:
        request["previous_response_id"] = previous_response_id
    request["extra_headers"] = _request_headers(config)
    client_options: dict[str, Any] = {
        "base_url": config.base_url, "api_key": config.api_key or _header_auth_key,
        "default_headers": config.headers, "default_query": config.query_params,
        "timeout": config.request_timeout, "max_retries": 0,
    }
    if config.provider != "openai":
        client_options.update(organization="", project="", admin_api_key="")
    try:
        async with asyncio.timeout(config.request_timeout):
            async with AsyncOpenAI(**client_options) as client:
                response = await client.responses.create(**request)
    except (APITimeoutError, TimeoutError):
        raise ImageToolError("图像生成请求超时。上游可能仍在处理，请确认后再重试，避免重复计费。") from None
    except APIStatusError as exc:
        if exc.status_code in {401, 403}:
            message = "上游拒绝认证或访问，请检查当前 provider 的凭据与模型权限。"
        elif exc.status_code == 429:
            message = "上游返回限流或额度不足（HTTP 429），请检查额度或稍后重试。"
        elif exc.status_code in {400, 404, 422}:
            message = f"上游拒绝请求（HTTP {exc.status_code}），请核对模型、图像参数和 Responses 图像工具支持情况。"
        else:
            message = f"上游请求失败（HTTP {exc.status_code}），本次没有自动重试。"
        raise ImageToolError(message) from None
    except APIConnectionError:
        raise ImageToolError("无法连接图像服务，请检查 provider 地址、网络和代理设置。") from None
    except OpenAIError:
        raise ImageToolError("上游 SDK 请求失败，请检查 provider 配置与接口兼容性。") from None
    prepared = _decode_results(response, selected_format)
    images = save_images(plan, prepared)
    result_warnings: list[str] = []
    requested_size = None if tool["size"] == "auto" else tuple(map(int, tool["size"].split("x")))
    for index, image in enumerate(prepared, start=1):
        if requested_size and (image.info.width, image.info.height) != requested_size:
            result_warnings.append(f"第 {index} 张图片的实际尺寸为 {image.info.width}x{image.info.height}，与请求的 {tool['size']} 不同；已保存上游原始图片。")
        if background == "transparent" and not image.info.has_alpha:
            result_warnings.append(f"第 {index} 张图片不含 alpha 透明通道，与透明背景请求不一致；已保存上游原始图片。")
    text, response_id = _get(response, "output_text", ""), _get(response, "id")
    return GenerationResult(images=images, text=text if isinstance(text, str) else "",
                            response_id=response_id if isinstance(response_id, str) else None,
                            warnings=result_warnings)
