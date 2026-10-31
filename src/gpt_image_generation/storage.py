"""Validate image bytes and save results without overwriting existing files."""

from __future__ import annotations

import io
import re
import tempfile
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from .errors import ImageToolError

MAX_INPUT_BYTES = 50_000_000
MAX_OUTPUT_BYTES = 100 * 1024 * 1024
FORMATS = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}
EXTENSIONS = {"png": ".png", "jpeg": ".jpg", "webp": ".webp"}
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
_RESERVED_NAMES.update(f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10))
_RESERVED_NAMES.update(f"{prefix}{number}" for prefix in ("COM", "LPT") for number in "¹²³")


@dataclass(frozen=True)
class ImageInfo:
    format: str
    width: int
    height: int
    has_alpha: bool

    @property
    def mime_type(self) -> str:
        return FORMATS[self.format]


@dataclass(frozen=True)
class PreparedImage:
    data: bytes
    info: ImageInfo
    revised_prompt: str | None = None


@dataclass(frozen=True)
class GeneratedImage:
    path: Path
    data: bytes
    mime_type: str
    width: int
    height: int
    revised_prompt: str | None = None


@dataclass(frozen=True)
class SavePlan:
    project_dir: Path
    output_dir: Path
    filename: str
    output_format: str

    def paths(self, count: int) -> list[Path]:
        if count == 1:
            return [self.output_dir / self.filename]
        name = Path(self.filename)
        return [self.output_dir / f"{name.stem}_{index + 1}{name.suffix}" for index in range(count)]


def normalize_format(value: str) -> str:
    if not isinstance(value, str):
        raise ImageToolError("output_format 必须是 png、jpeg 或 webp。")
    result = value.strip().lower()
    if result == "jpg":
        result = "jpeg"
    if result not in FORMATS:
        raise ImageToolError("只支持 PNG、JPEG/JPG 和 WebP 图片格式，不支持 SVG 或 GIF 输出。")
    return result


def _validate_filename(filename: str) -> None:
    if not isinstance(filename, str) or not filename or filename in {".", ".."}:
        raise ImageToolError("filename 必须是非空文件名。")
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', filename) or filename.endswith((".", " ")):
        raise ImageToolError("filename 只能是文件名，不能包含目录、控制字符或系统不允许的字符。")
    if filename.split(".", 1)[0].upper() in _RESERVED_NAMES:
        raise ImageToolError("filename 不能使用 Windows 保留名称。")
    if len(filename) > 220:
        raise ImageToolError("filename 太长，请使用不超过 220 个字符的文件名。")


def resolve_format(output_format: str | None, filename: str | None) -> str:
    inferred = None
    if filename is not None:
        _validate_filename(filename)
        suffix = Path(filename).suffix
        if suffix:
            inferred = normalize_format(suffix[1:])
    requested = normalize_format(output_format) if output_format is not None else None
    if requested and inferred and requested != inferred:
        raise ImageToolError("filename 的扩展名与 output_format 不一致，请调整其中一项。")
    return requested or inferred or "png"


def resolve_project(project_dir: str) -> Path:
    if not isinstance(project_dir, str) or not project_dir.strip():
        raise ImageToolError("请由 Codex 传入当前项目的绝对路径 project_dir。")
    project = Path(project_dir).expanduser()
    if not project.is_absolute():
        raise ImageToolError("project_dir 必须是当前项目的绝对路径。")
    try:
        project = project.resolve(strict=True)
        if not project.is_dir():
            raise ImageToolError("project_dir 必须指向已有的项目目录。")
    except (OSError, RuntimeError, ValueError):
        raise ImageToolError("无法访问 project_dir，请检查项目目录是否存在。") from None
    return project


