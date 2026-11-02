"""Run the MCP service, inspect settings, or print installation configuration."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import sys
from pathlib import Path

from . import __version__


def _configure_utf8_output() -> None:
    """Keep JSON, TOML, and diagnostics readable in redirected CLI output."""
    for stream, errors in ((sys.stdout, "strict"), (sys.stderr, "backslashreplace")):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors=errors)


def main() -> None:
    _configure_utf8_output()
    parser = argparse.ArgumentParser(description="Generate images using Codex's existing relay configuration.")
    parser.add_argument("--version", action="version", version=__version__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Inspect local configuration without an API request")
    mode.add_argument("--print-config", action="store_true", help="Print a Codex MCP installation snippet")
    mode.add_argument("--list-models", action="store_true", help="List dialogue models from the selected provider (no image generation)")
    mode.add_argument("--list-image-models", action="store_true", help="List image models from public official docs (no API key needed)")
    parser.add_argument("--refresh-models", action="store_true", help="Refresh model catalogs with a list command")
    parser.add_argument("--model-policy", choices=("quality", "fast"), help="Recommendation policy for a list command")
    parser.add_argument("--codex-home", help="Locate existing Codex configuration")
    parser.add_argument("--profile", help="Select an existing Codex configuration profile")
    options = parser.parse_args()
    if (options.refresh_models or options.model_policy) and not (options.list_models or options.list_image_models):
        parser.error("--refresh-models and --model-policy require --list-models or --list-image-models")
    if options.codex_home is not None:
        try:
            os.environ["CODEX_HOME"] = str(Path(options.codex_home).expanduser().resolve())
        except (OSError, ValueError, RuntimeError):
            parser.error("Cannot resolve --codex-home; provide a valid configuration directory.")
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
        arguments = ["-m", "codex_image_generation", "--codex-home", settings["codex_home"]]
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
    from .server import configuration_status, list_image_models, list_models, mcp

    if options.list_models or options.list_image_models:
        from fastmcp.tools import ToolResult
        listing = (list_models(refresh=options.refresh_models, model_policy=options.model_policy)
                   if options.list_models else list_image_models(refresh=options.refresh_models, model_policy=options.model_policy))
        result = asyncio.run(listing)
        if isinstance(result, ToolResult):
            result = result.structured_content
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("error") or result.get("source") == "unavailable":
            raise SystemExit(1)
        return

    if options.check:
        status = configuration_status()
        print(json.dumps(status, ensure_ascii=False, indent=2))
        if not status["configured"]:
            raise SystemExit(1)
        return
    mcp.run(transport="stdio", show_banner=False)
