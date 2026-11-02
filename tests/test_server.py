"""MCP contract and host integration tests, with no real upstream requests."""
from __future__ import annotations

import asyncio
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from PIL import Image

from codex_image_generation.errors import ImageToolError
from codex_image_generation.server import _preview, configuration_status, mcp


def png(width=32, height=24):
    buffer = io.BytesIO()
    Image.new("RGBA", (width, height), (40, 80, 120, 128)).save(buffer, "PNG")
    return buffer.getvalue()


def config(source="caller"):
    return SimpleNamespace(
        model="gpt-6-astra", model_source=source,
        public_status=lambda: {"provider": "test", "credential_source": "test"},
    )


def result(directory, **changes):
    image = SimpleNamespace(
        path=Path(directory).resolve() / "book.png", data=png(), mime_type="image/png",
        width=32, height=24, revised_prompt="A book", call_id="ig_test",
        metadata={"action": "generate"},
    )
    values = dict(
        images=[image], partials=[], text="Done", response_id="resp_test",
        request_id="req_test", usage={"total_tokens": 10}, warnings=[], errors=[],
        status="success",
        models={"responses_model_requested": "gpt-6-astra", "responses_model_source": "config",
                "image_model_selected": "gpt-image-2.5-sunburst", "image_model_source": "official_catalog",
                "image_model_reported": None, "image_model_evidence": "request_only"},
        tool_usage={"image_gen": {"total_tokens": 20}},
    )
    values.update(changes)
    return SimpleNamespace(**values)


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_tools_and_independent_model_options(self):
        async with Client(mcp) as client:
            tools = await client.list_tools()
        self.assertEqual({tool.name for tool in tools}, {"generate_image", "list_models", "list_image_models"})
        schema = next(tool for tool in tools if tool.name == "generate_image").input_schema
        self.assertEqual(set(schema["required"]), {"prompt", "cwd"})
        self.assertIn("current_model", schema["properties"])
        for key in ("input_images", "mask", "partial_images", "moderation", "image_model", "responses_model", "model_policy"):
            self.assertIn(key, schema["properties"])
        for key in ("api_key", "base_url", "project_dir", "ctx"):
            self.assertNotIn(key, schema["properties"])

    async def test_preview_paths_and_ids_have_text_fallback(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "codex_image_generation.server.resolve_config", return_value=config()
        ) as resolver, patch(
            "codex_image_generation.server.generate", new_callable=AsyncMock,
            return_value=result(directory, warnings=["test-warning"]),
        ):
            async with Client(mcp) as client:
                response = await client.call_tool_mcp("generate_image", {
                    "prompt": "Draw a book", "cwd": directory, "current_model": "gpt-6-astra",
                })
        self.assertFalse(response.is_error)
        self.assertTrue(any(item.type == "image" for item in response.content))
        text = " ".join(item.text for item in response.content if item.type == "text")
        for expected in ("resp_test", "req_test", "ig_test", "test-warning"):
            self.assertIn(expected, text)
        self.assertEqual(response.structured_content["model"], "gpt-6-astra")
        self.assertTrue(Path(response.structured_content["images"][0]["path"]).is_absolute())
        self.assertEqual(response.structured_content["image_model_selected"], "gpt-image-2.5-sunburst")
        self.assertEqual(response.structured_content["tool_usage"]["image_gen"]["total_tokens"], 20)
        self.assertIn("请求选择的图像模型", text)
        resolver.assert_called_once_with(current_model="gpt-6-astra", require_model=False)

    async def test_invalid_compression_never_reaches_config_or_api(self):
        for value in (True, "50", 80.0, -1, 101):
            with self.subTest(value=value), patch(
                "codex_image_generation.server.resolve_config"
            ) as resolver, patch(
                "codex_image_generation.server.generate", new_callable=AsyncMock
            ) as generator:
                async with Client(mcp) as client:
                    response = await client.call_tool_mcp("generate_image", {
                        "prompt": "Draw", "cwd": str(Path.cwd()), "output_compression": value,
                    })
                self.assertTrue(response.is_error)
                resolver.assert_not_called()
                generator.assert_not_awaited()

    async def test_invalid_source_is_rejected(self):
        for source in ({}, {"path": "a.png", "file_id": "file_other"}, {"file_id": "file_a", "extra": "x"}):
            with self.subTest(source=source), patch("codex_image_generation.server.generate") as generator:
                async with Client(mcp) as client:
                    response = await client.call_tool_mcp("generate_image", {
                        "prompt": "Edit", "cwd": str(Path.cwd()), "input_images": [source],
                    })
                self.assertTrue(response.is_error)
                generator.assert_not_called()

    async def test_sources_and_model_overrides_are_forwarded(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "codex_image_generation.server.resolve_config", return_value=config("config")
        ), patch(
            "codex_image_generation.server.generate", new_callable=AsyncMock, return_value=result(directory)
        ) as generator:
            async with Client(mcp) as client:
                response = await client.call_tool_mcp("generate_image", {
                    "prompt": "Edit", "cwd": directory,
                    "input_images": [{"file_id": "file_test", "detail": "high"}],
                    "include_preview": False,
                    "responses_model": "custom-dialogue", "image_model": "gpt-image-2.5-flare",
                })
        self.assertEqual(generator.call_args.kwargs["input_images"], [{"file_id": "file_test", "detail": "high"}])
        self.assertEqual(generator.call_args.kwargs["image_model"], "gpt-image-2.5-flare")
        self.assertEqual(generator.call_args.kwargs["responses_model"], "custom-dialogue")
        self.assertFalse(any(item.type == "image" for item in response.content))
        self.assertFalse(response.structured_content["session_model_auto_detection"])
        self.assertTrue(response.structured_content["warnings"])

    async def test_partial_success_is_not_marked_as_total_failure(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "codex_image_generation.server.resolve_config", return_value=config()
        ), patch(
            "codex_image_generation.server.generate", new_callable=AsyncMock,
            return_value=result(directory, status="partial_success", errors=[{"code": "failed_item", "message": "One failed"}]),
        ):
            async with Client(mcp) as client:
                response = await client.call_tool_mcp("generate_image", {"prompt": "Draw", "cwd": directory})
        self.assertFalse(response.is_error)
        self.assertEqual(response.structured_content["status"], "partial_success")
        self.assertIn("One failed", " ".join(item.text for item in response.content if item.type == "text"))

    async def test_total_failure_keeps_upstream_ids(self):
        with patch("codex_image_generation.server.resolve_config", return_value=config()), patch(
            "codex_image_generation.server.generate", new_callable=AsyncMock,
            return_value=result(".", images=[], status="error", errors=[{"code": "upstream", "message": "Failed"}]),
        ):
            async with Client(mcp) as client:
                response = await client.call_tool_mcp("generate_image", {"prompt": "Draw", "cwd": str(Path.cwd())})
        self.assertTrue(response.is_error)
        self.assertEqual(response.structured_content["response_id"], "resp_test")

    async def test_internal_exception_does_not_leak_secrets(self):
        with patch("codex_image_generation.server.resolve_config", return_value=config()), patch(
            "codex_image_generation.server.generate", new_callable=AsyncMock,
            side_effect=RuntimeError("secret-should-not-leak"),
        ):
            async with Client(mcp) as client:
                response = await client.call_tool_mcp("generate_image", {"prompt": "Draw", "cwd": str(Path.cwd())})
        self.assertTrue(response.is_error)
        self.assertNotIn("secret-should-not-leak", str(response))

    async def test_stdio_handshake_and_local_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "config.toml").write_text(
                'model_provider = "test"\nmodel = "gpt-6-astra"\n'
                '[model_providers.test]\nbase_url = "https://example.invalid/v1"\n'
                'experimental_bearer_token = "stdio-fake-key"\n', encoding="utf-8",
            )
            with (Path(directory) / "stderr.log").open("w", encoding="utf-8") as log:
                transport = StdioTransport(
                    command=sys.executable,
                    args=["-B", "-m", "codex_image_generation", "--codex-home", directory],
                    env={"PYTHONUTF8": "1", "CODEX_IMAGE_TIMEOUT": "1"},
                    cwd=str(Path(__file__).resolve().parents[1]), keep_alive=False, log_file=log,
                )
                async with asyncio.timeout(30):
                    async with Client(transport) as client:
                        names = {tool.name for tool in await client.list_tools()}
                        response = await client.call_tool_mcp("generate_image", {"prompt": "Draw", "cwd": "relative"})
            self.assertEqual(names, {"generate_image", "list_models", "list_image_models"})
            self.assertTrue(response.is_error)
            self.assertEqual(response.structured_content["errors"][0]["code"], "invalid_request")
            self.assertNotIn("stdio-fake-key", str(response))

    async def test_image_catalog_tool_does_not_read_provider_credentials(self):
        with patch("codex_image_generation.server.resolve_config") as resolver, patch(
            "codex_image_generation.server.list_official_image_models", new_callable=AsyncMock,
            return_value={"models": [{"id": "gpt-image-2.5-sunburst"}], "source": "official"},
        ) as listing:
            async with Client(mcp) as client:
                response = await client.call_tool_mcp("list_image_models", {"refresh": True})
        resolver.assert_not_called()
        self.assertTrue(listing.call_args.kwargs["refresh"])
        self.assertEqual(response.structured_content["source"], "official")

    async def test_dialogue_list_tool_handles_no_config_model(self):
        with patch("codex_image_generation.server.resolve_config", return_value=config()) as resolver, patch(
            "codex_image_generation.server.list_dialogue_models", new_callable=AsyncMock,
            return_value={"models": [{"id": "gpt-6-astra"}], "source": "provider"},
        ):
            async with Client(mcp) as client:
                response = await client.call_tool_mcp("list_models", {"model_policy": "fast"})
        resolver.assert_called_once_with(require_model=False)
        self.assertEqual(response.structured_content["source"], "provider")


class StatusTests(unittest.TestCase):
    def test_status_is_local_and_not_session_detection(self):
        with patch("codex_image_generation.server.resolve_config", return_value=config("config")) as resolver:
            status = configuration_status()
        resolver.assert_called_once_with(require_model=False)
        self.assertTrue(status["configured"])
        self.assertFalse(status["connection_verified"])
        self.assertFalse(status["session_model_auto_detection"])
        self.assertEqual(status["default_output_directory"], "<cwd>")

    def test_missing_configuration_returns_safe_diagnostic(self):
        with patch("codex_image_generation.server.resolve_config", side_effect=ImageToolError("Missing configuration")):
            self.assertFalse(configuration_status()["configured"])

    def test_preview_is_bounded_without_mutating_original(self):
        original = png(2048, 1536)
        preview, fmt = _preview(original)
        self.assertEqual(fmt, "png")
        with Image.open(io.BytesIO(preview)) as image:
            self.assertLessEqual(max(image.size), 1024)
        with Image.open(io.BytesIO(original)) as image:
            self.assertEqual(image.size, (2048, 1536))
