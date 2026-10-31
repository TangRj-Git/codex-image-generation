"""Installation config is independent of working directory and secret availability."""
import contextlib
import io
import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from gpt_image_generation.cli import main


class CliTests(unittest.TestCase):
    def launch(self, args, env=None):
        with patch.dict(os.environ, env or {}, clear=True), patch(
            "sys.argv", ["gpt-image-generation", *args]
        ), contextlib.redirect_stdout(io.StringIO()) as output:
            main()
        return output.getvalue()

    def test_print_config_has_no_model_and_uses_absolute_config_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            (home / "config.toml").write_text(
                'model_provider="relay"\n[model_providers.relay]\n'
                'base_url="https://relay.invalid/v1"\nenv_key="RELAY_KEY"\n', encoding="utf-8",
            )
            relative = os.path.relpath(home, Path.cwd()) if home.drive == Path.cwd().drive else str(home)
            rendered = self.launch(["--print-config", "--codex-home", relative])
            config = tomllib.loads(rendered)["mcp_servers"]["image_generation"]
        args = config["args"]
        self.assertTrue(Path(args[args.index("--codex-home") + 1]).is_absolute())
        self.assertIn("RELAY_KEY", config["env_vars"])
        self.assertNotIn("--responses-model", args)
        self.assertNotIn("--image-model", args)
        self.assertEqual(config["tool_timeout_sec"], 360)

    def test_relative_codex_home_is_fixed_when_host_cwd_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "settings").mkdir()
            (root / "settings" / "config.toml").write_text(
                'model_provider="relay"\n[model_providers.relay]\n'
                'base_url="https://relay.invalid/v1"\nenv_key="RELAY_KEY"\n', encoding="utf-8",
            )
            with contextlib.chdir(root):
                rendered = self.launch(["--print-config", "--codex-home", "settings"])
            config = tomllib.loads(rendered)["mcp_servers"]["image_generation"]
            self.assertEqual(config["args"][-1], str(root / "settings"))

    def test_missing_key_does_not_drop_environment_declarations(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "config.toml").write_text(
                'model_provider="relay"\n[model_providers.relay]\n'
                'base_url="https://relay.invalid/v1"\nenv_key="RELAY_KEY"\n'
                '[model_providers.relay.env_http_headers]\n"X-Tenant"="TENANT"\n',
                encoding="utf-8",
            )
            rendered = self.launch(
                ["--print-config", "--codex-home", directory],
                {"CODEX_IMAGE_TIMEOUT": "500", "RELAY_KEY": "fake-secret"},
            )
            config = tomllib.loads(rendered)["mcp_servers"]["image_generation"]
        self.assertTrue({"RELAY_KEY", "TENANT", "CODEX_IMAGE_TIMEOUT"} <= set(config["env_vars"]))
        self.assertEqual(config["tool_timeout_sec"], 560)
        self.assertNotIn("fake-secret", rendered)

    def test_removed_model_flags_are_rejected(self):
        for flag in ("--responses-model", "--image-model"):
            with self.subTest(flag=flag), patch("sys.argv", ["gpt-image-generation", flag, "fake"]), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main()
                self.assertEqual(error.exception.code, 2)

    def test_check_failure_is_json_and_nonzero(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True), patch(
            "sys.argv", ["gpt-image-generation", "--check", "--codex-home", directory]
        ), contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as raised:
                main()
        self.assertEqual(raised.exception.code, 1)
        status = json.loads(output.getvalue())
        self.assertFalse(status["configured"])
        self.assertFalse(status["connection_verified"])
