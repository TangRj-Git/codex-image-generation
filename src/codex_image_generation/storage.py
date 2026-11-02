"""Validate local image bytes and preserve each successfully saved asset."""

from __future__ import annotations

import errno
import hashlib
import io
import os
import re
import shutil
import stat
import tempfile
import unicodedata
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
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
_UNSUPPORTED_LINK_ERRORS = {
    errno.EPERM, errno.EXDEV, errno.ENOSYS,
    getattr(errno, "ENOTSUP", errno.EPERM),
    getattr(errno, "EOPNOTSUPP", errno.EPERM),
}


@dataclass(frozen=True)
class ImageInfo:
    format: str
    width: int
    height: int
    has_alpha: bool
    has_transparency: bool = False

    @property
    def mime_type(self) -> str:
        return FORMATS[self.format]


@dataclass(frozen=True)
class PreparedImage:
    data: bytes
    info: ImageInfo
    revised_prompt: str | None = None
    call_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GeneratedImage:
    path: Path
    data: bytes
    mime_type: str
    width: int
    height: int
    revised_prompt: str | None = None
    call_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SaveResult:
    images: list[GeneratedImage] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return info.st_dev, info.st_ino


def _utf16_length(value: str) -> int:
    try:
        return len(value.encode("utf-16-le")) // 2
    except UnicodeError:
        raise ImageToolError("filename 包含无效 Unicode 字符。") from None


def _filename_limit(directory: Path) -> int | None:
    if os.name == "nt" or not hasattr(os, "pathconf"):
        return None
    try:
        limit = os.pathconf(directory, "PC_NAME_MAX")
        return limit if limit > 0 else None
    except (OSError, ValueError):
        return None


