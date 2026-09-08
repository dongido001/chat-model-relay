from __future__ import annotations

import json
from pathlib import Path
import unittest

from src.api.cursor_adapter import (
    compact_cursor_followup,
    extract_actionable_user_request,
    extract_first_user_request,
    extract_latest_user_request,
    parse_cursor_envelope,
)
from src.api.openai_schemas import ChatCompletionRequest


class CursorAdapterTests(unittest.TestCase):
    def test_extracts_cursor_user_query(self) -> None:
        text = "agent contract\n<user_query>Read pyproject.toml</user_query>"
        self.assertEqual(extract_latest_user_request(text), "Read pyproject.toml")

    def test_extracts_vscode_camel_case_request(self) -> None:
        text = "context\n<userRequest>Change the chat theme.</userRequest>"
        envelope = parse_cursor_envelope(text)
        self.assertTrue(envelope.is_editor_envelope)
        self.assertEqual(envelope.user_request, "Change the chat theme.")

    def test_last_request_wins_in_replayed_history(self) -> None:
        text = (
            "<user_query>old request</user_query>\n"
            "tool history\n"
            "<user_request>new request</user_request>"
        )
        self.assertEqual(extract_latest_user_request(text), "new request")
        self.assertEqual(extract_first_user_request(text), "old request")
        stubby = (
            "<user_query>eee</user_query>\n"
            "<user_query>please research about Onwuka Gideon</user_query>"
        )
        self.assertEqual(
            extract_actionable_user_request(stubby),
            "please research about Onwuka Gideon",
        )
        hello = (
            "<user_query>Read README.md</user_query>\n"
            "<user_query>hello</user_query>"
        )
        self.assertEqual(extract_actionable_user_request(hello), "hello")

    def test_compaction_preserves_actionable_request(self) -> None:
        text = "x" * 10000 + "<user_query>Fix the failing test</user_query>"
        self.assertEqual(compact_cursor_followup(text), "Fix the failing test")

    def test_plain_text_is_bounded(self) -> None:
        compact = compact_cursor_followup("x" * 20, max_chars=8)
        self.assertEqual(compact, "xxxxxxxx\n\n[truncated]")

    def test_plain_short_text_is_unchanged_except_outer_space(self) -> None:
        self.assertEqual(compact_cursor_followup("  hello  "), "hello")

    def test_sanitized_captured_vscode_mcp_shape_validates(self) -> None:
        fixture = Path(__file__).with_name("fixtures") / "vscode_mcp_round.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        payload.pop("source")
        request = ChatCompletionRequest.model_validate(payload)
        self.assertEqual(len(request.tools or []), 2)
        self.assertEqual(request.tools[1].function.name, "CallDynamicTool")
        self.assertEqual(request.messages[-1].role, "tool")
        self.assertEqual(request.messages[-1].tool_call_id, "call_fixture_read")


if __name__ == "__main__":
    unittest.main()
