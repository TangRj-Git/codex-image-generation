"""Installation config is independent of working directory and secret availability."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from codex_image_generation.cli import main


class CliTests(unittest.TestCase):
    def launch(self, args, env=None):
        with patch.dict(os.environ, env or {}, clear=True), patch(
            "sys.argv", ["codex-image-generation", *args]
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
            with self.subTest(flag=flag), patch("sys.argv", ["codex-image-generation", flag, "fake"]), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main()
                self.assertEqual(error.exception.code, 2)

    def test_listing_commands_are_json_and_forward_refresh_and_policy(self):
        for flag, function in (("--list-models", "list_models"), ("--list-image-models", "list_image_models")):
            with self.subTest(flag=flag), patch("codex_image_generation.server." + function, new_callable=AsyncMock,
                                                return_value={"models": [], "source": "official"}) as listing:
                result = json.loads(self.launch([flag, "--refresh-models", "--model-policy", "fast"]))
            self.assertEqual(result["source"], "official")
            listing.assert_awaited_once_with(refresh=True, model_policy="fast")

    def test_unavailable_model_list_has_nonzero_exit_and_refresh_needs_list_mode(self):
        with patch("codex_image_generation.server.list_models", new_callable=AsyncMock,
                   return_value={"models": [], "source": "unavailable"}):
            with self.assertRaises(SystemExit) as raised:
                self.launch(["--list-models"])
            self.assertEqual(raised.exception.code, 1)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            self.launch(["--refresh-models"])
        self.assertEqual(raised.exception.code, 2)

    def test_check_failure_is_json_and_nonzero(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True), patch(
            "sys.argv", ["codex-image-generation", "--check", "--codex-home", directory]
        ), contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as raised:
                main()
        self.assertEqual(raised.exception.code, 1)
        status = json.loads(output.getvalue())
        self.assertFalse(status["configured"])
        self.assertFalse(status["connection_verified"])

    def test_cli_unicode_output_with_ascii_process_encoding(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve() / "\u914d\u7f6e \U0001f680"
            home.mkdir()
            (home / "config.toml").write_text(
                'model="fixture-model"\nmodel_provider="relay"\n'
                '[model_providers.relay]\nbase_url="https://relay.invalid/v1"\n'
                'env_key="RELAY_KEY"\n', encoding="utf-8",
            )
            # Keep runtime discovery variables without inheriting real credentials.
            runtime_names = {
                "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR",
                "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
            }
            env = {key: value for key, value in os.environ.items() if key.upper() in runtime_names}
            env.update(PYTHONIOENCODING="ascii", PYTHONUTF8="0", RELAY_KEY="fake-test-secret")
            outputs = {}
            for mode in ("--print-config", "--check"):
                with self.subTest(mode=mode):
                    result = subprocess.run(
                        [sys.executable, "-B", "-m", "codex_image_generation", mode, "--codex-home", str(home)],
                        cwd=directory, env=env, capture_output=True, timeout=30,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
                    outputs[mode] = result.stdout.decode("utf-8")
                    self.assertIn(str(home).replace("\\", "\\\\"), outputs[mode])
                    self.assertNotIn("fake-test-secret", outputs[mode])
            config = tomllib.loads(outputs["--print-config"])["mcp_servers"]["image_generation"]
            self.assertEqual(config["args"][-1], str(home))
            self.assertTrue(json.loads(outputs["--check"])["configured"])

            script = (
                "import sys; from codex_image_generation.cli import main; "
                "sys.argv=['\\u56fe\\u7247\\U0001f680', '--help']; main()"
            )
            result = subprocess.run(
                [sys.executable, "-B", "-c", script], cwd=directory, env=env,
                capture_output=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
            self.assertIn("\u56fe\u7247\U0001f680", result.stdout.decode("utf-8"))

            invalid = home / "invalid"
            invalid.mkdir()
            (invalid / "config.toml").write_text("model = [", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-B", "-m", "codex_image_generation", "--print-config", "--codex-home", str(invalid)],
                cwd=directory, env=env, capture_output=True, timeout=30,
            )
            self.assertEqual(result.returncode, 1)
            self.assertNotIn("UnicodeEncodeError", result.stderr.decode("utf-8"))
            self.assertTrue(result.stderr.decode("utf-8").strip())
