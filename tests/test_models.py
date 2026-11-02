"""Model discovery and selection contracts, using public-doc fixtures and fake APIs."""

from __future__ import annotations

import json
import base64
import io
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx2
from openai import AsyncOpenAI
from PIL import Image

from codex_image_generation.config import ResolvedConfig
from codex_image_generation.errors import ImageToolError
from codex_image_generation import model_catalog as catalog_module, model_selection as selection
from codex_image_generation.model_cache import read_cache, write_cache
from codex_image_generation.model_catalog import GUIDE_URL, apply_featured_recommendations, bundled_catalog, enrich_model, official_catalog, parse_guide
from codex_image_generation.model_selection import select_models, validate_model_options


GUIDE = '''<html><nav>gpt-image-bogus</nav><main>
<h1>Image generation</h1>
<p>The image generation tool uses <code>gpt-image-2.5-sunburst</code>,
<code>gpt-image-2.5-flare</code>, and <code>gpt-image-3-example</code>.</p>
<p>Set the image_generation tool's model to <code>gpt-image-3-example</code> for precise editing.</p>
<p><code>gpt-image-1</code> was shut down on October 23, 2026.</p>
<h2>Usage</h2><pre>model="gpt-6-astra"</pre>
<h2>Supported models</h2><p>Use these in the top-level model field.</p>
<ul><li>gpt-6-astra</li><li>gpt-6.1-sol</li><li>gpt-7-example</li></ul>
<h2>Unrelated</h2><p>gpt-unrelated</p>
<script>gpt-image-script-injection</script></main></html>'''


def catalog():
    return {**bundled_catalog(), "fetched_at": time.time(), "source": "official", "stale": False, "warnings": []}


def provider(*ids, source="provider"):
    return {"models": [{"id": identifier, "shutdown_date": None} for identifier in ids],
            "fetched_at": time.time(), "source": source, "stale": False, "warnings": []}


