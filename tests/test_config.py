"""Configuration tests use temporary files and fake credentials only."""

import json
import os
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from codex_image_generation.config import MAX_CONFIG_BYTES, _merge, get_launch_settings, resolve_config
from codex_image_generation.errors import ImageToolError


class ConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()

    def config(self, text, name="config.toml"):
        (self.home / name).write_text(text, encoding="utf-8")

    def auth(self, data):
        (self.home / "auth.json").write_text(json.dumps(data), encoding="utf-8")

    def resolve(self, env=None, **kwargs):
        return resolve_config(environ=env or {}, codex_home=self.home, **kwargs)

    def launch(self, env=None, **kwargs):
        return get_launch_settings(environ=env or {}, codex_home=self.home, **kwargs)

    def test_deep_profile_merge_is_iterative_and_preserves_sources(self):
        base, overlay = {}, {}
        left, right = base, overlay
        for _ in range(1200):
            left["nested"], right["nested"] = {}, {}
            left, right = left["nested"], right["nested"]
        left["value"], right["value"] = "old", "new"
        merged = _merge(base, overlay)
        for _ in range(1200):
            merged = merged["nested"]
        self.assertEqual(merged["value"], "new")
        self.assertEqual(left["value"], "old")

    def test_deep_configuration_or_auth_is_a_safe_diagnostic(self):
        self.config("value=" + "[" * 2000 + "1" + "]" * 2000)
        with self.assertRaises(ImageToolError):
            self.resolve()
        self.openai()
        (self.home / "auth.json").write_text('{"secret":"private-value","nested":' + "[" * 2000 + "0" + "]" * 2000 + "}", encoding="utf-8")
        with self.assertRaises(ImageToolError) as raised:
            self.resolve()
        self.assertNotIn("private-value", str(raised.exception))

    def test_oversized_configuration_and_auth_are_rejected_before_parsing(self):
        self.config("#" + "x" * MAX_CONFIG_BYTES)
        with self.assertRaisesRegex(ImageToolError, "1 MiB"):
            self.resolve()
        self.openai()
        (self.home / "auth.json").write_bytes(b" " * (MAX_CONFIG_BYTES + 1))
        with self.assertRaisesRegex(ImageToolError, "1 MiB"):
            self.resolve()

    def test_configuration_directory_is_not_opened_as_a_file(self):
        (self.home / "config.toml").mkdir()
        with patch("codex_image_generation.local_files.os.open") as open_file:
            with self.assertRaises(ImageToolError):
                self.resolve()
        open_file.assert_not_called()

    def provider(self, extra="", token=True, model="configured-model",
                 url="https://relay.example/v1"):
        root = "" if model is None else f"model = {json.dumps(model)}\n"
        self.config(root + 'model_provider = "relay"\n[model_providers.relay]\n'
                    + ("" if url is None else f"base_url = {json.dumps(url)}\n")
                    + 'wire_api = "responses"\n'
                    + ('experimental_bearer_token = "fake-inline-key"\n' if token else "") + extra)

    def openai(self, extra="", model="configured-model",
               url="https://openai-route.example/v1"):
        root = "" if model is None else f"model = {json.dumps(model)}\n"
        self.config(root + ("" if url is None else f"openai_base_url = {json.dumps(url)}\n") + extra)

    def test_config_model_and_inline_bearer(self):
        self.provider()
        result = self.resolve()
        self.assertEqual(result.model, "configured-model")
        self.assertEqual(result.model_source, "config")
        self.assertEqual(result.api_key, "fake-inline-key")
        self.assertEqual(result.base_url, "https://relay.example/v1")
        self.assertEqual(result.request_timeout, 300)
        self.assertIsNone(result.image_model)
        self.assertIsNone(result.responses_model)
        self.assertEqual(result.model_policy, "quality")

    def test_trusted_caller_model_precedes_config(self):
        self.provider()
        result = self.resolve(current_model="known-session-model")
        self.assertEqual((result.model, result.model_source), ("known-session-model", "caller"))
        self.assertFalse(result.public_status()["session_model_auto_detection"])
        self.assertFalse(result.public_status()["session_model_verified"])

    def test_missing_model_has_no_fallback(self):
        self.provider(model=None)
        with self.assertRaisesRegex(ImageToolError, "no model"):
            self.resolve()
        result = self.resolve(require_model=False)
        self.assertIsNone(result.model)
        self.assertEqual(result.model_source, "unavailable")
        self.assertIsNone(result.public_status()["model"])
        self.assertEqual(self.resolve(current_model="known-model").model, "known-model")

    def test_empty_caller_model_is_rejected(self):
        self.provider()
        with self.assertRaisesRegex(ImageToolError, "Current model"):
            self.resolve(current_model="")

    def test_removed_environment_overrides_do_not_change_connection_or_model(self):
        self.provider()
        removed = {
            "CODEX_IMAGE_BASE_URL": "https://unused.example/v1",
            "CODEX_IMAGE_API_KEY": "fake-unused-key",
            "CODEX_IMAGE_PROVIDER": "unused-provider",
            "OPENAI_BASE_URL": "https://also-unused.example/v1",
        }
        result = self.resolve(removed)
        self.assertEqual((result.model, result.provider), ("configured-model", "relay"))
        self.assertEqual(result.base_url, "https://relay.example/v1")
        self.assertEqual(result.api_key, "fake-inline-key")
        self.assertFalse(set(result.required_env_vars) & removed.keys())

    def test_model_preferences_are_separate_from_codex_session_model(self):
        self.provider()
        env = {"CODEX_IMAGE_RESPONSES_MODEL": "chosen-dialogue", "CODEX_IMAGE_MODEL": "gpt-image-2.5-sunburst",
               "CODEX_IMAGE_MODEL_POLICY": "fast"}
        result = self.resolve(env)
        self.assertEqual(result.model, "configured-model")
        self.assertEqual(result.responses_model, "chosen-dialogue")
        self.assertEqual(result.image_model, "gpt-image-2.5-sunburst")
        self.assertEqual(result.model_policy, "fast")
        self.assertTrue(set(env) <= set(result.required_env_vars))
        self.assertTrue(set(env) <= set(self.launch(env)["required_env_vars"]))
        with self.assertRaises(ImageToolError):
            self.resolve({"CODEX_IMAGE_MODEL_POLICY": "unsupported"})

    def test_builtin_reads_explicit_top_level_url(self):
        self.openai()
        result = self.resolve({"OPENAI_API_KEY": "fake-openai-key"})
        self.assertEqual(result.base_url, "https://openai-route.example/v1")
        self.assertEqual(result.provider, "openai")
        self.assertEqual(result.credential_source, "env:OPENAI_API_KEY")

    def test_builtin_never_guesses_an_endpoint(self):
        self.openai(url=None)
        with self.assertRaisesRegex(ImageToolError, "explicit top-level openai_base_url"):
            self.resolve({"OPENAI_API_KEY": "fake-key", "OPENAI_BASE_URL": "https://unused.example/v1"})

    def test_reserved_builtin_provider_cannot_be_overridden(self):
        self.openai('[model_providers.openai]\nbase_url = "https://wrong.example/v1"\n')
        for operation in (self.resolve, self.launch):
            with self.subTest(operation=operation.__name__), self.assertRaisesRegex(ImageToolError, "reserved"):
                operation({"OPENAI_API_KEY": "fake-key"})

    def test_custom_provider_requires_its_own_url(self):
        self.provider(url=None)
        with self.assertRaisesRegex(ImageToolError, "own base_url"):
            self.resolve()

    def test_provider_env_key(self):
        self.provider('env_key = "RELAY_KEY"\n', token=False)
        result = self.resolve({"RELAY_KEY": "fake-env-key", "OPENAI_API_KEY": "unrelated-key"})
        self.assertEqual(result.api_key, "fake-env-key")
        self.assertEqual(result.required_env_vars, ("RELAY_KEY",))

    def test_declared_env_key_is_required_even_with_other_credentials(self):
        self.provider('env_key = "RELAY_KEY"\n')
        with self.assertRaisesRegex(ImageToolError, "env_key is missing"):
            self.resolve({"OPENAI_API_KEY": "unrelated-key"})

    def test_conflicting_bearer_sources_are_rejected(self):
        self.provider('env_key = "RELAY_KEY"\n')
        with self.assertRaisesRegex(ImageToolError, "Conflicting Authorization"):
            self.resolve({"RELAY_KEY": "different-fake-key"})

    def test_identical_bearer_sources_are_coalesced(self):
        self.provider('env_key = "RELAY_KEY"\n[model_providers.relay.http_headers]\nAuthorization = "Bearer fake-inline-key"\n')
        result = self.resolve({"RELAY_KEY": "fake-inline-key"})
        self.assertEqual(result.api_key, "fake-inline-key")
        self.assertNotIn("Authorization", result.headers)

    def test_static_bearer_header_becomes_sdk_key(self):
        self.provider('[model_providers.relay.http_headers]\naUtHoRiZaTiOn = "bEaReR fake-header-key"\n"X-Trace" = "trace-value"\n', token=False)
        result = self.resolve()
        self.assertEqual(result.api_key, "fake-header-key")
        self.assertEqual(result.headers, {"X-Trace": "trace-value"})

    def test_bearer_and_distinct_authentication_headers_coexist(self):
        self.provider('[model_providers.relay.http_headers]\n"X-API-Key" = "fake-gateway-key"\n"X-Auth-Token" = "fake-route-token"\n')
        result = self.resolve()
        self.assertEqual(result.api_key, "fake-inline-key")
        self.assertEqual(result.headers, {"X-API-Key": "fake-gateway-key", "X-Auth-Token": "fake-route-token"})
        self.assertIn("X-API-Key", result.credential_source)
        self.assertNotIn("fake-gateway-key", result.credential_source)

    def test_multiple_non_bearer_authentication_headers_coexist(self):
        self.provider('[model_providers.relay.http_headers]\nAuthorization = "Basic fake-credential"\n"api-key" = "fake-gateway-key"\n', token=False)
        result = self.resolve()
        self.assertEqual(result.api_key, "")
        self.assertEqual(result.headers, {"Authorization": "Basic fake-credential", "api-key": "fake-gateway-key"})
        self.provider('[model_providers.relay.http_headers]\n"api-key" = "fake-one"\n"x-api-key" = "fake-two"\n', token=False)
        self.assertEqual(self.resolve().api_key, "")

    def test_non_bearer_authorization_conflicts_with_a_bearer_source(self):
        self.provider('[model_providers.relay.http_headers]\nAuthorization = "Basic fake-credential"\n')
        with self.assertRaisesRegex(ImageToolError, "Conflicting Authorization"):
            self.resolve()

    def test_supported_header_only_authentication(self):
        for name in ("api-key", "x-api-key", "Ocp-Apim-Subscription-Key"):
            with self.subTest(name=name):
                self.provider(f'[model_providers.relay.http_headers]\n"{name}" = "fake-key"\n', token=False)
                result = self.resolve()
                self.assertEqual((result.api_key, result.headers[name]), ("", "fake-key"))

    def test_env_headers_include_missing_optional_declarations(self):
        self.provider('[model_providers.relay.env_http_headers]\nAuthorization = "AUTH_HEADER"\n"X-Optional" = "OPTIONAL_HEADER"\n', token=False)
        result = self.resolve({"AUTH_HEADER": "Bearer fake-env-header-key"})
        self.assertEqual(result.api_key, "fake-env-header-key")
        self.assertEqual(result.headers, {})
        self.assertEqual(result.required_env_vars, ("AUTH_HEADER", "OPTIONAL_HEADER"))

    def test_conflicting_authorization_header_case_is_rejected(self):
        self.provider('[model_providers.relay.http_headers]\nAuthorization = "Bearer first-fake-key"\n[model_providers.relay.env_http_headers]\nauthorization = "AUTH_HEADER"\n', token=False)
        with self.assertRaisesRegex(ImageToolError, "Conflicting Authorization"):
            self.resolve({"AUTH_HEADER": "Bearer second-fake-key"})

    def test_non_authorization_env_header_overrides_static_value(self):
        self.provider('[model_providers.relay.http_headers]\n"api-key" = "fake-old"\n[model_providers.relay.env_http_headers]\n"API-Key" = "ROTATED_KEY"\n', token=False)
        result = self.resolve({"ROTATED_KEY": "fake-new"})
        self.assertEqual(result.api_key, "")
        self.assertEqual(result.headers, {"API-Key": "fake-new"})

    def test_trace_header_alone_is_not_authentication(self):
        self.provider('[model_providers.relay.http_headers]\n"X-Trace" = "not-a-key"\n', token=False)
        with self.assertRaisesRegex(ImageToolError, "no supported authentication"):
            self.resolve()

    def test_custom_provider_does_not_borrow_other_credentials(self):
        self.provider(token=False)
        self.auth({"OPENAI_API_KEY": "fake-file-key"})
        with self.assertRaisesRegex(ImageToolError, "unrelated OpenAI credentials"):
            self.resolve({"OPENAI_API_KEY": "fake-global-key"})

    def test_auth_json_for_builtin_openai(self):
        self.openai()
        self.auth({"OPENAI_API_KEY": "fake-file-key"})
        result = self.resolve()
        self.assertEqual(result.api_key, "fake-file-key")
        self.assertEqual(result.credential_source, "auth.json:OPENAI_API_KEY")

    def test_openai_auth_and_gateway_header_can_coexist(self):
        self.provider('requires_openai_auth = true\n[model_providers.relay.http_headers]\n"X-API-Key" = "fake-gateway-key"\n', token=False)
        self.auth({"OPENAI_API_KEY": "fake-file-key"})
        result = self.resolve()
        self.assertEqual(result.api_key, "fake-file-key")
        self.assertEqual(result.headers["X-API-Key"], "fake-gateway-key")

    def test_openai_env_precedes_auth_file(self):
        self.openai()
        self.auth({"OPENAI_API_KEY": "fake-file-key"})
        self.assertEqual(self.resolve({"OPENAI_API_KEY": "fake-env-key"}).api_key, "fake-env-key")

    def test_oauth_is_not_a_reusable_api_key(self):
        self.openai()
        self.auth({"auth_mode": "chatgpt", "OPENAI_API_KEY": "stale-fake-key", "tokens": {"access_token": "fake-oauth-token"}})
        with self.assertRaisesRegex(ImageToolError, "OAuth") as raised:
            self.resolve()
        self.assertNotIn("fake-oauth-token", str(raised.exception))
        self.assertNotIn("stale-fake-key", str(raised.exception))

    def test_unsupported_stores_cannot_use_stale_auth_file(self):
        self.auth({"OPENAI_API_KEY": "stale-fake-key"})
        for store in ("keyring", "ephemeral", "auto"):
            with self.subTest(store=store):
                self.openai(f'cli_auth_credentials_store = "{store}"\n')
                with self.assertRaises(ImageToolError) as raised:
                    self.resolve()
                self.assertNotIn("stale-fake-key", str(raised.exception))

    def test_explicit_provider_key_works_with_auto_storage(self):
        self.openai('cli_auth_credentials_store = "auto"\n')
        self.assertEqual(self.resolve({"OPENAI_API_KEY": "fake-env-key"}).api_key, "fake-env-key")

    def test_command_auth_is_explicitly_unsupported(self):
        self.provider('[model_providers.relay.auth]\ncommand = "do-not-execute"\n', token=False)
        with self.assertRaisesRegex(ImageToolError, "auth.command is not supported"):
            self.resolve()

    def test_profile_file_merges_base_provider(self):
        self.provider()
        self.config('model = "profile-model"\n[model_providers.relay]\nbase_url = "https://profile.example/v1"\n', "work.config.toml")
        result = self.resolve({"CODEX_IMAGE_PROFILE": "work"})
        self.assertEqual((result.model, result.model_source), ("profile-model", "config"))
        self.assertEqual(result.api_key, "fake-inline-key")
        self.assertEqual(result.base_url, "https://profile.example/v1")
        self.assertEqual(result.config_path, self.home / "work.config.toml")
        self.assertEqual(self.launch({"CODEX_IMAGE_PROFILE": "work"})["profile"], "work")

    def test_legacy_profile_and_file_precedence(self):
        self.provider('[profiles.work]\nmodel = "legacy-model"\n')
        self.assertEqual(self.resolve(profile="work").model, "legacy-model")
        self.config('model = "file-model"\n', "work.config.toml")
        self.assertEqual(self.resolve(profile="work").model, "file-model")

    def test_explicit_profile_precedes_environment(self):
        self.provider()
        self.config('model = "profile-model"\n', "work.config.toml")
        self.assertEqual(self.resolve({"CODEX_IMAGE_PROFILE": "missing"}, profile="work").model, "profile-model")

    def test_profile_path_traversal_is_rejected(self):
        for name in ("../outside", "..\\outside", "/absolute", ".", "..", "C:\\absolute"):
            with self.subTest(name=name), self.assertRaisesRegex(ImageToolError, "Profile names"):
                self.resolve(profile=name)

    def test_missing_profile_is_an_error(self):
        with self.assertRaisesRegex(ImageToolError, "profile was not found"):
            self.launch(profile="missing")

    def test_builtin_profile_top_level_url(self):
        self.openai()
        self.config('openai_base_url = "https://profile-route.example/v1"\n', "work.config.toml")
        result = self.resolve({"OPENAI_API_KEY": "fake-key"}, profile="work")
        self.assertEqual(result.base_url, "https://profile-route.example/v1")

    def test_launch_does_not_require_model_or_available_credentials(self):
        self.provider('env_key = "MISSING_KEY"\n[model_providers.relay.env_http_headers]\n"X-API-Key" = "MISSING_HEADER"\n', token=False, model=None)
        settings = self.launch()
        self.assertEqual(settings["required_env_vars"], ["MISSING_HEADER", "MISSING_KEY"])
        self.assertEqual(settings["codex_home"], str(self.home))
        self.assertIsNone(settings["profile"])
        self.assertEqual(settings["request_timeout"], 300)

    def test_launch_never_reads_auth_json(self):
        self.openai(model=None)
        (self.home / "auth.json").write_text('{"OPENAI_API_KEY": "fake-broken-secret', encoding="utf-8")
        settings = self.launch()
        self.assertEqual(settings["required_env_vars"], ["OPENAI_API_KEY"])

    def test_launch_normalizes_relative_codex_home(self):
        previous = Path.cwd()
        try:
            os.chdir(self.home.parent)
            settings = get_launch_settings(environ={}, codex_home=Path(self.home.name))
        finally:
            os.chdir(previous)
        self.assertEqual(settings["codex_home"], str(self.home))

    def test_launch_inherits_builtin_org_and_project_when_present(self):
        self.openai()
        settings = self.launch({"OPENAI_ORG_ID": "fake-org", "OPENAI_PROJECT_ID": "fake-project"})
        self.assertEqual(settings["required_env_vars"], ["OPENAI_API_KEY", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID"])
        self.assertNotIn("fake-org", str(settings))
        self.assertNotIn("fake-project", str(settings))

    def test_custom_launch_does_not_inherit_unrelated_openai_settings(self):
        self.provider()
        settings = self.launch({"OPENAI_API_KEY": "fake-global", "OPENAI_ORG_ID": "fake-org", "OPENAI_PROJECT_ID": "fake-project"})
        self.assertEqual(settings["required_env_vars"], [])

    def test_required_environment_metadata_has_no_values(self):
        self.provider('env_key = "RELAY_KEY"\n[model_providers.relay.env_http_headers]\n"X-Trace" = "TRACE_ID"\n', token=False)
        env = {"CODEX_HOME": str(self.home), "RELAY_KEY": "fake-provider-secret",
               "TRACE_ID": "fake-trace-value", "CODEX_IMAGE_TIMEOUT": "123"}
        result = resolve_config(environ=env)
        self.assertEqual(result.public_status()["required_env_vars"], ["CODEX_HOME", "CODEX_IMAGE_TIMEOUT", "RELAY_KEY", "TRACE_ID"])
        for secret in ("fake-provider-secret", "fake-trace-value"):
            self.assertNotIn(secret, str(result.public_status()))
            self.assertNotIn(secret, str(get_launch_settings(environ=env)))

    def test_query_and_header_values_are_private(self):
        self.provider('[model_providers.relay.query_params]\n"api-version" = "preview"\nsecret = "fake-query-secret"\n[model_providers.relay.http_headers]\n"X-Private" = "fake-header-secret"\n')
        result = self.resolve()
        self.assertEqual(result.query_params["api-version"], "preview")
        rendered = json.dumps(result.public_status()) + repr(result)
        for secret in ("fake-query-secret", "fake-inline-key", "fake-header-secret"):
            self.assertNotIn(secret, rendered)

    def test_url_query_is_separated_and_explicit_values_win(self):
        self.provider('[model_providers.relay.query_params]\ntenant = "new"\n', url="https://relay.example/v1?tenant=old&secret=fake%26secret&empty=")
        result = self.resolve()
        self.assertEqual(result.base_url, "https://relay.example/v1")
        self.assertEqual(result.query_params, {"tenant": "new", "secret": "fake&secret", "empty": ""})
        self.assertNotIn("fake&secret", str(result.public_status()) + repr(result))

    def test_duplicate_url_queries_are_rejected_not_dropped(self):
        for query in ("scope=read&scope=image", "key=fake-secret&key=fake-secret", "k=1&%6b=2"):
            with self.subTest(query=query):
                self.provider(url="https://relay.example/v1?" + query)
                with self.assertRaisesRegex(ImageToolError, "repeated query parameter") as raised:
                    self.resolve()
                self.assertNotIn("fake-secret", str(raised.exception))

    def test_builtin_query_is_separated(self):
        self.openai(url="https://openai-route.example/v1?tenant=one")
        result = self.resolve({"OPENAI_API_KEY": "fake-key"})
        self.assertEqual(result.base_url, "https://openai-route.example/v1")
        self.assertEqual(result.query_params, {"tenant": "one"})

    def test_invalid_urls_do_not_echo_input(self):
        for url in ("file:///fake-secret", "https://user:fake-secret@example.com/v1",
                    "https://example.com/v1#fake-secret", "https://example.com:99999/v1",
                    "https://", "https://example.com/fake secret", "https://example.com\\path"):
            with self.subTest(url=url):
                self.provider(url=url)
                with self.assertRaises(ImageToolError) as raised:
                    self.resolve()
                self.assertNotIn("fake-secret", str(raised.exception))

    def test_chat_wire_api_is_rejected(self):
        self.config('model = "known-model"\nmodel_provider = "relay"\n[model_providers.relay]\nbase_url = "https://relay.example/v1"\nwire_api = "chat"\nexperimental_bearer_token = "fake-key"\n')
        with self.assertRaisesRegex(ImageToolError, "wire_api"):
            self.resolve()

    def test_timeout_validation(self):
        self.provider()
        self.assertEqual(self.resolve({"CODEX_IMAGE_TIMEOUT": "12.5"}).request_timeout, 12.5)
        self.assertEqual(self.launch({"CODEX_IMAGE_TIMEOUT": "12.5"})["request_timeout"], 12.5)
        for value in ("0", "-1", "3601", "nan", "inf", "fake-secret-value"):
            with self.subTest(value=value), self.assertRaisesRegex(ImageToolError, "TIMEOUT") as raised:
                self.launch({"CODEX_IMAGE_TIMEOUT": value})
            self.assertNotIn("fake-secret-value", str(raised.exception))

    def test_configuration_is_reloaded(self):
        self.provider()
        self.assertEqual(self.resolve().api_key, "fake-inline-key")
        self.provider('env_key = "NEW_KEY"\n', token=False)
        self.assertEqual(self.resolve({"NEW_KEY": "fake-new-key"}).api_key, "fake-new-key")

    def test_toml_parse_errors_are_redacted(self):
        self.config('experimental_bearer_token = "fake-secret-without-end-quote\n')
        with self.assertRaises(ImageToolError) as raised:
            self.resolve()
        self.assertNotIn("fake-secret-without-end-quote", str(raised.exception))

    def test_auth_json_parse_errors_are_redacted(self):
        self.openai()
        (self.home / "auth.json").write_text('{"OPENAI_API_KEY": "fake-broken-secret', encoding="utf-8")
        with self.assertRaises(ImageToolError) as raised:
            self.resolve()
        self.assertNotIn("fake-broken-secret", str(raised.exception))

    def test_header_control_characters_are_redacted(self):
        self.provider('[model_providers.relay.http_headers]\nAuthorization = "Bearer fake-secret\\nheader"\n', token=False)
        with self.assertRaisesRegex(ImageToolError, "control characters") as raised:
            self.resolve()
        self.assertNotIn("fake-secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