@dataclass
class SavePlan:
    cwd: Path
    output_dir: Path
    filename: str
    output_format: str
    _locks: dict[Path, str] = field(default_factory=dict, repr=False)
    _temporary_paths: dict[Path, tuple[int, int]] = field(default_factory=dict, repr=False)
    _lock_identities: dict[Path, tuple[int, int]] = field(default_factory=dict, repr=False)
    _complete_locks: set[Path] = field(default_factory=set, repr=False)
    _released: bool = field(default=False, repr=False)
    _next_ordinal: int = field(default=1, repr=False)
    _name_limit: int | None = field(default=None, repr=False)

    @property
    def project_dir(self) -> Path:
        """Compatibility alias for callers migrating from the former API."""
        return self.cwd

    def _lock_path(self, filename: str) -> Path:
        # Locks live in the target directory, so only the stem identifies them.
        # Conservatively share reservations on case/normalization equivalents,
        # including macOS volumes whose rules differ from os.path.normcase().
        canonical = unicodedata.normalize("NFC", Path(filename).stem)
        canonical = unicodedata.normalize("NFC", canonical.casefold())
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
        return self.output_dir / f".codex-image-{digest}.lock"

    def _acquire(self, filename: str) -> Path:
        if self._released:
            raise ImageToolError("图片保存预留已经释放，不能继续使用。")
        lock_path = self._lock_path(filename)
        token = f"{os.getpid()}:{uuid4().hex}"
        with lock_path.open("xb") as handle:
            self._locks[lock_path] = token
            identity = _identity(lock_path)
            if identity is not None:
                self._lock_identities[lock_path] = identity
            encoded = token.encode("ascii")
            if handle.write(encoded) != len(encoded):
                raise OSError(errno.ENOSPC, "Incomplete lock write")
            handle.flush()
            self._complete_locks.add(lock_path)
        return lock_path

    def _release_lock(self, lock_path: Path) -> None:
        token = self._locks.get(lock_path)
        if token is None:
            return
        try:
            # A replaced lock is no longer ours. Never clear another request's lock.
            identity = _identity(lock_path)
            matches = identity == self._lock_identities.get(lock_path)
            if matches and lock_path in self._complete_locks:
                with lock_path.open("rb") as handle:
                    matches = handle.read(len(token) + 1) == token.encode("ascii")
            if not matches:
                self._locks.pop(lock_path, None)
                self._lock_identities.pop(lock_path, None)
                self._complete_locks.discard(lock_path)
                return
            lock_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return
        self._locks.pop(lock_path, None)
        self._lock_identities.pop(lock_path, None)
        self._complete_locks.discard(lock_path)

    def _remember_temporary(self, path: Path) -> None:
        identity = _identity(path)
        if identity is not None:
            self._temporary_paths[path] = identity

    def _cleanup_temporary(self, path: Path) -> None:
        identity = self._temporary_paths.get(path)
        if identity is None:
            return
        try:
            if _identity(path) == identity:
                path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return
        self._temporary_paths.pop(path, None)

    def _probe_target(self, target: Path) -> None:
        # Check the exact destination name, including filesystem length rules.
        # The cooperating request lock remains held after this empty probe is removed.
        with target.open("xb"):
            self._remember_temporary(target)
        # Another process may replace the probe after close. Never delete its file.
        self._cleanup_temporary(target)
        if target.exists() or target.is_symlink():
            raise FileExistsError(errno.EEXIST, "Destination was replaced or probe could not be removed")

    def _numbered_name(self, ordinal: int, actual_format: str | None = None) -> str:
        name = Path(self.filename)
        suffix = name.suffix
        if actual_format is not None and normalize_format(suffix[1:]) != actual_format:
            suffix = EXTENSIONS[actual_format]
        if ordinal == 1 and suffix == name.suffix:
            return self.filename
        tail = (f"_{ordinal}" if ordinal > 1 else "") + suffix
        stem = name.stem
        while stem:
            candidate = stem + tail
            if _utf16_length(candidate) <= 255 and (
                self._name_limit is None or len(os.fsencode(candidate)) <= self._name_limit
            ):
                return candidate
            stem = stem[:-1]
        raise ImageToolError("输出文件名无法容纳图片序号，请使用更短的 filename。")

    def paths(self, count: int) -> list[Path]:
        """Preview nominal names; actual later names may skip occupied numbers."""
        return [self.output_dir / self._numbered_name(index + 1) for index in range(count)]

    def _next_target(self, actual_format: str) -> Path:
        if self._released:
            raise ImageToolError("图片保存预留已经释放，不能继续使用。")
        ordinal = self._next_ordinal
        if ordinal == 1:
            self._next_ordinal = 2
            name = self._numbered_name(1, actual_format)
            target = self.output_dir / name
            if not target.exists() and not target.is_symlink():
                extra_lock = None
                try:
                    if name != self.filename:
                        # A longer corrected extension may truncate the original stem,
                        # giving this target a different reservation identity.
                        if self._lock_path(name) not in self._locks:
                            extra_lock = self._acquire(name)
                        self._probe_target(target)
                    return target
                except FileExistsError:
                    if extra_lock is not None:
                        self._release_lock(extra_lock)
                    if name == self.filename:
                        raise
                except BaseException:
                    if extra_lock is not None:
                        self._release_lock(extra_lock)
                    raise
            elif name == self.filename:
                raise FileExistsError(errno.EEXIST, "Reserved destination already exists")
            # A corrected-format destination may already exist; preserve it and
            # choose a numbered name for this valid, already generated image.
            ordinal = 2
        for ordinal in range(ordinal, ordinal + 10000):
            name = self._numbered_name(ordinal, actual_format)
            target = self.output_dir / name
            if target.exists() or target.is_symlink():
                continue
            try:
                lock_path = self._acquire(name)
            except FileExistsError:
                continue
            try:
                self._probe_target(target)
            except FileExistsError:
                self._release_lock(lock_path)
                continue
            except BaseException:
                self._release_lock(lock_path)
                raise
            self._next_ordinal = ordinal + 1
            return target
        raise ImageToolError("无法找到可用的图片序号，请更换 filename。")

    def release(self) -> list[dict[str, Any]]:
        """Release only this request's reservations and unfinished temporary files.

        Successful images are never tracked as temporary files. Cleanup is idempotent
        and best-effort so that a cleanup problem cannot replace a successful result.
        Calling it again retries any cleanup that the filesystem temporarily rejected.
        """
        self._released = True
        for path in list(self._temporary_paths):
            self._cleanup_temporary(path)
        for path in list(self._locks):
            self._release_lock(path)
        if self._temporary_paths or self._locks:
            return [{
                "code": "cleanup_incomplete",
                "message": "部分本请求的临时文件或预留锁无法清理，请检查输出目录；已保存图片不受影响。",
                "call_id": None,
            }]
        return []

    def __enter__(self) -> SavePlan:
        return self

    def __exit__(self, *args: Any) -> None:
        self.release()


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
    if re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', filename) or filename.endswith((".", " ")):
        raise ImageToolError("filename 只能是文件名，不能包含目录、控制字符或系统不允许的字符。")
    # Windows also treats "NUL .png" and superscript COM/LPT digits as device names.
    if filename.split(".", 1)[0].rstrip(" .").upper() in _RESERVED_NAMES:
        raise ImageToolError("filename 不能使用 Windows 保留名称。")
    if _utf16_length(filename) > 255:
        raise ImageToolError("filename 太长，不能超过 255 个 UTF-16 单位；表情等字符通常占两个单位。")


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