class CatalogTests(unittest.TestCase):
    def test_html_separates_image_models_from_top_level_models(self):
        result = parse_guide(GUIDE)
        self.assertEqual({m["id"] for m in result["images"]},
                         {"gpt-image-2.5-sunburst", "gpt-image-2.5-flare", "gpt-image-3-example"})
        self.assertEqual({m["id"] for m in result["responses"]}, {"gpt-6-astra", "gpt-6.1-sol", "gpt-7-example"})
        best = max(result["images"], key=lambda m: m["quality_rank"])
        self.assertEqual(best["id"], "gpt-image-3-example")
        self.assertEqual(best["capabilities"], {})

    def test_markdown_and_changed_structure(self):
        result = parse_guide('''# Image generation
The image generation tool uses `gpt-image-2.5-sunburst`.

`gpt-image-1` was shut down on October 23, 2026.

## Usage
sample
## Supported models
- `gpt-6-astra`
''')
        self.assertEqual([m["id"] for m in result["images"]], ["gpt-image-2.5-sunburst"])
        for body in ("<html>Access denied</html>", "# Image generation\nNo model list"):
            with self.assertRaises(ValueError):
                parse_guide(body)

    def test_model_descriptions_and_snapshots(self):
        entry = parse_guide(GUIDE)["images"][-1]
        extra = enrich_model(entry, '''# GPT Image Example
- Model ID: gpt-image-3-example
Our most capable model for image generation and editing.
## Snapshots
- gpt-image-3-example
- gpt-image-3-example-2027-01-01
''')
        self.assertEqual(entry["ranking_reason"], "official_most_capable_description")
        self.assertEqual(extra[0]["id"], "gpt-image-3-example-2027-01-01")
        self.assertEqual(extra[0]["alias_of"], entry["id"])

    def test_new_featured_flagship_supersedes_historical_recommendation(self):
        directory = parse_guide(GUIDE)
        apply_featured_recommendations(directory, '''<html><main>
<h1>Models</h1><h2>Featured models</h2>
<a href="/api/docs/models/gpt-7-example.md">GPT 7 Example</a>
<p>Our most capable model for the most demanding work.</p>
<a href="/api/docs/models/gpt-6-astra">GPT 6 Astra</a><p>Previous flagship.</p>
<h2>Other models</h2></main></html>''')
        chosen = max(directory["responses"], key=lambda item: item["quality_rank"])
        self.assertEqual(chosen["id"], "gpt-7-example")
        self.assertEqual(chosen["ranking_reason"], "official_featured_recommendation")

    def test_model_specific_options(self):
        for identifier, options in [
            ("gpt-image-2.5-sunburst", {"size": "1536x864", "quality": "max"}),
            ("gpt-image-2.5-flare-2026-09-08", {"size": "2160x3840"}),
            ("gpt-image-1.5", {"size": "1024x1536", "input_fidelity": "high"}),
            ("custom-image", {"size": "1000x700", "quality": "max"}),
        ]:
            validate_model_options(identifier, options)
        for identifier, options in [
            ("gpt-image-2", {"quality": "max"}),
            ("gpt-image-2", {"input_fidelity": "high"}),
            ("gpt-image-1-mini", {"input_fidelity": "high"}),
            ("gpt-image-1.5", {"size": "1536x864"}),
            ("gpt-image-2.5-sunburst", {"size": "1000x700"}),
            ("gpt-image-2.5-sunburst", {"size": "512x512"}),
            ("gpt-image-2.5-sunburst", {"size": "4096x1024"}),
            ("gpt-image-2.5-sunburst", {"size": "3840x3840"}),
        ]:
            with self.subTest(identifier=identifier, options=options), self.assertRaises(ImageToolError):
                validate_model_options(identifier, options)


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = ResolvedConfig(
            base_url="https://relay.example/v1", api_key="fixture-model-secret", headers={}, query_params={},
            model="configured-model", provider="relay", model_source="config", credential_source="fixture",
            config_path=self.root / "config.toml", request_timeout=30,
        )

    def sdk(self, handler):
        def factory(**options):
            self.assertEqual(options["max_retries"], 0)
            return AsyncOpenAI(**options, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
        return patch.object(selection, "AsyncOpenAI", side_effect=factory)

    async def test_official_cache_and_failed_refresh_keep_last_good(self):
        with patch.object(catalog_module, "fetch_catalog", new_callable=AsyncMock, return_value=bundled_catalog()) as fetch:
            value = bundled_catalog()
            value["fetched_at"] = time.time()
            fetch.return_value = value
            first = await official_catalog(self.root)
            second = await official_catalog(self.root)
        self.assertEqual(first["source"], "official")
        self.assertEqual(second["source"], "cache")
        fetch.assert_awaited_once()
        original = (self.root / "official-models.json").read_bytes()
        with patch.object(catalog_module, "fetch_catalog", side_effect=ValueError("changed HTML")):
            stale = await official_catalog(self.root, refresh=True)
        self.assertEqual(stale["source"], "stale_cache")
        self.assertTrue(stale["warnings"])
        self.assertEqual(original, (self.root / "official-models.json").read_bytes())

    async def test_corrupt_cache_falls_back_with_provenance(self):
        (self.root / "official-models.json").write_text('{"broken": true}')
        with patch.object(catalog_module, "fetch_catalog", side_effect=TimeoutError):
            result = await official_catalog(self.root)
        self.assertEqual(result["source"], "bundled")
        self.assertTrue(result["stale"])
        self.assertNotIn("gpt-image-1", [m["id"] for m in result["images"]])

    async def test_public_docs_never_receive_provider_auth_and_are_bounded(self):
        requests = []
        async def handler(request):
            requests.append(request)
            return httpx2.Response(200, text=GUIDE if str(request.url) in {GUIDE_URL, GUIDE_URL + ".md"} else "# Model\nNo extra metadata")
        real_client = httpx2.AsyncClient
        with patch.object(catalog_module.httpx2, "AsyncClient", side_effect=lambda **kw: real_client(**kw, transport=httpx2.MockTransport(handler))), patch.dict(
            os.environ, {"OPENAI_API_KEY": "unrelated-secret", "OPENAI_CUSTOM_HEADERS": "X-Key: unrelated-secret"},
        ):
            result = await catalog_module.fetch_catalog()
        self.assertTrue(result["images"])
        for request in requests:
            self.assertEqual(request.url.host, "developers.openai.com")
            self.assertNotIn("authorization", request.headers)
            self.assertNotIn("x-key", request.headers)
        async with real_client(transport=httpx2.MockTransport(lambda _: httpx2.Response(200, text="x" * 32))) as client:
            with patch.object(catalog_module, "MAX_DOCUMENT_BYTES", 16), self.assertRaises(ValueError):
                await catalog_module.fetch_document(client, GUIDE_URL)

    async def test_markdown_export_can_fall_back_to_fixed_html_url(self):
        requested = []
        def handler(request):
            requested.append(str(request.url))
            return httpx2.Response(404) if str(request.url).endswith(".md") else httpx2.Response(200, text=GUIDE)
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            body = await catalog_module.fetch_document(client, GUIDE_URL)
        self.assertEqual(body, GUIDE)
        self.assertEqual(requested, [GUIDE_URL + ".md", GUIDE_URL])

    async def test_provider_cache_scopes_credentials_and_reuses_provider_auth(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx2.Response(200, json={"object": "list", "data": [
                {"id": "gpt-6-astra", "object": "model", "created": 0, "owned_by": "openai"},
                {"id": "echo-" + request.headers["authorization"].split()[-1], "object": "model", "created": 0, "owned_by": "openai"},
            ]})
        with self.sdk(handler), patch.dict(os.environ, {"OPENAI_API_KEY": "unrelated", "OPENAI_CUSTOM_HEADERS": "X-Other: unrelated"}):
            first = await selection.provider_models(self.config)
            second = await selection.provider_models(self.config)
        self.assertEqual(first["models"], [{"id": "gpt-6-astra", "shutdown_date": None}])
        self.assertEqual(second["source"], "cache")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].headers["Authorization"], "Bearer fixture-model-secret")
        self.assertNotIn("X-Other", requests[0].headers)
        from dataclasses import replace
        with self.sdk(handler):
            await selection.provider_models(replace(self.config, api_key="other-secret"))
        self.assertEqual(len(requests), 2)
        for path in self.root.rglob("*.json"):
            self.assertNotIn("fixture-model-secret", path.name + path.read_text())
            self.assertNotIn("other-secret", path.name + path.read_text())

    async def test_provider_403_is_not_a_generation_probe(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx2.Response(403, json={"error": {"message": "fixture-model-secret private error"}})
        with self.sdk(handler):
            result = await selection.provider_models(self.config)
        self.assertEqual(result["source"], "unavailable")
        self.assertEqual(result["http_status"], 403)
        self.assertNotIn("fixture-model-secret", str(result))
        self.assertEqual([request.method for request in requests], ["GET"])

    async def test_auto_selection_keeps_two_model_sources_independent(self):
        # The relay intentionally advertises NO image models.
        upstream = provider("gpt-6.1-sol", "gpt-6-astra", "text-embedding-3-large", "custom-alias")
        with patch.object(selection, "provider_models", return_value=upstream), patch.object(selection, "official_catalog", return_value=catalog()):
            chosen = await select_models(self.config)
            listed = await selection.list_dialogue_models(self.config)
        self.assertEqual(chosen.responses_model, "gpt-6-astra")
        self.assertEqual(chosen.image_model, "gpt-image-2.5-sunburst")
        self.assertEqual(chosen.public()["image_model_evidence"], "request_only")
        self.assertIsNone(chosen.public()["image_model_reported"])
        self.assertEqual({m["id"] for m in listed["models"]}, {"gpt-6.1-sol", "gpt-6-astra"})
        self.assertEqual(listed["unclassified_models"], ["custom-alias"])
        self.assertFalse(listed["provider_tool_support_verified"])

    async def test_explicit_choices_and_default_mode_skip_discovery(self):
        with patch.object(selection, "provider_models") as relay, patch.object(selection, "official_catalog") as docs:
            chosen = await select_models(self.config, responses_model="custom/dialogue", image_model="custom-image")
            compatibility = await select_models(self.config, responses_model="custom/dialogue", image_model="upstream_default")
        relay.assert_not_called()
        docs.assert_not_called()
        self.assertEqual(chosen.responses_model, "custom/dialogue")
        self.assertIsNone(compatibility.image_model)
        self.assertEqual(compatibility.image_source, "upstream_default")

    async def test_environment_defaults_and_per_call_precedence(self):
        from dataclasses import replace
        settings = replace(self.config, responses_model="env-dialogue", image_model="gpt-image-2", model_policy="fast")
        with patch.object(selection, "official_catalog") as docs:
            chosen = await select_models(settings, image_model="gpt-image-2.5-flare")
        docs.assert_not_called()
        self.assertEqual((chosen.responses_model, chosen.responses_source), ("env-dialogue", "environment"))
        self.assertEqual((chosen.image_model, chosen.image_source), ("gpt-image-2.5-flare", "caller"))
        self.assertEqual(chosen.policy, "fast")

    async def test_failed_listing_falls_back_and_missing_fallback_errors(self):
        from dataclasses import replace
        unavailable = provider(source="unavailable")
        unavailable["warnings"] = ["listing denied"]
        with patch.object(selection, "provider_models", return_value=unavailable), patch.object(selection, "official_catalog", return_value=catalog()):
            chosen = await select_models(self.config)
            with self.assertRaisesRegex(ImageToolError, "responses_model"):
                await select_models(replace(self.config, model=None))
        self.assertEqual(chosen.responses_model, "configured-model")
        self.assertEqual(chosen.responses_source, "config")
        self.assertIn("listing denied", chosen.warnings)

    async def test_fast_policy_and_known_snapshots(self):
        with patch.object(selection, "provider_models", return_value=provider("gpt-6-astra", "gpt-6-luna")), patch.object(selection, "official_catalog", return_value=catalog()):
            chosen = await select_models(self.config, model_policy="fast")
        self.assertEqual(chosen.responses_model, "gpt-6-luna")
        self.assertEqual(chosen.image_model, "gpt-image-2.5-flare")
        directory = catalog()
        entry = next(m for m in directory["responses"] if m["id"] == "gpt-5.5")
        enrich_model(entry, "# GPT 5.5\nModel ID: gpt-5.5\n## Snapshots\n- gpt-5.5\n- gpt-5.5-2026-04-23\n")
        entries, _ = selection.dialogue_candidates(provider("gpt-5.5-2026-04-23", "gpt-5.5-2099-01-01"), directory)
        self.assertEqual(entries[0]["image_tool_support"], "documented")
        self.assertEqual(entries[1]["image_tool_support"], "unknown")

    async def test_retired_models_and_invalid_overrides_are_not_selected(self):
        for image in ("gpt-image-1", "bad\nmodel", "", "a" * 201):
            with self.subTest(image=image), self.assertRaises(ImageToolError):
                await select_models(self.config, responses_model="explicit", image_model=image)
        upstream = provider("gpt-6-astra", "gpt-6.1-sol", "gpt-image-2.5-sunburst")
        upstream["models"][0]["shutdown_date"] = "2000-01-01"
        entries, _ = selection.dialogue_candidates(upstream, catalog())
        self.assertEqual([m["id"] for m in entries], ["gpt-6.1-sol"])

    async def test_image_listing_needs_no_provider_config(self):
        with patch.object(selection, "official_catalog", return_value=catalog()), patch.object(selection, "provider_models") as relay:
            listed = await selection.list_official_image_models(self.root)
        relay.assert_not_called()
        self.assertEqual(listed["recommended_model"], "gpt-image-2.5-sunburst")
        self.assertFalse(listed["provider_support_verified"])

    async def test_complete_pipeline_discovers_then_sends_exactly_one_paid_request(self):
        from codex_image_generation import generator
        data = io.BytesIO()
        Image.new("RGB", (32, 32), "blue").save(data, "PNG")
        captured = []

        def handler(request):
            captured.append(request)
            if request.method == "GET":
                return httpx2.Response(200, json={"data": [{"id": "gpt-6-astra"}, {"id": "gpt-6.1-sol"}]})
            body = json.loads(request.content)
            self.assertEqual(body["model"], "gpt-6-astra")
            self.assertEqual(body["tools"][0]["model"], "gpt-image-2.5-sunburst")
            return httpx2.Response(200, json={
                "id": "resp_pipeline", "model": "gpt-6-astra", "status": "completed",
                "output": [{"id": "ig_pipeline", "type": "image_generation_call", "status": "completed",
                            "result": base64.b64encode(data.getvalue()).decode()}],
                "tool_usage": {"image_gen": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}},
            })

        def factory(**options):
            return AsyncOpenAI(**options, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))

        with patch.object(selection, "AsyncOpenAI", side_effect=factory), patch.object(generator, "AsyncOpenAI", side_effect=factory), patch.object(
            selection, "official_catalog", return_value=catalog(),
        ):
            result = await generator.generate(self.config, prompt="Draw", cwd=str(self.root), filename="pipeline.png")
        self.assertEqual(result.status, "success")
        self.assertTrue((self.root / "pipeline.png").is_file())
        self.assertEqual([request.method for request in captured], ["GET", "POST"])
        self.assertEqual(result.models["responses_model_source"], "provider_catalog")
        self.assertEqual(result.models["image_model_source"], "official_catalog")
        self.assertEqual(result.tool_usage["image_gen"]["total_tokens"], 30)

    async def test_rejected_image_model_is_not_silently_retried(self):
        from codex_image_generation import generator
        requests = []
        def handler(request):
            requests.append(request)
            return httpx2.Response(400, json={"error": {"code": "unsupported_model", "param": "tools[0].model",
                                                        "type": "invalid_request_error", "message": "fixture-model-secret"}})
        def factory(**options):
            return AsyncOpenAI(**options, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
        with patch.object(generator, "AsyncOpenAI", side_effect=factory), patch.object(selection, "official_catalog") as docs:
            result = await generator.generate(self.config, prompt="Draw", cwd=str(self.root),
                                               responses_model="gpt-6-astra", image_model="gpt-image-2.5-sunburst")
        docs.assert_not_called()
        self.assertEqual(result.status, "error")
        self.assertEqual(len(requests), 1)
        self.assertEqual(result.errors[0]["upstream_param"], "tools[0].model")
        self.assertEqual(result.errors[0]["upstream_code"], "unsupported_model")
        self.assertNotIn("fixture-model-secret", str(result.errors))
        self.assertEqual(result.models["image_model_selected"], "gpt-image-2.5-sunburst")

    async def test_no_configured_model_needed_when_provider_advertises_candidate(self):
        from dataclasses import replace
        with patch.object(selection, "provider_models", return_value=provider("gpt-6-astra")), patch.object(selection, "official_catalog", return_value=catalog()):
            chosen = await select_models(replace(self.config, model=None))
        self.assertEqual(chosen.responses_model, "gpt-6-astra")

    async def test_retired_configured_model_is_not_used_as_fallback(self):
        upstream = provider("configured-model")
        upstream["models"][0]["shutdown_date"] = "2000-01-01"
        with patch.object(selection, "provider_models", return_value=upstream), patch.object(selection, "official_catalog", return_value=catalog()):
            with self.assertRaises(ImageToolError):
                await select_models(self.config)

    async def test_cache_rejects_wrong_scope_and_future_timestamp(self):
        path = self.root / "test.json"
        self.assertTrue(write_cache(path, {"version": 1, "scope": "a", "fetched_at": time.time()}))
        self.assertIsNone(read_cache(path, "b"))
        self.assertTrue(write_cache(path, {"version": 1, "scope": "a", "fetched_at": time.time() + 3600}))
        self.assertIsNone(read_cache(path, "a"))
