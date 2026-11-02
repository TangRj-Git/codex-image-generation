"""Regression coverage for malformed catalogs, retirement evidence and refresh failures."""

from __future__ import annotations

import asyncio
import copy
import gc
import json
import os
from pathlib import Path
import tempfile
import time
import stat
import unittest
import weakref
from unittest.mock import AsyncMock, patch

import httpx2

from codex_image_generation import model_catalog as catalog
from codex_image_generation.model_cache import cache_lock, read_cache, write_cache
from codex_image_generation.local_files import read_regular_bytes


IMAGE = "gpt-image-2.5-sunburst"
RESPONSE = "gpt-6-astra"
GUIDE = f"""# Image generation
The image generation tool uses `{IMAGE}`.

Set the image_generation tool's model to [{IMAGE}](/api/docs/models/{IMAGE}) for precise editing.

## Usage
Use the tool.
## Supported models
- `{RESPONSE}`
"""
INDEX = f"""# Models
## Featured models
- [Astra](/api/docs/models/{RESPONSE}.md): Our most capable model for the most demanding work.
"""


def model_page(identifier: str, *, snapshots: str = "", notice: str = "") -> str:
    return f"# Model\nModel ID: {identifier}\nA model for useful work.\n{notice}\n## Snapshots\n- {identifier}\n{snapshots}\n"


def fresh_catalog() -> dict:
    result = catalog.bundled_catalog()
    result["fetched_at"] = time.time()
    return result


class CatalogParsingAuditTests(unittest.TestCase):
    def test_replacement_is_not_retired_by_a_notice_about_its_predecessor(self):
        entry = catalog.bundled_catalog()["images"][0]
        catalog.enrich_model(entry, model_page(IMAGE, notice="gpt-image-1 was shut down on October 23, 2026."))
        self.assertTrue(entry["details_verified"])
        self.assertFalse(entry.get("retired", False))

    def test_model_page_identity_uses_exact_id_not_substring_or_related_link(self):
        entry = catalog.bundled_catalog()["images"][2]
        original = copy.deepcopy(entry)
        catalog.enrich_model(entry, model_page(IMAGE) + "## Related\ngpt-image-2")
        self.assertEqual(entry, original)

    def test_shutdown_dates_are_real_calendar_dates_and_do_not_override_known_retirement(self):
        self.assertFalse(catalog.retired("custom-model", "2000-99-99"))
        self.assertFalse(catalog.retired("custom-model", "2999-01-01"))
        self.assertTrue(catalog.retired("gpt-image-1", "2999-01-01"))
        self.assertTrue(catalog.retired("gpt-image-1-2025-01-01"))

    def test_future_shutdown_notice_keeps_model_available(self):
        entry = catalog.bundled_catalog()["images"][0]
        catalog.enrich_model(entry, model_page(IMAGE, notice=f"{IMAGE} will shut down on January 1, 2999.\nShutdown date: 2999-01-01"))
        self.assertFalse(entry.get("retired", False))
        self.assertEqual(entry["shutdown_date"], "2999-01-01")

    def test_shutdown_notice_keeps_explicit_retirement_evidence(self):
        body = GUIDE.replace(f"The image generation tool uses `{IMAGE}`.",
                             f"The image generation tool uses `{IMAGE}`.\n\n`gpt-image-3-old` was shut down on October 1, 2026.")
        result = catalog.parse_guide(body)
        self.assertIn("gpt-image-3-old", result["retired_ids"])
        self.assertNotIn("gpt-image-3-old", [entry["id"] for entry in result["images"]])

    def test_linked_recommendation_and_featured_scoring_are_stable(self):
        result = catalog.parse_guide(GUIDE)
        self.assertEqual(result["images"][0]["ranking_reason"], "official_guide_recommendation")
        catalog.apply_featured_recommendations(result, INDEX)
        first = result["responses"][0]["quality_rank"]
        catalog.apply_featured_recommendations(result, INDEX)
        self.assertEqual(result["responses"][0]["quality_rank"], first)

    def test_snapshots_require_valid_dates_and_are_bounded(self):
        entry = catalog.bundled_catalog()["images"][0]
        dates = [f"- {IMAGE}-2027-{month:02d}-{day:02d}" for month in (1, 2) for day in range(1, 29)]
        text = model_page(IMAGE, snapshots="\n".join(dates + [f"- {IMAGE}-extra", f"- {IMAGE}-2027-99-99"]))
        additions = catalog.enrich_model(entry, text)
        self.assertEqual(len(entry["snapshots"]), catalog.MAX_SNAPSHOTS)
        self.assertTrue(entry["snapshots_truncated"])
        self.assertEqual(len(additions), catalog.MAX_SNAPSHOTS)
        self.assertFalse(any(item["id"].endswith("extra") for item in additions))

    def test_cache_payload_rejects_forged_origins_duplicates_and_bad_metadata(self):
        mutations = (
            lambda value: value["images"][0].update(source_url="https://example.invalid/model"),
            lambda value: value["images"].append(copy.deepcopy(value["images"][0])),
            lambda value: value["images"][0].update(quality_rank=10 ** 50),
            lambda value: value["images"][0].update(description=[]),
            lambda value: value["responses"][0].update(capabilities={"image_generation": 1}),
            lambda value: value["images"][0].update(snapshots=["gpt-image-unknown-2026-01-01"]),
            lambda value: value["images"][0].update(canonical_model={"bad": "id"}),
            lambda value: value["images"][0].update(unvalidated_payload={"arbitrary": "content"}),
            lambda value: value.update(retired_ids=[{}]),
        )
        for mutation in mutations:
            value = fresh_catalog()
            mutation(value)
            with self.subTest(mutation=mutation):
                self.assertFalse(catalog._valid_catalog(value))


class CatalogRefreshAuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    async def test_deep_or_huge_numeric_cache_is_ignored_and_nan_is_not_written(self):
        path = self.root / "cache.json"
        path.write_text('{"version":1,"scope":"test","fetched_at":1,"value":' + "[" * 1500 + "0" + "]" * 1500 + "}")
        self.assertIsNone(read_cache(path, "test"))
        path.write_text(json.dumps({"version": 1, "scope": "test", "fetched_at": 10 ** 400}))
        self.assertIsNone(read_cache(path, "test"))
        original = path.read_bytes()
        self.assertFalse(write_cache(path, {"value": float("nan")}))
        self.assertEqual(path.read_bytes(), original)

    async def test_cached_shutdown_becomes_effective_without_network(self):
        data = fresh_catalog()
        data["images"][0].update(shutdown_date="2000-01-01", snapshots=[IMAGE + "-1999-01-01"])
        write_cache(self.root / "official-models.json", data)
        with patch.object(catalog, "fetch_catalog") as fetch:
            result = await catalog.official_catalog(self.root)
        fetch.assert_not_called()
        self.assertIn(IMAGE, result["retired_ids"])
        self.assertIn(IMAGE + "-1999-01-01", result["retired_ids"])
        self.assertNotIn(IMAGE, [entry["id"] for entry in result["images"]])

    async def test_cache_cannot_supply_runtime_canonical_identity_or_extra_tool_fields(self):
        for extra in ({"canonical_model": {"bad": "id"}}, {"extra_payload": "not official model metadata"}):
            with self.subTest(extra=extra):
                value = fresh_catalog()
                value["images"][0].update(extra)
                write_cache(self.root / "official-models.json", value)
                self.assertIsNone(catalog.read_official_catalog(self.root))
                with patch.object(catalog, "fetch_catalog", side_effect=TimeoutError):
                    result = await catalog.official_catalog(self.root, refresh=True)
                self.assertEqual(result["source"], "bundled")
                self.assertTrue(all(not set(extra) & set(entry) for entry in result["images"]))

    async def test_single_failed_model_page_marks_partial_enrichment(self):
        async def fetch(client, url):
            if url == catalog.GUIDE_URL:
                return GUIDE
            if url == catalog.MODELS_URL:
                return INDEX
            if url.endswith(IMAGE):
                raise httpx2.ReadError("offline")
            return model_page(RESPONSE)
        with patch.object(catalog, "fetch_document", side_effect=fetch):
            result = await catalog.fetch_catalog()
        self.assertTrue(result["enrichment_incomplete"])
        self.assertEqual(result["images"][0]["id"], IMAGE)

    async def test_enrichment_timeout_preserves_completed_snapshots(self):
        async def fetch(client, url):
            if url == catalog.GUIDE_URL:
                return GUIDE
            if url == catalog.MODELS_URL:
                return INDEX
            if url.endswith(IMAGE):
                return model_page(IMAGE, snapshots=f"- {IMAGE}-2026-09-08")
            await asyncio.sleep(10)
            return model_page(RESPONSE)
        with patch.object(catalog, "fetch_document", side_effect=fetch), patch.object(catalog, "ENRICHMENT_TIMEOUT", 0.03):
            result = await catalog.fetch_catalog()
        self.assertTrue(result["enrichment_incomplete"])
        self.assertIn(IMAGE + "-2026-09-08", [entry["id"] for entry in result["images"]])

    async def test_authoritative_empty_catalog_does_not_resurrect_retired_models(self):
        async def fetch(client, url):
            if url == catalog.GUIDE_URL:
                return GUIDE
            if url == catalog.MODELS_URL:
                return INDEX
            if url.endswith(IMAGE):
                return model_page(IMAGE, notice=f"{IMAGE} was shut down on October 1, 2026.")
            return model_page(RESPONSE)
        with patch.object(catalog, "fetch_document", side_effect=fetch):
            result = await catalog.official_catalog(self.root)
        self.assertEqual(result["source"], "official")
        self.assertEqual(result["images"], [])
        self.assertIn(IMAGE, result["retired_ids"])
        with patch.object(catalog, "fetch_catalog") as fetch:
            cached = await catalog.official_catalog(self.root)
        fetch.assert_not_called()
        self.assertEqual(cached["images"], [])

    async def test_failed_refresh_has_cooldown_preserves_cache_and_allows_force(self):
        old = fresh_catalog()
        old["fetched_at"] -= catalog.CATALOG_TTL + 10
        path = self.root / "official-models.json"
        write_cache(path, old)
        original = path.read_bytes()
        with patch.object(catalog, "fetch_catalog", side_effect=TimeoutError) as fetch:
            first = await catalog.official_catalog(self.root)
            second = await catalog.official_catalog(self.root)
            forced = await catalog.official_catalog(self.root, refresh=True)
        self.assertEqual(fetch.await_count, 2)
        self.assertEqual(first["source"], "stale_cache")
        self.assertIn("retry_after", first)
        self.assertIn("retry_after", second)
        self.assertEqual(forced["fetched_at"], old["fetched_at"])
        self.assertEqual(path.read_bytes(), original)
        with patch.object(catalog, "fetch_catalog", return_value=fresh_catalog()) as fetch:
            result = await catalog.official_catalog(self.root, refresh=True)
        self.assertEqual(result["source"], "official")
        self.assertFalse((self.root / "official-models.failure.json").exists())

    async def test_recent_forced_failure_remains_visible_until_a_new_success(self):
        with patch.object(catalog, "fetch_catalog", side_effect=fresh_catalog):
            initial = await catalog.official_catalog(self.root)
        self.assertEqual(initial["source"], "official")
        with patch.object(catalog, "fetch_catalog", side_effect=TimeoutError) as fetch:
            failure = await catalog.official_catalog(self.root, refresh=True)
            ordinary = await catalog.official_catalog(self.root)
        fetch.assert_awaited_once()
        self.assertEqual(failure["source"], "stale_cache")
        self.assertTrue(ordinary["stale"])
        self.assertEqual(ordinary["source"], "stale_cache")
        self.assertEqual(ordinary["retry_after"], failure["retry_after"])
        self.assertEqual(ordinary["fetched_at"], initial["fetched_at"])
        with patch.object(catalog, "fetch_catalog", side_effect=fresh_catalog) as fetch:
            recovered = await catalog.official_catalog(self.root, refresh=True)
            cached = await catalog.official_catalog(self.root)
        fetch.assert_awaited_once()
        self.assertEqual(recovered["source"], "official")
        self.assertEqual(cached["source"], "cache")
        self.assertFalse(cached["stale"])
        self.assertNotIn("retry_after", cached)

    async def test_persisted_new_success_supersedes_failure_even_if_cleanup_fails(self):
        with patch.object(catalog, "fetch_catalog", side_effect=TimeoutError):
            await catalog.official_catalog(self.root)
        failure_path = self.root / "official-models.failure.json"
        with patch.object(catalog, "fetch_catalog", side_effect=fresh_catalog), patch.object(Path, "unlink", side_effect=OSError("locked")):
            recovered = await catalog.official_catalog(self.root, refresh=True)
        self.assertEqual(recovered["source"], "official")
        self.assertTrue(failure_path.exists())
        with patch.object(catalog, "fetch_catalog") as fetch:
            cached = await catalog.official_catalog(self.root)
        fetch.assert_not_called()
        self.assertEqual(cached["source"], "cache")
        self.assertFalse(cached["stale"])

    async def test_unpersisted_success_cannot_hide_failure_against_old_cache(self):
        with patch.object(catalog, "fetch_catalog", side_effect=fresh_catalog):
            initial = await catalog.official_catalog(self.root)
        with patch.object(catalog, "fetch_catalog", side_effect=TimeoutError):
            await catalog.official_catalog(self.root, refresh=True)
        failure_path = self.root / "official-models.failure.json"
        with patch.object(catalog, "fetch_catalog", side_effect=fresh_catalog), patch.object(catalog, "write_cache", return_value=False), patch.object(
            Path, "unlink", side_effect=OSError("locked"),
        ):
            recovered = await catalog.official_catalog(self.root, refresh=True)
        self.assertEqual(recovered["source"], "official")
        self.assertTrue(recovered["warnings"])
        self.assertTrue(failure_path.exists())
        with patch.object(catalog, "fetch_catalog") as fetch:
            cached = await catalog.official_catalog(self.root)
        fetch.assert_not_called()
        self.assertEqual(cached["source"], "stale_cache")
        self.assertTrue(cached["stale"])
        self.assertEqual(cached["fetched_at"], initial["fetched_at"])

    async def test_parallel_cold_requests_share_the_persisted_refresh(self):
        async def fetch():
            await asyncio.sleep(0.02)
            return fresh_catalog()
        with patch.object(catalog, "fetch_catalog", side_effect=fetch) as upstream:
            results = await asyncio.gather(*(catalog.official_catalog(self.root) for _ in range(4)))
        upstream.assert_awaited_once()
        self.assertEqual([result["source"] for result in results].count("official"), 1)

    async def test_cancelled_refresh_releases_lock_and_is_not_negative_cached(self):
        started, closed = asyncio.Event(), asyncio.Event()
        async def blocked():
            started.set()
            try:
                await asyncio.sleep(10)
            finally:
                closed.set()
        with patch.object(catalog, "fetch_catalog", side_effect=blocked):
            task = asyncio.create_task(catalog.official_catalog(self.root))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(closed.is_set())
        self.assertFalse((self.root / "official-models.failure.json").exists())
        with patch.object(catalog, "fetch_catalog", return_value=fresh_catalog()):
            result = await asyncio.wait_for(catalog.official_catalog(self.root), 1)
        self.assertEqual(result["source"], "official")

    async def test_public_fetch_rejects_nonofficial_urls_without_requesting_them(self):
        requests = []
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(lambda request: requests.append(request))) as client:
            for url in ("http://developers.openai.com/api/docs/models", "https://example.invalid/api/docs/models",
                        "https://developers.openai.com@other.invalid/api/docs/models", "https://developers.openai.com/api/docs/models?x=y"):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    await catalog.fetch_document(client, url)
        self.assertEqual(requests, [])


