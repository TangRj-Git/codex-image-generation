"""Storage regression tests; all fixtures are local and no API is called."""

from __future__ import annotations

import errno
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from gpt_image_generation import storage
from gpt_image_generation.errors import ImageToolError
from gpt_image_generation.storage import (
    GeneratedImage, ImageInfo, PreparedImage, SaveResult, inspect_image, prepare_save,
    read_input_image, resolve_cwd, resolve_format, resolve_project, save_images,
)


def image_bytes(fmt: str = "png", size: tuple[int, int] = (16, 24), alpha: int | None = None) -> bytes:
    buffer = io.BytesIO()
    color = (30, 70, 120, alpha) if alpha is not None else (30, 70, 120)
    Image.new("RGBA" if alpha is not None else "RGB", size, color).save(buffer, format=fmt)
    return buffer.getvalue()


def prepared(fmt: str = "png", call_id: str = "image-call") -> PreparedImage:
    data = image_bytes(fmt)
    return PreparedImage(data, inspect_image(data), "revised prompt", call_id, {"quality": "high"})


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cwd = Path(self.temp.name).resolve()

    def plan(self, **kwargs):
        plan = prepare_save(str(self.cwd), **kwargs)
        self.addCleanup(plan.release)
        return plan

    def test_format_defaults_and_filename_inference(self):
        for filename, expected in [
            (None, "png"), ("picture.PNG", "png"), ("picture.JPG", "jpeg"),
            ("picture.jpeg", "jpeg"), ("picture.webp", "webp"),
        ]:
            with self.subTest(filename=filename):
                self.assertEqual(resolve_format(None, filename), expected)
        self.assertEqual(resolve_format("jpg", "picture.jpeg"), "jpeg")

    def test_format_and_extension_conflict(self):
        with self.assertRaisesRegex(ImageToolError, "不一致"):
            resolve_format("png", "photo.jpg")

    def test_reject_non_raster_output(self):
        for filename in ["vector.svg", "movie.gif", "asset.bmp"]:
            with self.subTest(filename=filename), self.assertRaises(ImageToolError):
                resolve_format(None, filename)

    def test_filename_validation_including_windows_variants(self):
        for filename in [
            "../escape.png", r"folder\image.png", "CON.png", "AUX", "LPT2.jpg",
            "NUL .png", "AUX .part.png", "COM¹.png", "LPT².webp",
            "image:stream.png", "bad?.png", "bad. ", "", "bad\x7f.png",
        ]:
            with self.subTest(filename=filename), self.assertRaises(ImageToolError):
                prepare_save(str(self.cwd), filename=filename)
        self.assertEqual(list(self.cwd.iterdir()), [])

    def test_utf16_length_counts_non_bmp_characters(self):
        with self.assertRaisesRegex(ImageToolError, "UTF-16"):
            prepare_save(str(self.cwd), filename="\U0001f680" * 126 + ".png")
        self.assertEqual(resolve_format(None, "\U0001f680" * 125 + ".png"), "png")
        with self.assertRaises(ImageToolError):
            resolve_format(None, "\ud800.png")

    def test_added_extension_is_included_in_length_limit(self):
        with self.assertRaisesRegex(ImageToolError, "UTF-16"):
            prepare_save(str(self.cwd), filename="a" * 253, output_format="png")

    def test_cwd_must_be_existing_absolute_directory(self):
        for cwd in ["relative-task", str(self.cwd / "missing")]:
            with self.subTest(cwd=cwd), self.assertRaises(ImageToolError):
                resolve_cwd(cwd)
        self.assertEqual(resolve_project(str(self.cwd)), self.cwd)

    def test_default_is_directly_current_directory_with_unique_names(self):
        first = self.plan()
        second = self.plan()
        self.assertEqual(first.output_dir, self.cwd)
        self.assertNotEqual(first.filename, second.filename)
        self.assertFalse((self.cwd / "outputs").exists())
        self.assertFalse((self.cwd / first.filename).exists())
        self.assertTrue(first.filename.endswith(".png"))

    def test_relative_and_explicit_absolute_output_paths(self):
        plan = self.plan(output_dir="public/images", filename="hero", output_format="webp")
        self.assertEqual(plan.output_dir, self.cwd / "public" / "images")
        self.assertEqual(plan.filename, "hero.webp")
        with tempfile.TemporaryDirectory() as other:
            external = prepare_save(str(self.cwd), other, "external.jpg")
            try:
                self.assertEqual(external.output_dir, Path(other).resolve())
            finally:
                external.release()

    def test_empty_or_escaping_relative_output_is_rejected(self):
        for output_dir in ["", "../escape"]:
            with self.subTest(output_dir=output_dir), self.assertRaises(ImageToolError):
                prepare_save(str(self.cwd), output_dir=output_dir)

    def test_existing_file_rejected_without_any_change(self):
        target = self.cwd / "existing.png"
        target.write_bytes(b"original")
        with self.assertRaisesRegex(ImageToolError, "已存在"):
            prepare_save(str(self.cwd), filename="existing.png")
        self.assertEqual(target.read_bytes(), b"original")
        self.assertEqual(list(self.cwd.glob(".codex-image-*.lock")), [])

    def test_second_same_filename_is_rejected_before_release(self):
        first = self.plan(filename="asset.png")
        first_locks = list(self.cwd.glob(".codex-image-*.lock"))
        with self.assertRaisesRegex(ImageToolError, "预留"):
            prepare_save(str(self.cwd), filename="asset.png")
        self.assertEqual(list(self.cwd.glob(".codex-image-*.lock")), first_locks)
        first.release()
        second = self.plan(filename="asset.png")
        self.assertEqual(second.filename, "asset.png")

    def test_reservation_is_shared_across_image_formats(self):
        self.plan(filename="asset.png")
        with self.assertRaisesRegex(ImageToolError, "预留"):
            prepare_save(str(self.cwd), filename="asset.jpg")

    @unittest.skipUnless(os.name == "nt", "Windows filenames are case-insensitive")
    def test_reservation_normalizes_windows_case(self):
        self.plan(filename="Asset.PNG")
        with self.assertRaisesRegex(ImageToolError, "预留"):
            prepare_save(str(self.cwd), output_dir=".", filename="asset.png")

    def test_reservation_blocks_another_process(self):
        self.plan(filename="asset.png")
        script = (
            "import importlib.util,sys;"
            "spec=importlib.util.spec_from_file_location('gpt_image_generation.storage',sys.argv[1]);"
            "module=importlib.util.module_from_spec(spec);"
            "sys.modules[spec.name]=module;spec.loader.exec_module(module)\n"
            "try:\n"
            " plan=module.prepare_save(sys.argv[2],filename='asset.png')\n"
            "except module.ImageToolError:\n"
            " sys.exit(7)\n"
            "else:\n"
            " plan.release();sys.exit(0)\n"
        )
        process = subprocess.run(
            [sys.executable, "-B", "-c", script, storage.__file__, str(self.cwd)],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(process.returncode, 7, process.stderr)

    def test_release_is_idempotent_and_leaves_no_probe_file(self):
        plan = self.plan(filename="asset.png")
        self.assertFalse((self.cwd / "asset.png").exists())
        self.assertEqual(plan.release(), [])
        self.assertEqual(plan.release(), [])
        self.assertEqual(list(self.cwd.iterdir()), [])

    def test_release_does_not_remove_a_replaced_foreign_lock(self):
        plan = self.plan(filename="asset.png")
        lock = next(self.cwd.glob(".codex-image-*.lock"))
        lock.write_bytes(b"another-request-token")
        self.assertEqual(plan.release(), [])
        self.assertEqual(lock.read_bytes(), b"another-request-token")

    def test_exact_filename_preflight_failure_cleans_reservation(self):
        original_open = Path.open

        def fail_exact_name(path, *args, **kwargs):
            if path.name == "asset.png" and args and args[0] == "xb":
                raise OSError(errno.ENAMETOOLONG, "fixture actual filename limit")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", fail_exact_name), self.assertRaisesRegex(ImageToolError, "真实目标"):
            prepare_save(str(self.cwd), filename="asset.png")
        self.assertEqual(list(self.cwd.iterdir()), [])

    def test_partial_lock_write_failure_cleans_own_lock(self):
        original_open = Path.open

        class BrokenWriter:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()

            def write(self, value):
                self.stream.write(value[:3])
                self.stream.flush()
                raise OSError(errno.ENOSPC, "fixture lock write failure")

        def fail_lock_write(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            if path.suffix == ".lock" and args and args[0] == "xb":
                return BrokenWriter(stream)
            return stream

        with patch.object(Path, "open", fail_lock_write), self.assertRaises(ImageToolError):
            prepare_save(str(self.cwd), filename="asset.png")
        self.assertEqual(list(self.cwd.iterdir()), [])

    def test_formats_verified_from_content(self):
        for fmt in ["png", "jpeg", "webp"]:
            with self.subTest(fmt=fmt):
                info = inspect_image(image_bytes(fmt))
                self.assertEqual(info.format, fmt)
                self.assertEqual((info.width, info.height), (16, 24))
                self.assertEqual(info.mime_type, f"image/{fmt}")

    def test_alpha_presence_and_actual_transparency_are_distinct(self):
        opaque = inspect_image(image_bytes(alpha=255))
        transparent = inspect_image(image_bytes(alpha=0))
        partial = inspect_image(image_bytes(alpha=254))
        self.assertTrue(opaque.has_alpha)
        self.assertFalse(opaque.has_transparency)
        self.assertTrue(transparent.has_alpha)
        self.assertTrue(transparent.has_transparency)
        self.assertTrue(partial.has_transparency)
        jpeg = inspect_image(image_bytes("jpeg"))
        self.assertFalse(jpeg.has_alpha)
        self.assertFalse(jpeg.has_transparency)

    def test_palette_transparency_must_be_present_in_actual_pixels(self):
        image = Image.new("P", (2, 2), 1)
        image.putpalette([0, 0, 0, 255, 255, 255] + [0] * 762)
        opaque = io.BytesIO()
        image.save(opaque, "PNG", transparency=0)
        info = inspect_image(opaque.getvalue())
        self.assertTrue(info.has_alpha)
        self.assertFalse(info.has_transparency)
        image.putpixel((0, 0), 0)
        transparent = io.BytesIO()
        image.save(transparent, "PNG", transparency=0)
        self.assertTrue(inspect_image(transparent.getvalue()).has_transparency)

    def test_invalid_and_truncated_content_is_rejected(self):
        for data in [b"", b"not an image", image_bytes("jpeg")[:100], image_bytes()[:30]]:
            with self.subTest(size=len(data)), self.assertRaises(ImageToolError):
                inspect_image(data)

    def test_animated_png_is_rejected(self):
        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), "red").save(
            buffer, "PNG", save_all=True, append_images=[Image.new("RGB", (4, 4), "blue")],
        )
        with self.assertRaisesRegex(ImageToolError, "静态"):
            inspect_image(buffer.getvalue())

    def test_input_and_output_byte_limits_are_enforced(self):
        data = image_bytes()
        with patch.object(storage, "MAX_INPUT_BYTES", len(data)):
            with self.assertRaisesRegex(ImageToolError, "50 MB"):
                inspect_image(data, input_image=True)
        with patch.object(storage, "MAX_OUTPUT_BYTES", len(data)):
            with self.assertRaisesRegex(ImageToolError, "100 MiB"):
                inspect_image(data)

    def test_reference_paths_are_relative_to_cwd(self):
        data = image_bytes()
        (self.cwd / "reference.png").write_bytes(data)
        loaded, info = read_input_image(self.cwd, "reference.png")
        self.assertEqual(loaded, data)
        self.assertEqual(info.format, "png")
        for name in ["missing.png", "../outside.png"]:
            with self.subTest(name=name), self.assertRaises(ImageToolError):
                read_input_image(self.cwd, name)

    def test_first_image_uses_original_name_and_later_images_use_suffix(self):
        item = prepared()
        plan = self.plan(filename="asset.png")
        result = save_images(plan, [item, item])
        self.assertIsInstance(result, SaveResult)
        self.assertEqual(result.errors, [])
        self.assertEqual([image.path.name for image in result.images], ["asset.png", "asset_2.png"])
        for image in result.images:
            self.assertEqual(image.path.read_bytes(), item.data)
            self.assertEqual(image.revised_prompt, "revised prompt")
            self.assertEqual(image.call_id, "image-call")
            self.assertEqual(image.metadata, {"quality": "high"})
        plan.release()
        with self.assertRaisesRegex(ImageToolError, "已存在"):
            prepare_save(str(self.cwd), filename="asset.png")

    def test_numbered_images_skip_existing_and_reserved_names(self):
        (self.cwd / "asset_2.png").write_bytes(b"original")
        other = self.plan(filename="asset_3.webp")
        plan = self.plan(filename="asset.png")
        result = save_images(plan, [prepared(), prepared()])
        self.assertEqual([item.path.name for item in result.images], ["asset.png", "asset_4.png"])
        self.assertEqual((self.cwd / "asset_2.png").read_bytes(), b"original")
        plan.release()
        with self.assertRaisesRegex(ImageToolError, "预留"):
            prepare_save(str(self.cwd), filename="asset_3.png")
        other.release()

    def test_real_format_gets_correct_extension_and_warning(self):
        item = prepared("jpeg")
        result = save_images(self.plan(filename="asset.png"), [item])
        self.assertEqual(result.errors, [])
        self.assertEqual(result.images[0].path.name, "asset.jpg")
        self.assertEqual(result.images[0].mime_type, "image/jpeg")
        self.assertEqual(result.images[0].path.read_bytes(), item.data)
        self.assertFalse((self.cwd / "asset.png").exists())
        self.assertTrue(any("jpeg" in warning and "png" in warning for warning in result.warnings))

    def test_corrected_format_does_not_overwrite_existing_file(self):
        existing = self.cwd / "asset.jpg"
        existing.write_bytes(b"original")
        result = save_images(self.plan(filename="asset.png"), [prepared("jpeg")])
        self.assertEqual(result.errors, [])
        self.assertEqual(result.images[0].path.name, "asset_2.jpg")
        self.assertEqual(existing.read_bytes(), b"original")

    def test_existing_target_created_after_preflight_is_not_overwritten(self):
        plan = self.plan(filename="asset.png")
        target = self.cwd / "asset.png"
        target.write_bytes(b"created outside this server")
        result = save_images(plan, [prepared()])
        self.assertEqual(result.images, [])
        self.assertEqual(result.errors[0]["code"], "target_exists")
        self.assertEqual(target.read_bytes(), b"created outside this server")
        plan.release()
        self.assertEqual(target.read_bytes(), b"created outside this server")

    def test_later_write_failure_keeps_successful_images(self):
        plan = self.plan(filename="asset.png")
        original = storage._write_temporary
        calls = 0

        def fail_second(current_plan, data):
            nonlocal calls
            calls += 1
            if calls == 2:
                partial = current_plan.output_dir / ".codex-image-fixture.tmp"
                partial.write_bytes(b"partial")
                current_plan._remember_temporary(partial)
                raise OSError(errno.ENOSPC, "fixture write failure with private detail")
            return original(current_plan, data)

        with patch.object(storage, "_write_temporary", fail_second):
            result = save_images(plan, [
                prepared(call_id="first"), prepared(call_id="second"), prepared(call_id="third"),
            ])
        self.assertEqual([item.path.name for item in result.images], ["asset.png", "asset_3.png"])
        self.assertEqual([error["call_id"] for error in result.errors], ["second"])
        self.assertNotIn("private detail", str(result.errors))
        self.assertEqual(list(self.cwd.glob("*.tmp")), [])
        plan.release()
        for item in result.images:
            self.assertEqual(item.path.read_bytes(), item.data)

    def test_publish_failure_keeps_previous_asset_and_cleans_temporary(self):
        original = storage._publish_temporary

        def fail_second(plan, temporary, target):
            if target.name == "asset_2.png":
                raise PermissionError("fixture publish failure")
            return original(plan, temporary, target)

        plan = self.plan(filename="asset.png")
        with patch.object(storage, "_publish_temporary", fail_second):
            result = save_images(plan, [prepared(call_id="first"), prepared(call_id="second")])
        self.assertEqual([image.call_id for image in result.images], ["first"])
        self.assertEqual(result.errors[0]["call_id"], "second")
        self.assertTrue((self.cwd / "asset.png").exists())
        self.assertFalse((self.cwd / "asset_2.png").exists())
        self.assertEqual(list(self.cwd.glob("*.tmp")), [])

    def test_use_after_release_is_rejected(self):
        plan = self.plan(filename="asset.png")
        plan.release()
        with self.assertRaises(ImageToolError):
            save_images(plan, [prepared()])

    def test_metadata_defaults_do_not_share_mutable_objects(self):
        info = ImageInfo("png", 1, 1, False)
        first = PreparedImage(b"x", info)
        second = PreparedImage(b"x", info)
        first.metadata["one"] = 1
        self.assertEqual(second.metadata, {})
        saved = GeneratedImage(self.cwd / "x.png", b"x", "image/png", 1, 1)
        self.assertEqual(saved.metadata, {})


if __name__ == "__main__":
    unittest.main()
