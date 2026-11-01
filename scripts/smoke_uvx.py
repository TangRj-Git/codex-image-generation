"""Verify uvx CLI and MCP stdio from a wheel or a published package.

Uses temporary fake Codex settings and never makes an image API request.
Dependency installation may access PyPI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import shutil
import sys
import tempfile

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from smoke_wheel import FAKE_KEY, isolated_environment, run


async def check_stdio(command: list[str], cwd: Path, env: dict[str, str], log: Path) -> None:
    with log.open("w", encoding="utf-8") as handle:
        transport = StdioTransport(
            command=command[0], args=command[1:], env=env,
            cwd=str(cwd), keep_alive=False, log_file=handle,
        )
        async with asyncio.timeout(60):
            async with Client(transport) as client:
                tools = await client.list_tools()
                if [tool.name for tool in tools] != ["generate_image"]:
                    raise RuntimeError("uvx did not expose the expected MCP tool")
                result = await client.call_tool_mcp(
                    "generate_image", {"prompt": "Offline validation only", "cwd": "relative"},
                )
                if not result.is_error or result.structured_content["errors"][0]["code"] != "invalid_request":
                    raise RuntimeError("uvx MCP local validation did not return the expected error")
                if FAKE_KEY in str(result):
                    raise RuntimeError("uvx MCP diagnostics exposed the fake API key")


def smoke_uvx(package: str, version: str) -> None:
    uvx = shutil.which("uvx")
    if not uvx:
        raise RuntimeError("uvx is required for the isolated launch check")
    if Path(package).is_file():
        package = str(Path(package).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="image-mcp-uvx-smoke-") as directory:
        root = Path(directory).resolve()
        env = isolated_environment(root)
        settings = Path(env["CODEX_HOME"])
        settings.mkdir()
        (settings / "config.toml").write_text(
            'model_provider="smoke"\nmodel="smoke-model"\n'
            '[model_providers.smoke]\nbase_url="https://relay.invalid/v1"\n'
            'env_key="RELAY_SMOKE_KEY"\nwire_api="responses"\n', encoding="utf-8",
        )
        unrelated = root / "unrelated project"
        unrelated.mkdir()
        command = [
            uvx, "--no-config", "--no-env-file", "--isolated", "--python", sys.executable,
            "--default-index", "https://pypi.org/simple", "--from", package,
            "codex-image-generation",
        ]
        actual = run([*command, "--version"], cwd=unrelated, env=env, timeout=600).strip()
        if actual != version:
            raise RuntimeError(f"Expected {version}, got {actual!r}")
        output = run([*command, "--check"], cwd=unrelated, env=env)
        status = json.loads(output)
        if not status["configured"] or status["connection_verified"] or status["server_version"] != version:
            raise RuntimeError("uvx local configuration check failed")
        if FAKE_KEY in output:
            raise RuntimeError("uvx CLI diagnostics exposed the fake API key")
        asyncio.run(check_stdio(command, unrelated, env, root / "stdio.log"))
        if list(unrelated.iterdir()):
            raise RuntimeError("uvx offline smoke check unexpectedly created project files")
        print(f"PASS: uvx {version}; isolated CLI, local configuration, MCP discovery and validation.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, help="Wheel path or PyPI requirement")
    parser.add_argument("--expected-version", required=True)
    options = parser.parse_args()
    smoke_uvx(options.package, options.expected_version)


if __name__ == "__main__":
    main()
