"""Responses image tools with bounded local processing and partial-result recovery."""

from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping
from urllib.parse import urlsplit

from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, AsyncOpenAI, OpenAIError

from .errors import ImageToolError
from .model_selection import select_models, validate_model_options
from .model_catalog import image_capabilities, model_id
from .provider import client_options, request_headers as _request_headers
from .storage import (
    MAX_INPUT_BYTES, MAX_OUTPUT_BYTES, GeneratedImage, ImageInfo, PreparedImage,
    inspect_image, prepare_save, read_input_image, resolve_format, resolve_project, save_images,
)

if TYPE_CHECKING:
    from .config import ResolvedConfig

# Application resource budgets, not assertions about a model's capabilities.
MAX_INPUT_IMAGES = 16
MAX_INPUT_TOTAL_BYTES = 100_000_000
MAX_OUTPUT_IMAGES = 16
MAX_OUTPUT_TOTAL_BYTES = 200 * 1024 * 1024
MAX_PARTIAL_IMAGES = 12
MAX_PARTIAL_TOTAL_BYTES = 32 * 1024 * 1024
MAX_TEXT_CHARS = 32_768
ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class GenerationResult:
    images: list[GeneratedImage] = field(default_factory=list)
    text: str = ""
    response_id: str | None = None
    request_id: str | None = None
    usage: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    status: str = "error"
    partials: list[PreparedImage] = field(default_factory=list)
    model: str | None = None
    models: dict[str, Any] = field(default_factory=dict)
    tool_usage: dict[str, Any] | None = None


