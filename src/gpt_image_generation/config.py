"""Resolve Codex provider settings without copying or exposing credentials."""

from __future__ import annotations

import json
import math
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from .errors import ImageToolError

DEFAULT_RESPONSES_MODEL = "gpt-6-astra"
DEFAULT_IMAGE_MODEL = "gpt-image-2.5-flare"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_AUTH_HEADERS = frozenset({
    "authorization", "api-key", "x-api-key", "x-api-token",
    "x-auth-token", "x-access-token", "x-goog-api-key",
})


@dataclass(frozen=True)
class ResolvedConfig:
    # URLs may contain secret query values, so omit them from repr as well.
    base_url: str = field(repr=False)
    api_key: str = field(repr=False)
    headers: dict[str, str] = field(repr=False)
    query_params: dict[str, str] = field(repr=False)
    responses_model: str
    image_model: str
    provider: str
    model_source: str
    credential_source: str
    config_path: Path
    request_timeout: float
    required_env_vars: tuple[str, ...] = field(default=(), repr=False)

    def public_status(self) -> dict[str, Any]:
        """Return useful diagnostics without credential or header/query values."""
        parsed = urlsplit(self.base_url)
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        return {
            "provider": self.provider,
            "base_url": urlunsplit((parsed.scheme, netloc, parsed.path, "", "")),
            "responses_model": self.responses_model,
            "image_model": self.image_model,
            "model_source": self.model_source,
            "credential_source": self.credential_source,
            "config_path": str(self.config_path),
            "request_timeout": self.request_timeout,
            "required_env_vars": list(self.required_env_vars),
        }


