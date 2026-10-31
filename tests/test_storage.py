from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from gpt_image_generation.errors import ImageToolError
from gpt_image_generation.storage import (
    PreparedImage, inspect_image, prepare_save, read_input_image,
    resolve_format, resolve_project, save_images,
)


def image_bytes(fmt: str = "png", size: tuple[int, int] = (16, 24), alpha: bool = False) -> bytes:
    buffer = io.BytesIO()
    image = Image.new("RGBA" if alpha else "RGB", size, (30, 70, 120, 0) if alpha else (30, 70, 120))
    image.save(buffer, format=fmt)
    return buffer.getvalue()


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()

    def test_format_defaults_and_filename_inference(self):
        for filename, expected in [(None, "png"), ("picture.PNG", "png"), ("picture.JPG", "jpeg"), ("picture.jpeg", "jpeg"), ("picture.webp", "webp")]:
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

    def test_filename_validation(self):
        for filename in ["../escape.png", r"folder\image.png", "CON.png", "AUX", "LPT2.jpg", "image:stream.png", "bad?.png", "bad. ", ""]:
            with self.subTest(filename=filename), self.assertRaises(ImageToolError):
                prepare_save(str(self.project), filename=filename)

    def test_project_must_be_existing_absolute_directory(self):
        with self.assertRaises(ImageToolError):
            resolve_project("relative-project")
        with self.assertRaises(ImageToolError):
            resolve_project(str(self.project / "missing"))

    def test_default_directory_and_unique_names(self):
        first = prepare_save(str(self.project))
        second = prepare_save(str(self.project))
        self.assertEqual(first.output_dir, self.project / "outputs" / "images")
        self.assertNotEqual(first.filename, second.filename)
        self.assertTrue(first.filename.endswith(".png"))

    def test_relative_and_absolute_output_paths(self):
        plan = prepare_save(str(self.project), "public/images", "hero", "webp")
        self.assertEqual(plan.output_dir, self.project / "public" / "images")
        self.assertEqual(plan.filename, "hero.webp")
        with tempfile.TemporaryDirectory() as other:
            explicit = prepare_save(str(self.project), other, "external.jpg")
            self.assertEqual(explicit.output_dir, Path(other).resolve())

    def test_relative_output_cannot_escape_project(self):
        with self.assertRaisesRegex(ImageToolError, "项目之外"):
            prepare_save(str(self.project), "../escape")

    def test_existing_filename_is_rejected_before_generation(self):
        (self.project / "existing.png").write_bytes(b"original")
        with self.assertRaisesRegex(ImageToolError, "已存在"):
            prepare_save(str(self.project), ".", "existing.png")
        self.assertEqual((self.project / "existing.png").read_bytes(), b"original")

    def test_formats_verified_from_real_content(self):
        for fmt in ["png", "jpeg", "webp"]:
            with self.subTest(fmt=fmt):
                info = inspect_image(image_bytes(fmt))
                self.assertEqual(info.format, fmt)
                self.assertEqual((info.width, info.height), (16, 24))
                self.assertEqual(info.mime_type, f"image/{fmt}")

    def test_alpha_channel_detected(self):
        self.assertTrue(inspect_image(image_bytes(alpha=True)).has_alpha)
        self.assertFalse(inspect_image(image_bytes("jpeg")).has_alpha)

    def test_invalid_content_and_truncation_rejected(self):
        for data in [b"", b"not an image", image_bytes("jpeg")[:100], image_bytes()[:30]]:
            with self.subTest(size=len(data)), self.assertRaises(ImageToolError):
                inspect_image(data)

    def test_read_reference_relative_to_project(self):
        data = image_bytes()
        (self.project / "reference.png").write_bytes(data)
        loaded, info = read_input_image(self.project, "reference.png")
        self.assertEqual(loaded, data)
        self.assertEqual(info.format, "png")
        with self.assertRaises(ImageToolError):
            read_input_image(self.project, "missing.png")

    def test_multiple_images_receive_indexed_names(self):
        data = image_bytes()
        image = PreparedImage(data, inspect_image(data), "revised prompt")
        plan = prepare_save(str(self.project), filename="asset.png")
        results = save_images(plan, [image, image])
        self.assertEqual([result.path.name for result in results], ["asset_1.png", "asset_2.png"])
        for result in results:
            self.assertEqual(result.path.read_bytes(), data)
            self.assertEqual(result.revised_prompt, "revised prompt")
            self.assertEqual(result.mime_type, "image/png")

    def test_actual_format_mismatch_saves_nothing(self):
        data = image_bytes("jpeg")
        plan = prepare_save(str(self.project), filename="asset.png")
        with self.assertRaisesRegex(ImageToolError, "实际格式"):
            save_images(plan, [PreparedImage(data, inspect_image(data))])
        self.assertEqual(list(plan.output_dir.iterdir()), [])

    def test_race_existing_target_is_not_overwritten(self):
        data = image_bytes()
        plan = prepare_save(str(self.project), filename="asset.png")
        target = plan.paths(1)[0]
        target.write_bytes(b"created concurrently")
        with self.assertRaises(ImageToolError):
            save_images(plan, [PreparedImage(data, inspect_image(data))])
        self.assertEqual(target.read_bytes(), b"created concurrently")

    def test_batch_failure_rolls_back_only_new_files(self):
        data = image_bytes()
        image = PreparedImage(data, inspect_image(data))
        plan = prepare_save(str(self.project), filename="asset.png")
        untouched = plan.output_dir / "existing.png"
        untouched.write_bytes(b"original")
        original_open = Path.open

        def failing_open(path, *args, **kwargs):
            if path.name == "asset_2.png" and args and args[0] == "xb":
                raise PermissionError("disk failed")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", failing_open), self.assertRaisesRegex(ImageToolError, "已清理"):
            save_images(plan, [image, image])
        self.assertFalse((plan.output_dir / "asset_1.png").exists())
        self.assertEqual(untouched.read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()
