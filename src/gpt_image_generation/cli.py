"""Command-line entry point. Normal operation reserves stdout for MCP."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys

from . import __version__


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate and edit images through a local stdio MCP server."
    )
    parser.add_argument("--version", action="version", version=__version__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="Check local configuration; no API request")
    modes.add_argument("--print-config", action="store_true", help="Print a Codex MCP configuration snippet")
    parser.add_argument("--responses-model", help="Override the dialogue model, for example gpt-6-astra")
    parser.add_argument("--image-model", help="Override the image model; default gpt-image-2.5-flare")
    parser.add_argument("--profile", help="Codex user configuration profile")
    parser.add_argument("--codex-home", help="Explicit Codex configuration directory")
    options = parser.parse_args()
    overrides = {
        "CODEX_IMAGE_RESPONSES_MODEL": options.responses_model,
        "CODEX_IMAGE_MODEL": options.image_model,
        "CODEX_IMAGE_PROFILE": options.profile,
        "CODEX_HOME": options.codex_home,
    }
    for key, value in overrides.items():
        if value is not None:
            os.environ[key] = value

    if options.print_config:
        from .config import resolve_config
        from .errors import ImageToolError

        environment_names = {"CODEX_HOME"}
        tool_timeout = 360
        try:
            settings = resolve_config()
        except ImageToolError:
            print("# Local credentials are not resolved yet; run --check before using this server.")
        else:
            environment_names.update(settings.required_env_vars)
            tool_timeout = max(360, math.ceil(settings.request_timeout) + 60)
        # Flags are already carried in args; no inherited value is necessary.
        environment_names.difference_update(
            key for key, value in overrides.items() if value is not None and key != "CODEX_HOME"
        )
        arguments = ["-m", "gpt_image_generation"]
        for name in ("responses_model", "image_model", "profile", "codex_home"):
            value = getattr(options, name)
            if value is not None:
                arguments.extend(["--" + name.replace("_", "-"), value])
        print("[mcp_servers.image_generation]")
        print("command = " + json.dumps(sys.executable, ensure_ascii=False))
        print("args = " + json.dumps(arguments, ensure_ascii=False))
        print("startup_timeout_sec = 60")
        print("tool_timeout_sec = " + str(tool_timeout))
        print("env_vars = " + json.dumps(sorted(environment_names), ensure_ascii=False))
        return

    # Never enable HTTP debug logging: provider URLs and headers can contain secrets.
    for logger_name in ("openai", "httpx", "httpx2", "httpcore"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    from .server import configuration_status, mcp

    if options.check:
        status = configuration_status()
        print(json.dumps(status, ensure_ascii=False, indent=2))
        if not status["configured"]:
            raise SystemExit(1)
        return

    mcp.run(transport="stdio", show_banner=False)
