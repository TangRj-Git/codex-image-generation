"""Offline checks for release contents and isolation of package smoke tests."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile


def load_script(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = load_script("build_release")
smoke = load_script("smoke_wheel")
NAME = "gpt-image-generation"
MODULE = "gpt_image_generation"
VERSION = "0.3.0"
DIST_INFO = f"{MODULE}-{VERSION}.dist-info"
METADATA = f"Metadata-Version: 2.3\nName: {NAME}\nVersion: {VERSION}\n".encode()


def make_wheel(directory: Path, extra=None) -> Path:
    path = directory / f"{MODULE}-{VERSION}-py3-none-any.whl"
    members = {
        f"{MODULE}/__init__.py": b"# fixture\n",
        f"{MODULE}/__main__.py": b"# fixture\n",
        f"{DIST_INFO}/METADATA": METADATA,
        f"{DIST_INFO}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{DIST_INFO}/entry_points.txt": b"[console_scripts]\ngpt-image-generation = gpt_image_generation:main\n",
        f"{DIST_INFO}/RECORD": b"",
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in {**members, **(extra or {})}.items():
            archive.writestr(name, data)
    return path


def make_sdist(directory: Path, extra=None) -> Path:
    path = directory / f"{MODULE}-{VERSION}.tar.gz"
    members = {
        "pyproject.toml": b"[project]\n",
        "README.md": b"Fixture documentation\n",
        "PKG-INFO": METADATA,
        f"src/{MODULE}/__init__.py": b"# fixture\n",
        f"src/{MODULE}/__main__.py": b"# fixture\n",
    }
    with tarfile.open(path, "w:gz") as archive:
        for name, data in {**members, **(extra or {})}.items():
            entry = tarfile.TarInfo(f"{MODULE}-{VERSION}/{name}")
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    return path


class DistributionTests(unittest.TestCase):
    def test_allows_uv_original_project_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            sdist = make_sdist(Path(directory), {"pyproject.toml.orig": b"[project]\n"})
            files = release.validate_sdist(sdist, NAME, MODULE, VERSION)
            self.assertIn("pyproject.toml.orig", files)

    def test_rejects_unsafe_paths_on_every_os(self):
        for name in ("/etc/config", "../auth.json", "a/../secret", "C:/auth.json", "a\\config", "a//config", "a/./config", ""):
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.safe_member(name)

    def test_rejects_local_credentials_and_generated_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("auth.json", ".env", "sunny-anime-puppy.png", f"{MODULE}/secret.png"):
                with self.subTest(wheel=name), self.assertRaises(ValueError):
                    release.validate_wheel(make_wheel(root, {name: b"local-only"}), NAME, MODULE, VERSION)
            for name in ("auth.json", ".codex/config.toml", ".env", "private-generation.png", "outputs/image.png", "src/gpt_image_generation/config.toml"):
                with self.subTest(sdist=name), self.assertRaises(ValueError):
                    release.validate_sdist(make_sdist(root, {name: b"local-only"}), NAME, MODULE, VERSION)

    def test_rejects_symlink_members_without_extracting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel = make_wheel(root)
            with zipfile.ZipFile(wheel, "a") as archive:
                link = zipfile.ZipInfo(f"{MODULE}/link.py")
                link.create_system = 3
                link.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(link, "../../auth.json")
            with self.assertRaisesRegex(ValueError, "Symlink"):
                release.validate_wheel(wheel, NAME, MODULE, VERSION)
            sdist = root / "link.tar.gz"
            with tarfile.open(sdist, "w:gz") as archive:
                link = tarfile.TarInfo(f"{MODULE}-{VERSION}/src/{MODULE}/link.py")
                link.type = tarfile.SYMTYPE
                link.linkname = "../../auth.json"
                archive.addfile(link)
            with self.assertRaisesRegex(ValueError, "Non-regular"):
                release.validate_sdist(sdist, NAME, MODULE, VERSION)

    def test_rejects_wrong_version_and_nonportable_wheel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrong_metadata = METADATA.replace(b"Version: 0.3.0", b"Version: 0.2.0")
            with self.assertRaisesRegex(ValueError, "metadata differs"):
                release.validate_wheel(make_wheel(root, {f"{DIST_INFO}/METADATA": wrong_metadata}), NAME, MODULE, VERSION)
            with self.assertRaisesRegex(ValueError, "portable"):
                release.validate_wheel(make_wheel(root, {f"{DIST_INFO}/WHEEL": b"Root-Is-Purelib: false\nTag: cp311-cp311-win_amd64\n"}), NAME, MODULE, VERSION)

    def test_release_bundle_checksums_and_other_versions_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text(f'[project]\nname="{NAME}"\nversion="{VERSION}"\n', encoding="utf-8")
            (root / "docs").mkdir()
            (root / "docs" / "INSTALL.md").write_text("Install the local wheel.\n", encoding="utf-8")
            (root / "examples").mkdir()
            (root / "examples" / "codex-uvx.toml").write_text('command="uv"\n', encoding="utf-8")
            destination = root / "dist"
            destination.mkdir()
            previous = destination / "previous-release.zip"
            previous.write_bytes(b"preserve this release")

            def fake_build(command, **kwargs):
                built = Path(command[command.index("--out-dir") + 1])
                make_wheel(built)
                make_sdist(built)

            with patch.object(release.shutil, "which", return_value="uv"), patch.object(release.subprocess, "run", side_effect=fake_build):
                paths = release.build_release(root, destination)
            self.assertEqual(len(paths), 4)
            self.assertEqual(previous.read_bytes(), b"preserve this release")
            external = destination / f"SHA256SUMS-{VERSION}.txt"
            for line in external.read_text(encoding="utf-8").splitlines():
                digest, filename = line.split("  ")
                self.assertEqual(digest, hashlib.sha256((destination / filename).read_bytes()).hexdigest())
            with zipfile.ZipFile(destination / f"{NAME}-{VERSION}.zip") as archive:
                self.assertEqual(set(archive.namelist()), {
                    f"{MODULE}-{VERSION}-py3-none-any.whl", f"{MODULE}-{VERSION}.tar.gz",
                    "INSTALL.md", "codex-uvx.toml", "SHA256SUMS",
                })
                for line in archive.read("SHA256SUMS").decode().splitlines():
                    digest, filename = line.split("  ")
                    self.assertEqual(digest, hashlib.sha256(archive.read(filename)).hexdigest())

    def test_failed_validation_does_not_replace_existing_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text(f'[project]\nname="{NAME}"\nversion="{VERSION}"\n', encoding="utf-8")
            (root / "docs").mkdir()
            (root / "docs" / "INSTALL.md").touch()
            (root / "examples").mkdir()
            (root / "examples" / "codex-uvx.toml").touch()
            destination = root / "dist"
            destination.mkdir()
            old = destination / f"{MODULE}-{VERSION}-py3-none-any.whl"
            old.write_bytes(b"known release")

            def fake_build(command, **kwargs):
                built = Path(command[command.index("--out-dir") + 1])
                make_wheel(built, {"auth.json": b"must not ship"})
                make_sdist(built)

            with patch.object(release.shutil, "which", return_value="uv"), patch.object(release.subprocess, "run", side_effect=fake_build), self.assertRaises(ValueError):
                release.build_release(root, destination)
            self.assertEqual(old.read_bytes(), b"known release")

    def test_smoke_environment_does_not_inherit_credentials_or_source_paths(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "OPENAI_API_KEY": "actual-secret", "UNKNOWN_PROVIDER_TOKEN": "actual-secret",
            "CODEX_HOME": "/real/codex", "PYTHONPATH": "/real/source", "PATH": "/bin",
            "UV_TOOL_DIR": "/real/tools", "UV_TOOL_BIN_DIR": "/real/bin",
        }, clear=True):
            env = smoke.isolated_environment(Path(directory))
            self.assertNotIn("actual-secret", env.values())
            self.assertNotIn("PYTHONPATH", env)
            self.assertEqual(env["PATH"], "/bin")
            self.assertTrue(Path(env["CODEX_HOME"]).is_relative_to(directory))
            self.assertTrue(Path(env["UV_TOOL_DIR"]).is_relative_to(directory))
            self.assertTrue(Path(env["UV_TOOL_BIN_DIR"]).is_relative_to(directory))


if __name__ == "__main__":
    unittest.main()
