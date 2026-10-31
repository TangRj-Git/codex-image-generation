"""Install a local wheel in a temporary uv tool environment and check MCP stdio.

No real user configuration is read and no upstream image request is made.
The first installation may access the package index to download dependencies.
"""

from __future__ import annotations

import argparse
from email.parser import BytesParser
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile


FAKE_KEY = "wheel-smoke-fake-key-never-use"


def isolated_environment(root: Path) -> dict[str, str]:
    """Keep OS/runtime essentials, excluding user API and Python configuration."""
    allowed = {
        "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "SYSTEMDRIVE",
        "TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
        "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "LANG", "LC_ALL",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "UV_CACHE_DIR", "UV_SYSTEM_CERTS",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update({
        "UV_TOOL_DIR": str(root / "tools"),
        "UV_TOOL_BIN_DIR": str(root / "bin"),
        "UV_NO_CONFIG": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONNOUSERSITE": "1",
        "CODEX_HOME": str(root / "Codex settings \u4e2d\u6587 \U0001f436"),
        "RELAY_SMOKE_KEY": FAKE_KEY,
        "CODEX_IMAGE_TIMEOUT": "1",
        "DO_NOT_TRACK": "1",
    })
    return env


def run(command: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 60) -> str:
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="strict", timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Command exited with {result.returncode}: {command[0]}\n{result.stdout}\n{result.stderr}")
    return result.stdout


STDIO_CHECK = r'''
import asyncio
import json
import os
from pathlib import Path
import sys

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
import gpt_image_generation

async def check():
    config = json.loads(sys.argv[1])
    root = Path(sys.argv[2])
    installed_file = Path(gpt_image_generation.__file__).resolve()
    assert installed_file.is_relative_to(root / "tools"), str(installed_file)
    with (root / "stdio.log").open("w", encoding="utf-8") as log:
        transport = StdioTransport(
            command=config["command"], args=config["args"],
            env=dict(os.environ), cwd=str(Path.cwd()), keep_alive=False, log_file=log,
        )
        async with asyncio.timeout(40):
            async with Client(transport) as client:
                tools = await client.list_tools()
                assert [tool.name for tool in tools] == ["generate_image"]
                properties = tools[0].input_schema["properties"]
                assert {"prompt", "cwd", "current_model", "output_format"} <= set(properties)
                assert not {"api_key", "base_url", "image_model"} & set(properties)
                result = await client.call_tool_mcp("generate_image", {"prompt": "Offline validation only", "cwd": "relative"})
                assert result.is_error
                assert result.structured_content["errors"][0]["code"] == "invalid_request"
                assert os.environ["RELAY_SMOKE_KEY"] not in str(result)
    print(json.dumps({"stdio": "passed", "installed_module": str(installed_file)}, ensure_ascii=False))

asyncio.run(check())
'''


def smoke_wheel(wheel: Path) -> None:
    wheel = wheel.resolve(strict=True)
    if wheel.suffix != ".whl":
        raise ValueError("--wheel must point to a built .whl file")
    with zipfile.ZipFile(wheel) as archive:
        metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise ValueError("Expected exactly one wheel METADATA file")
        metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
        if metadata["Name"] != "gpt-image-generation":
            raise ValueError("Wheel is not gpt-image-generation")
        version = metadata["Version"]
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is required for the isolated installation check")
    with tempfile.TemporaryDirectory(prefix="image-mcp-wheel-smoke-") as directory:
        root = Path(directory).resolve()
        env = isolated_environment(root)
        home = Path(env["CODEX_HOME"])
        home.mkdir()
        (home / "config.toml").write_text(
            'model_provider="smoke"\nmodel="smoke-model"\n'
            '[model_providers.smoke]\nbase_url="https://relay.invalid/v1"\n'
            'env_key="RELAY_SMOKE_KEY"\nwire_api="responses"\n', encoding="utf-8",
        )
        unrelated = root / "unrelated project \u4e2d\u6587"
        unrelated.mkdir()
        print(f"Installing {wheel.name} in an isolated temporary environment...", flush=True)
        run([uv, "tool", "install", "--python", sys.executable, str(wheel)], cwd=unrelated, env=env, timeout=600)
        executable = root / "bin" / ("gpt-image-generation.exe" if os.name == "nt" else "gpt-image-generation")
        if not executable.is_file():
            raise RuntimeError(f"Installed console entry point was not found: {executable}")
        actual_version = run([str(executable), "--version"], cwd=unrelated, env=env).strip()
        if actual_version != version:
            raise RuntimeError(f"Installed version {actual_version!r} differs from wheel {version!r}")
        check_text = run([str(executable), "--check"], cwd=unrelated, env=env)
        check = json.loads(check_text)
        if not check["configured"] or check["connection_verified"] or check["server_version"] != version:
            raise RuntimeError(f"Unexpected local configuration status: {check}")
        # Exercise the installed CLI under an ASCII host environment as well.
        ascii_env = {**env, "PYTHONIOENCODING": "ascii", "PYTHONUTF8": "0"}
        config_text = run([str(executable), "--print-config"], cwd=unrelated, env=ascii_env)
        config = tomllib.loads(config_text)["mcp_servers"]["image_generation"]
        interpreter = Path(config["command"]).absolute()
        if not interpreter.is_file() or not interpreter.is_relative_to(root / "tools"):
            raise RuntimeError("Printed MCP command must point into the installed tool environment")
        args = config["args"]
        if Path(args[args.index("--codex-home") + 1]) != home:
            raise RuntimeError("Printed Codex configuration path was not preserved")
        if "RELAY_SMOKE_KEY" not in config["env_vars"]:
            raise RuntimeError("Printed MCP configuration omitted the provider environment variable")
        if any(FAKE_KEY in output for output in (check_text, config_text)):
            raise RuntimeError("CLI diagnostics exposed the fake API key")
        result = run([str(interpreter), "-c", STDIO_CHECK, json.dumps(config, ensure_ascii=False), str(root)], cwd=unrelated, env=env)
        if json.loads(result)["stdio"] != "passed":
            raise RuntimeError("The installed MCP stdio check did not complete")
        if list(unrelated.iterdir()):
            raise RuntimeError("Offline smoke check unexpectedly created project files")
        print(f"PASS: {version}; installed CLI, UTF-8 TOML, provider reuse, MCP discovery and local validation.")


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, type=Path)
    options = parser.parse_args()
    smoke_wheel(options.wheel)


if __name__ == "__main__":
    main()
