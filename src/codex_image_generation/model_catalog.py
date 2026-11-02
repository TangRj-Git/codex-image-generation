"""Discover image tools and their dialogue models from public OpenAI documentation.

The bundled recommendation order was checked on 2026-11-02. Live descriptions
and guide recommendations take precedence; IDs are never ranked by version name.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2

from .model_cache import cache_lock, fresh, read_cache, write_cache

GUIDE_URL = "https://developers.openai.com/api/docs/guides/tools-image-generation"
IMAGE_GUIDE_URL = "https://developers.openai.com/api/docs/guides/image-generation"
MODEL_ROOT = "https://developers.openai.com/api/docs/models/"
MODELS_URL = "https://developers.openai.com/api/docs/models"
CATALOG_TTL = 24 * 60 * 60
DISCOVERY_TIMEOUT = 25
ENRICHMENT_TIMEOUT = 16
MAX_DOCUMENT_BYTES = 2_000_000
MAX_CATALOG_MODELS = 100
MAX_SNAPSHOTS = 32
RETRY_COOLDOWN = 60
MAX_RETIRED_MODELS = 2 * MAX_CATALOG_MODELS * (MAX_SNAPSHOTS + 1)
SNAPSHOT_DATE = "2026-11-02"
_MODEL_TOKEN = re.compile(r"(?<![\w.-])(?:gpt-[a-z0-9][a-z0-9.-]*|chatgpt-image-latest|chat-latest)(?![\w.-])")
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}\Z")
_RETIRED = {"gpt-image-1": "2026-10-23"}
_RESPONSE_ORDER = (
    "gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "gpt-5.6-sol", "gpt-5.5-pro",
    "gpt-5.5", "gpt-6.1-terra", "gpt-5.6-terra", "gpt-5.4-pro", "gpt-5.4",
    "gpt-5.2-pro", "gpt-5.2", "gpt-6-luna", "gpt-5.6-luna", "gpt-5.4-mini", "chat-latest",
    "gpt-5.6-cyber",
)
_IMAGE_ORDER = ("gpt-image-2.5-sunburst", "gpt-image-2.5-flare", "gpt-image-2", "gpt-image-1.5", "gpt-image-1-mini")
_ENTRY_FIELDS = frozenset({
    "id", "kind", "description", "source_url", "quality_rank", "ranking_reason", "capabilities",
    "details_verified", "capability_description_ranked", "snapshots", "snapshots_truncated",
    "shutdown_date", "retired", "alias_of", "recommendation_url",
})


def model_id(value: Any) -> str | None:
    return value if isinstance(value, str) and _MODEL_ID.fullmatch(value) else None


def retired(identifier: str, shutdown_date: str | None = None) -> bool:
    dates = (shutdown_date, _RETIRED.get(re.sub(r"-\d{4}-\d{2}-\d{2}$", "", identifier)))
    today = datetime.now(timezone.utc).date()
    return any(parsed is not None and parsed <= today for parsed in (_iso_date(value) for value in dates))


def _iso_date(value: Any) -> date | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _snapshot_of(identifier: str, base: str) -> bool:
    return identifier.startswith(base + "-") and _iso_date(identifier[len(base) + 1:]) is not None


def _retirement_notice(text: str, identifier: str) -> bool:
    """Associate a shutdown statement with its subject, not a replacement nearby."""
    plain = re.sub(r"[`*]", "", text)
    pattern = (rf"(?<![\w.-]){re.escape(identifier)}(?![\w.-])"
               r"(?P<between>[^!?\n]{0,140}?)\b(?:was|has been|is)\s+(?:shut down|retired|no longer available)\b")
    for match in re.finditer(pattern, plain, re.I):
        if not any(other != identifier for other in _tokens(match["between"])):
            return True
    return False


def image_capabilities(identifier: str) -> dict[str, Any]:
    family = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", identifier)
    if family not in _IMAGE_ORDER:
        return {}
    modern = family in {"gpt-image-2.5-sunburst", "gpt-image-2.5-flare", "gpt-image-2"}
    result: dict[str, Any] = {
        "quality": ["auto", "low", "medium", "high"] + (["xhigh", "max"] if family.startswith("gpt-image-2.5-") else []),
        "custom_size": modern, "source_url": IMAGE_GUIDE_URL, "checked_at": SNAPSHOT_DATE,
    }
    if modern:
        result.update(size_multiple=16, max_edge=3840, min_pixels=655360, max_pixels=8294400, max_aspect_ratio=3)
    else:
        result["sizes"] = ["auto", "1024x1024", "1536x1024", "1024x1536"]
    if family == "gpt-image-2":
        result["input_fidelity"] = []
    elif family == "gpt-image-1-mini":
        result["input_fidelity"] = ["low"]
    elif family == "gpt-image-1.5":
        result["input_fidelity"] = ["low", "high"]
    return result


class _Page(HTMLParser):
    """Keep main/article text and heading boundaries, excluding scripts and nav."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.main_parts: list[str] = []
        self.main = 0
        self.skip = 0

    def put(self, text: str) -> None:
        self.parts.append(text)
        if self.main:
            self.main_parts.append(text)

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in {"main", "article"}:
            self.main += 1
        if tag in {"script", "style", "nav"}:
            self.skip += 1
        if self.skip:
            return
        if tag == "a":
            href = dict(attrs).get("href", "")
            match = re.fullmatch(r"(?:https://developers\.openai\.com)?/api/docs/models/([a-z0-9.-]+?)(?:\.md)?/?", href)
            if match:
                self.put(" [" + match[1] + "] ")
        if re.fullmatch(r"h[1-6]", tag):
            self.put("\n" + "#" * int(tag[1]) + " ")
        elif tag in {"p", "li", "div", "br", "section"}:
            self.put("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "nav"}:
            self.skip = max(0, self.skip - 1)
        if not self.skip and (tag in {"p", "li", "div", "section"} or re.fullmatch(r"h[1-6]", tag)):
            self.put("\n")
        if tag in {"main", "article"}:
            self.main = max(0, self.main - 1)

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.put(data)


def document_text(body: str) -> str:
    if re.search(r"<(?:html|main|article)\b", body, re.I):
        parser = _Page()
        parser.feed(body)
        body = "".join(parser.main_parts or parser.parts)
    # Keep paragraph boundaries so retirement notices don't suppress nearby models.
    return "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in body.splitlines())


def _tokens(text: str) -> list[str]:
    return list(dict.fromkeys(token.rstrip(".").removesuffix(".md") for token in _MODEL_TOKEN.findall(text)))


def _entry(identifier: str, kind: str, description: str = "") -> dict[str, Any]:
    order = _IMAGE_ORDER if kind == "image" else _RESPONSE_ORDER
    base = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", identifier)
    priority = len(order) - order.index(base) if base in order else 0
    return {
        "id": identifier, "kind": kind, "description": description[:600],
        "source_url": MODEL_ROOT + identifier, "quality_rank": priority,
        "ranking_reason": f"reviewed_policy:{SNAPSHOT_DATE}" if priority else "unranked",
        "capabilities": image_capabilities(identifier) if kind == "image" else {"image_generation": True},
    }


def bundled_catalog() -> dict[str, Any]:
    return {
        "version": 1, "scope": "official-models", "fetched_at": datetime.fromisoformat(SNAPSHOT_DATE).replace(tzinfo=timezone.utc).timestamp(),
        "source_url": GUIDE_URL, "images": [_entry(i, "image") for i in _IMAGE_ORDER],
        "responses": [_entry(i, "responses") for i in _RESPONSE_ORDER],
        "retired_ids": [identifier for identifier in _RETIRED if retired(identifier)],
    }


def parse_guide(body: str) -> dict[str, Any]:
    text = document_text(body)
    # The Supported models section lists TOP-LEVEL dialogue models, not image models.
    supported = re.search(r"^## Supported models\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    intro = re.split(r"^## ", text, maxsplit=1, flags=re.M)[0]
    if not supported or "image generation" not in intro.lower():
        raise ValueError("Unrecognized official guide structure")
    mentioned = _tokens(intro + "\n" + supported[1])
    retired_ids = {identifier for identifier in mentioned
                   if retired(identifier) or _retirement_notice(intro + "\n" + supported[1], identifier)}
    image_ids = []
    for paragraph in re.split(r"\n\s*\n|(?<=[.!])\s+(?=[A-Z`])", intro):
        if re.search(r"shut down|deprecated|retired", paragraph, re.I):
            continue
        image_ids.extend(i for i in _tokens(paragraph) if i.startswith("gpt-image-") or i == "chatgpt-image-latest")
    images = [_entry(i, "image") for i in dict.fromkeys(image_ids) if i not in retired_ids]
    responses = [_entry(i, "responses") for i in _tokens(supported[1]) if not i.startswith(("gpt-image-", "chatgpt-image-")) and i not in retired_ids]
    missing_images = not images and not any(i.startswith(("gpt-image-", "chatgpt-image-")) for i in retired_ids)
    missing_responses = not responses and not any(not i.startswith(("gpt-image-", "chatgpt-image-")) for i in retired_ids)
    if missing_images or missing_responses or len(images) > 30 or len(responses) > 60:
        raise ValueError("Incomplete official model directory")
    preferred = re.search(r"[Ss]et\s+the\s+.*?\bmodel\b.*?\bto\s+(.{1,250}?)(?=\n\s*\n|\Z)", intro, re.S)
    if preferred:
        preferred_id = next((identifier for identifier in _tokens(preferred[1]) if identifier.startswith(("gpt-image-", "chatgpt-image-"))), None)
        for entry in images:
            if entry["id"] == preferred_id:
                entry.update(quality_rank=10000, ranking_reason="official_guide_recommendation")
    return {"version": 1, "scope": "official-models", "fetched_at": time.time(),
            "source_url": GUIDE_URL, "images": images, "responses": responses, "retired_ids": sorted(retired_ids)}


def enrich_model(entry: dict[str, Any], body: str) -> list[dict[str, Any]]:
    text = document_text(body)
    intro = re.split(r"^## ", text, maxsplit=1, flags=re.M)[0]
    identity = re.search(r"(?:^|\n)\s*(?:-\s*)?Model ID\s*:?\s*`?([A-Za-z0-9][A-Za-z0-9_.-]*)", intro, re.I)
    if (identity and identity[1] != entry["id"]) or (not identity and entry["id"] not in _tokens(intro)):
        return []
    entry["details_verified"] = True
    # Public descriptions guide selection; source and reason remain visible.
    description = re.sub(r"[#`*]", "", intro).strip()
    entry["description"] = re.sub(r"\s+", " ", description)[:600]
    if re.search(r"(?:our |the )?most capable model", intro, re.I) and not entry.get("capability_description_ranked"):
        entry["quality_rank"] += 1000
        entry["capability_description_ranked"] = True
        if entry["ranking_reason"] != "official_featured_recommendation":
            entry["ranking_reason"] = "official_most_capable_description"
    snapshots = re.search(r"^## Snapshots\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    result = []
    if snapshots:
        candidates = [identifier for identifier in _tokens(snapshots[1]) if _snapshot_of(identifier, entry["id"])]
        entry["snapshots"] = candidates[:MAX_SNAPSHOTS]
        if len(candidates) > MAX_SNAPSHOTS:
            entry["snapshots_truncated"] = True
    shutdown = re.search(r"(?:^|\n)\s*(?:-\s*)?Shutdown date\s*:\s*`?(\d{4}-\d{2}-\d{2})", intro, re.I)
    if shutdown and _iso_date(shutdown[1]) is not None:
        entry["shutdown_date"] = shutdown[1]
    if (_retirement_notice(intro, entry["id"]) or retired(entry["id"], entry.get("shutdown_date"))
            or re.search(r"\bThis model (?:was|has been|is) (?:shut down|retired|no longer available)\b", intro, re.I)):
        entry["retired"] = True
        return []
    if snapshots:
        if entry["kind"] == "image":
            for identifier in entry["snapshots"]:
                result.append({**entry, "id": identifier, "alias_of": entry["id"],
                               "capabilities": image_capabilities(identifier)})
    return result


def apply_featured_recommendations(catalog: dict[str, Any], body: str) -> bool:
    """Use the current catalog's featured descriptions ahead of historical taglines."""
    text = document_text(body)
    section = re.search(r"^## (?:Featured|Flagship) models\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not section:
        return False
    matches = list(_MODEL_TOKEN.finditer(section[1]))
    for index, match in enumerate(matches):
        identifier = match[0].rstrip(".").removesuffix(".md")
        end = matches[index + 1].start() if index + 1 < len(matches) else len(section[1])
        description = section[1][match.end():end]
        if not re.search(r"our most capable|most demanding work|our flagship", description, re.I):
            continue
        for entry in catalog["responses"]:
            if entry["id"] == identifier and entry["quality_rank"] < 100000:
                entry.update(quality_rank=100000 + entry["quality_rank"],
                             ranking_reason="official_featured_recommendation", recommendation_url=MODELS_URL)
    return True


async def fetch_document(client: httpx2.AsyncClient, url: str) -> str:
    # URLs are constructed only from a fixed official host and validated model IDs.
    # The site's documented Markdown export avoids navigation and large HTML bundles.
    # Older pages can still fall back to their fixed official HTML URL.
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "developers.openai.com"
            or not parsed.path.startswith("/api/docs/") or parsed.query or parsed.fragment):
        raise ValueError("Only fixed official documentation URLs are supported")
    candidates = [url] if url.endswith(".md") else [url + ".md", url]
    for index, candidate in enumerate(candidates):
        async with client.stream("GET", candidate, follow_redirects=False) as response:
            if index == 0 and len(candidates) > 1 and response.status_code in {301, 302, 303, 307, 308, 404, 406}:
                continue
            response.raise_for_status()
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_DOCUMENT_BYTES:
                    raise ValueError("Official document exceeds the local limit")
                chunks.append(chunk)
        return b"".join(chunks).decode("utf-8")
    raise ValueError("Official document unavailable")


async def fetch_catalog() -> dict[str, Any]:
    async with asyncio.timeout(DISCOVERY_TIMEOUT), httpx2.AsyncClient(
        timeout=8, headers={"User-Agent": "codex-image-generation model discovery", "Accept": "text/html,text/markdown"},
    ) as client:
        catalog = parse_guide(await fetch_document(client, GUIDE_URL))
        semaphore = asyncio.Semaphore(4)
        additions: list[dict[str, Any]] = []

        async def enrich(entry: dict[str, Any]) -> None:
            async with semaphore:
                try:
                    additions.extend(enrich_model(entry, await fetch_document(client, entry["source_url"])))
                    if not entry.get("details_verified") or entry.get("snapshots_truncated"):
                        catalog["enrichment_incomplete"] = True
                except (httpx2.HTTPError, ValueError, UnicodeError):
                    catalog["enrichment_incomplete"] = True

        entries = catalog["images"] + catalog["responses"]

        async def featured() -> None:
            async with semaphore:
                try:
                    if not apply_featured_recommendations(catalog, await fetch_document(client, MODELS_URL)):
                        catalog["enrichment_incomplete"] = True
                except (httpx2.HTTPError, ValueError, UnicodeError):
                    catalog["enrichment_incomplete"] = True

        try:
            async with asyncio.timeout(ENRICHMENT_TIMEOUT):
                await asyncio.gather(featured(), *(enrich(entry) for entry in entries))
        except TimeoutError:
            # The guide is already valid. Optional model-page latency must not discard it.
            catalog["enrichment_incomplete"] = True
        retired_ids = set(catalog.get("retired_ids", []))
        for entry in entries:
            if entry.get("retired"):
                retired_ids.update([entry["id"], *entry.get("snapshots", [])])
        catalog["retired_ids"] = sorted(retired_ids)
        by_id = {entry["id"]: entry for entry in catalog["images"]}
        for entry in additions:
            if len(by_id) >= MAX_CATALOG_MODELS and entry["id"] not in by_id:
                catalog["enrichment_incomplete"] = True
                continue
            by_id.setdefault(entry["id"], entry)
        catalog["images"] = list(by_id.values())
        for key in ("images", "responses"):
            catalog[key] = [entry for entry in catalog[key] if not entry.get("retired") and entry["id"] not in retired_ids]
        # An authoritative withdrawal must not revive old models through cache fallback.
        if not _valid_catalog(catalog):
            raise ValueError("Invalid official model directory")
        return catalog


def _same_json_shape(actual: Any, expected: Any) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(_same_json_shape(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(_same_json_shape(left, right) for left, right in zip(actual, expected))
    return actual == expected


def _valid_catalog(entry: dict[str, Any] | None) -> bool:
    if not isinstance(entry, dict) or entry.get("source_url") != GUIDE_URL:
        return False
    retired_ids = entry.get("retired_ids", [])
    if (not isinstance(retired_ids, list) or len(retired_ids) > MAX_RETIRED_MODELS
            or any(not model_id(value) for value in retired_ids) or len(set(retired_ids)) != len(retired_ids)):
        return False
    if "enrichment_incomplete" in entry and type(entry["enrichment_incomplete"]) is not bool:
        return False
    for key, kind in (("images", "image"), ("responses", "responses")):
        values = entry.get(key)
        if not isinstance(values, list) or len(values) > MAX_CATALOG_MODELS:
            return False
        if not values and not any(identifier.startswith(("gpt-image-", "chatgpt-image-")) == (kind == "image") for identifier in retired_ids):
            return False
        seen: set[str] = set()
        for item in values:
            if (not isinstance(item, dict) or set(item) - _ENTRY_FIELDS
                    or not model_id(item.get("id")) or item.get("kind") != kind
                    or type(item.get("quality_rank")) is not int or not 0 <= item["quality_rank"] < 200000
                    or not isinstance(item.get("ranking_reason"), str) or len(item["ranking_reason"]) > 100
                    or not isinstance(item.get("description", ""), str) or len(item.get("description", "")) > 600
                    or not isinstance(item.get("snapshots", []), list)
                    or len(item.get("snapshots", [])) > MAX_SNAPSHOTS
                    or any(model_id(value) is None for value in item.get("snapshots", []))):
                return False
            identifier = item["id"]
            if identifier in seen or identifier in retired_ids:
                return False
            seen.add(identifier)
            base = item.get("alias_of", identifier)
            if not model_id(base) or (base != identifier and not _snapshot_of(identifier, base)):
                return False
            snapshots = item.get("snapshots", [])
            if len(set(snapshots)) != len(snapshots) or any(not _snapshot_of(snapshot, base) for snapshot in snapshots):
                return False
            if item.get("source_url") != MODEL_ROOT + base:
                return False
            if "recommendation_url" in item and item["recommendation_url"] != MODELS_URL:
                return False
            for flag in ("retired", "details_verified", "snapshots_truncated", "capability_description_ranked"):
                if flag in item and type(item[flag]) is not bool:
                    return False
            if "shutdown_date" in item and _iso_date(item["shutdown_date"]) is None:
                return False
            is_image = item["id"].startswith(("gpt-image-", "chatgpt-image-"))
            if is_image != (kind == "image"):
                return False
            capabilities = image_capabilities(identifier) if is_image else {"image_generation": True}
            if not _same_json_shape(item.get("capabilities"), capabilities):
                return False
    return True


def _apply_retirements(catalog: dict[str, Any]) -> dict[str, Any]:
    retired_ids = set(catalog.get("retired_ids", []))
    for key in ("images", "responses"):
        for item in catalog[key]:
            if item.get("retired") or retired(item["id"], item.get("shutdown_date")):
                retired_ids.update([item["id"], *item.get("snapshots", [])])
    for key in ("images", "responses"):
        retired_ids.update(item["id"] for item in catalog[key] if item.get("alias_of") in retired_ids)
    result = {**catalog, "retired_ids": sorted(retired_ids)}
    for key in ("images", "responses"):
        result[key] = [item for item in catalog[key] if item["id"] not in retired_ids and item.get("alias_of") not in retired_ids]
    return result


def read_official_catalog(cache_dir: Path) -> dict[str, Any] | None:
    """Read validated local evidence only; never refresh or require credentials."""
    cached = read_cache(cache_dir / "official-models.json", "official-models")
    return _apply_retirements(cached) if _valid_catalog(cached) else None


async def official_catalog(cache_dir: Path, *, refresh: bool = False) -> dict[str, Any]:
    """Serialize refreshes per cache path without retaining closed event loops."""
    async with cache_lock(cache_dir / "official-models.json"):
        return await _official_catalog(cache_dir, refresh=refresh)


async def _official_catalog(cache_dir: Path, *, refresh: bool = False) -> dict[str, Any]:
    path = cache_dir / "official-models.json"
    failure_path = cache_dir / "official-models.failure.json"
    cached = await asyncio.to_thread(read_official_catalog, cache_dir)
    failure = await asyncio.to_thread(read_cache, failure_path, "official-models-failure")
    if (not refresh and failure and failure.get("reason") == "fetch_failed" and fresh(failure, RETRY_COOLDOWN)
            and (cached is None or cached["fetched_at"] < failure["fetched_at"])):
        return {**(cached or bundled_catalog()), "source": "stale_cache" if cached else "bundled",
                "stale": True, "retry_after": failure["fetched_at"] + RETRY_COOLDOWN,
                "warnings": ["官方目录刚刚刷新失败，60 秒内暂缓自动重查；使用旧缓存或标明日期的内置快照，可手动强制刷新。"]}
    if not refresh and fresh(cached, CATALOG_TTL):
        return {**cached, "source": "cache", "stale": False,
                "warnings": ["目录中的部分模型页面未完成读取，推荐可能使用已核对的本地策略。"] if cached.get("enrichment_incomplete") else []}
    try:
        result = await fetch_catalog()
        if not _valid_catalog(result):
            raise ValueError("Invalid official catalog")
    except (httpx2.HTTPError, ValueError, UnicodeError, TimeoutError):
        failed_at = time.time()
        await asyncio.to_thread(write_cache, failure_path, {
            "version": 1, "scope": "official-models-failure", "fetched_at": failed_at, "reason": "fetch_failed",
        })
        return {**(cached or bundled_catalog()), "source": "stale_cache" if cached else "bundled",
                "stale": True, "retry_after": failed_at + RETRY_COOLDOWN,
                "warnings": ["官方目录暂时不可刷新；使用上次有效目录或 2026-11-02 内置快照，型号与能力可能已变化。"]}
    result = _apply_retirements(result)
    saved = await asyncio.to_thread(write_cache, path, result)
    if saved:
        try:
            await asyncio.to_thread(failure_path.unlink, missing_ok=True)
        except OSError:
            pass  # The newer persisted success also supersedes this failure record.
    warnings = [] if saved else ["模型目录缓存无法写入，本次仍使用已获取的官方目录。"]
    if result.get("enrichment_incomplete"):
        warnings.append("部分模型页面未能及时读取；目录使用官方指南，推荐同时参考已核对的本地策略。")
    return {**result, "source": "official", "stale": False, "warnings": warnings}