def resolve_project_path(project_dir: Path, value: str, *, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ImageToolError(f"{label} 必须是非空路径。")
    try:
        path = Path(value).expanduser()
        if path.is_absolute():
            return path.resolve()
        path = (project_dir / path).resolve()
        if not path.is_relative_to(project_dir):
            raise ImageToolError(f"相对 {label} 不能指向当前项目之外；请明确提供绝对路径。")
        return path
    except (OSError, RuntimeError, ValueError):
        raise ImageToolError(f"{label} 不是可用的文件系统路径。") from None


def prepare_save(
    project_dir: str,
    output_dir: str | None = None,
    filename: str | None = None,
    output_format: str | None = None,
) -> SavePlan:
    project = resolve_project(project_dir)
    image_format = resolve_format(output_format, filename)
    directory = resolve_project_path(project, output_dir or "outputs/images", label="output_dir")
    if filename is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"image_{stamp}_{uuid4().hex[:12]}{EXTENSIONS[image_format]}"
    elif not Path(filename).suffix:
        filename += EXTENSIONS[image_format]
    target = directory / filename
    try:
        if target.exists() or target.is_symlink():
            raise ImageToolError("目标文件已存在，请更换 filename；工具不会覆盖已有图片。")
        directory.mkdir(parents=True, exist_ok=True)
        # Fail before a paid request if the destination is not writable.
        with tempfile.TemporaryFile(dir=directory):
            pass
    except OSError:
        raise ImageToolError("无法写入图片输出目录，请检查路径及目录权限。") from None
    return SavePlan(project, directory, filename, image_format)


def inspect_image(data: bytes, *, input_image: bool = False) -> ImageInfo:
    limit = MAX_INPUT_BYTES if input_image else MAX_OUTPUT_BYTES
    if not data or len(data) >= limit:
        label = "输入图片必须小于 50 MB" if input_image else "返回图片必须小于 100 MiB"
        raise ImageToolError(f"{label}，且不能是空文件。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                image_format = (image.format or "").lower()
                if image_format not in FORMATS:
                    raise ImageToolError("图片内容必须是 PNG、JPEG 或 WebP，修改扩展名不能转换格式。")
                if getattr(image, "n_frames", 1) != 1:
                    raise ImageToolError("仅支持静态图片，请先将动画转换为单帧图片。")
                info = ImageInfo(image_format, image.width, image.height, "A" in image.getbands() or "transparency" in image.info)
                image.verify()
            # JPEG verify() does not decode pixel data; load once to catch truncation.
            with Image.open(io.BytesIO(data)) as image:
                image.load()
    except ImageToolError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageToolError("图片内容损坏、无法解码或像素数量过大。") from None
    return info


def read_input_image(project_dir: Path, path: str) -> tuple[bytes, ImageInfo]:
    source = resolve_project_path(project_dir, path, label="参考图片路径")
    try:
        if source.stat().st_size >= MAX_INPUT_BYTES:
            raise ImageToolError("参考图片或蒙版必须小于 50 MB。")
        with source.open("rb") as stream:
            data = stream.read(MAX_INPUT_BYTES)
    except OSError:
        raise ImageToolError("无法读取参考图片或蒙版，请检查路径及文件权限。") from None
    return data, inspect_image(data, input_image=True)


def save_images(plan: SavePlan, images: list[PreparedImage]) -> list[GeneratedImage]:
    if not images:
        raise ImageToolError("没有可保存的图片。")
    paths = plan.paths(len(images))
    for path, image in zip(paths, images):
        if image.info.format != plan.output_format:
            raise ImageToolError("上游返回图片的实际格式与请求格式不符，未保存文件。")
        if path.exists() or path.is_symlink():
            raise ImageToolError("目标文件已存在，未保存本批图片；请更换 filename。")
    created: list[Path] = []
    try:
        for path, image in zip(paths, images):
            with path.open("xb") as stream:
                created.append(path)
                stream.write(image.data)
    except OSError:
        cleanup_failed = False
        for path in created:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                cleanup_failed = True
        message = "保存图片失败；本次已创建的部分文件已清理，原有文件未覆盖。"
        if cleanup_failed:
            message = "保存图片失败，部分本次创建的文件无法清理，请检查输出目录；原有文件未覆盖。"
        raise ImageToolError(message) from None
    return [GeneratedImage(path, image.data, image.info.mime_type, image.info.width, image.info.height, image.revised_prompt)
            for path, image in zip(paths, images)]
