"""Regression checks for discovery failure handling and honest model selection."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx2
from fastmcp import Client
from fastmcp.tools import ToolResult
from openai import AsyncOpenAI

from codex_image_generation.cli import main
from codex_image_generation.config import ResolvedConfig, get_model_preferences, model_cache_directory, resolve_config
from codex_image_generation.errors import ImageToolError
from codex_image_generation import model_selection as selection
from codex_image_generation.model_catalog import bundled_catalog
from codex_image_generation.model_cache import write_cache
from codex_image_generation.server import mcp


def official():
    return {**bundled_catalog(), "source": "official", "stale": False, "fetched_at": time.time(), "warnings": []}


def provider(*ids):
    return {"models": [{"id": value, "shutdown_date": None} for value in ids],
            "source": "provider", "stale": False, "fetched_at": time.time(), "warnings": []}


class PreferencesTests(unittest.TestCase):
    def test_preferences_are_available_without_reading_provider_configuration(self):
        with patch("codex_image_generation.config._load_config") as config, patch("codex_image_generation.config._credentials") as credentials:
            preferences = get_model_preferences(environ={"CODEX_IMAGE_MODEL_POLICY": "fast", "CODEX_IMAGE_MODEL": "chosen-image"})
        config.assert_not_called()
        credentials.assert_not_called()
        self.assertEqual(preferences["model_policy"], "fast")
        self.assertEqual(preferences["image_model"], "chosen-image")

    def test_invalid_environment_model_ids_are_rejected_without_echoing_values(self):
        for name in ("CODEX_IMAGE_RESPONSES_MODEL", "CODEX_IMAGE_MODEL"):
            with self.subTest(name=name), self.assertRaises(ImageToolError) as error:
                get_model_preferences(environ={name: "invalid model fixture-secret"})
            self.assertNotIn("fixture-secret", str(error.exception))

    def test_image_only_preferences_ignore_invalid_dialogue_override(self):
        result = get_model_preferences(environ={"CODEX_IMAGE_RESPONSES_MODEL": "invalid dialogue", "CODEX_IMAGE_MODEL_POLICY": "invalid"},
                                       include_responses=False, policy="fast")
        self.assertEqual(result["model_policy"], "fast")
        self.assertIsNone(result["responses_model"])

    def test_explicit_model_preferences_override_invalid_environment_values(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "config.toml").write_text('model_provider="relay"\n[model_providers.relay]\nbase_url="https://relay.example/v1"\nexperimental_bearer_token="fixture-key"\n', encoding="utf-8")
            result = resolve_config(codex_home=Path(directory), require_model=False,
                environ={"CODEX_IMAGE_RESPONSES_MODEL": "invalid dialogue", "CODEX_IMAGE_MODEL_POLICY": "invalid"},
                responses_model="gpt-6-astra", model_policy="fast")
        self.assertEqual(result.responses_model, "gpt-6-astra")
        self.assertEqual(result.model_policy, "fast")
        self.assertNotIn("CODEX_IMAGE_RESPONSES_MODEL", result.required_env_vars)

    def test_dialogue_listing_config_ignores_invalid_image_preference(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "config.toml").write_text('model_provider="relay"\n[model_providers.relay]\nbase_url="https://relay.example/v1"\nexperimental_bearer_token="fixture-key"\n', encoding="utf-8")
            result = resolve_config(codex_home=Path(directory), require_model=False, include_image_preferences=False,
                                    environ={"CODEX_IMAGE_MODEL": "bad image\nname", "CODEX_IMAGE_MODEL_POLICY": "bad"}, model_policy="fast")
        self.assertEqual(result.model_policy, "fast")
        self.assertIsNone(result.image_model)

    def test_cli_preserves_model_listing_failure_semantics(self):
        result = ToolResult(content=["unavailable"], structured_content={"status": "error", "source": "unavailable", "models": []}, is_error=True)
        with patch("sys.argv", ["codex-image-generation", "--list-image-models"]), patch(
            "codex_image_generation.server.list_image_models", new_callable=AsyncMock, return_value=result,
        ) as listing, contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as raised:
                main()
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "error")
        listing.assert_awaited_once_with(refresh=False, model_policy=None)

    def test_cli_invalid_home_reports_usage_error_without_traceback(self):
        with patch("sys.argv", ["codex-image-generation", "--check", "--codex-home", "bad\x00path"]), contextlib.redirect_stderr(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as raised:
                main()
        self.assertEqual(raised.exception.code, 2)
        self.assertNotIn("Traceback", output.getvalue())

    def test_cyclic_home_path_is_a_safe_configuration_error(self):
        with patch("pathlib.Path.resolve", side_effect=RuntimeError("private path")):
            with self.assertRaises(ImageToolError) as error:
                model_cache_directory(environ={"CODEX_HOME": "loop"})
            self.assertNotIn("private path", str(error.exception))
            with patch("sys.argv", ["codex-image-generation", "--check", "--codex-home", "loop"]), contextlib.redirect_stderr(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as error:
                    main()
            self.assertEqual(error.exception.code, 2)
            self.assertNotIn("private path", output.getvalue())


class SelectionAuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = ResolvedConfig(
            base_url="https://relay.example/v1", api_key="fixture-selection-secret", headers={}, query_params={},
            model="configured-model", provider="relay", model_source="config", credential_source="fixture",
            config_path=self.root / "config.toml", request_timeout=30,
        )

    def sdk(self, handler):
        def factory(**options):
            return AsyncOpenAI(**options, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
        return patch.object(selection, "AsyncOpenAI", side_effect=factory)

    async def test_nonempty_malformed_list_never_overwrites_last_good_cache(self):
        bodies = iter([{"data": [{"id": "gpt-6-astra"}]}, {"unexpected": True}, {"data": [{"missing_id": True}]}, {"data": []}])
        requests = []
        def handler(request):
            requests.append(request)
            return httpx2.Response(200, json=next(bodies))
        with self.sdk(handler):
            first = await selection.provider_models(self.config)
            path = selection.cache_directory(self.config) / f"provider-{selection._provider_scope(self.config)}.json"
            original = path.read_bytes()
            for _ in range(2):
                result = await selection.provider_models(self.config, refresh=True)
                self.assertEqual(result["source"], "stale_cache")
                self.assertEqual(result["error_code"], "invalid_response")
                self.assertEqual(result["models"], first["models"])
                self.assertEqual(path.read_bytes(), original)
            # A real, empty list is valid and must replace old visibility information.
            empty = await selection.provider_models(self.config, refresh=True)
        self.assertEqual(empty["models"], [])
        self.assertEqual(empty["source"], "provider")
        self.assertEqual(len(requests), 4)

    async def test_failed_refresh_stays_visible_during_backoff_and_force_refresh_recovers(self):
        fail = False
        requests = []
        def handler(request):
            requests.append(request)
            return httpx2.Response(403, json={"error": {"message": "private"}}) if fail else httpx2.Response(200, json={"data": [{"id": "gpt-6-astra"}]})
        with self.sdk(handler):
            await selection.provider_models(self.config)
            fail = True
            failed = await selection.provider_models(self.config, refresh=True)
            cached_failure = await selection.provider_models(self.config)
            self.assertEqual(len(requests), 2)
            self.assertEqual(cached_failure["source"], "stale_cache")
            self.assertTrue(cached_failure["stale"])
            self.assertEqual(cached_failure["retry_after"], failed["retry_after"])
            fail = False
            await selection.provider_models(self.config, refresh=True)
            recovered = await selection.provider_models(self.config)
        self.assertEqual(len(requests), 3)
        self.assertEqual(recovered["source"], "cache")
        self.assertFalse(recovered["stale"])

    async def test_cold_discovery_is_shared_between_concurrent_calls(self):
        requests = []
        async def handler(request):
            requests.append(request)
            await asyncio.sleep(0.01)
            return httpx2.Response(200, json={"data": [{"id": "gpt-6-astra"}]})
        with self.sdk(handler):
            results = await asyncio.gather(*(selection.provider_models(self.config) for _ in range(5)))
        self.assertEqual(len(requests), 1)
        self.assertTrue(all(result["models"] == results[0]["models"] for result in results))

    async def test_cancelled_discovery_releases_lock_and_does_not_cache_failure(self):
        started = asyncio.Event()
        blocked = True
        async def handler(request):
            if blocked:
                started.set()
                await asyncio.Event().wait()
            return httpx2.Response(200, json={"data": [{"id": "gpt-6-astra"}]})
        with self.sdk(handler):
            task = asyncio.create_task(selection.provider_models(self.config))
            await asyncio.wait_for(started.wait(), 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            blocked = False
            result = await asyncio.wait_for(selection.provider_models(self.config), 5)
        self.assertEqual(result["source"], "provider")
        self.assertFalse(list(self.root.rglob("*-failure.json")))

    async def test_query_failure_without_cache_has_short_backoff(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx2.Response(503, json={"error": {"message": "fixture-selection-secret"}})
        with self.sdk(handler):
            first = await selection.provider_models(self.config)
            second = await selection.provider_models(self.config)
        self.assertEqual(len(requests), 1)
        self.assertEqual(first["source"], "unavailable")
        self.assertEqual(second["retry_after"], first["retry_after"])
        self.assertNotIn("fixture-selection-secret", str(second))

    async def test_malformed_failure_record_does_not_block_discovery(self):
        scope = selection._provider_scope(self.config)
        path = selection.cache_directory(self.config) / f"provider-{scope}-failure.json"
        for value in ([], {}):
            self.assertTrue(write_cache(path, {"version": 1, "scope": scope + ":failure", "fetched_at": time.time(), "error_code": value}))
            with self.subTest(value=value), self.sdk(lambda _: httpx2.Response(200, json={"data": [{"id": "gpt-6-astra"}]})):
                result = await selection.provider_models(self.config)
            self.assertIn(result["source"], {"provider", "cache"})
            self.assertEqual(result["models"][0]["id"], "gpt-6-astra")

    async def test_fast_recommendation_does_not_interpret_negation_as_speed_evidence(self):
        entry = {"id": "custom-example", "quality_rank": 1, "description": "This is not fast.", "ranking_reason": "fixture"}
        self.assertEqual(selection._rank(entry, "fast")[:2], (0, 1))
        alias = {"id": "gpt-image-2.5-flare", "quality_rank": 4}
        snapshot = {**alias, "id": "gpt-image-2.5-flare-2026-09-08", "alias_of": alias["id"]}
        self.assertEqual(selection._rank(alias, "fast")[0], selection._rank(snapshot, "fast")[0])
        self.assertEqual(selection._recommend([snapshot, alias], "fast")["id"], alias["id"])

    async def test_speed_fallback_keeps_full_quality_evidence(self):
        models = [{"id": "gpt-6-astra", "quality_rank": 1017}, {"id": "gpt-7-example", "quality_rank": 100001}]
        self.assertEqual(selection._recommend(models, "fast")["id"], "gpt-7-example")

    async def test_invalid_derived_canonical_metadata_cannot_break_or_change_image_ranking(self):
        entry = {"id": "gpt-image-2.5-sunburst", "kind": "image", "quality_rank": 10}
        for value in ({"bad": "id"}, [], "gpt-image-2.5-flare"):
            with self.subTest(value=value):
                candidate = {**entry, "canonical_model": value}
                self.assertEqual(selection._canonical(candidate), entry["id"])
                self.assertEqual(selection._rank(candidate, "fast"), selection._rank(entry, "fast"))

    async def test_unranked_or_ambiguous_models_do_not_get_name_based_recommendations(self):
        entries = [{"id": "gpt-9-example", "quality_rank": 0, "ranking_reason": "unranked"}]
        self.assertIsNone(selection._recommend(entries, "quality"))
        entries = [{"id": name, "quality_rank": 7, "ranking_reason": "equal_evidence"} for name in ("gpt-a-example", "gpt-z-example")]
        self.assertIsNone(selection._recommend(entries, "quality"))
        self.assertIsNone(selection._recommend(entries[::-1], "quality"))

    async def test_wrong_model_roles_and_auto_placeholders_fail_without_discovery(self):
        for response, image in (("gpt-6-astra", "gpt-6-luna"), ("gpt-image-2", "gpt-image-2.5-flare"),
                                ("auto", "gpt-image-2"), ("gpt-6-astra", "auto"), ("upstream_default", "gpt-image-2")):
            with self.subTest(response=response, image=image), patch.object(selection, "official_catalog") as docs, patch.object(selection, "provider_models") as relay:
                with self.assertRaises(ImageToolError):
                    await selection.select_models(self.config, responses_model=response, image_model=image)
                docs.assert_not_called()
                relay.assert_not_called()

    async def test_catalog_retirement_blocks_configured_fallback_and_explicit_image(self):
        directory = official()
        directory["retired_ids"] = ["configured-model", "gpt-image-2.5-flare"]
        with patch.object(selection, "official_catalog", return_value=directory), patch.object(selection, "provider_models", return_value=provider()):
            with self.assertRaises(ImageToolError):
                await selection.select_models(self.config)
            with self.assertRaises(ImageToolError):
                await selection.select_models(self.config, image_model="gpt-image-2.5-flare")

    async def test_fixed_models_still_consult_known_retirements_without_network(self):
        directory = bundled_catalog()
        directory["fetched_at"] = time.time()
        directory["images"] = [item for item in directory["images"] if item["id"] != "gpt-image-2.5-flare"]
        directory["retired_ids"] = [*directory.get("retired_ids", []), "gpt-image-2.5-flare"]
        self.assertTrue(write_cache(selection.cache_directory(self.config) / "official-models.json", directory))
        with patch.object(selection, "official_catalog") as docs, patch.object(selection, "provider_models") as relay:
            with self.assertRaises(ImageToolError):
                await selection.select_models(self.config, responses_model="gpt-6-astra", image_model="gpt-image-2.5-flare")
        docs.assert_not_called()
        relay.assert_not_called()

    async def test_known_credentials_cannot_be_exposed_as_selected_models(self):
        for response, image in ((self.config.api_key, "gpt-image-2"), ("gpt-6-astra", self.config.api_key)):
            with self.subTest(response=response, image=image), self.assertRaises(ImageToolError) as raised:
                await selection.select_models(self.config, responses_model=response, image_model=image)
            self.assertNotIn(self.config.api_key, str(raised.exception))
        settings = replace(self.config, model=self.config.api_key, responses_model=self.config.api_key)
        self.assertNotIn(self.config.api_key, str(settings.public_status()))
        with patch.object(selection, "official_catalog", return_value=official()), patch.object(selection, "provider_models", return_value=provider()):
            listed = await selection.list_dialogue_models(settings)
        self.assertNotIn(self.config.api_key, str(listed))

    async def test_routing_parameters_are_not_treated_as_authentication_secrets(self):
        settings = replace(self.config, query_params={"model": "gpt-6-astra", "api-version": "2026-04-23", "max_tokens": "1"})
        rows = selection._safe_models([{"id": "gpt-6-astra"}, {"id": "gpt-5.5-2026-04-23"}], settings)
        self.assertEqual(len(rows), 2)
        chosen = await selection.select_models(settings, responses_model="gpt-6-astra", image_model="gpt-image-2")
        self.assertEqual(chosen.responses_model, "gpt-6-astra")

    async def test_custom_private_header_and_basic_token_are_not_exposed_as_models(self):
        settings = replace(self.config, api_key="", headers={"X-Private-Context": "fixture-private-context", "Authorization": "Basic dXNlcjpwYXNz"})
        rows = selection._safe_models([{"id": "gpt-6-astra"}, {"id": "fixture-private-context"}, {"id": "dXNlcjpwYXNz"}], settings)
        self.assertEqual([row["id"] for row in rows], ["gpt-6-astra"])
        with self.assertRaises(ImageToolError):
            await selection.select_models(settings, responses_model="dXNlcjpwYXNz", image_model="gpt-image-2")

    async def test_selection_reports_stale_provider_and_catalog_separately(self):
        listed = provider("gpt-6-astra")
        listed.update(source="stale_cache", stale=True, warnings=["provider stale"])
        directory = official()
        directory.update(source="bundled", stale=True, warnings=["docs stale"])
        with patch.object(selection, "official_catalog", return_value=directory), patch.object(selection, "provider_models", return_value=listed):
            result = await selection.select_models(self.config)
        metadata = result.public()
        self.assertTrue(metadata["provider_models_stale"])
        self.assertTrue(metadata["catalog_stale"])
        self.assertEqual(metadata["provider_models_source"], "stale_cache")
        self.assertEqual(metadata["provider_models_fetched_at"], listed["fetched_at"])

    async def test_image_listing_inherits_environment_policy_and_exposes_override(self):
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root), "CODEX_IMAGE_MODEL_POLICY": "fast", "CODEX_IMAGE_MODEL": "gpt-image-2", "CODEX_IMAGE_RESPONSES_MODEL": "invalid dialogue"}, clear=True), patch(
            "codex_image_generation.server.list_official_image_models", new_callable=AsyncMock,
            return_value={"models": [], "source": "official", "warnings": []},
        ) as listing, patch("codex_image_generation.server.resolve_config") as resolve:
            async with Client(mcp) as client:
                result = await client.call_tool_mcp("list_image_models", {})
        self.assertEqual(listing.call_args.kwargs["policy"], "fast")
        self.assertEqual(result.structured_content["image_model_override"], "gpt-image-2")
        resolve.assert_not_called()

    async def test_mcp_distinguishes_unavailable_listing_from_stale_success(self):
        for source, expected_error in (("unavailable", True), ("stale_cache", False)):
            with self.subTest(source=source), patch("codex_image_generation.server.resolve_config", return_value=self.config), patch(
                "codex_image_generation.server.list_dialogue_models", new_callable=AsyncMock,
                return_value={"models": [], "source": source, "stale": True, "warnings": ["discovery failed"]},
            ):
                async with Client(mcp) as client:
                    result = await client.call_tool_mcp("list_models", {})
            self.assertEqual(result.is_error, expected_error)
            self.assertEqual(result.structured_content["status"], "error" if expected_error else "partial_success")

    async def test_image_listing_internal_error_is_safe_for_cli_and_mcp(self):
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root)}, clear=True), patch("codex_image_generation.server.list_official_image_models", side_effect=RuntimeError("private-secret")) as listing:
            async with Client(mcp) as client:
                result = await client.call_tool_mcp("list_image_models", {})
        self.assertTrue(result.is_error)
        self.assertNotIn("private-secret", str(result))
        listing.assert_awaited_once()