def resolve_cwd(cwd: str) -> Path:
    if not isinstance(cwd, str) or not cwd.strip():
        raise ImageToolError("请由 Codex 传入当前任务的绝对路径 cwd。")
    try:
        directory = Path(cwd).expanduser()
        if not directory.is_absolute():
            raise ImageToolError("cwd 必须是当前任务的绝对路径。")
        directory = directory.resolve(strict=True)
        if not directory.is_dir():
            raise ImageToolError("cwd 必须指向已有的任务目录。")
    except (OSError, RuntimeError, ValueError):
        raise ImageToolError("无法访问 cwd，请检查任务目录是否存在。") from None
    return directory


def resolve_project(project_dir: str) -> Path:
    """Compatibility alias; paths now refer to the current task directory."""
    return resolve_cwd(project_dir)


def resolve_project_path(project_dir: Path, value: str, *, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ImageToolError(f"{label} 必须是非空路径。")
    try:
        path = Path(value).expanduser()
        if path.is_absolute():
            return path.resolve()
        path = (project_dir / path).resolve()
        if not path.is_relative_to(project_dir):
            raise ImageToolError(f"相对 {label} 不能指向当前任务目录之外；请明确提供绝对路径。")
        return path
    except (OSError, RuntimeError, ValueError):
        raise ImageToolError(f"{label} 不是可用的文件系统路径。") from None


def prepare_save(
    cwd: str,
    output_dir: str | None = None,
    filename: str | None = None,
    output_format: str | None = None,
) -> SavePlan:
    directory_root = resolve_cwd(cwd)
    image_format = resolve_format(output_format, filename)
    directory = resolve_project_path(
        directory_root, "." if output_dir is None else output_dir, label="output_dir"
    )
    if filename is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"image_{stamp}_{uuid4().hex[:12]}{EXTENSIONS[image_format]}"
    elif not Path(filename).suffix:
        filename += EXTENSIONS[image_format]
    _validate_filename(filename)
    plan = SavePlan(directory_root, directory, filename, image_format)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        plan._name_limit = _filename_limit(directory)
        target = directory / filename
        if target.exists() or target.is_symlink():
            raise ImageToolError("目标文件已存在，请更换 filename；工具不会覆盖已有图片。")
        try:
            plan._acquire(filename)
        except FileExistsError:
            raise ImageToolError("目标文件已由另一个生成请求预留，请等待其完成；不会删除其他请求的锁。") from None
        plan._probe_target(target)
    except BaseException as exc:
        plan.release()
        if isinstance(exc, ImageToolError):
            raise
        if isinstance(exc, (OSError, ValueError)):
            raise ImageToolError("无法预留真实目标文件名，请检查文件名长度、输出路径和目录权限。") from None
        raise
    return plan


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
                width, height = image.size
                has_alpha = "A" in image.getbands() or "transparency" in image.info
                image.verify()
            # Decode pixels as well: verify() alone does not catch every truncated JPEG.
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                has_transparency = (
                    has_alpha and image.convert("RGBA").getchannel("A").getextrema()[0] < 255
                )
    except ImageToolError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageToolError("图片内容损坏、无法解码或像素数量过大。") from None
    return ImageInfo(image_format, width, height, has_alpha, has_transparency)


def read_input_image(project_dir: Path, path: str) -> tuple[bytes, ImageInfo]:
    source = resolve_project_path(project_dir, path, label="参考图片路径")
    descriptor = None
    try:
        info = source.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ImageToolError("参考图片或蒙版必须是普通文件，不能是目录、管道或设备。")
        if info.st_size >= MAX_INPUT_BYTES:
            raise ImageToolError("参考图片或蒙版必须小于 50 MB。")
        # Recheck the opened object as well as the path. O_NONBLOCK prevents a
        # replaced FIFO from trapping an uncancellable worker thread on POSIX.
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(source, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ImageToolError("参考图片或蒙版必须是普通文件，不能是目录、管道或设备。")
        if opened.st_size >= MAX_INPUT_BYTES:
            raise ImageToolError("参考图片或蒙版必须小于 50 MB。")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None  # Ownership transferred to the stream.
            data = stream.read(MAX_INPUT_BYTES)
    except OSError:
        raise ImageToolError("无法读取参考图片或蒙版，请检查路径及文件权限。") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return data, inspect_image(data, input_image=True)


def _write_temporary(plan: SavePlan, data: bytes) -> Path:
    with tempfile.NamedTemporaryFile(
        mode="wb", prefix=".codex-image-", suffix=".tmp", dir=plan.output_dir, delete=False
    ) as stream:
        path = Path(stream.name)
        plan._remember_temporary(path)
        if stream.write(data) != len(data):
            raise OSError(errno.ENOSPC, "Incomplete image write")
        stream.flush()
        os.fsync(stream.fileno())
    return path


def _publish_temporary(plan: SavePlan, temporary: Path, target: Path) -> None:
    if os.name == "nt":
        # On Windows os.rename fails if the destination already exists.
        os.rename(temporary, target)
        plan._temporary_paths.pop(temporary, None)
        return
    try:
        # Hard-link publication is atomic and never replaces an existing destination.
        os.link(temporary, target)
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno not in _UNSUPPORTED_LINK_ERRORS:
            raise
        # Filesystems without hard links still get a complete staged image first,
        # followed by an exclusive target write, with partial targets cleaned up.
        with target.open("xb") as output:
            plan._remember_temporary(target)
            with temporary.open("rb") as source:
                shutil.copyfileobj(source, output)
            output.flush()
            os.fsync(output.fileno())
        plan._temporary_paths.pop(target, None)
    plan._cleanup_temporary(temporary)


def save_images(plan: SavePlan, images: list[PreparedImage]) -> SaveResult:
    """Save independent completed items; later failures never remove earlier assets."""
    if plan._released:
        raise ImageToolError("图片保存预留已经释放，不能继续使用。")
    result = SaveResult()
    for index, image in enumerate(images):
        if image.info.format not in FORMATS:
            result.errors.append({
                "code": "unsupported_format",
                "message": "返回图片不是可保存的 PNG、JPEG 或 WebP 格式，该图片未保存。",
                "call_id": image.call_id,
                "index": index,
            })
            continue
        try:
            metadata = dict(image.metadata)
            target = plan._next_target(image.info.format)
            temporary = _write_temporary(plan, image.data)
            _publish_temporary(plan, temporary, target)
        except (OSError, ImageToolError, ValueError, TypeError) as exc:
            # Failed writes can leave owned temporary/partial files, but successful
            # final assets were removed from that tracking set before this point.
            for path in list(plan._temporary_paths):
                plan._cleanup_temporary(path)
            result.errors.append({
                "code": "target_exists" if isinstance(exc, FileExistsError) else "save_failed",
                "message": (
                    "目标文件已存在，该图片未保存，原文件未覆盖。"
                    if isinstance(exc, FileExistsError)
                    else "该图片保存失败，请检查输出目录及可用空间；其他已保存图片仍保留。"
                ),
                "call_id": image.call_id,
                "index": index,
            })
            continue
        if image.info.format != plan.output_format:
            result.warnings.append(
                f"第 {index + 1} 张图片的实际格式为 {image.info.format}，与请求的 {plan.output_format} 不同；已按真实格式另存为 {target.name}。"
            )
        result.images.append(GeneratedImage(
            target, image.data, image.info.mime_type, image.info.width, image.info.height,
            image.revised_prompt, image.call_id, metadata,
        ))
    return result
