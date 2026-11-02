"""Share the selected provider's authentication across listing and generation."""

from __future__ import annotations

import os
import re
from typing import Any

from openai import omit

from .config import ResolvedConfig


async def _header_auth_key() -> str:
    return ""


def request_headers(config: ResolvedConfig) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    for line in os.environ.get("OPENAI_CUSTOM_HEADERS", "").splitlines():
        name, separator, _ = line.partition(":")
        name = name.strip().lower()
        if separator and re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-z-]+", name):
            headers[name] = omit
    if config.provider != "openai":
        headers.update({"openai-organization": omit, "openai-project": omit})
    headers.update({name.lower(): value for name, value in config.headers.items()})
    if config.api_key:
        headers["authorization"] = f"Bearer {config.api_key}"
    elif not isinstance(headers.get("authorization"), str):
        headers["authorization"] = omit
    return headers


def client_options(config: ResolvedConfig, *, timeout: float | None = None) -> dict[str, Any]:
    options: dict[str, Any] = {
        "base_url": config.base_url, "api_key": config.api_key or _header_auth_key,
        "default_headers": config.headers, "default_query": config.query_params,
        "timeout": config.request_timeout if timeout is None else timeout, "max_retries": 0,
    }
    if config.provider != "openai":
        options.update(organization="", project="", admin_api_key="")
    return options
