from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.api.round_trace import record_round_trace, sanitize_trace_value, trace_id_from_request
from src.config import Config


class RoundTraceTests(unittest.TestCase):
    def test_redacts_secret_keys_and_bearer_values(self) -> None:
        with patch.object(Config, "API_TRACE_INCLUDE_CONTENT", True):
            sanitized = sanitize_trace_value({
                "authorization": "Bearer abc.secret",
                "nested": {"api_key": "sk-privatevalue12345"},
                "text": "use Bearer visible-secret and sk-anothersecret12345",
            })
        self.assertEqual(sanitized["authorization"], "[REDACTED]")
        self.assertEqual(sanitized["nested"]["api_key"], "[REDACTED]")
        self.assertNotIn("visible-secret", sanitized["text"])
        self.assertNotIn("sk-another", sanitized["text"])

    def test_redacts_free_form_content_by_default(self) -> None:
        with patch.object(Config, "API_TRACE_INCLUDE_CONTENT", False):
            sanitized = sanitize_trace_value({
                "request": {"messages": [{"role": "user", "content": "Password: private-value"}]},
                "prompt": "Password: another-private-value",
                "arguments": {"command": "login --password private-value"},
            })
        rendered = json.dumps(sanitized)
        self.assertNotIn("private-value", rendered)
        self.assertNotIn("another-private-value", rendered)
        self.assertIn("CONTENT REDACTED", rendered)

    def test_bounds_large_values(self) -> None:
        sanitized = sanitize_trace_value("x" * 100, max_chars=20)
        self.assertTrue(sanitized.startswith("x" * 20))
        self.assertIn("TRUNCATED", sanitized)

    def test_uses_safe_request_id(self) -> None:
        request = SimpleNamespace(state=SimpleNamespace(request_id="../../request id"))
        self.assertEqual(trace_id_from_request(request, "fallback"), ".._.._request_id")

    def test_tracing_is_opt_in_and_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(Config, "API_TRACE_DIR", Path(directory)), patch.object(Config, "API_TRACE_ENABLED", False):
                record_round_trace("off", "request", {"value": 1})
                self.assertFalse((Path(directory) / "off.jsonl").exists())
            with patch.object(Config, "API_TRACE_DIR", Path(directory)), patch.object(Config, "API_TRACE_ENABLED", True):
                record_round_trace("on", "request", {"token": "secret", "value": 1})
                event = json.loads((Path(directory) / "on.jsonl").read_text(encoding="utf-8"))
                self.assertEqual(event["stage"], "request")
                self.assertEqual(event["payload"]["token"], "[REDACTED]")

    def test_trace_io_failure_does_not_break_request(self) -> None:
        with patch.object(Config, "API_TRACE_DIR", Path("/dev/null/nope")), patch.object(Config, "API_TRACE_ENABLED", True):
            record_round_trace("unwritable", "request", {"value": 1})


if __name__ == "__main__":
    unittest.main()
