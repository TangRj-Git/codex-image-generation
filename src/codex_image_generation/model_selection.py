"""Choose dialogue models from the provider and image models from official docs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, OpenAIError

from .config import ResolvedConfig, credential_values, model_cache_directory, redact_connection_text
from .errors import ImageToolError
from .model_cache import cache_lock, fresh, read_cache, write_cache
from .model_catalog import (
    GUIDE_URL, SNAPSHOT_DATE, bundled_catalog, image_capabilities, model_id, official_catalog, read_official_catalog, retired,
)
from .provider import client_options, request_headers

PROVIDER_TTL = 5 * 60
PROVIDER_TIMEOUT = 8
DISCOVERY_RETRY_DELAY = 60
MAX_PROVIDER_MODELS = 5000
UPSTREAM_DEFAULT = "upstream_default"
_FAST_PREFERENCES = {"gpt-image-2.5-flare": 3, "gpt-6-luna": 3, "gpt-5.6-luna": 2, "gpt-5.4-mini": 1}


def _policy(value: str | None, default: str = "quality") -> str:
    value = default if value is None else value
    if value not in {"quality", "fast"}:
        raise ImageToolError("model_policy 必须是 quality 或 fast。")
    return value


def _get(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def cache_directory(config: ResolvedConfig) -> Path:
    return model_cache_directory(codex_home=config.config_path.parent)


def _provider_scope(config: ResolvedConfig) -> str:
    # Include credential/tenant identity only in a digest, never in cache contents.
    identity = [config.base_url, config.api_key, config.headers, config.query_params,
                str(config.config_path), config.provider]
    if config.provider == "openai":
        identity.extend(os.environ.get(name, "") for name in ("OPENAI_ORG_ID", "OPENAI_PROJECT_ID"))
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def _safe_models(rows: Any, config: ResolvedConfig) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or len(rows) > MAX_PROVIDER_MODELS:
        raise ValueError("Invalid provider model list")
    secrets = credential_values(config)
    result = {}
    for row in rows:
        identifier = model_id(_get(row, "id"))
        if identifier is None or any(secret and (secret in identifier if len(secret) >= 6 else secret == identifier) for secret in secrets):
            continue
        shutdown = _get(row, "shutdown_date")
        try:
            if not isinstance(shutdown, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", shutdown):
                raise ValueError
            date.fromisoformat(shutdown)
        except ValueError:
            shutdown = None
        result[identifier] = {"id": identifier, "shutdown_date": shutdown}
    if rows and not result:
        raise ValueError("Provider returned no valid model records")
    return list(result.values())


async def provider_models(config: ResolvedConfig, *, refresh: bool = False) -> dict[str, Any]:
    scope = _provider_scope(config)
    path = cache_directory(config) / f"provider-{scope}.json"
    async with cache_lock(path):
        return await _provider_models(config, path, scope, refresh=refresh)


def _provider_failure(cached: dict[str, Any] | None, failure: dict[str, Any]) -> dict[str, Any]:
    status = failure.get("http_status")
    warning = ("中转站不允许或不支持 /models 查询；仍可使用明确指定或已有配置的对话模型。"
               if status in {403, 404, 405} else "中转站模型列表暂时不可获取；未进行生成探测或自动重试。")
    if cached:
        warning += " 当前展示上次有效目录，已标记过期，当前可用性未经验证。"
    return {**(cached or {"models": [], "fetched_at": None}),
            "source": "stale_cache" if cached else "unavailable", "stale": True,
            "http_status": status, "error_code": failure.get("error_code", "discovery_error"),
            "retry_after": failure["fetched_at"] + DISCOVERY_RETRY_DELAY, "warnings": [warning]}


async def _provider_models(config: ResolvedConfig, path: Path, scope: str, *, refresh: bool) -> dict[str, Any]:
    cached = await asyncio.to_thread(read_cache, path, scope)
    if cached is not None:
        try:
            cached["models"] = _safe_models(cached.get("models"), config)
        except ValueError:
            cached = None
    failure_path = path.with_name(path.stem + "-failure.json")
    failure = await asyncio.to_thread(read_cache, failure_path, scope + ":failure")
    if (not refresh and fresh(failure, DISCOVERY_RETRY_DELAY)
            and (failure.get("http_status") is None or type(failure.get("http_status")) is int)
            and isinstance(failure.get("error_code"), str)
            and failure.get("error_code") in {"http_error", "timeout", "connection_error", "invalid_response", "sdk_error"}
            and (cached is None or cached["fetched_at"] < failure["fetched_at"])):
        return _provider_failure(cached, failure)
    if not refresh and fresh(cached, PROVIDER_TTL):
        return {**cached, "source": "cache", "stale": False, "warnings": []}
    try:
        async with asyncio.timeout(PROVIDER_TIMEOUT), AsyncOpenAI(**client_options(config, timeout=PROVIDER_TIMEOUT)) as client:
            page = await client.models.list(extra_headers=request_headers(config))
            models = _safe_models(_get(page, "data"), config)
        entry = {"version": 1, "scope": scope, "fetched_at": time.time(), "models": models}
        saved = await asyncio.to_thread(write_cache, path, entry)
        return {**entry, "source": "provider", "stale": False,
                "warnings": [] if saved else ["中转站模型列表缓存无法写入。"]}
    except (OpenAIError, TimeoutError, ValueError) as exc:
        status = exc.status_code if isinstance(exc, APIStatusError) else None
        code = ("http_error" if status is not None else "timeout" if isinstance(exc, (APITimeoutError, TimeoutError))
                else "connection_error" if isinstance(exc, APIConnectionError)
                else "invalid_response" if isinstance(exc, ValueError) else "sdk_error")
        failure = {"version": 1, "scope": scope + ":failure", "fetched_at": time.time(),
                   "http_status": status, "error_code": code}
        await asyncio.to_thread(write_cache, failure_path, failure)
        return _provider_failure(cached, failure)


def _catalog_match(identifier: str, entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    exact = next((entry for entry in entries if entry["id"] == identifier), None)
    if exact:
        return exact
    # Accept only snapshots actually listed by the model page, not arbitrary dates.
    return next((entry for entry in entries if identifier in entry.get("snapshots", [])), None)


def _text_candidate(identifier: str) -> bool:
    if re.search(r"(?:image|audio|realtime|transcrib|tts|embedding|moderation|whisper|dall-e|sora)", identifier, re.I):
        return False
    return bool(re.match(r"(?:gpt-\d|chatgpt-|chat-latest$|o[134](?:-|$)|claude-|deepseek-|qwen|llama)", identifier, re.I))


def _canonical(entry: dict[str, Any]) -> str:
    identifier = entry["id"]
    if entry.get("kind") == "image" or identifier.startswith(("gpt-image-", "chatgpt-image-")):
        alias = model_id(entry.get("alias_of"))
        return alias if alias and identifier.startswith(alias + "-") else identifier
    canonical = model_id(entry.get("canonical_model"))
    if canonical and (canonical == identifier or identifier in entry.get("snapshots", [])):
        return canonical
    return identifier


def _rank(entry: dict[str, Any], policy: str) -> tuple[int, int, int]:
    rank = entry.get("quality_rank", 0)
    speed = _FAST_PREFERENCES.get(_canonical(entry), 0) if policy == "fast" else 0
    # Prefer a moving alias over its dated snapshot when recommendation scores tie.
    return speed, rank, int(not re.search(r"-\d{4}-\d{2}-\d{2}$", entry["id"]))


def _reason(entry: dict[str, Any], policy: str) -> str:
    if policy == "fast":
        return f"reviewed_speed_policy:{SNAPSHOT_DATE}" if _canonical(entry) in _FAST_PREFERENCES else "quality_fallback_no_speed_evidence"
    return entry["ranking_reason"]


def _catalog_retired(identifier: str, catalog: dict[str, Any]) -> bool:
    if retired(identifier) or identifier in catalog.get("retired_ids", []):
        return True
    return any(entry.get("retired") and (entry["id"] == identifier or identifier in entry.get("snapshots", []))
               for entry in catalog["images"] + catalog["responses"])


def _active_images(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in catalog["images"] if not _catalog_retired(item["id"], catalog)]


def dialogue_candidates(provider: dict[str, Any], catalog: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    models, unknown = [], []
    for item in provider["models"]:
        identifier = item["id"]
        if retired(identifier, item.get("shutdown_date")) or _catalog_retired(identifier, catalog):
            continue
        documented = _catalog_match(identifier, catalog["responses"])
        if documented:
            models.append({**documented, **item, "canonical_model": documented["id"], "image_tool_support": "documented",
                           "provider_tool_support_verified": False})
        elif _text_candidate(identifier):
            models.append({**item, "kind": "responses", "quality_rank": 0,
                           "image_tool_support": "unknown", "ranking_reason": "unranked",
                           "text_capability": "inferred_from_id", "provider_tool_support_verified": False})
        elif not re.search(r"image|audio|realtime|transcrib|tts|embedding|moderation|whisper|dall-e|sora", identifier, re.I):
            unknown.append(identifier)
    return models, unknown


def _recommend(entries: list[dict[str, Any]], policy: str) -> dict[str, Any] | None:
    ranked = [entry for entry in entries if any(value > 0 for value in _rank(entry, policy)[:2])]
    if not ranked:
        return None
    best_score = max(_rank(entry, policy) for entry in ranked)
    best = [entry for entry in ranked if _rank(entry, policy) == best_score]
    # Equal evidence for different families cannot establish a strongest model.
    return best[0] if len({_canonical(entry) for entry in best}) == 1 else None


async def list_dialogue_models(config: ResolvedConfig, *, refresh: bool = False, policy: str | None = None) -> dict[str, Any]:
    selected_policy = _policy(policy, config.model_policy)
    provider, catalog = await asyncio.gather(provider_models(config, refresh=refresh), official_catalog(cache_directory(config), refresh=refresh))
    models, unknown = dialogue_candidates(provider, catalog)
    recommendation = _recommend([m for m in models if m["image_tool_support"] == "documented"], selected_policy)
    return {
        "models": sorted(models, key=lambda m: _rank(m, selected_policy), reverse=True),
        "unclassified_models": unknown, "configured_model": redact_connection_text(config, config.model) if config.model else None,
        "responses_model_override": redact_connection_text(config, config.responses_model) if config.responses_model else None, "policy": selected_policy,
        "recommended_model": recommendation["id"] if recommendation else None,
        "recommendation_reason": _reason(recommendation, selected_policy) if recommendation else None,
        "source": provider["source"], "fetched_at": provider["fetched_at"], "stale": provider["stale"],
        "http_status": provider.get("http_status"),
        "error_code": provider.get("error_code"), "retry_after": provider.get("retry_after"),
        "catalog_source": catalog["source"], "catalog_fetched_at": catalog["fetched_at"],
        "catalog_stale": catalog["stale"],
        "provider_tool_support_verified": False,
        "warnings": provider["warnings"] + catalog["warnings"],
    }


async def list_official_image_models(cache_dir: Path, *, refresh: bool = False, policy: str = "quality") -> dict[str, Any]:
    policy = _policy(policy)
    catalog = await official_catalog(cache_dir, refresh=refresh)
    models = sorted(_active_images(catalog), key=lambda item: _rank(item, policy), reverse=True)
    recommendation = _recommend(models, policy)
    return {
        "models": models, "policy": policy,
        "recommended_model": recommendation["id"] if recommendation else None,
        "recommendation_reason": _reason(recommendation, policy) if recommendation else None,
        "source": catalog["source"], "source_url": GUIDE_URL, "fetched_at": catalog["fetched_at"],
        "stale": catalog["stale"], "provider_support_verified": False, "warnings": catalog["warnings"],
        "retry_after": catalog.get("retry_after"),
    }


@dataclass
class SelectedModels:
    responses_model: str
    responses_source: str
    image_model: str | None
    image_source: str
    policy: str = "quality"
    catalog_source: str | None = None
    catalog_fetched_at: float | None = None
    warnings: list[str] = field(default_factory=list)
    responses_reason: str | None = None
    image_reason: str | None = None
    catalog_stale: bool | None = None
    provider_models_source: str | None = None
    provider_models_fetched_at: float | None = None
    provider_models_stale: bool | None = None

    def public(self) -> dict[str, Any]:
        return {
            "responses_model_requested": self.responses_model,
            "responses_model_source": self.responses_source,
            "image_model_selected": self.image_model, "image_model_source": self.image_source,
            "image_model_reported": None,
            "image_model_evidence": "request_only" if self.image_model else "upstream_default_unknown",
            "model_selection_policy": self.policy,
            "catalog_source": self.catalog_source, "catalog_fetched_at": self.catalog_fetched_at,
            "responses_model_reason": self.responses_reason, "image_model_reason": self.image_reason,
            "catalog_stale": self.catalog_stale,
            "provider_models_source": self.provider_models_source,
            "provider_models_fetched_at": self.provider_models_fetched_at,
            "provider_models_stale": self.provider_models_stale,
        }


def _requested(value: str | None, label: str) -> str | None:
    if value is not None and model_id(value) is None:
        raise ImageToolError(f"{label} 必须是有效的单行模型 ID（最多 200 个字符）。")
    if value == "auto":
        raise ImageToolError(f"{label} 请省略以启用自动选择；auto 不是模型 ID。")
    if label == "responses_model" and value == UPSTREAM_DEFAULT:
        raise ImageToolError("upstream_default 仅适用于 image_model；对话模型请明确指定或省略以自动选择。")
    return value


def _reject_credential_models(config: ResolvedConfig, *models: str | None) -> None:
    if any(value is not None and redact_connection_text(config, value) != value for value in models):
        raise ImageToolError("模型 ID 包含已知认证值，请检查模型配置；凭据不能用作模型名称。")


def _validate_roles(responses_model: str | None, image_model: str | None, catalog: dict[str, Any]) -> None:
    if responses_model and (responses_model.startswith(("gpt-image-", "chatgpt-image-")) or _catalog_match(responses_model, catalog["images"])):
        raise ImageToolError("responses_model 必须是对话模型；专用图像型号请放入 image_model。")
    if image_model and image_model != UPSTREAM_DEFAULT and _catalog_match(image_model, catalog["responses"]):
        raise ImageToolError("image_model 必须是专用图像模型；对话型号请放入 responses_model。")
    for identifier in (responses_model, image_model):
        if identifier and _catalog_retired(identifier, catalog):
            raise ImageToolError("所选模型已被已知目录标为停用，请刷新模型列表并选择其他型号。")


async def select_models(config: ResolvedConfig, *, responses_model: str | None = None,
                        image_model: str | None = None, model_policy: str | None = None) -> SelectedModels:
    policy = _policy(model_policy, config.model_policy)
    response_source = "caller" if responses_model is not None else "environment"
    image_source = "caller" if image_model is not None else "environment"
    responses_model = _requested(responses_model if responses_model is not None else config.responses_model, "responses_model")
    image_model = _requested(image_model if image_model is not None else config.image_model, "image_model")
    _reject_credential_models(config, responses_model, image_model)
    _validate_roles(responses_model, image_model, bundled_catalog())
    catalog = None
    provider = None
    warnings: list[str] = []
    response_reason, image_reason = None, None
    if responses_model is None or image_model is None:
        catalog = await official_catalog(cache_directory(config))
        warnings.extend(catalog["warnings"])
        _validate_roles(responses_model, image_model, catalog)
    else:
        cached = await asyncio.to_thread(read_official_catalog, cache_directory(config))
        if cached:
            _validate_roles(responses_model, image_model, cached)
    if responses_model is None:
        provider = await provider_models(config)
        warnings.extend(provider["warnings"])
        candidates, _ = dialogue_candidates(provider, catalog)
        chosen = _recommend([m for m in candidates if m["image_tool_support"] == "documented"], policy)
        if chosen is not None:
            responses_model, response_source = chosen["id"], "provider_catalog"
            response_reason = _reason(chosen, policy)
        elif config.model:
            configured_entry = next((entry for entry in provider["models"] if entry["id"] == config.model), None)
            if configured_entry and retired(config.model, configured_entry.get("shutdown_date")):
                raise ImageToolError("配置中的对话模型已被中转站标为停用，请通过 list_models 选择其他型号。")
            responses_model, response_source = _requested(config.model, "responses_model"), config.model_source
            warnings.append("未发现已知支持图像工具的可选对话模型；使用调用上下文或 Codex 配置中的模型，工具支持尚未验证。")
        else:
            raise ImageToolError("未发现可自动选择的对话模型，配置也没有回退型号。请调用 list_models，或显式传 responses_model。")
    if image_model is None:
        chosen = _recommend(_active_images(catalog), policy)
        if chosen is None:
            raise ImageToolError("没有可自动推荐的图像模型。请调用 list_image_models 后显式指定 image_model，或刷新目录。")
        image_model, image_source = chosen["id"], "official_catalog"
        image_reason = _reason(chosen, policy)
    _validate_roles(responses_model, image_model, catalog or bundled_catalog())
    _reject_credential_models(config, responses_model, image_model)
    if image_model == UPSTREAM_DEFAULT:
        image_model, image_source = None, "upstream_default"
        warnings.append("已选择上游默认图像模型；本次不会发送 tools[].model，实际图像型号未知。")
    elif not image_capabilities(image_model):
        warnings.append("所选图像型号没有已核对的本地能力规则；参数支持由上游验证。")
    return SelectedModels(responses_model, response_source, image_model, image_source, policy,
                          catalog["source"] if catalog else None, catalog["fetched_at"] if catalog else None, warnings,
                          response_reason, image_reason, catalog["stale"] if catalog else None,
                          provider["source"] if provider else None, provider["fetched_at"] if provider else None,
                          provider["stale"] if provider else None)


def validate_model_options(identifier: str | None, tool: dict[str, Any]) -> None:
    if identifier is None:
        return
    caps = image_capabilities(identifier)
    if not caps:
        return
    quality = tool.get("quality")
    if quality is not None and quality not in caps["quality"]:
        raise ImageToolError(f"{identifier} 不支持 quality={quality}；请使用该型号支持的质量级别。")
    fidelity = tool.get("input_fidelity")
    if fidelity is not None and "input_fidelity" in caps and fidelity not in caps["input_fidelity"]:
        raise ImageToolError(f"{identifier} 不支持此 input_fidelity，请省略或选择该型号支持的值。")
    size = tool.get("size")
    if not size or size == "auto":
        return
    if not caps["custom_size"]:
        if size not in caps["sizes"]:
            raise ImageToolError(f"{identifier} 仅支持 auto、1024x1024、1536x1024、1024x1536。")
        return
    width, height = (int(value) for value in size.split("x"))
    if (width % caps["size_multiple"] or height % caps["size_multiple"]
            or max(width, height) > caps["max_edge"]
            or max(width, height) > caps["max_aspect_ratio"] * min(width, height)
            or not caps["min_pixels"] <= width * height <= caps["max_pixels"]):
        raise ImageToolError(f"{identifier} 的尺寸要求：宽高为 16 的倍数，比例 1:3 至 3:1，单边不超过 3840，总像素 655360 至 8294400。")
