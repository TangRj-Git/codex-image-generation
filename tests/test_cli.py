"""CLI diagnostics and generated installation configuration."""

import contextlib
import io
import os
import tempfile
import tomllib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from gpt_image_generation.cli import main
from gpt_image_generation.errors import ImageToolError


class CliTests(unittest.TestCase):
    def test_print_config_is_valid_toml_with_model_override(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "sys.argv", ["gpt-image-generation", "--print-config", "--responses-model", "gpt-6-astra"]
        ), patch(
            "gpt_image_generation.config.resolve_config",
            side_effect=ImageToolError("Test configuration is intentionally absent"),
        ), contextlib.redirect_stdout(io.StringIO()) as output:
            main()
        config = tomllib.loads(output.getvalue())["mcp_servers"]["image_generation"]
        self.assertEqual(config["args"], ["-m", "gpt_image_generation", "--responses-model", "gpt-6-astra"])
        self.assertEqual(config["tool_timeout_sec"], 360)

    def test_print_config_includes_required_environment_names_and_timeout(self):
        settings = SimpleNamespace(
            required_env_vars=("PROVIDER_KEY", "TRACE_CONTEXT", "CODEX_IMAGE_TIMEOUT"),
            request_timeout=500,
        )
        with patch.dict(os.environ, {"PROVIDER_KEY": "fake-secret"}, clear=True), patch(
            "sys.argv", ["gpt-image-generation", "--print-config"]
        ), patch("gpt_image_generation.config.resolve_config", return_value=settings), contextlib.redirect_stdout(io.StringIO()) as output:
            main()
        rendered = output.getvalue()
        config = tomllib.loads(rendered)["mcp_servers"]["image_generation"]
        self.assertEqual(config["tool_timeout_sec"], 560)
        self.assertEqual(config["env_vars"], [
            "CODEX_HOME", "CODEX_IMAGE_TIMEOUT", "PROVIDER_KEY", "TRACE_CONTEXT",
        ])
        self.assertNotIn("fake-secret", rendered)

    def test_check_failure_returns_json_and_nonzero(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True), patch(
            "sys.argv", ["gpt-image-generation", "--check", "--codex-home", directory]
        ), contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as raised:
                main()
        self.assertEqual(raised.exception.code, 1)
        import json
        status = json.loads(output.getvalue())
        self.assertFalse(status["configured"])
        self.assertFalse(status["connection_verified"])
