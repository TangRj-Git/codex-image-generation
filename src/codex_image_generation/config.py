"""Read the selected Codex provider without inventing models, endpoints or credentials."""

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

_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_AUTH_HEADERS = frozenset({
    "authorization", "api-key", "x-api-key", "x-api-token",
    "x-auth-token", "x-access-token", "x-goog-api-key", "ocp-apim-subscription-key",
})


@dataclass(frozen=True)
class ResolvedConfig:
    base_url: str = field(repr=False)
    api_key: str = field(repr=False)
    headers: dict[str, str] = field(repr=False)
    query_params: dict[str, str] = field(repr=False)
    model: str | None
    provider: str
    model_source: str
    credential_source: str
    config_path: Path
    request_timeout: float
    required_env_vars: tuple[str, ...] = field(default=(), repr=False)
    responses_model: str | None = None
    image_model: str | None = None
    model_policy: str = "quality"

    def public_status(self) -> dict[str, Any]:
        """Expose configuration metadata, not secrets or proof of a session model."""
        parsed = urlsplit(self.base_url)
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        return {
            "provider": self.provider,
            "base_url": urlunsplit((parsed.scheme, netloc, parsed.path, "", "")),
            "model": self.model,
            "model_source": self.model_source,
            "credential_source": self.credential_source,
            "config_path": str(self.config_path),
            "request_timeout": self.request_timeout,
            "required_env_vars": list(self.required_env_vars),
            "session_model_auto_detection": False,
            "session_model_verified": False,
            "responses_model_override": self.responses_model,
            "image_model_override": self.image_model,
            "model_selection_policy": self.model_policy,
        }


