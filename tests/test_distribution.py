"""Offline checks for release contents and isolation of package smoke tests."""

from __future__ import annotations

import hashlib
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import textwrap
import unittest
from unittest.mock import patch
import zipfile


def load_script(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def workflow_python(filename: str, marker: str) -> str:
    """Exercise the workflow's actual Python branch logic without a YAML dependency."""
    path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / filename
    if not path.is_file():
        raise unittest.SkipTest("Workflow sources are not included in source distributions")
    lines = path.read_text(encoding="utf-8").splitlines()
    starts = [index for index, line in enumerate(lines) if line.strip() == "# " + marker]
    if len(starts) != 1:
        raise AssertionError(f"Expected one workflow check marker: {marker}")
    start = starts[0]
    end = next(index for index in range(start + 1, len(lines)) if lines[index].strip() == "PY")
    return textwrap.dedent("\n".join(lines[start:end]))


release = load_script("build_release")
smoke = load_script("smoke_wheel")
NAME = "codex-image-generation"
MODULE = "codex_image_generation"
VERSION = "0.3.0"
DIST_INFO = f"{MODULE}-{VERSION}.dist-info"
METADATA = f"Metadata-Version: 2.4\nName: {NAME}\nVersion: {VERSION}\nLicense-Expression: MIT\nLicense-File: LICENSE\n".encode()
LICENSE = b"MIT License\n\nCopyright (c) 2026 TangRuoJie\nFixture license text.\n"
MODULE_INIT = f'__version__ = "{VERSION}"\n'.encode()
PROJECT_METADATA = f'[project]\nname="{NAME}"\nversion="{VERSION}"\n'.encode()


def make_wheel(directory: Path, extra=None, *, omit=()) -> Path:
    path = directory / f"{MODULE}-{VERSION}-py3-none-any.whl"
    members = {
        f"{MODULE}/__init__.py": MODULE_INIT,
        f"{MODULE}/__main__.py": b"# fixture\n",
        f"{DIST_INFO}/METADATA": METADATA,
        f"{DIST_INFO}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{DIST_INFO}/entry_points.txt": b"[console_scripts]\ncodex-image-generation = codex_image_generation:main\n",
        f"{DIST_INFO}/RECORD": b"",
        f"{DIST_INFO}/licenses/LICENSE": LICENSE,
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in {**members, **(extra or {})}.items():
            if name not in omit:
                archive.writestr(name, data)
    return path


def make_sdist(directory: Path, extra=None, *, omit=()) -> Path:
    path = directory / f"{MODULE}-{VERSION}.tar.gz"
    members = {
        "pyproject.toml": PROJECT_METADATA,
        "README.md": b"Fixture documentation\n",
        "PKG-INFO": METADATA,
        "LICENSE": LICENSE,
        f"src/{MODULE}/__init__.py": MODULE_INIT,
        f"src/{MODULE}/__main__.py": b"# fixture\n",
    }
    with tarfile.open(path, "w:gz") as archive:
        for name, data in {**members, **(extra or {})}.items():
            if name in omit:
                continue
            entry = tarfile.TarInfo(f"{MODULE}-{VERSION}/{name}")
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    return path


class DistributionTests(unittest.TestCase):
    def test_release_installation_workflow_selects_tagged_verifier(self):
        code = workflow_python("publish-pypi.yml", "RELEASE_INSTALLATION_CHECK")
        for available, selected in (
            (("smoke_uvx.py", "smoke_wheel.py"), "smoke_uvx.py"),
            (("smoke_wheel.py",), "smoke_wheel.py"),
            ((), None),
        ):
            with self.subTest(available=available), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "release-source"
                (source / "scripts").mkdir(parents=True)
                for name in available:
                    (source / "scripts" / name).touch()
                wheel = root / "dist" / "pypi" / f"{MODULE}-{VERSION}-py3-none-any.whl"
                wheel.parent.mkdir(parents=True)
                wheel.touch()
                with contextlib.chdir(source), patch.dict(os.environ, {"GITHUB_WORKSPACE": str(root), "RELEASE_VERSION": VERSION}), patch(
                    "subprocess.run"
                ) as run, contextlib.redirect_stdout(io.StringIO()) as output:
                    if selected is None:
                        with self.assertRaisesRegex(SystemExit, "no supported installation verifier"):
                            exec(compile(code, "publish-pypi.yml", "exec"), {})
                        run.assert_not_called()
                        continue
                    exec(compile(code, "publish-pypi.yml", "exec"), {})
                run.assert_called_once()
                command = run.call_args.args[0]
                self.assertEqual(command[2], "scripts/" + selected)
                self.assertIn(str(wheel), command)
                self.assertTrue(run.call_args.kwargs["check"])
                if selected == "smoke_uvx.py":
                    self.assertEqual(command[-2:], ["--expected-version", VERSION])
                else:
                    self.assertEqual(command[-2:], ["--wheel", str(wheel)])
                    self.assertIn("uvx is not validated", output.getvalue())

    def test_historical_test_workflow_skips_only_missing_uvx_verifier(self):
        code = workflow_python("test.yml", "OPTIONAL_UVX_CHECK")
        for available in (("smoke_uvx.py", "smoke_wheel.py"), ("smoke_wheel.py",), ()):
            with self.subTest(available=available), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "scripts").mkdir()
                for name in available:
                    (root / "scripts" / name).touch()
                with contextlib.chdir(root), patch.dict(os.environ, {"RELEASE_VERSION": VERSION}), patch(
                    "subprocess.run"
                ) as run, contextlib.redirect_stdout(io.StringIO()) as output:
                    if not available:
                        with self.assertRaisesRegex(SystemExit, "no supported installation verifier"):
                            exec(compile(code, "test.yml", "exec"), {})
                        run.assert_not_called()
                        continue
                    exec(compile(code, "test.yml", "exec"), {})
                if "smoke_uvx.py" in available:
                    run.assert_called_once()
                    self.assertEqual(run.call_args.args[0][2], "scripts/smoke_uvx.py")
                    self.assertEqual(run.call_args.args[0][-2:], ["--expected-version", VERSION])
                else:
                    run.assert_not_called()
                    self.assertIn("uvx-specific check is skipped", output.getvalue())

    def test_accepts_pep639_license_and_community_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel = make_wheel(root)
            with zipfile.ZipFile(wheel, "a") as archive:
                archive.writestr(f"{DIST_INFO}/licenses/", b"")
            files = release.validate_wheel(wheel, NAME, MODULE, VERSION, expected_license=LICENSE)
            self.assertIn(f"{DIST_INFO}/licenses/LICENSE", files)
            sdist = make_sdist(root, {"CONTRIBUTING.md": b"Contribute", "SECURITY.md": b"Report privately"})
            files = release.validate_sdist(sdist, NAME, MODULE, VERSION, expected_license=LICENSE)
            self.assertTrue({"LICENSE", "CONTRIBUTING.md", "SECURITY.md"} <= set(files))

    def test_rejects_missing_or_empty_license(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for builder, validator, path in (
                (make_wheel, release.validate_wheel, f"{DIST_INFO}/licenses/LICENSE"),
                (make_sdist, release.validate_sdist, "LICENSE"),
            ):
                with self.subTest(kind=builder.__name__, missing=True), self.assertRaisesRegex(ValueError, "Incomplete"):
                    validator(builder(root, omit={path}), NAME, MODULE, VERSION)
                with self.subTest(kind=builder.__name__, empty=True), self.assertRaisesRegex(ValueError, "LICENSE is empty"):
                    validator(builder(root, {path: b"\n"}), NAME, MODULE, VERSION)

    def test_rejects_license_metadata_or_contents_that_differ(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for builder, validator, metadata_path in (
                (make_wheel, release.validate_wheel, f"{DIST_INFO}/METADATA"),
                (make_sdist, release.validate_sdist, "PKG-INFO"),
            ):
                for metadata in (
                    METADATA.replace(b"License-Expression: MIT\n", b""),
                    METADATA.replace(b"License-Expression: MIT", b"License-Expression: Apache-2.0"),
                    METADATA.replace(b"License-File: LICENSE", b"License-File: OTHER"),
                ):
                    with self.subTest(kind=builder.__name__, metadata=metadata), self.assertRaisesRegex(ValueError, "declare MIT"):
                        validator(builder(root, {metadata_path: metadata}), NAME, MODULE, VERSION)
                with self.subTest(kind=builder.__name__, stale=True), self.assertRaisesRegex(ValueError, "differs from the project LICENSE"):
                    validator(builder(root), NAME, MODULE, VERSION, expected_license=LICENSE + b"Updated terms\n")

    def test_allows_uv_original_project_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            sdist = make_sdist(Path(directory), {"pyproject.toml.orig": b"[project]\n"})
            files = release.validate_sdist(sdist, NAME, MODULE, VERSION)
            self.assertIn("pyproject.toml.orig", files)

    def test_accepts_separate_pypi_readme_alongside_github_readme(self):
        with tempfile.TemporaryDirectory() as directory:
            sdist = make_sdist(Path(directory), {
                "pyproject.toml": PROJECT_METADATA + b'readme = {file = "README_PYPI.md", content-type = "text/markdown"}\n',
                "README_PYPI.md": b"Package purpose and MCP configuration.\n",
            })
            files = release.validate_sdist(sdist, NAME, MODULE, VERSION)
            self.assertTrue({"README.md", "README_PYPI.md"} <= set(files))

    def test_rejects_unsafe_paths_on_every_os(self):
        for name in ("/etc/config", "../auth.json", "a/../secret", "C:/auth.json", "a\\config", "a//config", "a/./config", "",
                     "module/CON.py", "module/LPT1.py", "module/COM².py", "module/aux .py", "module/file. ", "module/a?.py"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.safe_member(name)

    def test_rejects_unicode_normalization_collisions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for builder, validator, parent in (
                (make_wheel, release.validate_wheel, MODULE),
                (make_sdist, release.validate_sdist, f"src/{MODULE}"),
            ):
                with self.subTest(kind=builder.__name__), self.assertRaisesRegex(ValueError, "Duplicate"):
                    validator(builder(root, {f"{parent}/caf\u00e9.py": b"", f"{parent}/cafe\u0301.py": b""}), NAME, MODULE, VERSION)

    def test_package_version_and_source_readme_must_match_distribution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for builder, validator, parent in (
                (make_wheel, release.validate_wheel, MODULE),
                (make_sdist, release.validate_sdist, f"src/{MODULE}"),
            ):
                with self.subTest(kind=builder.__name__), self.assertRaisesRegex(ValueError, "module version"):
                    validator(builder(root, {f"{parent}/__init__.py": b'__version__="99.0.0"\n'}), NAME, MODULE, VERSION)
            sdist = make_sdist(root, {"pyproject.toml": PROJECT_METADATA + b'readme="README_PYPI.md"\n'})
            with self.assertRaisesRegex(ValueError, "declared README"):
                release.validate_sdist(sdist, NAME, MODULE, VERSION)
            sdist = make_sdist(root, {"pyproject.toml": PROJECT_METADATA.replace(VERSION.encode(), b"99.0.0")})
            with self.assertRaisesRegex(ValueError, "Source project metadata"):
                release.validate_sdist(sdist, NAME, MODULE, VERSION)

    def test_duplicate_identity_metadata_is_rejected(self):
        for extra in (b"Name: other-package\n", b"Version: 99.0.0\n"):
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, "metadata differs"):
                release.validate_metadata(METADATA + extra, NAME, VERSION)

    def test_source_version_drift_is_rejected_before_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_bytes(PROJECT_METADATA)
            (root / "src" / MODULE).mkdir(parents=True)
            (root / "src" / MODULE / "__init__.py").write_text('__version__ = "99.0.0"\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "module version"):
                release.project_identity(root)

    def test_rejects_local_credentials_and_generated_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("auth.json", ".env", "sunny-anime-puppy.png", f"{MODULE}/secret.png"):
                with self.subTest(wheel=name), self.assertRaises(ValueError):
                    release.validate_wheel(make_wheel(root, {name: b"local-only"}), NAME, MODULE, VERSION)
            for name in ("auth.json", ".codex/config.toml", ".env", "private-generation.png", "outputs/image.png", "src/codex_image_generation/config.toml"):
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

    def test_rejects_non_regular_wheel_file(self):
        with tempfile.TemporaryDirectory() as directory:
            wheel = make_wheel(Path(directory))
            with zipfile.ZipFile(wheel, "a") as archive:
                fifo = zipfile.ZipInfo(f"{MODULE}/pipe.py")
                fifo.create_system = 3
                fifo.external_attr = (stat.S_IFIFO | 0o600) << 16
                archive.writestr(fifo, b"")
            with self.assertRaisesRegex(ValueError, "Non-regular wheel"):
                release.validate_wheel(wheel, NAME, MODULE, VERSION)

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
            (root / "src" / MODULE).mkdir(parents=True)
            (root / "src" / MODULE / "__init__.py").write_bytes(MODULE_INIT)
            (root / "LICENSE").write_bytes(LICENSE)
            (root / "docs").mkdir()
            (root / "docs" / "INSTALL.md").write_text("Use [the example](../examples/codex-uvx.toml).\n", encoding="utf-8")
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
                    "INSTALL.md", "codex-uvx.toml", "LICENSE", "SHA256SUMS",
                })
                self.assertEqual(archive.read("LICENSE"), LICENSE)
                self.assertIn(b"(codex-uvx.toml)", archive.read("INSTALL.md"))
                self.assertNotIn(b"../examples/", archive.read("INSTALL.md"))
                for line in archive.read("SHA256SUMS").decode().splitlines():
                    digest, filename = line.split("  ")
                    self.assertEqual(digest, hashlib.sha256(archive.read(filename)).hexdigest())

    def test_failed_validation_does_not_replace_existing_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text(f'[project]\nname="{NAME}"\nversion="{VERSION}"\n', encoding="utf-8")
            (root / "src" / MODULE).mkdir(parents=True)
            (root / "src" / MODULE / "__init__.py").write_bytes(MODULE_INIT)
            (root / "LICENSE").write_bytes(LICENSE)
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
