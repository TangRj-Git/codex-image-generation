"""Run the MCP service, inspect settings, or print installation configuration."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from pathlib import Path

from . import __version__


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate images using Codex's existing relay configuration.")
    parser.add_argument("--version", action="version", version=__version__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Inspect local configuration without an API request")
    mode.add_argument("--print-config", action="store_true", help="Print a Codex MCP installation snippet")
    parser.add_argument("--codex-home", help="Locate existing Codex configuration")
    parser.add_argument("--profile", help="Select an existing Codex configuration profile")
    options = parser.parse_args()
    if options.codex_home is not None:
        os.environ["CODEX_HOME"] = str(Path(options.codex_home).expanduser().resolve())
    if options.profile is not None:
        os.environ["CODEX_IMAGE_PROFILE"] = options.profile

    if options.print_config:
        from .config import get_launch_settings
        from .errors import ImageToolError

        try:
            settings = get_launch_settings()
        except ImageToolError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(1) from None
        arguments = ["-m", "gpt_image_generation", "--codex-home", settings["codex_home"]]
        names = set(settings["required_env_vars"])
        names.discard("CODEX_HOME")
        if options.profile is not None:
            arguments.extend(["--profile", options.profile])
            names.discard("CODEX_IMAGE_PROFILE")
        timeout = max(360, math.ceil(settings["request_timeout"]) + 60)
        print("[mcp_servers.image_generation]")
        print("command = " + json.dumps(sys.executable, ensure_ascii=False))
        print("args = " + json.dumps(arguments, ensure_ascii=False))
        print("startup_timeout_sec = 60")
        print("tool_timeout_sec = " + str(timeout))
        print("env_vars = " + json.dumps(sorted(names), ensure_ascii=False))
        return

    for name in ("openai", "httpx", "httpx2", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    from .server import configuration_status, mcp

    if options.check:
        status = configuration_status()
        print(json.dumps(status, ensure_ascii=False, indent=2))
        if not status["configured"]:
            raise SystemExit(1)
        return
    mcp.run(transport="stdio", show_banner=False)