def _text(value: Any, label: str) -> str:
    if (not isinstance(value, str) or not value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ImageToolError(f"{label} must be a non-empty, single-line string.")
    return value.strip()


def _env_text(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _text(value, name)


def _table(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ImageToolError(f"{label} must be a TOML table.")
    return value


def _variable(value: Any, label: str) -> str:
    value = _text(value, label)
    if not _ENV_NAME.fullmatch(value):
        raise ImageToolError(f"{label} must be a valid environment variable name.")
    return value


def _home(env: Mapping[str, str], codex_home: Path | None) -> Path:
    value = codex_home if codex_home is not None else _env_text(env, "CODEX_HOME")
    try:
        return (Path(value).expanduser() if value is not None else Path.home() / ".codex").resolve()
    except (OSError, ValueError, TypeError):
        raise ImageToolError("Cannot resolve the Codex configuration directory.") from None


def model_cache_directory(*, environ: Mapping[str, str] | None = None,
                          codex_home: Path | None = None) -> Path:
    """Locate the public catalog cache without reading provider credentials."""
    return _home(os.environ if environ is None else environ, codex_home) / "cache" / "image-generation"


def _model_policy(env: Mapping[str, str]) -> str:
    value = _env_text(env, "CODEX_IMAGE_MODEL_POLICY") or "quality"
    if value not in {"quality", "fast"}:
        raise ImageToolError("CODEX_IMAGE_MODEL_POLICY must be quality or fast.")
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
                 ) -> tuple[dict[str, Any], Path, str | None]:
    path = home / "config.toml"
    config = _read_toml(path, required=False)
    selected = profile if profile is not None else _env_text(env, "CODEX_IMAGE_PROFILE")
    if selected is None and config.get("profile") is not None:
        selected = _text(config["profile"], "Codex profile")
    if selected is None:
        return config, path, None
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
    return _merge(config, overlay), path, selected


def _provider(config: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    name = _text(config.get("model_provider", "openai"), "Codex model_provider")
    providers = _table(config.get("model_providers", {}), "Codex model_providers")
    if "openai" in providers:
        raise ImageToolError("The built-in openai provider is reserved. Set top-level openai_base_url instead of model_providers.openai.")
    if name == "openai":
        return name, {}
    if name not in providers:
        raise ImageToolError("The selected custom provider is not defined in Codex model_providers.")
    provider = _table(providers[name], "Selected Codex provider")
    if not isinstance(provider.get("requires_openai_auth", False), bool):
        raise ImageToolError("Provider requires_openai_auth must be a boolean.")
    return name, provider


def _timeout(env: Mapping[str, str]) -> float:
    try:
        value = float(_env_text(env, "CODEX_IMAGE_TIMEOUT") or "300")
    except (ValueError, TypeError):
        raise ImageToolError("CODEX_IMAGE_TIMEOUT must be a positive number no greater than 3600 seconds.") from None
    if not math.isfinite(value) or not 0 < value <= 3600:
        raise ImageToolError("CODEX_IMAGE_TIMEOUT must be a positive number no greater than 3600 seconds.")
    return value


def _base_url(value: Any) -> tuple[str, dict[str, str]]:
    value = _text(value, "Provider base URL")
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in {"https", "http"} and bool(parsed.hostname)
                 and parsed.username is None and parsed.password is None
                 and "#" not in value and "\\" not in value
                 and not any(c.isspace() for c in value))
        parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ImageToolError("Provider base URL must be an HTTP(S) URL without user information, whitespace, backslashes, or a fragment.")
    query: dict[str, str] = {}
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        if key in query:
            raise ImageToolError("Provider base URL contains a repeated query parameter. Use one value per name; repeated values cannot be forwarded without changing their meaning.")
        query[key] = item
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")), query


def _string_map(value: Any, label: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, item in _table(value, label).items():
        if not isinstance(key, str) or not key or not isinstance(item, str):
            raise ImageToolError(f"{label} must contain string names and string values.")
        if any(ord(c) < 32 or ord(c) == 127 for c in key + item):
            raise ImageToolError(f"{label} must not contain control characters.")
        result[key] = item
    return result


def _env_header_declarations(provider: dict[str, Any]) -> dict[str, str]:
    result = _string_map(provider.get("env_http_headers", {}), "Provider env_http_headers")
    for name, variable in result.items():
        if not _HEADER_NAME.fullmatch(name):
            raise ImageToolError("Provider HTTP header names must be valid HTTP field names.")
        result[name] = _variable(variable, "Provider env_http_headers")
    return result


def _required_env(env: Mapping[str, str], name: str, provider: dict[str, Any], *,
                  codex_home: Path | None, profile: str | None) -> tuple[str, ...]:
    names: set[str] = set()
    for variable, relevant in (
        ("CODEX_HOME", codex_home is None),
        ("CODEX_IMAGE_PROFILE", profile is None),
        ("CODEX_IMAGE_TIMEOUT", True),
        ("CODEX_IMAGE_RESPONSES_MODEL", True),
        ("CODEX_IMAGE_MODEL", True),
        ("CODEX_IMAGE_MODEL_POLICY", True),
    ):
        if relevant and _env_text(env, variable) is not None:
            names.add(variable)
    if "env_key" in provider:
        names.add(_variable(provider["env_key"], "Provider env_key"))
    # Declarations, not currently available values, determine what the child needs.
    names.update(_env_header_declarations(provider).values())
    if name == "openai" or provider.get("requires_openai_auth", False):
        names.add("OPENAI_API_KEY")
    if name == "openai":
        for variable in ("OPENAI_ORG_ID", "OPENAI_PROJECT_ID"):
            if _env_text(env, variable) is not None:
                names.add(variable)
    return tuple(sorted(names))


def get_launch_settings(*, environ: Mapping[str, str] | None = None,
                        codex_home: Path | None = None,
                        profile: str | None = None) -> dict[str, Any]:
    """Describe child-process configuration without reading or requiring credentials."""
    env = os.environ if environ is None else environ
    home = _home(env, codex_home)
    config, _, selected_profile = _load_config(home, env, profile)
    name, provider = _provider(config)
    return {
        "codex_home": str(home),
        "profile": selected_profile,
        "required_env_vars": list(_required_env(env, name, provider, codex_home=codex_home, profile=profile)),
        "request_timeout": _timeout(env),
    }


def _headers(provider: dict[str, Any], env: Mapping[str, str]
             ) -> tuple[dict[str, str], dict[str, str]]:
    headers: dict[str, tuple[str, str, str]] = {}

    def put(name: str, value: str, source: str) -> None:
        if not _HEADER_NAME.fullmatch(name):
            raise ImageToolError("Provider HTTP header names must be valid HTTP field names.")
        normalized = name.lower()
        previous = headers.get(normalized)
        if previous and normalized == "authorization" and previous[1] != value:
            raise ImageToolError("Conflicting Authorization headers are configured for the selected provider.")
        headers[normalized] = (name, value, source)

    for name, value in _string_map(provider.get("http_headers", {}), "Provider http_headers").items():
        put(name, value, "provider.http_headers")
    for name, variable in _env_header_declarations(provider).items():
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
        raise ImageToolError("Codex automatic credential storage may prefer the OS keyring over auth.json, so this server cannot determine the active credential. Configure API-key authentication for the selected Codex provider.")
    if store in {"keyring", "ephemeral"}:
        raise ImageToolError("Codex keyring or ephemeral credentials cannot be reused by this server; configure API-key authentication for the selected Codex provider.")
    if store != "file":
        raise ImageToolError("The selected Codex credential storage mode is not supported by this server.")
    try:
        with (home / "auth.json").open("r", encoding="utf-8") as handle:
            auth = json.load(handle)
    except FileNotFoundError:
        raise ImageToolError("No reusable API key was found. Configure API-key authentication for the selected Codex provider; ChatGPT OAuth, OS keyring, and ephemeral credentials are not supported.") from None
    except (OSError, ValueError):
        raise ImageToolError("Cannot read or parse Codex auth.json; inspect the file locally without sharing its contents.") from None
    if not isinstance(auth, dict):
        raise ImageToolError("Codex auth.json must contain an authentication object.")
    mode = auth.get("auth_mode")
    if isinstance(mode, str) and any(term in mode.lower() for term in ("chatgpt", "oauth", "ephemeral")):
        raise ImageToolError("Codex is using ChatGPT OAuth or ephemeral authentication, not a reusable API key.")
    if auth.get("OPENAI_API_KEY") is not None:
        return _text(auth["OPENAI_API_KEY"], "Codex auth.json OPENAI_API_KEY"), "auth.json:OPENAI_API_KEY"
    if auth.get("tokens"):
        raise ImageToolError("Codex contains ChatGPT OAuth tokens, which this server cannot use as an API key.")
    raise ImageToolError("Codex auth.json does not contain a reusable OPENAI_API_KEY.")


def _credentials(name: str, provider: dict[str, Any], headers: dict[str, str],
                 sources: dict[str, str], config: dict[str, Any], home: Path,
                 env: Mapping[str, str]) -> tuple[str, str]:
    auth = _table(provider.get("auth", {}), "Provider auth")
    if "command" in auth:
        raise ImageToolError("Provider auth.command is not supported by this server; configure API-key authentication for the selected Codex provider.")
    candidates: list[tuple[str, str]] = []
    if "env_key" in provider:
        variable = _variable(provider["env_key"], "Provider env_key")
        key = _env_text(env, variable)
        if key is None:
            raise ImageToolError("The environment variable declared by provider.env_key is missing or empty; it must also be passed to the MCP process.")
        candidates.append((key, f"provider.env_key:{variable}"))
    if "experimental_bearer_token" in provider:
        candidates.append((_text(provider["experimental_bearer_token"], "Provider experimental_bearer_token"),
                           "provider.experimental_bearer_token"))

    bearer_header: str | None = None
    explicit_authorization: str | None = None
    other_auth: list[str] = []
    for header, value in headers.items():
        normalized = header.lower()
        if normalized not in _AUTH_HEADERS:
            continue
        value = _text(value, "Provider authentication header")
        source = f"{sources[normalized]}.{header}"
        if normalized != "authorization":
            other_auth.append(source)
            continue
        parts = value.split(None, 1)
        if parts[0].lower() == "bearer":
            if len(parts) != 2 or not parts[1].strip():
                raise ImageToolError("Provider Authorization Bearer header is missing its token.")
            candidates.append((parts[1].strip(), source))
            bearer_header = header
        else:
            explicit_authorization = source

    if len({key for key, _ in candidates}) > 1 or (candidates and explicit_authorization):
        raise ImageToolError("Conflicting Authorization sources are configured for the selected provider.")
    if candidates:
        if bearer_header is not None:
            del headers[bearer_header]
        key, source = candidates[0]
    elif explicit_authorization:
        key, source = "", explicit_authorization
    elif name == "openai" or provider.get("requires_openai_auth", False):
        key = _env_text(env, "OPENAI_API_KEY")
        if key is not None:
            source = "env:OPENAI_API_KEY"
        else:
            key, source = _auth_file_key(home, config)
    elif other_auth:
        return "", " + ".join(other_auth)
    else:
        raise ImageToolError("The selected custom provider has no supported authentication source. Configure provider.env_key, experimental_bearer_token, or an authentication header; unrelated OpenAI credentials are not reused.")
    return key, " + ".join([source, *other_auth])


def resolve_config(current_model: str | None = None, *,
                   environ: Mapping[str, str] | None = None,
                   codex_home: Path | None = None, profile: str | None = None,
                   require_model: bool = True) -> ResolvedConfig:
    """Resolve only the caller's known model and the selected user's Codex settings."""
    env = os.environ if environ is None else environ
    home = _home(env, codex_home)
    config, config_path, _ = _load_config(home, env, profile)
    if current_model is not None:
        model, model_source = _text(current_model, "Current model"), "caller"
    elif "model" in config:
        model, model_source = _text(config["model"], "Codex model"), "config"
    elif require_model:
        raise ImageToolError("No current model was supplied and the selected Codex configuration has no model. Pass the known current_model or configure model; no model will be guessed.")
    else:
        model, model_source = None, "unavailable"

    name, provider = _provider(config)
    if provider.get("wire_api", "responses") != "responses":
        raise ImageToolError("The selected provider must use wire_api = 'responses' to call the image generation tool.")
    if name == "openai":
        if "openai_base_url" not in config:
            raise ImageToolError("The built-in openai provider requires an explicit top-level openai_base_url for this server; no endpoint will be guessed.")
        base_url, url_query = _base_url(config["openai_base_url"])
    elif "base_url" in provider:
        base_url, url_query = _base_url(provider["base_url"])
    else:
        raise ImageToolError("The selected custom provider must define its own base_url; no endpoint will be guessed.")
    headers, sources = _headers(provider, env)
    query = {**url_query, **_string_map(provider.get("query_params", {}), "Provider query_params")}
    api_key, credential_source = _credentials(name, provider, headers, sources, config, home, env)
    return ResolvedConfig(
        base_url=base_url, api_key=api_key, headers=headers, query_params=query,
        model=model, provider=name, model_source=model_source, credential_source=credential_source,
        config_path=config_path, request_timeout=_timeout(env),
        required_env_vars=_required_env(env, name, provider, codex_home=codex_home, profile=profile),
        responses_model=_env_text(env, "CODEX_IMAGE_RESPONSES_MODEL"),
        image_model=_env_text(env, "CODEX_IMAGE_MODEL"), model_policy=_model_policy(env),
    )