class CacheLockLifecycleTests(unittest.TestCase):
    def test_contended_lock_does_not_retain_a_closed_loop(self):
        loop = asyncio.new_event_loop()
        loop_reference = weakref.ref(loop)

        async def contend():
            lock = cache_lock(Path("catalog-lifecycle-test.json"))
            async with lock:
                waiter = asyncio.create_task(lock.acquire())
                await asyncio.sleep(0)
            await waiter
            lock.release()
            return weakref.ref(lock)

        try:
            lock_reference = loop.run_until_complete(contend())
        finally:
            loop.close()
        del loop
        gc.collect()
        self.assertIsNone(lock_reference())
        self.assertIsNone(loop_reference())


class RegularFileReadTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_regular_file_limit_and_empty_file(self):
        path = self.root / "data.bin"
        path.write_bytes(b"abc")
        self.assertEqual(read_regular_bytes(path, 3), b"abc")
        with self.assertRaises(ValueError):
            read_regular_bytes(path, 2)
        path.write_bytes(b"")
        self.assertEqual(read_regular_bytes(path, 0), b"")

    def test_directory_and_device_are_rejected_before_open(self):
        with patch("codex_image_generation.local_files.os.open") as opened:
            with self.assertRaises(ValueError):
                read_regular_bytes(self.root, 10)
            with self.assertRaises(ValueError):
                read_regular_bytes(Path(os.devnull), 10)
        opened.assert_not_called()
        self.assertIsNone(read_cache(self.root, "test"))
        self.assertIsNone(read_cache(Path(os.devnull), "test"))

    def test_descriptor_recheck_rejects_special_replacement_and_closes_it(self):
        path = self.root / "cache.json"
        path.write_bytes(b"{}")
        descriptor = None
        real_open = os.open
        real_fstat = os.fstat

        def opened(name, flags):
            nonlocal descriptor
            descriptor = real_open(name, flags)
            return descriptor

        def special_info(fd):
            info = real_fstat(fd)
            values = list(info)
            values[0] = stat.S_IFIFO | 0o600
            return os.stat_result(values)

        with patch("codex_image_generation.local_files.os.open", side_effect=opened), patch(
            "codex_image_generation.local_files.os.fstat", side_effect=special_info,
        ), patch("codex_image_generation.local_files.os.read") as read:
            self.assertIsNone(read_cache(path, "test"))
        read.assert_not_called()
        with self.assertRaises(OSError):
            real_fstat(descriptor)

    def test_replacement_larger_than_preflight_size_is_rejected(self):
        path = self.root / "data.bin"
        path.write_bytes(b"a")
        real_open = os.open
        real_fstat = os.fstat
        descriptors = []

        def opened(name, flags):
            path.write_bytes(b"abcdef")
            descriptor = real_open(name, flags)
            descriptors.append(descriptor)
            return descriptor

        with patch("codex_image_generation.local_files.os.open", side_effect=opened), self.assertRaises(ValueError):
            read_regular_bytes(path, 3)
        with self.assertRaises(OSError):
            real_fstat(descriptors[0])

    def test_growth_after_fstat_is_bounded_and_closes_descriptor(self):
        path = self.root / "data.bin"
        path.write_bytes(b"a")
        real_fstat = os.fstat
        real_read = os.read
        descriptors = []
        requests = []

        def info_then_grow(descriptor):
            info = real_fstat(descriptor)
            descriptors.append(descriptor)
            path.write_bytes(b"abcdef")
            return info

        def bounded_read(descriptor, size):
            requests.append(size)
            return real_read(descriptor, size)

        with patch("codex_image_generation.local_files.os.fstat", side_effect=info_then_grow), patch(
            "codex_image_generation.local_files.os.read", side_effect=bounded_read,
        ), self.assertRaises(ValueError):
            read_regular_bytes(path, 3)
        self.assertEqual(requests, [4])
        with self.assertRaises(OSError):
            real_fstat(descriptors[0])

    def test_read_exception_closes_descriptor(self):
        path = self.root / "data.bin"
        path.write_bytes(b"a")
        real_open = os.open
        real_fstat = os.fstat
        descriptors = []

        def opened(name, flags):
            descriptor = real_open(name, flags)
            descriptors.append(descriptor)
            return descriptor

        with patch("codex_image_generation.local_files.os.open", side_effect=opened), patch(
            "codex_image_generation.local_files.os.read", side_effect=OSError("read failed"),
        ), self.assertRaises(OSError):
            read_regular_bytes(path, 3)
        with self.assertRaises(OSError):
            real_fstat(descriptors[0])

    @unittest.skipUnless(hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO support required")
    def test_fifo_and_regular_to_fifo_replacement_never_wait_for_writer(self):
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        self.assertIsNone(read_cache(fifo, "test"))
        path = self.root / "replaced"
        path.write_bytes(b"{}")
        real_open = os.open

        def replace_with_fifo(name, flags):
            self.assertTrue(flags & os.O_NONBLOCK)
            path.unlink()
            os.mkfifo(path)
            return real_open(name, flags)

        with patch("codex_image_generation.local_files.os.open", side_effect=replace_with_fifo):
            self.assertIsNone(read_cache(path, "test"))