def _get(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def _choice(value: str, allowed: set[str], name: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ImageToolError(f"{name} 必须是以下值之一：{', '.join(sorted(allowed))}。")
    return value


def _nonempty(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 for char in value):
        raise ImageToolError(f"{name} 必须是非空的单行字符串。")
    return value


def validate_options(
    *, output_format: str, size: str | None = None, quality: str | None = None,
    background: str | None = None, output_compression: int | None = None,
    action: str | None = None, input_fidelity: str | None = None,
    moderation: str | None = None, partial_images: int = 0,
) -> dict[str, Any]:
    """Validate syntax and incompatible options without guessing model capability."""
    tool: dict[str, Any] = {"type": "image_generation", "output_format": output_format}
    choices = {
        "quality": (quality, {"auto", "low", "medium", "high", "xhigh", "max"}),
        "background": (background, {"auto", "opaque", "transparent"}),
        "action": (action, {"auto", "generate", "edit"}),
        "input_fidelity": (input_fidelity, {"low", "high"}),
        "moderation": (moderation, {"auto", "low"}),
    }
    for name, (value, allowed) in choices.items():
        if value is not None:
            tool[name] = _choice(value, allowed, name)
    if size is not None:
        if not isinstance(size, str) or len(size) > 64:
            raise ImageToolError("size 必须是 auto 或正整数 WIDTHxHEIGHT；本地参数长度限制为 64 字符。")
        size = size.strip().lower().replace("×", "x")
        if size != "auto" and not re.fullmatch(r"[1-9][0-9]*x[1-9][0-9]*", size):
            raise ImageToolError("size 必须是 auto 或正整数 WIDTHxHEIGHT，例如 1536x1024。")
        tool["size"] = size
    if background == "transparent" and output_format == "jpeg":
        raise ImageToolError("JPEG 不支持透明背景，请使用 PNG 或 WebP。")
    if output_compression is not None:
        if type(output_compression) is not int or not 0 <= output_compression <= 100:
            raise ImageToolError("output_compression 必须是 0 到 100 的整数。")
        if output_format not in {"jpeg", "webp"}:
            raise ImageToolError("output_compression 仅用于 JPEG/WebP；PNG 请省略此参数。")
        tool["output_compression"] = output_compression
    if type(partial_images) is not int or not 0 <= partial_images <= 3:
        raise ImageToolError("partial_images 必须是 0 到 3 的整数。")
    if partial_images:
        tool["partial_images"] = partial_images
    return tool


async def _settle(task: asyncio.Task[Any]) -> Any:
    """Wait for a started critical operation even after a repeated cancellation."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


async def _offload(function: Callable[..., Any], *args: Any,
                   on_cancel: Callable[[Any], Any] | None = None, **kwargs: Any) -> Any:
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A thread cannot be killed safely. Collect its result and finish critical
        # cleanup before propagating cancellation, so reservations never outlive it.
        try:
            result = await _settle(task)
            if on_cancel is not None:
                await _settle(asyncio.create_task(asyncio.to_thread(on_cancel, result)))
        except Exception:
            pass
        raise


def _decode_base64(value: Any, byte_limit: int, label: str) -> bytes:
    if not isinstance(value, str) or not value or len(value) > ((byte_limit + 2) // 3) * 4:
        raise ImageToolError(f"{label} 为空或超过本地字节限制。")
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ImageToolError(f"{label} 不是有效的 Base64 数据。") from None
    if not data or len(data) >= byte_limit:
        raise ImageToolError(f"{label} 为空或超过本地字节限制。")
    return data


def _source(source: Any, cwd: Any, *, is_mask: bool = False) -> tuple[dict[str, Any], ImageInfo | None, int]:
    if not isinstance(source, dict):
        raise ImageToolError("每个图像来源必须是包含 path、image_url 或 file_id 之一的对象。")
    allowed = {"path", "image_url", "file_id"} | (set() if is_mask else {"detail"})
    sources = set(source) & {"path", "image_url", "file_id"}
    if len(sources) != 1 or set(source) - allowed:
        raise ImageToolError("每个图像必须且只能指定 path、image_url、file_id 之一；参考图可另带 detail。")
    key = next(iter(sources))
    value = _nonempty(source[key], key)
    info = None
    byte_count = 0
    if key == "path":
        data, info = read_input_image(cwd, value)
        result = {"image_url": f"data:{info.mime_type};base64,{base64.b64encode(data).decode('ascii')}"}
        byte_count = len(data)
    elif key == "file_id":
        result = {"file_id": value}
    elif value.lower().startswith("data:"):
        header, comma, payload = value.partition(",")
        match = re.fullmatch(r"data:(image/(?:png|jpeg|webp));base64", header, re.IGNORECASE)
        if not comma or not match:
            raise ImageToolError("本地支持的 data URL 必须是 PNG/JPEG/WebP 的 base64 图像。")
        data = _decode_base64(payload, MAX_INPUT_BYTES, "输入图片")
        info = inspect_image(data, input_image=True)
        if info.mime_type != match[1].lower():
            raise ImageToolError("data URL 的 MIME 类型与实际图片格式不一致。")
        result, byte_count = {"image_url": value}, len(data)
    else:
        try:
            url = urlsplit(value)
            valid = url.scheme.lower() in {"http", "https"} and bool(url.hostname)
            _ = url.port
        except ValueError:
            valid = False
        if not valid:
            raise ImageToolError("image_url 必须是完整 HTTP(S) URL 或图像 data URL。")
        # Do not fetch remote images locally. The configured provider resolves them.
        result = {"image_url": value}
    if not is_mask and "detail" in source:
        result["detail"] = _choice(source["detail"], {"auto", "low", "high", "original"}, "detail")
    return result, info, byte_count


def _prepare_input(prompt: str, cwd: str, tool: dict[str, Any], input_images: list[dict[str, Any]] | None,
                   mask: dict[str, Any] | None, previous_response_id: str | None,
                   previous_image_ids: list[str] | None) -> str | list[dict[str, Any]]:
    project = resolve_project(cwd)
    images = [] if input_images is None else input_images
    previous_ids = [] if previous_image_ids is None else previous_image_ids
    if not isinstance(images, list) or not isinstance(previous_ids, list):
        raise ImageToolError("input_images 和 previous_image_ids 必须是列表。")
    if len(images) + len(previous_ids) > MAX_INPUT_IMAGES:
        raise ImageToolError(f"超过本地单次输入限制：最多 {MAX_INPUT_IMAGES} 张图像引用。")
    for image_id in previous_ids:
        _nonempty(image_id, "previous_image_ids")
    has_reference = bool(images or previous_ids or previous_response_id)
    if tool.get("action") == "edit" and not has_reference:
        raise ImageToolError("编辑需要 input_images、previous_image_ids 或 previous_response_id。")
    if mask is not None and (not has_reference or tool.get("action") == "generate"):
        raise ImageToolError("蒙版需要图像引用，并且 action 不能为 generate。")
    if "input_fidelity" in tool and not has_reference:
        raise ImageToolError("input_fidelity 需要参考图片或前一轮图像上下文。")
    content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    first_info = None
    total = 0
    for index, source in enumerate(images):
        converted, info, size = _source(source, project)
        total += size
        if total > MAX_INPUT_TOTAL_BYTES:
            raise ImageToolError("输入的本地/data 图片超过本地总字节预算 100 MB。")
        if index == 0:
            first_info = info
        content.append({"type": "input_image", **converted})
    if mask is not None:
        converted, info, size = _source(mask, project, is_mask=True)
        if total + size > MAX_INPUT_TOTAL_BYTES:
            raise ImageToolError("图片与蒙版超过本地总字节预算 100 MB。")
        if info is not None:
            if not info.has_alpha:
                raise ImageToolError("本地/data 蒙版必须包含 alpha 通道。")
            if first_info is not None and (info.format, info.width, info.height) != (first_info.format, first_info.width, first_info.height):
                raise ImageToolError("蒙版必须与第一张参考图具有相同格式和像素尺寸。")
        tool["input_image_mask"] = converted
    if not images and not previous_ids:
        return prompt
    return ([{"type": "image_generation_call", "id": image_id} for image_id in previous_ids]
            + [{"role": "user", "content": content}])


def _numeric_usage(value: Any, depth: int = 0, *, redact: Callable[[Any], str] | None = None) -> Any:
    if depth > 6:
        return None
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if isinstance(value, dict):
        return {key: _numeric_usage(item, depth + 1, redact=redact) for key, item in list(value.items())[:64]
                if isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_]{1,80}", key)
                and (redact is None or redact(key) == key)}
    if value is None or type(value) in {int, bool}:
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


class _State:
    def __init__(self, config: ResolvedConfig):
        self.result = GenerationResult()
        self.secrets = [value for value in [config.api_key, *config.headers.values(), *config.query_params.values()]
                        if isinstance(value, str) and value]
        self.secrets = sorted(set(self.secrets), key=len, reverse=True)
        self.redaction_padding = max((len(value) for value in self.secrets), default=0)
        self.items: dict[str, Any] = {}
        self.encoded_bytes = 0
        self.partial_bytes = 0
        self.partial_keys: set[tuple[str | None, int]] = set()
        self.terminal = False
        self.prepared: list[PreparedImage] = []
        self.decoded = False
        self.saved = False

    def text(self, value: Any) -> str:
        if not isinstance(value, str):
            return ""
        # Include enough look-ahead to redact a secret crossing the text cutoff.
        value = value[:MAX_TEXT_CHARS + self.redaction_padding]
        for secret in self.secrets:
            if len(secret) >= 6:
                value = value.replace(secret, "[redacted]")
            elif value == secret:
                return "[redacted]"
        return value[:MAX_TEXT_CHARS]

    def identifier(self, value: Any) -> str | None:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,511}", value):
            return None
        return value if self.text(value) == value else None

    def warn(self, message: str) -> None:
        if len(self.result.warnings) < 32 and message not in self.result.warnings:
            self.result.warnings.append(message)

    def error(self, code: str, message: str, **metadata: Any) -> None:
        if len(self.result.errors) < 32:
            self.result.errors.append({"code": code, "message": message, **metadata})

    def remember(self, item: Any, index: int | None = None) -> None:
        if _get(item, "type") != "image_generation_call":
            return
        raw_id = _get(item, "id")
        key = f"id:{raw_id}" if isinstance(raw_id, str) and raw_id else f"index:{index if index is not None else len(self.items)}"
        old = self.items.get(key)
        if old is not None and _get(old, "status") in {None, "completed"} and _get(old, "result"):
            return
        encoded = _get(item, "result")
        length = len(encoded) if isinstance(encoded, str) else 0
        old_encoded = _get(old, "result")
        old_length = len(old_encoded) if isinstance(old_encoded, str) else 0
        if old is None and len(self.items) >= MAX_OUTPUT_IMAGES:
            self.error("local_output_limit", "上游图片数量超过本地限制，其余图片未保留。")
            return
        if self.encoded_bytes - old_length + length > ((MAX_OUTPUT_TOTAL_BYTES + 2) // 3) * 4:
            self.error("local_output_limit", "上游图片总数据超过本地 200 MiB 预算，其余图片未保留。")
            return
        self.items[key] = item
        self.encoded_bytes += length - old_length

    def response(self, response: Any, *, terminal: bool = False) -> None:
        self.result.response_id = self.identifier(_get(response, "id")) or self.result.response_id
        self.result.request_id = self.identifier(_get(response, "_request_id")) or self.result.request_id
        actual_model = model_id(_get(response, "model"))
        if actual_model and self.text(actual_model) != actual_model:
            actual_model = None
        if actual_model:
            self.result.model = actual_model
        usage = _numeric_usage(_get(response, "usage"), redact=self.text)
        if isinstance(usage, dict):
            self.result.usage = usage
        tool_usage = _numeric_usage(_get(response, "tool_usage"), redact=self.text)
        if isinstance(tool_usage, dict):
            self.result.tool_usage = tool_usage
        try:
            text = self.text(_get(response, "output_text"))
        except Exception:
            text = ""
        items = _get(response, "output")
        if not isinstance(items, (list, tuple)):
            items = []
        text_parts = []
        remaining = MAX_TEXT_CHARS + self.redaction_padding
        for index, item in enumerate(items):
            self.remember(item, index)
            if _get(item, "type") == "message":
                parts = _get(item, "content", [])
                if not isinstance(parts, (list, tuple)):
                    continue
                for part in parts:
                    part_type = _get(part, "type")
                    if part_type in {"output_text", "refusal"}:
                        part_text = _get(part, "refusal" if part_type == "refusal" else "text")
                        if isinstance(part_text, str) and remaining > 0:
                            fragment = part_text[:remaining]
                            text_parts.append(fragment)
                            remaining -= len(fragment)
        # A credential can span adjacent output_text blocks.
        self.result.text = self.text("".join(text_parts)) or text or self.result.text
        if terminal:
            self.terminal = True
            status = _get(response, "status")
            failure = _get(response, "error")
            if status not in {None, "completed"} or failure is not None:
                details = _error_details(failure, self)
                reason = self.identifier(_get(_get(response, "incomplete_details"), "reason"))
                if reason:
                    details["incomplete_reason"] = reason
                self.error("response_incomplete", "上游响应未完整完成；已完成且有效的图片仍会保存。", **details)

    def accept_decoded(self, value: tuple[list[PreparedImage], list[dict[str, Any]], list[str]]) -> None:
        self.prepared, errors, warnings = value
        self.result.errors.extend(errors[:max(0, 32 - len(self.result.errors))])
        for warning in warnings:
            self.warn(warning)
        self.decoded = True

    def accept_saved(self, value: Any) -> None:
        self.result.images.extend(value.images)
        self.result.errors.extend(value.errors[:max(0, 32 - len(self.result.errors))])
        for warning in getattr(value, "warnings", []):
            self.warn(warning)
        self.saved = True


def _decode_items(state: _State, tool: dict[str, Any]) -> tuple[list[PreparedImage], list[dict[str, Any]], list[str]]:
    prepared, errors, warnings = [], [], []
    total = 0
    requested_size = tool.get("size")
    for index, item in enumerate(state.items.values()):
        call_id = state.identifier(_get(item, "id"))
        if _get(item, "status") not in {None, "completed"} or _get(item, "error") is not None:
            errors.append({"code": "image_generation_failed", "message": "该图片工具调用失败或未完成。", "call_id": call_id, "index": index,
                           **_error_details(_get(item, "error"), state)})
            continue
        try:
            data = _decode_base64(_get(item, "result"), MAX_OUTPUT_BYTES, "返回图片")
            total += len(data)
            if total > MAX_OUTPUT_TOTAL_BYTES:
                raise ImageToolError("返回图片超过本地总字节预算 200 MiB。")
            info = inspect_image(data)
        except ImageToolError as exc:
            errors.append({"code": "invalid_image", "message": str(exc), "call_id": call_id, "index": index})
            continue
        metadata = {"output_format": info.format, "size": f"{info.width}x{info.height}"}
        for name, allowed in {"action": {"auto", "generate", "edit"}, "quality": {"auto", "low", "medium", "high", "xhigh", "max"},
                              "background": {"auto", "opaque", "transparent"}}.items():
            value = _get(item, name)
            if isinstance(value, str) and value in allowed:
                metadata[name] = value
        prepared.append(PreparedImage(data, info, state.text(_get(item, "revised_prompt")) or None, call_id, metadata))
        if requested_size and requested_size != "auto" and requested_size != metadata["size"]:
            warnings.append(f"第 {index + 1} 张图片实际尺寸为 {metadata['size']}，与请求的 {requested_size} 不同。")
        if tool.get("background") == "transparent" and not info.has_transparency:
            warnings.append(f"第 {index + 1} 张图片没有透明像素，与透明背景请求不一致。")
    return prepared, errors, warnings


def _decode_partial(event: Any, state: _State) -> PreparedImage:
    data = _decode_base64(_get(event, "partial_image_b64"), min(MAX_OUTPUT_BYTES, MAX_PARTIAL_TOTAL_BYTES), "中间图片")
    info = inspect_image(data)
    item_id = state.identifier(_get(event, "item_id"))
    return PreparedImage(data, info, call_id=item_id, metadata={
        "partial_index": _get(event, "partial_image_index"), "item_id": item_id,
        "output_format": info.format, "size": f"{info.width}x{info.height}", "is_partial": True,
    })


async def _notify(callback: ProgressCallback | None, event: dict[str, Any], state: _State) -> None:
    if callback is not None:
        try:
            await callback(event)
        except Exception:
            state.warn("进度通知不可用，生成与保存继续进行。")


async def _consume_stream(stream: Any, state: _State, on_progress: ProgressCallback | None) -> None:
    headers = _get(_get(stream, "response"), "headers", {})
    if hasattr(headers, "get"):
        state.result.request_id = state.identifier(headers.get("x-request-id"))
    try:
        async for event in stream:
            event_type = _get(event, "type", "")
            if event_type in {"response.created", "response.in_progress"}:
                state.response(_get(event, "response"))
                await _notify(on_progress, {"stage": event_type.rsplit(".", 1)[-1]}, state)
            elif event_type == "response.output_item.done":
                item = _get(event, "item")
                state.remember(item, _get(event, "output_index"))
                if _get(item, "type") == "image_generation_call":
                    await _notify(on_progress, {"stage": "image_generated", "item_id": state.identifier(_get(item, "id"))}, state)
            elif event_type == "response.image_generation_call.partial_image":
                index = _get(event, "partial_image_index")
                item_id = state.identifier(_get(event, "item_id"))
                key = (item_id, index)
                if type(index) is not int or index < 0:
                    state.warn("忽略索引无效的中间图片事件。")
                    continue
                if key in state.partial_keys:
                    continue
                if len(state.result.partials) >= MAX_PARTIAL_IMAGES:
                    state.warn("中间图片超过本地数量限制，后续仅保留最终图片。")
                    continue
                encoded = _get(event, "partial_image_b64")
                remaining = MAX_PARTIAL_TOTAL_BYTES - state.partial_bytes
                if not isinstance(encoded, str) or len(encoded) > ((remaining + 2) // 3) * 4:
                    state.warn("中间图片超过本地 32 MiB 总预算，后续仅保留最终图片。")
                    continue
                try:
                    partial = await _offload(_decode_partial, event, state)
                except ImageToolError:
                    state.warn("忽略无法解码的中间图片，最终图片仍会独立校验。")
                    continue
                if state.partial_bytes + len(partial.data) > MAX_PARTIAL_TOTAL_BYTES:
                    state.warn("中间图片超过本地 32 MiB 总预算。")
                    continue
                state.partial_bytes += len(partial.data)
                state.result.partials.append(partial)
                state.partial_keys.add(key)
                await _notify(on_progress, {"stage": "partial_image", "index": index, "item_id": item_id}, state)
            elif event_type in {"response.completed", "response.incomplete", "response.failed"}:
                state.response(_get(event, "response"), terminal=True)
                if event_type != "response.completed" and not state.result.errors:
                    state.error("response_incomplete", "上游响应未完整完成；已完成图片仍会保存。")
                if event_type == "response.completed":
                    await _notify(on_progress, {"stage": "completed"}, state)
                break
            elif event_type == "error":
                state.error("upstream_error", "上游流式请求报告错误；已完成图片仍会保存。",
                            **_error_details(_get(event, "error", event), state))
            elif event_type in {"response.image_generation_call.in_progress", "response.image_generation_call.generating"}:
                await _notify(on_progress, {"stage": event_type.rsplit(".", 1)[-1], "item_id": state.identifier(_get(event, "item_id"))}, state)
    except (APITimeoutError, APIStatusError) as exc:
        _record_failure(state, exc)
    except APIConnectionError:
        state.error("stream_interrupted", "图片流发生连接错误，已完成图片仍会保存；未自动重试。")
    except APIError as exc:
        # The SDK raises APIError before yielding SSE records containing `error`.
        _record_failure(state, exc)
    except Exception:
        state.error("stream_interrupted", "图片流发生连接或协议错误，已完成图片仍会保存；未自动重试。")
    finally:
        close = getattr(stream, "close", None) or getattr(stream, "aclose", None)
        if close is not None:
            pending = close()
            if inspect.isawaitable(pending):
                task = asyncio.create_task(pending)
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    await _settle(task)
                    raise
    if not state.terminal and not any(error["code"] in {"stream_interrupted", "timeout", "upstream_error"} for error in state.result.errors):
        state.error("stream_interrupted", "图片流在完整终态前结束，已完成图片仍会保存；未自动重试。")


def _record_failure(state: _State, error: Exception) -> None:
    if isinstance(error, (APITimeoutError, TimeoutError)):
        state.error("timeout", "操作超时。已完成图片会尽量保存；上游可能仍在处理，请确认后再重试以免重复计费。")
    elif isinstance(error, APIStatusError):
        state.result.request_id = state.identifier(getattr(error, "request_id", None)) or state.result.request_id
        status = error.status_code
        if status in {401, 403}:
            code, message = "authentication_error", "上游拒绝认证或访问，请检查所选 provider 的凭据和权限。"
        elif status == 429:
            code, message = "rate_limit", "上游返回限流或额度不足，请检查额度或稍后重试。"
        elif status in {400, 404, 422}:
            code, message = "invalid_request", "上游拒绝请求，请核对模型、图像参数和 Responses 工具支持情况。"
        else:
            code, message = "upstream_http_error", "上游 HTTP 请求失败，未自动重试。"
        body = getattr(error, "body", None)
        details = _get(body, "error", body)
        state.error(code, message, http_status=status, **_error_details(details, state))
    elif isinstance(error, APIConnectionError):
        state.error("connection_error", "上游连接中断或不可达；已完成图片会尽量保存，未自动重试。")
    elif isinstance(error, APIError):
        body = getattr(error, "body", None)
        state.error("upstream_error", "上游请求报告错误；已完成图片会尽量保存，未自动重试。",
                    **_error_details(_get(body, "error", body), state))
    elif isinstance(error, OpenAIError):
        state.error("sdk_error", "上游 SDK 请求失败，请检查接口兼容性。")
    else:
        state.error("processing_error", "图像处理未完整完成；已完成图片会尽量保存。")


def _error_details(error: Any, state: _State) -> dict[str, Any]:
    """Expose bounded error identifiers, never raw provider bodies or messages."""
    details = {}
    for name in ("code", "param", "type"):
        value = _get(error, name)
        if name == "param":
            value = value if (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.:\[\]-]{0,199}", value)
                              and state.text(value) == value) else None
        else:
            value = state.identifier(value)
        if value:
            details["upstream_" + name] = value
    return details


async def _finalize(state: _State, plan: Any, tool: dict[str, Any]) -> None:
    if not state.decoded:
        decoded = await _offload(_decode_items, state, tool, on_cancel=state.accept_decoded)
        state.accept_decoded(decoded)
    if state.prepared and not state.saved:
        saved = await _offload(save_images, plan, state.prepared, on_cancel=state.accept_saved)
        state.accept_saved(saved)


async def generate(
    config: ResolvedConfig, *, prompt: str, cwd: str,
    output_dir: str | None = None, filename: str | None = None,
    output_format: str | None = None, size: str | None = None,
    quality: str | None = None, background: str | None = None,
    output_compression: int | None = None, action: str | None = None,
    input_images: list[dict[str, Any]] | None = None, mask: dict[str, Any] | None = None,
    input_fidelity: str | None = None, previous_response_id: str | None = None,
    previous_image_ids: list[str] | None = None, moderation: str | None = None,
    partial_images: int = 0, safety_identifier: str | None = None,
    responses_model: str | None = None, image_model: str | None = None,
    model_policy: str | None = None,
    on_progress: ProgressCallback | None = None,
) -> GenerationResult:
    """Generate/edit with no automatic retry; retain validated completed assets.

    The deadline covers local preparation, networking and processing. When it expires
    or cancellation arrives, already-started worker threads finish their critical work
    and reservations are released; this cleanup may extend past the deadline.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ImageToolError("prompt 不能为空。")
    if previous_response_id is not None:
        _nonempty(previous_response_id, "previous_response_id")
    if safety_identifier is not None:
        _nonempty(safety_identifier, "safety_identifier")
    selected_format = resolve_format(output_format, filename)
    tool = validate_options(output_format=selected_format, size=size, quality=quality, background=background,
                            output_compression=output_compression, action=action, input_fidelity=input_fidelity,
                            moderation=moderation, partial_images=partial_images)
    state = _State(config)
    plan = None
    try:
        try:
            async with asyncio.timeout(config.request_timeout):
                plan = await _offload(prepare_save, cwd, output_dir, filename, selected_format,
                                      on_cancel=lambda value: value.release())
                request_input = await _offload(_prepare_input, prompt, cwd, tool, input_images, mask,
                                               previous_response_id, previous_image_ids)
                await _notify(on_progress, {"stage": "discovering_models"}, state)
                selected = await select_models(config, responses_model=responses_model, image_model=image_model, model_policy=model_policy)
                state.result.models = selected.public()
                for warning in selected.warnings:
                    state.warn(warning)
                if selected.image_model is not None:
                    tool["model"] = selected.image_model
                try:
                    validate_model_options(selected.image_model, tool)
                except ImageToolError as exc:
                    # The selected model explains why an otherwise valid option failed.
                    # Keep that provenance in the result instead of losing it in the
                    # MCP adapter's minimal preflight error response.
                    state.error("invalid_request", state.text(str(exc)))
                    return state.result
                known_dimensions = selected.image_model and image_capabilities(selected.image_model).get("custom_size")
                if known_dimensions and tool.get("size") not in {None, "auto"}:
                    width, height = (int(value) for value in tool["size"].split("x"))
                    if width * height > 2560 * 1440:
                        state.warn("请求尺寸超过 2560×1440 像素面积；官方将这一范围标为实验性支持。")
                await _notify(on_progress, {"stage": "models_selected", "responses_model": selected.responses_model,
                                            "image_model": selected.image_model}, state)
                request: dict[str, Any] = {"model": selected.responses_model, "input": request_input, "tools": [tool],
                                           "tool_choice": {"type": "image_generation"}, "extra_headers": _request_headers(config)}
                if previous_response_id is not None:
                    request["previous_response_id"] = previous_response_id
                if safety_identifier is not None:
                    request["safety_identifier"] = safety_identifier
                async with AsyncOpenAI(**client_options(config)) as client:
                    if partial_images:
                        stream = await client.responses.create(**request, stream=True)
                        await _consume_stream(stream, state, on_progress)
                    else:
                        response = await client.responses.create(**request)
                        state.response(response, terminal=True)
                await _finalize(state, plan, tool)
        except ImageToolError:
            raise
        except asyncio.CancelledError:
            # Preserve known completed items even when the caller abandons the reply.
            if plan is not None and state.items:
                try:
                    await _settle(asyncio.create_task(_finalize(state, plan, tool)))
                except Exception:
                    pass
            raise
        except Exception as exc:
            _record_failure(state, exc)
            if plan is not None:
                try:
                    await _finalize(state, plan, tool)
                except Exception:
                    state.error("recovery_failed", "部分已完成图片无法恢复保存，请检查输出目录。")
        if not state.result.images and not state.result.errors:
            state.error("no_images", "上游没有返回可保存的 image_generation_call 图片。")
        state.result.status = ("partial_success" if state.result.errors else "success") if state.result.images else "error"
        return state.result
    finally:
        if plan is not None:
            cleanup_errors = await _offload(plan.release)
            if cleanup_errors:
                state.result.errors.extend(cleanup_errors[:max(0, 32 - len(state.result.errors))])
                state.result.status = "partial_success" if state.result.images else "error"
