"""In-memory and real stdio MCP checks; no upstream generation requests."""

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

from gpt_image_generation.errors import ImageToolError
from gpt_image_generation.server import _preview, configuration_status, mcp


def png(width: int = 32, height: int = 24) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", (width, height), (40, 80, 120, 128)).save(buffer, "PNG")
    return buffer.getvalue()


def fake_config() -> SimpleNamespace:
    return SimpleNamespace(
        responses_model="gpt-6-astra",
        image_model="gpt-image-2.5-flare",
        model_source="test",
        public_status=lambda: {"provider": "test", "credential_source": "test fixture"},
    )


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_schema_exposes_paths_and_image_options_not_credentials(self):
        async with Client(mcp) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
        self.assertEqual(set(tools), {"generate_image", "get_image_generation_status"})
        schema = tools["generate_image"].input_schema
        self.assertEqual(set(schema["required"]), {"prompt", "project_dir"})
        for name in ("output_format", "size", "quality", "background", "mask_image", "output_compression"):
            self.assertIn(name, schema["properties"])
        for name in ("api_key", "base_url", "token"):
            self.assertNotIn(name, schema["properties"])

    async def test_tool_returns_preview_and_structured_absolute_path(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "book.png"
            image = SimpleNamespace(
                path=target, data=png(), mime_type="image/png", width=32, height=24,
                revised_prompt="A book on a desk",
            )
            result = SimpleNamespace(images=[image], text="Done", response_id="resp_test", warnings=[])
            with patch("gpt_image_generation.server.resolve_config", return_value=fake_config()), patch(
                "gpt_image_generation.server.generate", new_callable=AsyncMock, return_value=result
            ) as generator:
                async with Client(mcp) as client:
                    response = await client.call_tool_mcp(
                        "generate_image", {"prompt": "Draw a book", "project_dir": directory}
                    )
            self.assertFalse(response.is_error)
            self.assertTrue(any(block.type == "image" for block in response.content))
            self.assertEqual(response.structured_content["images"][0]["path"], str(target.resolve()))
            self.assertEqual(response.structured_content["responses_model"], "gpt-6-astra")
            generator.assert_awaited_once()

    async def test_no_preview_option(self):
        image = SimpleNamespace(
            path=Path("book.png").resolve(), data=png(), mime_type="image/png", width=32,
            height=24, revised_prompt=None,
        )
        result = SimpleNamespace(images=[image], text="", response_id="r", warnings=[])
        with patch("gpt_image_generation.server.resolve_config", return_value=fake_config()), patch(
            "gpt_image_generation.server.generate", new_callable=AsyncMock, return_value=result
        ):
            async with Client(mcp) as client:
                response = await client.call_tool_mcp("generate_image", {
                    "prompt": "Draw a book", "project_dir": str(Path.cwd()), "include_preview": False,
                })
        self.assertFalse(any(block.type == "image" for block in response.content))
        self.assertFalse(response.structured_content["images"][0]["preview_included"])

    async def test_status_does_not_call_generator(self):
        with patch("gpt_image_generation.server.resolve_config", return_value=fake_config()), patch(
            "gpt_image_generation.server.generate", new_callable=AsyncMock
        ) as generator:
            async with Client(mcp) as client:
                response = await client.call_tool_mcp("get_image_generation_status", {})
        self.assertTrue(response.structured_content["configured"])
        self.assertFalse(response.structured_content["connection_verified"])
        generator.assert_not_awaited()

    async def test_unexpected_error_is_not_exposed(self):
        secret = "secret-provider-key-should-never-appear"
        with patch("gpt_image_generation.server.resolve_config", return_value=fake_config()), patch(
            "gpt_image_generation.server.generate", new_callable=AsyncMock,
            side_effect=RuntimeError(secret),
        ):
            async with Client(mcp) as client:
                response = await client.call_tool_mcp("generate_image", {
                    "prompt": "Draw", "project_dir": str(Path.cwd()),
                })
        self.assertTrue(response.is_error)
        self.assertNotIn(secret, str(response))

    async def test_stdio_handshake_and_status(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            config.write_text(
                'model_provider = "test"\nmodel = "gpt-6-astra"\n'
                '[model_providers.test]\nbase_url = "https://example.invalid/v1"\n'
                'experimental_bearer_token = "stdio-test-key"\n', encoding="utf-8",
            )
            with (Path(directory) / "stderr.log").open("w", encoding="utf-8") as log:
                transport = StdioTransport(
                    command=sys.executable,
                    args=["-B", "-m", "gpt_image_generation", "--codex-home", directory],
                    env={"PYTHONUTF8": "1", "CODEX_HOME": directory},
                    cwd=str(Path(__file__).resolve().parents[1]),
                    keep_alive=False,
                    log_file=log,
                )
                async with asyncio.timeout(40):
                    async with Client(transport) as client:
                        names = {tool.name for tool in await client.list_tools()}
                        status = await client.call_tool_mcp("get_image_generation_status", {})
            self.assertIn("generate_image", names)
            self.assertTrue(status.structured_content["configured"])
            self.assertFalse(status.structured_content["connection_verified"])
            self.assertNotIn("stdio-test-key", str(status))


class StatusAndPreviewTests(unittest.TestCase):
    def test_config_failure_is_a_diagnostic(self):
        with patch("gpt_image_generation.server.resolve_config", side_effect=ImageToolError("Missing configuration")):
            status = configuration_status()
        self.assertFalse(status["configured"])
        self.assertFalse(status["connection_verified"])

    def test_preview_is_bounded_and_preserves_alpha(self):
        original = png(2048, 1536)
        preview, image_format = _preview(original)
        self.assertEqual(image_format, "png")
        with Image.open(io.BytesIO(preview)) as image:
            self.assertLessEqual(max(image.size), 1024)
            self.assertIn("A", image.getbands())
        with Image.open(io.BytesIO(original)) as image:
            self.assertEqual(image.size, (2048, 1536))
