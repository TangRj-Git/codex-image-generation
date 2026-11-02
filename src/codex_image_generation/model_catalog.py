"""Discover image tools and their dialogue models from public OpenAI documentation.

The bundled recommendation order was checked on 2026-11-02. Live descriptions
and guide recommendations take precedence; IDs are never ranked by version name.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx2

from .model_cache import fresh, read_cache, write_cache

GUIDE_URL = "https://developers.openai.com/api/docs/guides/tools-image-generation"
IMAGE_GUIDE_URL = "https://developers.openai.com/api/docs/guides/image-generation"
MODEL_ROOT = "https://developers.openai.com/api/docs/models/"
MODELS_URL = "https://developers.openai.com/api/docs/models"
CATALOG_TTL = 24 * 60 * 60
DISCOVERY_TIMEOUT = 25
MAX_DOCUMENT_BYTES = 2_000_000
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


def model_id(value: Any) -> str | None:
    return value if isinstance(value, str) and _MODEL_ID.fullmatch(value) else None


def retired(identifier: str, shutdown_date: str | None = None) -> bool:
    date = shutdown_date or _RETIRED.get(re.sub(r"-\d{4}-\d{2}-\d{2}$", "", identifier))
    return bool(date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date)
                and date <= datetime.now(timezone.utc).date().isoformat())


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
    }


def parse_guide(body: str) -> dict[str, Any]:
    text = document_text(body)
    # The Supported models section lists TOP-LEVEL dialogue models, not image models.
    supported = re.search(r"^## Supported models\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    intro = re.split(r"^## ", text, maxsplit=1, flags=re.M)[0]
    if not supported or "image generation" not in intro.lower():
        raise ValueError("Unrecognized official guide structure")
    image_ids = []
    for paragraph in re.split(r"\n\s*\n|(?<=[.!])\s+(?=[A-Z`])", intro):
        if re.search(r"shut down|deprecated|retired", paragraph, re.I):
            continue
        image_ids.extend(i for i in _tokens(paragraph) if i.startswith("gpt-image-") or i == "chatgpt-image-latest")
    images = [_entry(i, "image") for i in dict.fromkeys(image_ids) if not retired(i)]
    responses = [_entry(i, "responses") for i in _tokens(supported[1]) if not i.startswith(("gpt-image-", "chatgpt-image-")) and not retired(i)]
    if not images or not responses or len(images) > 30 or len(responses) > 60:
        raise ValueError("Incomplete official model directory")
    preferred = re.search(r"[Ss]et\s+the\s+.*?\bmodel\b.*?\bto\s+[`\s]*(gpt-image-[a-z0-9.-]+)", intro, re.S)
    if preferred:
        for entry in images:
            if entry["id"] == preferred[1]:
                entry.update(quality_rank=10000, ranking_reason="official_guide_recommendation")
    return {"version": 1, "scope": "official-models", "fetched_at": time.time(),
            "source_url": GUIDE_URL, "images": images, "responses": responses}


def enrich_model(entry: dict[str, Any], body: str) -> list[dict[str, Any]]:
    text = document_text(body)
    intro = re.split(r"^## ", text, maxsplit=1, flags=re.M)[0]
    if entry["id"] not in text or not intro.strip():
        return []
    if re.search(r"(?:was|has been) (?:shut down|retired)", intro, re.I):
        entry["retired"] = True
        return []
    # Public descriptions guide selection; source and reason remain visible.
    description = re.sub(r"[#`*]", "", intro).strip()
    entry["description"] = re.sub(r"\s+", " ", description)[:600]
    if re.search(r"(?:our |the )?most capable model", intro, re.I):
        entry["quality_rank"] += 1000
        if entry["ranking_reason"] != "official_featured_recommendation":
            entry["ranking_reason"] = "official_most_capable_description"
    snapshots = re.search(r"^## Snapshots\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    result = []
    if snapshots:
        entry["snapshots"] = [identifier for identifier in _tokens(snapshots[1])
                              if identifier.startswith(entry["id"] + "-") and not retired(identifier)]
        if entry["kind"] == "image":
            for identifier in entry["snapshots"]:
                result.append({**entry, "id": identifier, "alias_of": entry["id"],
                               "capabilities": image_capabilities(identifier)})
    return result


def apply_featured_recommendations(catalog: dict[str, Any], body: str) -> None:
    """Use the current catalog's featured descriptions ahead of historical taglines."""
    text = document_text(body)
    section = re.search(r"^## (?:Featured|Flagship) models\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not section:
        return
    matches = list(_MODEL_TOKEN.finditer(section[1]))
    for index, match in enumerate(matches):
        identifier = match[0].rstrip(".").removesuffix(".md")
        end = matches[index + 1].start() if index + 1 < len(matches) else len(section[1])
        description = section[1][match.end():end]
        if not re.search(r"our most capable|most demanding work|our flagship", description, re.I):
            continue
        for entry in catalog["responses"]:
            if entry["id"] == identifier:
                entry.update(quality_rank=100000 + entry["quality_rank"],
                             ranking_reason="official_featured_recommendation", recommendation_url=MODELS_URL)


async def fetch_document(client: httpx2.AsyncClient, url: str) -> str:
    # URLs are constructed only from a fixed official host and validated model IDs.
    # The site's documented Markdown export avoids navigation and large HTML bundles.
    # Older pages can still fall back to their fixed official HTML URL.
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

        async def enrich(entry: dict[str, Any]) -> list[dict[str, Any]]:
            async with semaphore:
                try:
                    return enrich_model(entry, await fetch_document(client, entry["source_url"]))
                except (httpx2.HTTPError, ValueError, UnicodeError):
                    return []

        entries = catalog["images"] + catalog["responses"]

        async def featured() -> None:
            async with semaphore:
                try:
                    apply_featured_recommendations(catalog, await fetch_document(client, MODELS_URL))
                except (httpx2.HTTPError, ValueError, UnicodeError):
                    pass

        try:
            async with asyncio.timeout(16):
                batches = await asyncio.gather(featured(), *(enrich(entry) for entry in entries))
                additions = batches[1:]
        except TimeoutError:
            # The guide is already valid. Optional model-page latency must not discard it.
            additions = []
            catalog["enrichment_incomplete"] = True
        catalog["images"].extend(item for batch in additions for item in batch)
        for key in ("images", "responses"):
            catalog[key] = [entry for entry in catalog[key] if not entry.get("retired")]
        if not catalog["images"] or not catalog["responses"]:
            raise ValueError("No active models in the official directory")
        return catalog


def _valid_catalog(entry: dict[str, Any] | None) -> bool:
    if not entry:
        return False
    for key, kind in (("images", "image"), ("responses", "responses")):
        values = entry.get(key)
        if not isinstance(values, list) or not 0 < len(values) <= 100:
            return False
        for item in values:
            if (not isinstance(item, dict) or not model_id(item.get("id")) or item.get("kind") != kind
                    or type(item.get("quality_rank")) is not int or not isinstance(item.get("capabilities"), dict)
                    or not isinstance(item.get("ranking_reason"), str) or not isinstance(item.get("description", ""), str)
                    or not isinstance(item.get("snapshots", []), list)
                    or any(model_id(value) is None for value in item.get("snapshots", []))):
                return False
            is_image = item["id"].startswith(("gpt-image-", "chatgpt-image-"))
            if is_image != (kind == "image"):
                return False
    return True


async def official_catalog(cache_dir: Path, *, refresh: bool = False) -> dict[str, Any]:
    path = cache_dir / "official-models.json"
    cached = await asyncio.to_thread(read_cache, path, "official-models")
    if not _valid_catalog(cached):
        cached = None
    if not refresh and fresh(cached, CATALOG_TTL):
        return {**cached, "source": "cache", "stale": False,
                "warnings": ["目录中的部分模型页面未完成读取，推荐可能使用已核对的本地策略。"] if cached.get("enrichment_incomplete") else []}
    try:
        result = await fetch_catalog()
    except (httpx2.HTTPError, ValueError, UnicodeError, TimeoutError):
        return {**(cached or bundled_catalog()), "source": "stale_cache" if cached else "bundled",
                "stale": True, "warnings": ["官方目录暂时不可刷新；使用上次有效目录或 2026-11-02 内置快照，型号与能力可能已变化。"]}
    saved = await asyncio.to_thread(write_cache, path, result)
    warnings = [] if saved else ["模型目录缓存无法写入，本次仍使用已获取的官方目录。"]
    if result.get("enrichment_incomplete"):
        warnings.append("部分模型页面未能及时读取；目录使用官方指南，推荐同时参考已核对的本地策略。")
    return {**result, "source": "official", "stale": False, "warnings": warnings}
