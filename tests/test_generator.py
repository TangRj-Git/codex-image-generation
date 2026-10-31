from __future__ import annotations

import base64
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image
import httpx2
from openai import APIStatusError, APITimeoutError, AsyncOpenAI

from gpt_image_generation.errors import ImageToolError
from gpt_image_generation.generator import generate, validate_options


def image_bytes(fmt="png", size=(16, 24), alpha=False):
    buffer = io.BytesIO()
    Image.new("RGBA" if alpha else "RGB", size, (40, 80, 120, 0) if alpha else (40, 80, 120)).save(buffer, format=fmt)
    return buffer.getvalue()


def image_item(fmt="png", **kwargs):
    return {"type": "image_generation_call", "status": "completed",
            "result": base64.b64encode(image_bytes(fmt)).decode("ascii"), **kwargs}


class OptionTests(unittest.TestCase):
    def options(self, **overrides):
        args = {"image_model": "gpt-image-2.5-flare", "output_format": "png", "size": "auto",
                "quality": "auto", "background": "auto", "output_compression": None,
                "action": "auto", "input_fidelity": None}
        args.update(overrides)
        return validate_options(**args)

    def test_jpeg_transparency_rejected(self):
        with self.assertRaisesRegex(ImageToolError, "JPEG"):
            self.options(output_format="jpeg", background="transparent")

    def test_webp_transparency_and_compression(self):
        result = self.options(output_format="webp", background="transparent", output_compression=75)
        self.assertEqual(result["output_compression"], 75)

    def test_compression_validation(self):
        for compression in [-1, 101, 5.5, True]:
            with self.subTest(value=compression), self.assertRaises(ImageToolError):
                self.options(output_format="jpeg", output_compression=compression)
        with self.assertRaises(ImageToolError):
            self.options(output_compression=80)

    def test_supported_flexible_sizes(self):
        for size in ["1536x864", "3840x2160", "2160x3840", "2048x2048", "1024×1024", "auto"]:
            with self.subTest(size=size):
                self.options(size=size)

    def test_invalid_flexible_sizes(self):
        for size in ["0x1024", "1024", "512x512", "1000x1024", "4096x2048", "3840x3840", "3072x512"]:
            with self.subTest(size=size), self.assertRaises(ImageToolError):
                self.options(size=size)

    def test_old_model_limits_and_unknown_alias(self):
        with self.assertRaises(ImageToolError):
            self.options(image_model="gpt-image-1.5", size="1536x864")
        self.options(image_model="custom-image-alias", size="640x480", quality="max")

    def test_quality_model_capability(self):
        for model in ["gpt-image-2.5-flare", "gpt-image-2.5-sunburst-2026-09-08"]:
            self.options(image_model=model, quality="max")
        for model in ["gpt-image-2", "gpt-image-1.5", "gpt-image-1-mini"]:
            with self.subTest(model=model), self.assertRaises(ImageToolError):
                self.options(image_model=model, quality="xhigh")

    def test_input_fidelity_model_capability(self):
        for model in ["gpt-image-2", "gpt-image-2-2026-04-21", "gpt-image-1-mini"]:
            with self.subTest(model=model), self.assertRaises(ImageToolError):
                self.options(image_model=model, input_fidelity="high")
        self.options(image_model="gpt-image-1-mini", input_fidelity="low")
        self.options(image_model="gpt-image-2.5-flare", input_fidelity="high")


class GeneratorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()
        self.config = SimpleNamespace(base_url="https://proxy.example/v1", api_key="secret-test-key",
                                      provider="custom",
                                      headers={"X-Example": "header-secret"}, query_params={"version": "1"},
                                      responses_model="gpt-6-astra", image_model="gpt-image-2.5-flare",
                                      request_timeout=30)
        self.client = MagicMock()
        self.client.__aenter__ = AsyncMock(return_value=self.client)
        self.client.__aexit__ = AsyncMock(return_value=False)
        self.client.responses.create = AsyncMock(return_value={
            "id": "resp_example", "status": "completed", "output_text": "image description",
            "output": [{"type": "message"}, image_item(revised_prompt="revised")],
        })
        self.factory_patch = patch("gpt_image_generation.generator.AsyncOpenAI", return_value=self.client)
        self.factory = self.factory_patch.start()
        self.addCleanup(self.factory_patch.stop)

    async def run_generate(self, **kwargs):
        return await generate(self.config, prompt="画一张书桌插画", project_dir=str(self.project), **kwargs)

    def written_files(self):
        return list((self.project / "outputs" / "images").glob("*"))

    async def test_responses_request_and_saved_result(self):
        result = await self.run_generate(filename="desk.png")
        request = self.client.responses.create.call_args.kwargs
        self.assertEqual(request["model"], "gpt-6-astra")
        self.assertEqual(request["tools"][0]["model"], "gpt-image-2.5-flare")
        self.assertEqual(request["tool_choice"], "required")
        self.assertEqual(request["input"], "画一张书桌插画")
        self.assertEqual(self.factory.call_args.kwargs["max_retries"], 0)
        self.assertEqual(self.factory.call_args.kwargs["default_query"], {"version": "1"})
        self.assertEqual(result.response_id, "resp_example")
        self.assertEqual(result.text, "image description")
        self.assertEqual(result.images[0].path, self.project / "outputs/images/desk.png")
        self.assertEqual(result.images[0].path.read_bytes(), image_bytes())
        self.assertEqual(result.images[0].revised_prompt, "revised")
        self.client.__aexit__.assert_awaited_once()

    async def test_infers_jpeg_from_filename(self):
        self.client.responses.create.return_value["output"] = [image_item("jpeg")]
        result = await self.run_generate(filename="photo.jpg", output_compression=80)
        self.assertEqual(self.client.responses.create.call_args.kwargs["tools"][0]["output_format"], "jpeg")
        self.assertEqual(result.images[0].mime_type, "image/jpeg")

    async def test_invalid_options_never_call_upstream(self):
        with self.assertRaises(ImageToolError):
            await self.run_generate(output_format="jpeg", background="transparent")
        self.factory.assert_not_called()

    async def test_existing_file_never_calls_upstream(self):
        (self.project / "desk.png").write_bytes(b"original")
        with self.assertRaises(ImageToolError):
            await self.run_generate(output_dir=".", filename="desk.png")
        self.factory.assert_not_called()

    async def test_multiple_items_not_just_first(self):
        self.client.responses.create.return_value["output"] = [image_item(), {"type": "message"}, image_item()]
        result = await self.run_generate(filename="desk.png")
        self.assertEqual([image.path.name for image in result.images], ["desk_1.png", "desk_2.png"])

    async def test_missing_or_bad_image_results_save_nothing(self):
        bad_outputs = [[], [{"type": "message"}], [image_item(result="%%%")], [image_item(result="")],
                       [image_item(result=base64.b64encode(b"not image bytes").decode())],
                       [image_item(), image_item(status="failed")], [image_item("jpeg")]]
        for output in bad_outputs:
            with self.subTest(output_type=len(output)):
                self.client.responses.create.return_value["output"] = output
                with self.assertRaises(ImageToolError):
                    await self.run_generate()
                self.assertEqual(self.written_files(), [])

    async def test_incomplete_response_rejected(self):
        self.client.responses.create.return_value["status"] = "incomplete"
        with self.assertRaises(ImageToolError):
            await self.run_generate()
        self.assertEqual(self.written_files(), [])

    async def test_reference_and_mask_are_data_urls(self):
        (self.project / "reference.png").write_bytes(image_bytes())
        (self.project / "mask.png").write_bytes(image_bytes(alpha=True))
        await self.run_generate(reference_images=["reference.png"], mask_image="mask.png", action="edit")
        request = self.client.responses.create.call_args.kwargs
        self.assertTrue(request["input"][0]["content"][1]["image_url"].startswith("data:image/png;base64,"))
        self.assertTrue(request["tools"][0]["input_image_mask"]["image_url"].startswith("data:image/png;base64,"))

    async def test_invalid_mask_rejected_before_request(self):
        (self.project / "reference.png").write_bytes(image_bytes())
        for data in [image_bytes(), image_bytes(size=(32, 32), alpha=True), image_bytes("webp", alpha=True)]:
            (self.project / "mask.png").write_bytes(data)
            with self.subTest(size=len(data)), self.assertRaises(ImageToolError):
                await self.run_generate(reference_images=["reference.png"], mask_image="mask.png")
        self.factory.assert_not_called()

    async def test_edit_requires_input(self):
        with self.assertRaises(ImageToolError):
            await self.run_generate(action="edit")
        self.factory.assert_not_called()

    async def test_followup_response_id_forwarded(self):
        await self.run_generate(action="edit", previous_response_id="resp_previous")
        self.assertEqual(self.client.responses.create.call_args.kwargs["previous_response_id"], "resp_previous")

    async def test_provider_size_and_transparency_mismatch_are_reported(self):
        result = await self.run_generate(size="1024x1024", background="transparent")
        self.assertEqual(len(result.images), 1)
        self.assertEqual(len(result.warnings), 2)
        self.assertIn("16x24", result.warnings[0])
        self.assertIn("alpha", result.warnings[1])

    async def test_auth_errors_are_sanitized(self):
        response = SimpleNamespace(status_code=401, headers={"x-request-id": "secret-test-key"}, request=SimpleNamespace())
        self.client.responses.create.side_effect = APIStatusError("secret-test-key header-secret", response=response, body={"secret": "secret-test-key"})
        with self.assertRaises(ImageToolError) as captured:
            await self.run_generate()
        self.assertNotIn("secret-test-key", str(captured.exception))
        self.assertNotIn("header-secret", str(captured.exception))
        self.assertIn("认证", str(captured.exception))
        self.client.responses.create.assert_awaited_once()

    async def test_timeout_has_no_automatic_retry(self):
        self.client.responses.create.side_effect = APITimeoutError(SimpleNamespace())
        with self.assertRaisesRegex(ImageToolError, "重复计费"):
            await self.run_generate()
        self.client.responses.create.assert_awaited_once()

    async def test_rate_limit_has_safe_message(self):
        response = SimpleNamespace(status_code=429, headers={}, request=SimpleNamespace())
        self.client.responses.create.side_effect = APIStatusError("private upstream detail", response=response, body={})
        with self.assertRaisesRegex(ImageToolError, "429") as captured:
            await self.run_generate()
        self.assertNotIn("private upstream detail", str(captured.exception))


class SDKHeaderAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_sdk_uses_header_auth_without_dummy_bearer(self):
        cases = [
            ("", {"X-API-Key": "custom-key"}),
            ("", {"Authorization": "Basic dXNlcjpwYXNz"}),
            ("selected-key", {}),
            ("selected-key", {"OpenAI-Organization": "selected-org", "openai-project": "selected-project", "X-Custom": "selected-custom"}),
        ]
        for api_key, headers in cases:
            with self.subTest(api_key=bool(api_key), headers=list(headers)), tempfile.TemporaryDirectory() as project:
                captured = []

                def handler(request):
                    captured.append(request)
                    return httpx2.Response(200, json={
                        "id": "resp_mock", "object": "response", "status": "completed",
                        "created_at": 0, "model": "gpt-6-astra",
                        "output": [image_item(id="ig_mock")],
                    })

                def client_factory(**kwargs):
                    self.assertNotIn("_enforce_credentials", kwargs)
                    return AsyncOpenAI(**kwargs, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))

                config = SimpleNamespace(base_url="https://proxy.example/v1", api_key=api_key, provider="custom",
                                         headers=headers, query_params={}, responses_model="gpt-6-astra",
                                         image_model="gpt-image-2.5-flare", request_timeout=30)
                ambient = {"OPENAI_API_KEY": "unrelated-key", "OPENAI_ADMIN_KEY": "unrelated-admin",
                           "OPENAI_ORG_ID": "unrelated-org", "OPENAI_PROJECT_ID": "unrelated-project",
                           "OPENAI_CUSTOM_HEADERS": "Authorization: Bearer unrelated-fixture\nX-Custom: unrelated-custom\nX-Other: unrelated-other"}
                with patch.dict(os.environ, ambient), patch("gpt_image_generation.generator.AsyncOpenAI", side_effect=client_factory):
                    result = await generate(config, prompt="Draw a desk", project_dir=project)
                self.assertEqual(len(result.images), 1)
                self.assertEqual(len(captured), 1)
                self.assertEqual(str(captured[0].url), "https://proxy.example/v1/responses")
                if api_key:
                    self.assertEqual(captured[0].headers["Authorization"], f"Bearer {api_key}")
                elif "Authorization" in headers:
                    self.assertEqual(captured[0].headers["Authorization"], headers["Authorization"])
                else:
                    self.assertNotIn("Authorization", captured[0].headers)
                    self.assertEqual(captured[0].headers["X-API-Key"], "custom-key")
                self.assertNotIn("X-Other", captured[0].headers)
                for name in ["OpenAI-Organization", "openai-project", "X-Custom"]:
                    if name in headers:
                        self.assertEqual(captured[0].headers[name], headers[name])
                    else:
                        self.assertNotIn(name, captured[0].headers)


if __name__ == "__main__":
    unittest.main()
