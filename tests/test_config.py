"""Use temporary config files and fake keys; never inspect the user's credentials."""

import json
import tempfile
import unittest
from pathlib import Path

from gpt_image_generation.config import resolve_config
from gpt_image_generation.errors import ImageToolError


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

    def provider(self, extra="", token=True):
        self.config('model = "configured-model"\nmodel_provider = "relay"\n'
                    '[model_providers.relay]\nbase_url = "https://relay.example/v1"\n'
                    'wire_api = "responses"\nrequires_openai_auth = false\n'
                    + ('experimental_bearer_token = "fake-inline-key"\n' if token else "") + extra)

    def test_inline_bearer_and_defaults(self):
        self.provider()
        result = self.resolve()
        self.assertEqual(result.api_key, "fake-inline-key")
        self.assertEqual(result.base_url, "https://relay.example/v1")
        self.assertEqual(result.responses_model, "configured-model")
        self.assertEqual(result.image_model, "gpt-image-2.5-flare")
        self.assertEqual(result.request_timeout, 300)

    def test_builtin_openai_defaults_and_env_key(self):
        result = self.resolve({"OPENAI_API_KEY": "fake-openai-key"})
        self.assertEqual(result.responses_model, "gpt-6-astra")
        self.assertEqual(result.base_url, "https://api.openai.com/v1")
        self.assertEqual(result.credential_source, "env:OPENAI_API_KEY")

    def test_provider_env_key(self):
        self.provider('env_key = "RELAY_KEY"\n', token=False)
        result = self.resolve({"RELAY_KEY": "fake-env-key", "OPENAI_API_KEY": "unrelated-key"})
        self.assertEqual(result.api_key, "fake-env-key")
        self.assertEqual(result.required_env_vars, ("RELAY_KEY",))

    def test_declared_env_key_is_required(self):
        self.provider('env_key = "RELAY_KEY"\n')
        with self.assertRaisesRegex(ImageToolError, "env_key is missing"):
            self.resolve({"OPENAI_API_KEY": "unrelated-key"})

    def test_conflicting_explicit_credentials(self):
        self.provider('env_key = "RELAY_KEY"\n')
        with self.assertRaisesRegex(ImageToolError, "Conflicting authentication"):
            self.resolve({"RELAY_KEY": "different-fake-key"})

    def test_identical_auth_sources_are_coalesced(self):
        self.provider('env_key = "RELAY_KEY"\n[model_providers.relay.http_headers]\nAuthorization = "Bearer fake-inline-key"\n')
        result = self.resolve({"RELAY_KEY": "fake-inline-key"})
        self.assertEqual(result.api_key, "fake-inline-key")
        self.assertNotIn("Authorization", result.headers)

    def test_static_bearer_header_becomes_sdk_api_key(self):
        self.provider('[model_providers.relay.http_headers]\naUtHoRiZaTiOn = "bEaReR fake-header-key"\n"X-Trace" = "trace-value"\n', token=False)
        result = self.resolve()
        self.assertEqual(result.api_key, "fake-header-key")
        self.assertEqual(result.headers, {"X-Trace": "trace-value"})

    def test_non_bearer_auth_headers_are_preserved(self):
        for name, value in (("Authorization", "Basic fake-credential"), ("api-key", "fake-header-key"), ("x-api-key", "fake-header-key")):
            with self.subTest(name=name):
                self.provider(f'[model_providers.relay.http_headers]\n"{name}" = "{value}"\n', token=False)
                result = self.resolve()
                self.assertEqual(result.api_key, "")
                self.assertEqual(result.headers[name], value)

    def test_env_headers_and_missing_optional_variable(self):
        self.provider('[model_providers.relay.env_http_headers]\nAuthorization = "AUTH_HEADER"\n"X-Optional" = "OPTIONAL_HEADER"\n', token=False)
        result = self.resolve({"AUTH_HEADER": "Bearer fake-env-header-key"})
        self.assertEqual(result.api_key, "fake-env-header-key")
        self.assertEqual(result.headers, {})
        self.assertEqual(result.required_env_vars, ("AUTH_HEADER",))

    def test_conflicting_header_case(self):
        self.provider('[model_providers.relay.http_headers]\nAuthorization = "Bearer first-fake-key"\n[model_providers.relay.env_http_headers]\nauthorization = "AUTH_HEADER"\n', token=False)
        with self.assertRaisesRegex(ImageToolError, "Conflicting authentication"):
            self.resolve({"AUTH_HEADER": "Bearer second-fake-key"})

    def test_trace_header_alone_is_not_authentication(self):
        self.provider('[model_providers.relay.http_headers]\n"X-Trace" = "not-a-key"\n', token=False)
        with self.assertRaisesRegex(ImageToolError, "no supported authentication"):
            self.resolve()

    def test_custom_provider_does_not_borrow_openai_credentials(self):
        self.provider(token=False)
        self.auth({"OPENAI_API_KEY": "fake-file-key"})
        with self.assertRaisesRegex(ImageToolError, "unrelated OpenAI credentials"):
            self.resolve({"OPENAI_API_KEY": "fake-global-key"})

    def test_auth_json_for_builtin_openai(self):
        self.auth({"OPENAI_API_KEY": "fake-file-key"})
        result = self.resolve()
        self.assertEqual(result.api_key, "fake-file-key")
        self.assertEqual(result.credential_source, "auth.json:OPENAI_API_KEY")

    def test_requires_openai_auth_allows_file_credentials(self):
        self.config('model_provider = "relay"\n[model_providers.relay]\nbase_url = "https://relay.example/v1"\nrequires_openai_auth = true\n')
        self.auth({"OPENAI_API_KEY": "fake-file-key"})
        self.assertEqual(self.resolve().api_key, "fake-file-key")

    def test_openai_env_precedes_auth_json(self):
        self.auth({"OPENAI_API_KEY": "fake-file-key"})
        self.assertEqual(self.resolve({"OPENAI_API_KEY": "fake-env-key"}).api_key, "fake-env-key")

    def test_oauth_is_not_a_reusable_api_key(self):
        self.auth({"auth_mode": "chatgpt", "OPENAI_API_KEY": "stale-fake-key", "tokens": {"access_token": "fake-oauth-token"}})
        with self.assertRaisesRegex(ImageToolError, "OAuth") as raised:
            self.resolve()
        self.assertNotIn("fake-oauth-token", str(raised.exception))
        self.assertNotIn("stale-fake-key", str(raised.exception))

    def test_keyring_and_ephemeral_ignore_stale_auth_file(self):
        self.auth({"OPENAI_API_KEY": "stale-fake-key"})
        for store in ("keyring", "ephemeral"):
            with self.subTest(store=store):
                self.config(f'cli_auth_credentials_store = "{store}"\n')
                with self.assertRaisesRegex(ImageToolError, "keyring or ephemeral"):
                    self.resolve()

    def test_command_auth_is_explicitly_unsupported(self):
        self.provider('[model_providers.relay.auth]\ncommand = "do-not-execute"\n', token=False)
        with self.assertRaisesRegex(ImageToolError, "auth.command is not supported"):
            self.resolve()

    def test_profile_file_merges_base_provider(self):
        self.provider()
        self.config('model = "profile-model"\n[model_providers.relay]\nbase_url = "https://profile.example/v1"\n', "work.config.toml")
        result = self.resolve({"CODEX_IMAGE_PROFILE": "work"})
        self.assertEqual(result.responses_model, "profile-model")
        self.assertEqual(result.api_key, "fake-inline-key")
        self.assertEqual(result.base_url, "https://profile.example/v1")
        self.assertEqual(result.config_path, self.home / "work.config.toml")
        self.assertEqual(result.model_source, "profile:work")

    def test_legacy_profile_and_new_profile_precedence(self):
        self.provider('[profiles.work]\nmodel = "legacy-model"\n')
        self.assertEqual(self.resolve(profile="work").responses_model, "legacy-model")
        self.config('model = "file-model"\n', "work.config.toml")
        self.assertEqual(self.resolve(profile="work").responses_model, "file-model")

    def test_explicit_profile_precedes_env_profile(self):
        self.provider()
        self.config('model = "profile-model"\n', "work.config.toml")
        self.assertEqual(self.resolve({"CODEX_IMAGE_PROFILE": "missing"}, profile="work").responses_model, "profile-model")

    def test_profile_path_traversal_is_rejected(self):
        for name in ("../outside", "..\\outside", "/absolute", ".", "..", "C:\\absolute"):
            with self.subTest(name=name), self.assertRaisesRegex(ImageToolError, "Profile names"):
                self.resolve(profile=name)

    def test_missing_profile_is_an_error(self):
        with self.assertRaisesRegex(ImageToolError, "profile was not found"):
            self.resolve(profile="missing")

    def test_model_precedence(self):
        self.provider()
        env = {"CODEX_IMAGE_RESPONSES_MODEL": "env-model", "CODEX_IMAGE_MODEL": "env-image-model"}
        result = self.resolve(env, responses_model="explicit-model", image_model="explicit-image-model")
        self.assertEqual((result.responses_model, result.image_model, result.model_source), ("explicit-model", "explicit-image-model", "argument"))
        result = self.resolve(env)
        self.assertEqual((result.responses_model, result.image_model), ("env-model", "env-image-model"))
        self.assertEqual(result.required_env_vars, ("CODEX_IMAGE_MODEL", "CODEX_IMAGE_RESPONSES_MODEL"))

    def test_provider_override_uses_its_own_credentials(self):
        self.provider('[model_providers.second]\nbase_url = "https://second.example/v1"\nexperimental_bearer_token = "fake-second-key"\n')
        result = self.resolve({"CODEX_IMAGE_PROVIDER": "second"})
        self.assertEqual((result.provider, result.api_key), ("second", "fake-second-key"))

    def test_complete_override_drops_old_headers_query_and_env_vars(self):
        self.provider('env_key = "UNAVAILABLE_PROVIDER_KEY"\n[model_providers.relay.http_headers]\n"api-key" = "fake-header-key"\n[model_providers.relay.query_params]\nsecret = "fake-query-key"\n')
        result = self.resolve({"CODEX_IMAGE_BASE_URL": "https://override.example/v1", "CODEX_IMAGE_API_KEY": "fake-override-key"})
        self.assertEqual(result.api_key, "fake-override-key")
        self.assertEqual(result.base_url, "https://override.example/v1")
        self.assertEqual((result.headers, result.query_params), ({}, {}))
        self.assertEqual(result.required_env_vars, ("CODEX_IMAGE_API_KEY", "CODEX_IMAGE_BASE_URL"))

    def test_partial_override_is_rejected(self):
        self.provider()
        for env in ({"CODEX_IMAGE_BASE_URL": "https://override.example/v1"}, {"CODEX_IMAGE_API_KEY": "fake-key"}):
            with self.subTest(keys=list(env)), self.assertRaisesRegex(ImageToolError, "provided together"):
                self.resolve(env)

    def test_query_and_header_values_are_private(self):
        self.provider('[model_providers.relay.query_params]\n"api-version" = "preview"\nsecret = "fake-query-secret"\n[model_providers.relay.http_headers]\n"X-Private" = "fake-header-secret"\n')
        result = self.resolve()
        self.assertEqual(result.query_params["api-version"], "preview")
        rendered = json.dumps(result.public_status()) + repr(result)
        for secret in ("fake-query-secret", "fake-inline-key", "fake-header-secret"):
            self.assertNotIn(secret, rendered)

    def test_url_query_and_key_are_private(self):
        result = self.resolve({"CODEX_IMAGE_BASE_URL": "https://relay.example/v1?token=fake-url-secret", "CODEX_IMAGE_API_KEY": "fake-override-key"})
        rendered = json.dumps(result.public_status()) + repr(result)
        self.assertNotIn("fake-url-secret", rendered)
        self.assertNotIn("fake-override-key", rendered)
        self.assertEqual(result.public_status()["base_url"], "https://relay.example/v1")

    def test_invalid_urls_do_not_echo_input(self):
        for url in ("file:///fake-secret", "https://user:fake-secret@example.com/v1", "https://example.com/v1#fake-secret", "https://example.com:99999/v1", "https://", "https://example.com/fake secret"):
            with self.subTest(url=url), self.assertRaises(ImageToolError) as raised:
                self.resolve({"CODEX_IMAGE_BASE_URL": url, "CODEX_IMAGE_API_KEY": "fake-key"})
            self.assertNotIn("fake-secret", str(raised.exception))

    def test_custom_provider_requires_base_url(self):
        self.config('model_provider = "relay"\n[model_providers.relay]\nexperimental_bearer_token = "fake-key"\n')
        with self.assertRaisesRegex(ImageToolError, "own base_url"):
            self.resolve()

    def test_chat_wire_api_is_rejected(self):
        self.config('model_provider = "relay"\n[model_providers.relay]\nbase_url = "https://relay.example/v1"\nwire_api = "chat"\nexperimental_bearer_token = "fake-key"\n')
        with self.assertRaisesRegex(ImageToolError, "wire_api"):
            self.resolve()

    def test_timeout_validation(self):
        self.provider()
        self.assertEqual(self.resolve({"CODEX_IMAGE_TIMEOUT": "12.5"}).request_timeout, 12.5)
        for value in ("0", "-1", "3601", "nan", "inf", "fake-secret-value"):
            with self.subTest(value=value), self.assertRaisesRegex(ImageToolError, "TIMEOUT") as raised:
                self.resolve({"CODEX_IMAGE_TIMEOUT": value})
            self.assertNotIn("fake-secret-value", str(raised.exception))

    def test_config_is_reloaded(self):
        self.provider()
        self.assertEqual(self.resolve().api_key, "fake-inline-key")
        self.provider('env_key = "NEW_KEY"\n', token=False)
        self.assertEqual(self.resolve({"NEW_KEY": "fake-new-key"}).api_key, "fake-new-key")

    def test_codex_home_environment(self):
        self.provider()
        result = resolve_config(environ={"CODEX_HOME": str(self.home)})
        self.assertEqual(result.config_path, self.home / "config.toml")
        self.assertEqual(result.required_env_vars, ("CODEX_HOME",))

    def test_toml_parser_errors_are_redacted(self):
        self.config('experimental_bearer_token = "fake-secret-without-end-quote\n')
        with self.assertRaises(ImageToolError) as raised:
            self.resolve()
        self.assertNotIn("fake-secret-without-end-quote", str(raised.exception))

    def test_auth_json_parser_errors_are_redacted(self):
        (self.home / "auth.json").write_text('{"OPENAI_API_KEY": "fake-broken-secret', encoding="utf-8")
        with self.assertRaises(ImageToolError) as raised:
            self.resolve()
        self.assertNotIn("fake-broken-secret", str(raised.exception))

    def test_header_control_characters_are_redacted(self):
        self.provider('[model_providers.relay.http_headers]\nAuthorization = "Bearer fake-secret\\nheader"\n', token=False)
        with self.assertRaisesRegex(ImageToolError, "control characters") as raised:
            self.resolve()
        self.assertNotIn("fake-secret", str(raised.exception))

    def test_required_environment_list_is_complete_and_value_free(self):
        self.provider('env_key = "RELAY_KEY"\n[model_providers.relay.env_http_headers]\n"X-Trace" = "TRACE_ID"\n', token=False)
        result = resolve_config(environ={
            "CODEX_HOME": str(self.home), "RELAY_KEY": "fake-provider-secret",
            "TRACE_ID": "fake-trace-value", "CODEX_IMAGE_TIMEOUT": "123",
            "CODEX_IMAGE_RESPONSES_MODEL": "env-model",
        })
        self.assertEqual(result.public_status()["required_env_vars"], [
            "CODEX_HOME", "CODEX_IMAGE_RESPONSES_MODEL", "CODEX_IMAGE_TIMEOUT", "RELAY_KEY", "TRACE_ID",
        ])
        self.assertNotIn("fake-provider-secret", str(result.public_status()))
        self.assertNotIn("fake-trace-value", str(result.public_status()))

    def test_auto_storage_cannot_select_a_stale_auth_file(self):
        self.config('cli_auth_credentials_store = "auto"\n')
        self.auth({"OPENAI_API_KEY": "stale-fake-key"})
        with self.assertRaisesRegex(ImageToolError, "cannot determine the active credential") as raised:
            self.resolve()
        self.assertNotIn("stale-fake-key", str(raised.exception))

    def test_explicit_credentials_still_work_with_auto_storage(self):
        self.config('cli_auth_credentials_store = "auto"\n')
        self.assertEqual(self.resolve({"OPENAI_API_KEY": "fake-env-key"}).api_key, "fake-env-key")
        self.config('cli_auth_credentials_store = "auto"\nmodel_provider = "relay"\n'
                    '[model_providers.relay]\nbase_url = "https://relay.example/v1"\n'
                    'experimental_bearer_token = "fake-inline-key"\n')
        self.assertEqual(self.resolve().api_key, "fake-inline-key")

    def test_url_query_moves_to_query_params_with_explicit_values_winning(self):
        self.config('model_provider = "relay"\n[model_providers.relay]\n'
                    'base_url = "https://relay.example/v1?tenant=old&secret=fake%26url%3Dsecret&empty="\n'
                    'experimental_bearer_token = "fake-inline-key"\n'
                    '[model_providers.relay.query_params]\ntenant = "new"\n')
        result = self.resolve()
        self.assertEqual(result.base_url, "https://relay.example/v1")
        self.assertEqual(result.query_params, {"tenant": "new", "secret": "fake&url=secret", "empty": ""})
        self.assertNotIn("fake&url=secret", str(result.public_status()) + repr(result))

    def test_override_url_query_does_not_inherit_old_provider_query(self):
        self.provider('[model_providers.relay.query_params]\nold = "fake-old-secret"\n')
        result = self.resolve({"CODEX_IMAGE_BASE_URL": "https://override.example/v1?tenant=mine&key=fake-new-secret", "CODEX_IMAGE_API_KEY": "fake-override-key"})
        self.assertEqual(result.base_url, "https://override.example/v1")
        self.assertEqual(result.query_params, {"tenant": "mine", "key": "fake-new-secret"})
        self.assertNotIn("fake-new-secret", str(result.public_status()) + repr(result))


if __name__ == "__main__":
    unittest.main()
