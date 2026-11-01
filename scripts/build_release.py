"""Build and inspect a downloadable release without publishing it anywhere."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from email.parser import BytesParser


def project_identity(project: Path) -> tuple[str, str, str]:
    metadata = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    name, version = metadata["name"], metadata["version"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise ValueError("Project name cannot be used as a release filename")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.!+_-]*", version):
        raise ValueError("Project version cannot be used as a release filename")
    return name, re.sub(r"[-_.]+", "_", name).lower(), version


def safe_member(name: str) -> PurePosixPath:
    """Do not accept archive paths that change meaning on another platform."""
    if not name or "\\" in name or ":" in name or "\x00" in name:
        raise ValueError(f"Unsafe archive member: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in name.rstrip("/").split("/")):
        raise ValueError(f"Unsafe archive member: {name!r}")
    return path


def validate_metadata(data: bytes, name: str, version: str) -> None:
    metadata = BytesParser().parsebytes(data)
    normalize = lambda value: re.sub(r"[-_.]+", "-", value).lower()
    if normalize(metadata.get("Name", "")) != normalize(name) or metadata.get("Version") != version:
        raise ValueError("Built distribution metadata differs from pyproject.toml")
    if metadata.get_all("License-Expression") != ["MIT"] or metadata.get_all("License-File") != ["LICENSE"]:
        raise ValueError("Built distribution must declare MIT and the LICENSE file")


def validate_license(data: bytes, expected: bytes | None) -> None:
    if not data.strip():
        raise ValueError("Packaged LICENSE is empty")
    if expected is not None and data != expected:
        raise ValueError("Packaged LICENSE differs from the project LICENSE")


def validate_wheel(path: Path, name: str, module: str, version: str, *, expected_license: bytes | None = None) -> list[str]:
    dist_info = f"{module}-{version}.dist-info"
    license_path = f"{dist_info}/licenses/LICENSE"
    seen: set[str] = set()
    files: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for entry in archive.infolist():
            member = safe_member(entry.filename)
            key = str(member).casefold()
            if key in seen:
                raise ValueError(f"Duplicate archive member: {entry.filename}")
            seen.add(key)
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError(f"Symlink in wheel: {entry.filename}")
            if entry.is_dir():
                if str(member) not in {module, dist_info, f"{dist_info}/licenses"}:
                    raise ValueError(f"Unexpected wheel directory: {entry.filename}")
                continue
            allowed = str(member) == license_path or (
                len(member.parts) == 2
                and (
                    (member.parts[0] == module and member.suffix == ".py")
                    or (
                        member.parts[0] == dist_info
                        and member.name in {"METADATA", "WHEEL", "RECORD", "entry_points.txt", "top_level.txt"}
                    )
                )
            )
            if not allowed:
                raise ValueError(f"Unexpected wheel file: {entry.filename}")
            files.append(str(member))
        required = {f"{module}/__init__.py", f"{module}/__main__.py", f"{dist_info}/METADATA", f"{dist_info}/WHEEL", f"{dist_info}/entry_points.txt", f"{dist_info}/RECORD", license_path}
        if not required <= set(files):
            raise ValueError(f"Incomplete wheel: {sorted(required - set(files))}")
        validate_metadata(archive.read(f"{dist_info}/METADATA"), name, version)
        validate_license(archive.read(license_path), expected_license)
        wheel_metadata = BytesParser().parsebytes(archive.read(f"{dist_info}/WHEEL"))
        if wheel_metadata.get("Root-Is-Purelib") != "true" or "py3-none-any" not in wheel_metadata.get_all("Tag", []):
            raise ValueError("Expected a portable pure-Python py3-none-any wheel")
    return files


def validate_sdist(path: Path, name: str, module: str, version: str, *, expected_license: bytes | None = None) -> list[str]:
    root = f"{module}-{version}"
    # uv may preserve the original TOML beside its normalized copy.
    root_files = {"pyproject.toml", "pyproject.toml.orig", "README.md", "README_PYPI.md", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md", "LICENSE", "uv.lock", ".python-version", "PKG-INFO", "sunny-anime-puppy.png"}
    allowed_dirs = {"src", f"src/{module}", "tests", "scripts", "docs", "examples"}
    seen: set[str] = set()
    files: list[str] = []
    with tarfile.open(path, "r:gz") as archive:
        for entry in archive.getmembers():
            member = safe_member(entry.name)
            if member.parts[0] != root:
                raise ValueError(f"Unexpected sdist root: {entry.name}")
            key = str(member).casefold()
            if key in seen:
                raise ValueError(f"Duplicate archive member: {entry.name}")
            seen.add(key)
            relative = member.relative_to(root)
            if entry.isdir():
                if str(relative) != "." and str(relative) not in allowed_dirs:
                    raise ValueError(f"Unexpected sdist directory: {entry.name}")
                continue
            if not entry.isfile():
                raise ValueError(f"Non-regular sdist member: {entry.name}")
            allowed = (
                str(relative) in root_files
                or (relative.parent == PurePosixPath("src") / module and relative.suffix == ".py")
                or (str(relative.parent) in {"tests", "scripts"} and relative.suffix == ".py")
                or (str(relative.parent) == "docs" and relative.suffix == ".md")
                or (str(relative.parent) == "examples" and relative.suffix == ".toml")
            )
            if not allowed:
                raise ValueError(f"Unexpected sdist file: {entry.name}")
            files.append(str(relative))
        required = {"pyproject.toml", "README.md", "PKG-INFO", "LICENSE", f"src/{module}/__init__.py", f"src/{module}/__main__.py"}
        if not required <= set(files):
            raise ValueError(f"Incomplete sdist: {sorted(required - set(files))}")
        metadata_file = archive.extractfile(f"{root}/PKG-INFO")
        if metadata_file is None:
            raise ValueError("Source distribution has no metadata")
        with metadata_file:
            validate_metadata(metadata_file.read(), name, version)
        license_file = archive.extractfile(f"{root}/LICENSE")
        if license_file is None:
            raise ValueError("Source distribution has no LICENSE")
        with license_file:
            validate_license(license_file.read(), expected_license)
    return files


def checksums(paths: list[Path]) -> str:
    lines = []
    for path in sorted(paths, key=lambda item: item.name):
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        lines.append(f"{digest}  {path.name}\n")
    return "".join(lines)


def copy_atomic(source: Path, target: Path) -> None:
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{target.name}.", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def build_release(project: Path, out_dir: Path) -> list[Path]:
    project = project.resolve()
    out_dir = out_dir.resolve()
    name, module, version = project_identity(project)
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is required to build a release: https://docs.astral.sh/uv/")
    guide = project / "docs" / "INSTALL.md"
    example = project / "examples" / "codex-uvx.toml"
    license_source = project / "LICENSE"
    for required in (guide, example, license_source):
        if not required.is_file():
            raise FileNotFoundError(f"Missing release documentation: {required}")
    expected_license = license_source.read_bytes()
    validate_license(expected_license, None)
    with tempfile.TemporaryDirectory(prefix="image-mcp-release-") as directory:
        temporary = Path(directory)
        subprocess.run([uv, "build", "--no-create-gitignore", "--out-dir", str(temporary), str(project)], check=True, cwd=project)
        wheel = temporary / f"{module}-{version}-py3-none-any.whl"
        sdist = temporary / f"{module}-{version}.tar.gz"
        validate_wheel(wheel, name, module, version, expected_license=expected_license)
        validate_sdist(sdist, name, module, version, expected_license=expected_license)
        guide_copy = temporary / "INSTALL.md"
        example_copy = temporary / "codex-uvx.toml"
        license_copy = temporary / "LICENSE"
        shutil.copyfile(guide, guide_copy)
        shutil.copyfile(example, example_copy)
        license_copy.write_bytes(expected_license)
        members = [wheel, sdist, guide_copy, example_copy, license_copy]
        inner_checksums = temporary / "SHA256SUMS"
        inner_checksums.write_text(checksums(members), encoding="utf-8", newline="\n")
        bundle = temporary / f"{name}-{version}.zip"
        with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for member in [*members, inner_checksums]:
                archive.write(member, arcname=member.name)
        external_checksums = temporary / f"SHA256SUMS-{version}.txt"
        artifacts = [wheel, sdist, bundle]
        external_checksums.write_text(checksums(artifacts), encoding="utf-8", newline="\n")
        artifacts.append(external_checksums)
        out_dir.mkdir(parents=True, exist_ok=True)
        for artifact in artifacts:
            copy_atomic(artifact, out_dir / artifact.name)
        return [out_dir / artifact.name for artifact in artifacts]


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, help="Output directory (default: project/dist)")
    options = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    for path in build_release(project, options.out_dir or project / "dist"):
        print(path)


if __name__ == "__main__":
    main()
