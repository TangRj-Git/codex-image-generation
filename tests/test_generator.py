from __future__ import annotations

import asyncio
import base64
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx2
from openai import APIStatusError, AsyncOpenAI
from PIL import Image

from codex_image_generation.errors import ImageToolError
from codex_image_generation import generator
from codex_image_generation.generator import generate, validate_options
from codex_image_generation.model_selection import SelectedModels


def image_bytes(fmt="png", alpha=255, size=(16, 24)):
    output = io.BytesIO()
    image = Image.new("RGBA", size, (40, 80, 120, alpha))
    if fmt == "jpeg":
        image = image.convert("RGB")
    image.save(output, format=fmt)
    return output.getvalue()


def image_item(identifier="ig_fixture", fmt="png", **kwargs):
    return {"id": identifier, "type": "image_generation_call", "status": "completed",
            "result": base64.b64encode(image_bytes(fmt)).decode(), "revised_prompt": "revised prompt", **kwargs}


def response(items=None, **kwargs):
    return {"id": "resp_fixture", "object": "response", "created_at": 0,
            "status": "completed", "model": "session-model", "output": [image_item()] if items is None else items,
            "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}, **kwargs}


def config(**kwargs):
    return SimpleNamespace(**({"base_url": "https://proxy.example/v1", "api_key": "fixture-selected-key",
                              "provider": "custom", "headers": {}, "query_params": {},
                              "model": "session-model", "request_timeout": 30} | kwargs))


def client(result=None):
    instance = MagicMock()
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=False)
    instance.responses.create = AsyncMock(return_value=response() if result is None else result)
    return instance


async def selected_models(settings, *, responses_model=None, image_model=None, model_policy=None):
    model = responses_model or settings.model
    if model is None:
        raise ImageToolError("No model was selected")
    return SelectedModels(model, "caller", image_model, "caller", model_policy or "quality")


class OptionTests(unittest.TestCase):
    def test_minimal_tool_has_no_model_or_unrequested_defaults(self):
        self.assertEqual(validate_options(output_format="png"), {"type": "image_generation", "output_format": "png"})

    def test_syntax_without_model_family_assumptions(self):
        for size in ["512x512", "1000x700", "4096x4096", "1024×768", "auto"]:
            with self.subTest(size=size):
                tool = validate_options(output_format="png", size=size, quality="max", input_fidelity="high")
                self.assertNotIn("model", tool)
        for size in ["0x1024", "-1x50", "1024", "1.5x20", "Auto x Auto"]:
            with self.subTest(size=size), self.assertRaises(ImageToolError):
                validate_options(output_format="png", size=size)

    def test_format_constraints(self):
        with self.assertRaises(ImageToolError):
            validate_options(output_format="jpeg", background="transparent")
        with self.assertRaises(ImageToolError):
            validate_options(output_format="png", output_compression=75)
        self.assertEqual(validate_options(output_format="webp", output_compression=0)["output_compression"], 0)

    def test_strict_integer_ranges(self):
        for value in [True, -1, 101, 2.5]:
            with self.subTest(compression=value), self.assertRaises(ImageToolError):
                validate_options(output_format="jpeg", output_compression=value)
        for value in [True, -1, 4, 1.5]:
            with self.subTest(partials=value), self.assertRaises(ImageToolError):
                validate_options(output_format="png", partial_images=value)


class GenerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cwd = Path(self.temp.name).resolve()
        self.client = client()
        self.mock = patch.object(generator, "AsyncOpenAI", return_value=self.client)
        self.factory = self.mock.start()
        self.addCleanup(self.mock.stop)
        selection_patch = patch.object(generator, "select_models", side_effect=selected_models)
        self.selection = selection_patch.start()
        self.addCleanup(selection_patch.stop)

    async def call(self, **kwargs):
        settings = kwargs.pop("settings", config())
        return await generate(settings, prompt="Draw a desk", cwd=str(self.cwd), **kwargs)

    def all_files(self):
        return [path for path in self.cwd.rglob("*") if path.is_file()]

    async def test_request_and_result_metadata(self):
        self.client.responses.create.return_value["_request_id"] = "req_fixture"
        result = await self.call(filename="desk.png", safety_identifier="hashed-user-id")
        request = self.client.responses.create.call_args.kwargs
        self.assertEqual(request["model"], "session-model")
        self.assertEqual(request["tools"], [{"type": "image_generation", "output_format": "png"}])
        self.assertEqual(request["tool_choice"], {"type": "image_generation"})
        self.assertEqual(request["safety_identifier"], "hashed-user-id")
        self.assertNotIn("safety_identifier", request["tools"][0])
        self.assertEqual(result.status, "success")
        self.assertEqual(result.response_id, "resp_fixture")
        self.assertEqual(result.request_id, "req_fixture")
        self.assertEqual(result.usage["total_tokens"], 30)
        self.assertEqual(result.images[0].call_id, "ig_fixture")
        self.assertEqual(result.images[0].path, self.cwd / "desk.png")
        self.assertEqual(self.factory.call_args.kwargs["max_retries"], 0)

    async def test_missing_model_rejected_locally(self):
        with self.assertRaises(ImageToolError):
            await self.call(settings=config(model=None))
        self.factory.assert_not_called()

    async def test_two_models_and_tool_usage_are_preserved(self):
        self.client.responses.create.return_value["tool_usage"] = {
            "image_gen": {"input_tokens": 100, "output_tokens": 200, "total_tokens": 300},
        }
        result = await self.call(responses_model="chosen-dialogue", image_model="gpt-image-2.5-sunburst")
        request = self.client.responses.create.call_args.kwargs
        self.assertEqual(request["model"], "chosen-dialogue")
        self.assertEqual(request["tools"][0]["model"], "gpt-image-2.5-sunburst")
        self.assertEqual(result.models["image_model_selected"], "gpt-image-2.5-sunburst")
        self.assertIsNone(result.models["image_model_reported"])
        self.assertEqual(result.model, "session-model")
        self.assertEqual(result.tool_usage["image_gen"]["total_tokens"], 300)
        self.assertEqual(result.usage["total_tokens"], 30)

    async def test_incompatible_image_model_options_never_generate(self):
        with self.assertRaises(ImageToolError):
            await self.call(image_model="gpt-image-2", quality="max")
        self.factory.assert_not_called()
        self.assertFalse(self.all_files())
        self.assertEqual(self.all_files(), [])

    async def test_format_inference_and_provider_mismatch_preserved(self):
        self.client.responses.create.return_value = response([image_item(fmt="jpeg")])
        result = await self.call(filename="photo.jpg", output_compression=75)
        self.assertEqual(result.images[0].mime_type, "image/jpeg")
        self.assertEqual(self.client.responses.create.call_args.kwargs["tools"][0]["output_format"], "jpeg")
        self.client.responses.create.return_value = response()
        result = await self.call(filename="provider-ignored-format.jpg")
        self.assertEqual(result.images[0].path.suffix, ".png")
        self.assertTrue(result.warnings)

    async def test_completed_results_survive_incomplete_parent_and_failed_sibling(self):
        self.client.responses.create.return_value = response(
            [image_item(), image_item("ig_failed", status="failed", result=None)], status="incomplete")
        result = await self.call(filename="partial.png")
        self.assertEqual(result.status, "partial_success")
        self.assertEqual(len(result.images), 1)
        self.assertEqual(result.response_id, "resp_fixture")
        self.assertEqual({error["code"] for error in result.errors}, {"response_incomplete", "image_generation_failed"})
        self.assertTrue(result.images[0].path.exists())

    async def test_bad_base64_does_not_discard_good_image(self):
        self.client.responses.create.return_value = response([image_item("bad", result="%%%"), image_item()])
        result = await self.call()
        self.assertEqual(result.status, "partial_success")
        self.assertEqual(len(result.images), 1)
        self.assertEqual(result.errors[0]["call_id"], "bad")

    async def test_no_images_is_structured_error(self):
        self.client.responses.create.return_value = response([])
        result = await self.call()
        self.assertEqual(result.status, "error")
        self.assertEqual(result.errors[0]["code"], "no_images")
        self.assertEqual(result.response_id, "resp_fixture")
        self.assertEqual(self.all_files(), [])

    async def test_remote_file_and_historical_inputs_are_forwarded(self):
        result = await self.call(input_images=[{"image_url": "https://images.example/picture.png", "detail": "original"},
                                              {"file_id": "file_example"}], mask={"file_id": "file_mask"},
                                 previous_response_id="resp_previous", previous_image_ids=["ig_previous"], action="edit")
        request = self.client.responses.create.call_args.kwargs
        self.assertEqual(request["input"][0], {"type": "image_generation_call", "id": "ig_previous"})
        self.assertEqual(request["input"][1]["content"][1]["image_url"], "https://images.example/picture.png")
        self.assertEqual(request["input"][1]["content"][1]["detail"], "original")
        self.assertEqual(request["input"][1]["content"][2]["file_id"], "file_example")
        self.assertEqual(request["tools"][0]["input_image_mask"], {"file_id": "file_mask"})
        self.assertEqual(result.status, "success")

    async def test_local_and_data_mask_validation(self):
        (self.cwd / "reference.png").write_bytes(image_bytes())
        mask_url = "data:image/png;base64," + base64.b64encode(image_bytes(alpha=0)).decode()
        await self.call(input_images=[{"path": "reference.png"}], mask={"image_url": mask_url}, action="edit")
        request = self.client.responses.create.call_args.kwargs
        self.assertTrue(request["input"][0]["content"][1]["image_url"].startswith("data:image/png;base64,"))
        self.assertEqual(request["tools"][0]["input_image_mask"]["image_url"], mask_url)

    async def test_invalid_sources_never_reach_upstream_and_release_reservation(self):
        invalid = [{"path": "x.png", "file_id": "file_x"}, {"image_url": "ftp://example/image.png"},
                   {"file_id": "file_x", "detail": "unsupported"}, {"image_url": "data:image/jpeg;base64," + base64.b64encode(image_bytes()).decode()}]
        for source in invalid:
            with self.subTest(source=list(source)), self.assertRaises(ImageToolError):
                await self.call(input_images=[source], filename="reusable.png")
        self.factory.assert_not_called()
        self.assertEqual(self.all_files(), [])

    async def test_local_input_budget(self):
        data = image_bytes()
        (self.cwd / "ref.png").write_bytes(data)
        with patch.object(generator, "MAX_INPUT_TOTAL_BYTES", len(data)), self.assertRaises(ImageToolError):
            await self.call(input_images=[{"path": "ref.png"}, {"path": "ref.png"}])
        self.factory.assert_not_called()

    async def test_output_budget_retains_first_valid_asset(self):
        self.client.responses.create.return_value = response([image_item("one"), image_item("two")])
        with patch.object(generator, "MAX_OUTPUT_IMAGES", 1):
            result = await self.call()
        self.assertEqual(result.status, "partial_success")
        self.assertEqual(len(result.images), 1)

    async def test_opaque_rgba_warns_for_transparency(self):
        result = await self.call(background="transparent")
        self.assertTrue(any("透明像素" in warning for warning in result.warnings))

    async def test_http_error_is_safe_and_retains_request_id(self):
        mock_response = SimpleNamespace(status_code=429, headers={"x-request-id": "req_failure"}, request=SimpleNamespace())
        self.client.responses.create.side_effect = APIStatusError("fixture-selected-key private body", response=mock_response, body={"token": "fixture-selected-key"})
        result = await self.call()
        self.assertEqual(result.request_id, "req_failure")
        self.assertEqual(result.status, "error")
        self.assertEqual(result.errors[0]["http_status"], 429)
        self.assertNotIn("fixture-selected-key", repr(result))
        self.client.responses.create.assert_awaited_once()
        self.assertEqual(self.all_files(), [])

    async def test_worker_does_not_block_loop_and_deadline_prevents_upstream(self):
        original = generator._prepare_input
        started = threading.Event()
        ticks = []

        def slow_input(*args):
            started.set()
            time.sleep(0.12)
            return original(*args)

        async def beat():
            for _ in range(10):
                ticks.append(time.monotonic())
                await asyncio.sleep(0.01)

        with patch.object(generator, "_prepare_input", side_effect=slow_input):
            heartbeat = asyncio.create_task(beat())
            result = await self.call(settings=config(request_timeout=0.03))
            await heartbeat
        self.assertEqual(result.errors[0]["code"], "timeout")
        self.assertGreaterEqual(len(ticks), 10)
        self.assertLess(max(b-a for a,b in zip(ticks,ticks[1:])), 0.09)
        self.factory.assert_not_called()
        self.assertEqual(self.all_files(), [])

    async def test_cancel_prepare_waits_and_releases_new_reservation(self):
        original = generator.prepare_save
        started = threading.Event()

        def slow_prepare(*args):
            plan = original(*args)
            started.set()
            time.sleep(0.08)
            return plan

        with patch.object(generator, "prepare_save", side_effect=slow_prepare):
            task = asyncio.create_task(self.call(filename="cancelled.png"))
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.all_files(), [])
        self.factory.assert_not_called()

    async def test_cancel_during_save_finishes_worker_and_preserves_completed_file(self):
        original = generator.save_images
        started = threading.Event()

        def slow_save(*args):
            started.set()
            time.sleep(0.08)
            return original(*args)

        with patch.object(generator, "save_images", side_effect=slow_save):
            task = asyncio.create_task(self.call(filename="completed.png"))
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual([path.name for path in self.all_files()], ["completed.png"])

    async def test_concurrent_explicit_name_is_rejected_before_second_request(self):
        entered, finish = asyncio.Event(), asyncio.Event()

        async def wait_upstream(**kwargs):
            entered.set()
            await finish.wait()
            return response()

        self.client.responses.create.side_effect = wait_upstream
        first = asyncio.create_task(self.call(filename="same.png"))
        await asyncio.wait_for(entered.wait(), 2)
        try:
            with self.assertRaises(ImageToolError):
                await self.call(filename="same.png")
            self.assertEqual(self.client.responses.create.await_count, 1)
        finally:
            finish.set()
        self.assertEqual((await first).status, "success")


