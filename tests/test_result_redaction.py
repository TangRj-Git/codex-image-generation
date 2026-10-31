"""Malformed/extra relay output must not expose known connection secrets."""
import unittest
from types import SimpleNamespace

from codex_image_generation.generator import MAX_TEXT_CHARS, _State


class ResultRedactionTests(unittest.TestCase):
    def setUp(self):
        self.secret = "fixture_secret_abcdef123456"
        self.state = _State(SimpleNamespace(
            api_key=self.secret, headers={}, query_params={}, model="requested-model",
        ))

    def test_secret_split_across_text_blocks_is_redacted_after_join(self):
        self.state.response({"output": [{"type": "message", "content": [
            {"type": "output_text", "text": "Hello " + self.secret[:12]},
            {"type": "output_text", "text": self.secret[12:] + "."},
        ]}]})
        self.assertEqual(self.state.result.text, "Hello [redacted].")

    def test_secret_crossing_truncation_boundary_is_redacted_first(self):
        prefix = "z" * (MAX_TEXT_CHARS - 3)
        value = self.state.text(prefix + self.secret + " suffix")
        self.assertEqual(value, (prefix + "[redacted] suffix")[:MAX_TEXT_CHARS])

    def test_usage_keys_cannot_echo_secrets(self):
        self.state.response({"usage": {
            "input_tokens": 1, self.secret: 99,
            "output_tokens_details": {self.secret: 8, "reasoning_tokens": 2},
        }})
        self.assertNotIn(self.secret, str(self.state.result.usage))
        self.assertEqual(self.state.result.usage["output_tokens_details"], {"reasoning_tokens": 2})

    def test_upstream_model_is_not_fabricated(self):
        self.state.response({"id": "resp_test", "output": []})
        self.assertIsNone(self.state.result.model)
        self.state.response({"model": "provider-reported", "output": []})
        self.assertEqual(self.state.result.model, "provider-reported")