def _text(value: Any, label: str) -> str:
    if (not isinstance(value, str) or not value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ImageToolError(f"{label} must be a non-empty, single-line string.")
    return value.strip()


def _env_text(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _text(value, name)


def _table(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ImageToolError(f"{label} must be a TOML table.")
    return value


def _read_toml(path: Path, *, required: bool) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        if not required:
            return {}
        raise ImageToolError("The selected Codex profile file does not exist.") from None
    except (OSError, ValueError):
        # Parser exceptions may include a secret-bearing source line.
        raise ImageToolError("Cannot read or parse the Codex TOML configuration; check the file locally.") from None


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_config(home: Path, env: Mapping[str, str], profile: str | None
                 ) -> tuple[dict[str, Any], Path, str]:
    path = home / "config.toml"
    config = _read_toml(path, required=False)
    selected = profile if profile is not None else _env_text(env, "CODEX_IMAGE_PROFILE")
    if selected is None and config.get("profile") is not None:
        selected = _text(config["profile"], "Codex profile")
    if selected is None:
        return config, path, "config.toml"
    if not isinstance(selected, str) or not _PROFILE_NAME.fullmatch(selected):
        raise ImageToolError("Profile names may contain only letters, digits, dots, underscores, and hyphens, and must begin with a letter or digit.")
    profile_path = home / f"{selected}.config.toml"
    try:
        has_file = profile_path.exists()
    except OSError:
        raise ImageToolError("Cannot access the selected Codex profile configuration.") from None
    if has_file:
        overlay = _read_toml(profile_path, required=True)
        path = profile_path
    else:
        profiles = _table(config.get("profiles", {}), "Codex profiles")
        if selected not in profiles:
            raise ImageToolError("The selected Codex profile was not found as a profile file or legacy profiles table.")
        overlay = _table(profiles[selected], "Selected Codex profile")
    source = f"profile:{selected}" if "model" in overlay else "config.toml"
    return _merge(config, overlay), path, source


def _base_url(value: Any) -> tuple[str, dict[str, str]]:
    value = _text(value, "Provider base URL")
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in {"https", "http"} and bool(parsed.hostname)
                 and parsed.username is None and parsed.password is None
                 and "#" not in value and not any(c.isspace() for c in value))
        parsed.port  # Validate malformed and out-of-range ports.
    except ValueError:
        valid = False
    if not valid:
        raise ImageToolError("Provider base URL must be an HTTP(S) URL without user information, whitespace, or a fragment.")
    # SDK paths must be appended to a URL without a query string. Pass query
    # values separately so /responses cannot accidentally become part of a value.
    clean_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return clean_url, dict(parse_qsl(parsed.query, keep_blank_values=True))


def _string_map(value: Any, label: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, item in _table(value, label).items():
        if not isinstance(key, str) or not key or not isinstance(item, str):
            raise ImageToolError(f"{label} must contain string names and string values.")
        if any(ord(c) < 32 or ord(c) == 127 for c in key + item):
            raise ImageToolError(f"{label} must not contain control characters.")
        result[key] = item
    return result


def _headers(provider: dict[str, Any], env: Mapping[str, str]
             ) -> tuple[dict[str, str], dict[str, str]]:
    # HTTP names are case-insensitive: never produce two Authorization headers.
    headers: dict[str, tuple[str, str, str]] = {}

    def put(name: str, value: str, source: str) -> None:
        if not _HEADER_NAME.fullmatch(name):
            raise ImageToolError("Provider HTTP header names must be valid HTTP field names.")
        key = name.lower()
        previous = headers.get(key)
        if previous and key in _AUTH_HEADERS and previous[1] != value:
            raise ImageToolError("Conflicting authentication headers are configured for the selected provider.")
        headers[key] = (name, value, source)

    for name, value in _string_map(provider.get("http_headers", {}), "Provider http_headers").items():
        put(name, value, "provider.http_headers")
    for name, variable in _string_map(provider.get("env_http_headers", {}), "Provider env_http_headers").items():
        if not _ENV_NAME.fullmatch(variable):
            raise ImageToolError("Provider env_http_headers must reference valid environment variable names.")
        # Missing optional header variables are skipped, unlike required env_key.
        value = _env_text(env, variable)
        if value is not None:
            put(name, value, f"provider.env_http_headers:{variable}")
    return ({name: value for name, value, _ in headers.values()},
            {name.lower(): source for name, _, source in headers.values()})


def _auth_file_key(home: Path, config: dict[str, Any]) -> tuple[str, str]:
    store = config.get("cli_auth_credentials_store", "file")
    if not isinstance(store, str):
        raise ImageToolError("Codex cli_auth_credentials_store must be a string.")
    if store == "auto":
        raise ImageToolError("Codex automatic credential storage may prefer the OS keyring over auth.json, so this server cannot determine the active credential. Configure a provider API key or explicit image-server credentials.")
    if store in {"keyring", "ephemeral"}:
        raise ImageToolError("Codex keyring or ephemeral credentials cannot be reused by this server; configure a provider API key or explicit image-server credentials.")
    if store != "file":
        raise ImageToolError("The selected Codex credential storage mode is not supported by this server.")
    try:
        with (home / "auth.json").open("r", encoding="utf-8") as handle:
            auth = json.load(handle)
    except FileNotFoundError:
        raise ImageToolError("No reusable API key was found. ChatGPT OAuth, OS keyring, and ephemeral credentials are not supported; configure provider.env_key or explicit image-server credentials.") from None
    except (OSError, ValueError):
        raise ImageToolError("Cannot read or parse Codex auth.json; inspect the file locally without sharing its contents.") from None
    if not isinstance(auth, dict):
        raise ImageToolError("Codex auth.json must contain an authentication object.")
    mode = auth.get("auth_mode")
    if isinstance(mode, str) and any(term in mode.lower() for term in ("chatgpt", "oauth", "ephemeral")):
        raise ImageToolError("Codex is using ChatGPT OAuth or ephemeral authentication, not a reusable API key.")
    key = auth.get("OPENAI_API_KEY")
    if key is not None:
        return _text(key, "Codex auth.json OPENAI_API_KEY"), "auth.json:OPENAI_API_KEY"
    if auth.get("tokens"):
        raise ImageToolError("Codex contains ChatGPT OAuth tokens, which this server cannot use as an API key.")
    raise ImageToolError("Codex auth.json does not contain a reusable OPENAI_API_KEY.")


def _credentials(provider_name: str, provider: dict[str, Any],
                 headers: dict[str, str], sources: dict[str, str],
                 config: dict[str, Any], home: Path, env: Mapping[str, str]
                 ) -> tuple[str, str]:
    auth = _table(provider.get("auth", {}), "Provider auth")
    if "command" in auth:
        raise ImageToolError("Provider auth.command is not supported by this server; configure an API key or explicit image-server credentials.")
    requires_auth = provider.get("requires_openai_auth", False)
    if not isinstance(requires_auth, bool):
        raise ImageToolError("Provider requires_openai_auth must be a boolean.")
    candidates: list[tuple[str, str]] = []
    if "env_key" in provider:
        variable = _text(provider["env_key"], "Provider env_key")
        if not _ENV_NAME.fullmatch(variable):
            raise ImageToolError("Provider env_key must be a valid environment variable name.")
        key = _env_text(env, variable)
        if key is None:
            raise ImageToolError("The environment variable declared by provider.env_key is missing or empty; it must also be passed to the MCP process.")
        candidates.append((key, f"provider.env_key:{variable}"))
    if "experimental_bearer_token" in provider:
        candidates.append((_text(provider["experimental_bearer_token"], "Provider experimental_bearer_token"),
                           "provider.experimental_bearer_token"))
    custom_auth: list[str] = []
    bearer_header: str | None = None
    for name, value in headers.items():
        normalized = name.lower()
        if normalized not in _AUTH_HEADERS:
            continue
        value = _text(value, "Provider authentication header")
        if normalized == "authorization":
            parts = value.split(None, 1)
            if parts[0].lower() == "bearer":
                if len(parts) != 2 or not parts[1].strip():
                    raise ImageToolError("Provider Authorization Bearer header is missing its token.")
                candidates.append((parts[1].strip(), f"{sources[normalized]}.Authorization"))
                bearer_header = name
                continue
        custom_auth.append(f"{sources[normalized]}.{name}")
    if len({key for key, _ in candidates}) > 1 or (custom_auth and candidates) or len(custom_auth) > 1:
        raise ImageToolError("Conflicting authentication sources are configured for the selected provider; choose one API-key or authentication-header source.")
    if candidates:
        if bearer_header is not None:
            del headers[bearer_header]
        return candidates[0]
    if custom_auth:
        # The caller suppresses the SDK's default Authorization header in this case.
        return "", custom_auth[0]
    if provider_name == "openai" or requires_auth:
        key = _env_text(env, "OPENAI_API_KEY")
        if key is not None:
            return key, "env:OPENAI_API_KEY"
        return _auth_file_key(home, config)
    raise ImageToolError("The selected custom provider has no supported authentication source. Configure provider.env_key, experimental_bearer_token, an Authorization/api-key header, or explicit image-server credentials; unrelated OpenAI credentials are not reused.")


def resolve_config(responses_model: str | None = None, image_model: str | None = None, *,
                   environ: Mapping[str, str] | None = None, codex_home: Path | None = None,
                   profile: str | None = None) -> ResolvedConfig:
    """Read settings on every call, with no cached credentials or config mutation."""
    env = os.environ if environ is None else environ
    used_env: set[str] = set()
    home_value = codex_home if codex_home is not None else _env_text(env, "CODEX_HOME")
    if codex_home is None and home_value is not None:
        used_env.add("CODEX_HOME")
    try:
        home = (Path(home_value).expanduser() if home_value is not None else Path.home() / ".codex").resolve()
    except (OSError, ValueError, TypeError):
        raise ImageToolError("Cannot resolve the Codex configuration directory.") from None
    config, config_path, configured_model_source = _load_config(home, env, profile)
    if profile is None and _env_text(env, "CODEX_IMAGE_PROFILE") is not None:
        used_env.add("CODEX_IMAGE_PROFILE")
    if responses_model is not None:
        selected_model = _text(responses_model, "Responses model")
        model_source = "argument"
    elif (selected_model := _env_text(env, "CODEX_IMAGE_RESPONSES_MODEL")) is not None:
        model_source = "env:CODEX_IMAGE_RESPONSES_MODEL"
        used_env.add("CODEX_IMAGE_RESPONSES_MODEL")
    elif "model" in config:
        selected_model = _text(config["model"], "Codex model")
        model_source = configured_model_source
    else:
        selected_model, model_source = DEFAULT_RESPONSES_MODEL, "default"
    selected_image_model = (_text(image_model, "Image model") if image_model is not None
                            else _env_text(env, "CODEX_IMAGE_MODEL") or DEFAULT_IMAGE_MODEL)
    if image_model is None and _env_text(env, "CODEX_IMAGE_MODEL") is not None:
        used_env.add("CODEX_IMAGE_MODEL")
    timeout_setting = _env_text(env, "CODEX_IMAGE_TIMEOUT")
    if timeout_setting is not None:
        used_env.add("CODEX_IMAGE_TIMEOUT")
    try:
        timeout = float(timeout_setting or "300")
    except (ValueError, TypeError):
        raise ImageToolError("CODEX_IMAGE_TIMEOUT must be a positive number no greater than 3600 seconds.") from None
    if not math.isfinite(timeout) or not 0 < timeout <= 3600:
        raise ImageToolError("CODEX_IMAGE_TIMEOUT must be a positive number no greater than 3600 seconds.")
    override_url = _env_text(env, "CODEX_IMAGE_BASE_URL")
    override_key = _env_text(env, "CODEX_IMAGE_API_KEY")
    if (override_url is None) != (override_key is None):
        raise ImageToolError("CODEX_IMAGE_BASE_URL and CODEX_IMAGE_API_KEY must be provided together to avoid sending a provider's credential to a different endpoint.")
    if override_url is not None and override_key is not None:
        used_env.update({"CODEX_IMAGE_BASE_URL", "CODEX_IMAGE_API_KEY"})
        clean_override_url, override_query = _base_url(override_url)
        return ResolvedConfig(
            base_url=clean_override_url, api_key=override_key, headers={}, query_params=override_query,
            responses_model=selected_model, image_model=selected_image_model, provider="explicit_override",
            model_source=model_source, credential_source="env:CODEX_IMAGE_API_KEY",
            config_path=config_path, request_timeout=timeout, required_env_vars=tuple(sorted(used_env)))
    provider_name = _env_text(env, "CODEX_IMAGE_PROVIDER")
    if provider_name is None:
        provider_name = _text(config.get("model_provider", "openai"), "Codex model_provider")
    else:
        used_env.add("CODEX_IMAGE_PROVIDER")
    providers = _table(config.get("model_providers", {}), "Codex model_providers")
    if provider_name not in providers and provider_name != "openai":
        raise ImageToolError("The selected custom provider is not defined in Codex model_providers.")
    provider = _table(providers.get(provider_name, {}), "Selected Codex provider")
    if provider.get("wire_api", "responses") != "responses":
        raise ImageToolError("The selected provider must use wire_api = 'responses' to call the image generation tool.")
    if "base_url" in provider:
        base_url, url_query = _base_url(provider["base_url"])
    elif provider_name == "openai":
        base_url, url_query = DEFAULT_BASE_URL, {}
    else:
        raise ImageToolError("The selected custom provider must define its own base_url; no endpoint will be guessed.")
    headers, sources = _headers(provider, env)
    query_params = {**url_query, **_string_map(provider.get("query_params", {}), "Provider query_params")}
    api_key, credential_source = _credentials(provider_name, provider, headers, sources, config, home, env)
    if "env_key" in provider:
        used_env.add(provider["env_key"].strip())
    used_env.update(source.split(":", 1)[1] for source in sources.values()
                    if source.startswith("provider.env_http_headers:"))
    if credential_source == "env:OPENAI_API_KEY":
        used_env.add("OPENAI_API_KEY")
    return ResolvedConfig(
        base_url=base_url, api_key=api_key, headers=headers, query_params=query_params,
        responses_model=selected_model, image_model=selected_image_model, provider=provider_name,
        model_source=model_source, credential_source=credential_source, config_path=config_path,
        request_timeout=timeout, required_env_vars=tuple(sorted(used_env)))