class BytesStream(httpx2.AsyncByteStream):
    def __init__(self, chunks, *, broken=False, wait_after=None):
        self.chunks, self.broken, self.wait_after = chunks, broken, wait_after
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
            await asyncio.sleep(0)
        if self.wait_after:
            await asyncio.sleep(self.wait_after)
        if self.broken:
            raise httpx2.ReadError("fixture-selected-key transport body")

    async def aclose(self):
        self.closed = True


def sse(event):
    return ("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode()


class SDKTests(unittest.IsolatedAsyncioTestCase):
    async def run_sdk(self, handler, *, settings=None, **kwargs):
        captured = []

        async def wrapped(request):
            captured.append(request)
            result = handler(request)
            return await result if asyncio.iscoroutine(result) else result

        def factory(**options):
            self.assertNotIn("_enforce_credentials", options)
            return AsyncOpenAI(**options, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(wrapped)))

        with tempfile.TemporaryDirectory() as cwd, patch.object(generator, "AsyncOpenAI", side_effect=factory), patch.object(
            generator, "select_models", side_effect=selected_models,
        ):
            result = await generate(settings or config(), prompt="Draw an image", cwd=cwd, **kwargs)
            self.assertTrue(all(image.path.exists() for image in result.images))
        return result, captured

    async def test_real_sdk_header_auth_and_environment_isolation(self):
        ambient = {"OPENAI_API_KEY": "unrelated-key", "OPENAI_ADMIN_KEY": "unrelated-admin",
                   "OPENAI_ORG_ID": "unrelated-org", "OPENAI_PROJECT_ID": "unrelated-project",
                   "OPENAI_CUSTOM_HEADERS": "Authorization: Bearer unrelated-fixture\nX-Other: unrelated-other"}
        for key, headers in [("fixture-selected-key", {}), ("", {"X-API-Key": "custom-key"}),
                             ("", {"Authorization": "Basic dXNlcjpwYXNz"})]:
            with self.subTest(headers=list(headers)), patch.dict(os.environ, ambient):
                result, captured = await self.run_sdk(lambda _: httpx2.Response(200, headers={"x-request-id": "req_sdk"}, json=response()),
                                                       settings=config(api_key=key, headers=headers))
            request = captured[0]
            body = json.loads(request.content)
            self.assertEqual(body["model"], "session-model")
            self.assertNotIn("model", body["tools"][0])
            self.assertEqual(result.request_id, "req_sdk")
            expected = f"Bearer {key}" if key else headers.get("Authorization")
            self.assertEqual(request.headers.get("Authorization"), expected)
            if "X-API-Key" in headers:
                self.assertEqual(request.headers["X-API-Key"], headers["X-API-Key"])
            for name in ["OpenAI-Organization", "OpenAI-Project", "X-Other"]:
                self.assertNotIn(name, request.headers)

    def stream_events(self, terminal=True):
        events = [
            {"type": "response.created", "sequence_number": 0, "response": response([], status="in_progress")},
            {"type": "response.image_generation_call.partial_image", "sequence_number": 1, "item_id": "ig_fixture", "output_index": 0,
             "partial_image_index": 0, "partial_image_b64": base64.b64encode(image_bytes()).decode()},
            {"type": "response.output_item.done", "sequence_number": 2, "output_index": 0, "item": image_item()},
        ]
        if terminal:
            events.append({"type": "response.completed", "sequence_number": 3, "response": response()})
        return [sse(event) for event in events]

    async def test_real_sse_partial_progress_terminal_deduplication(self):
        stream = BytesStream(self.stream_events())
        progress = []

        async def notify(event):
            progress.append(event)

        result, captured = await self.run_sdk(lambda _: httpx2.Response(200, headers={"content-type": "text/event-stream", "x-request-id": "req_stream"}, stream=stream),
                                               partial_images=2, on_progress=notify)
        self.assertEqual(result.status, "success")
        self.assertEqual(len(result.images), 1)
        self.assertEqual(len(result.partials), 1)
        self.assertEqual(result.partials[0].metadata["partial_index"], 0)
        self.assertEqual(result.request_id, "req_stream")
        self.assertTrue(stream.closed)
        self.assertTrue(any(event["stage"] == "partial_image" for event in progress))
        body = json.loads(captured[0].content)
        self.assertTrue(body["stream"])
        self.assertEqual(body["tools"][0]["partial_images"], 2)

    async def test_stream_disconnect_retains_completed_asset(self):
        stream = BytesStream(self.stream_events(terminal=False), broken=True)
        result, requests = await self.run_sdk(lambda _: httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=stream), partial_images=1)
        self.assertEqual(result.status, "partial_success")
        self.assertEqual(len(result.images), 1)
        self.assertEqual(result.response_id, "resp_fixture")
        self.assertTrue(any(error["code"] == "stream_interrupted" for error in result.errors))
        self.assertEqual(len(requests), 1)
        self.assertNotIn("fixture-selected-key", repr(result.errors))
        self.assertTrue(stream.closed)

    async def test_streamed_final_response_keeps_selected_models_and_image_usage(self):
        events = self.stream_events(terminal=False)
        events.append(sse({"type": "response.completed", "sequence_number": 3, "response": response(
            tool_usage={"image_gen": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}},
        )}))
        stream = BytesStream(events)
        result, requests = await self.run_sdk(
            lambda _: httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=stream),
            partial_images=1, image_model="gpt-image-2.5-sunburst",
        )
        self.assertEqual(result.status, "success")
        self.assertEqual(result.models["image_model_selected"], "gpt-image-2.5-sunburst")
        self.assertEqual(result.tool_usage["image_gen"]["total_tokens"], 30)
        self.assertEqual(len(requests), 1)
        self.assertTrue(stream.closed)

    async def test_stream_timeout_retains_completed_asset(self):
        stream = BytesStream(self.stream_events(terminal=False), wait_after=1)
        result, requests = await self.run_sdk(lambda _: httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=stream),
                                              settings=config(request_timeout=0.12), partial_images=1)
        self.assertEqual(result.status, "partial_success")
        self.assertEqual(len(result.images), 1)
        self.assertTrue(any(error["code"] == "timeout" for error in result.errors))
        self.assertTrue(stream.closed)
        self.assertEqual(len(requests), 1)

    async def test_partial_budget_is_bounded_without_losing_final(self):
        with patch.object(generator, "MAX_PARTIAL_TOTAL_BYTES", 8):
            stream = BytesStream(self.stream_events())
            result, _ = await self.run_sdk(lambda _: httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=stream), partial_images=1)
        self.assertEqual(result.partials, [])
        self.assertEqual(len(result.images), 1)
        self.assertTrue(result.warnings)


if __name__ == "__main__":
    unittest.main()
