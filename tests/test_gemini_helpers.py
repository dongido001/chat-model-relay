from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.api.openai_schemas import FunctionDefinition, ToolDefinition
from src.gemini.detector import (
    _wait_for_fast_completion_signal,
)
from src.api.tool_protocol import (
    build_gemini_browser_tool_prompt,
    looks_like_workspace_refusal,
    parse_gemini_final_response,
)
from src.browser.manager import _provider_login_selectors
from src.config import Config
from src.gemini.detector import is_incomplete_response_text, normalize_assistant_text
from src.gemini.client import (
    _compact_prompt_for_composer,
    _gemini_prompt_too_long_for_composer,
    _write_prompt_pdf,
    thread_id_from_gemini_url,
)
from src.gemini.selectors import GeminiSelectors


class _Selectors:
    CHAT_INPUT = ["chatgpt-input"]
    LOGIN_INDICATORS = ["chatgpt-login"]
    LOGGED_IN_INDICATORS = []


class _ClaudeSelectors:
    CHAT_INPUT = ["claude-input"]
    LOGIN_INDICATORS = ["claude-login"]
    LOGGED_IN_INDICATORS = ["claude-menu"]


class GeminiProviderTests(unittest.TestCase):
    def test_gemini_config_exposes_browser_model(self) -> None:
        with patch.object(Config, "PROVIDER", "gemini"):
            self.assertEqual(Config.provider_url(), Config.GEMINI_URL)
            self.assertEqual(Config.provider_name(), "Gemini")
            self.assertEqual(Config.provider_model_ids(), ("gemini-browser",))
            self.assertEqual(Config.default_model_id(), "gemini-browser")
            self.assertEqual(Config.provider_owner(), "google")
            self.assertTrue(Config.uses_browser())
            self.assertFalse(Config.supports_image_generation())
            self.assertEqual(Config.resolve_model_id(None), "gemini-browser")
            self.assertEqual(Config.resolve_model_id("catgpt-browser"), "gemini-browser")
            self.assertEqual(Config.resolve_model_id("gemini-browser"), "gemini-browser")
            with self.assertRaises(ValueError):
                Config.resolve_model_id("gpt-5.5")

    def test_login_selectors_use_gemini_lists(self) -> None:
        with patch.object(Config, "PROVIDER", "gemini"):
            chat, login, logged_in = _provider_login_selectors(
                _Selectors, _ClaudeSelectors, GeminiSelectors
            )
        self.assertEqual(chat, GeminiSelectors.CHAT_INPUT)
        self.assertEqual(login, GeminiSelectors.LOGIN_INDICATORS)
        self.assertEqual(logged_in, GeminiSelectors.LOGGED_IN_INDICATORS)
        self.assertTrue(any("prompt" in item.lower() or "textarea" in item.lower() or "contenteditable" in item.lower() for item in chat))
        self.assertTrue(any("sign in" in item.lower() or "login" in item.lower() or "username" in item.lower() for item in login))

    def test_incomplete_response_heuristic(self) -> None:
        self.assertEqual(normalize_assistant_text("Gemini said: Hello"), "Hello")
        self.assertTrue(is_incomplete_response_text("Thinking"))
        self.assertTrue(is_incomplete_response_text(""))
        self.assertTrue(is_incomplete_response_text("Defining the Task\nGemini said"))
        self.assertFalse(is_incomplete_response_text("The sky is blue because of Rayleigh scattering."))

    def test_thread_id_from_gemini_url(self) -> None:
        self.assertEqual(
            thread_id_from_gemini_url("https://gemini.google.com/app/c0e2ba5d4301f80e"),
            "c0e2ba5d4301f80e",
        )
        self.assertEqual(
            thread_id_from_gemini_url("https://gemini.google.com/u/0/app/c0e2ba5d4301f80e?hl=en"),
            "c0e2ba5d4301f80e",
        )
        self.assertEqual(thread_id_from_gemini_url("https://gemini.google.com/app"), "")
        self.assertEqual(thread_id_from_gemini_url("https://gemini.google.com/app/settings"), "")

    def test_write_prompt_pdf_embeds_text(self) -> None:
        import fitz

        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        try:
            _write_prompt_pdf("hello café\nsecond line", path)
            doc = fitz.open(path)
            extracted = "".join(page.get_text() for page in doc)
            doc.close()
            self.assertIn("hello", extracted)
            self.assertIn("second line", extracted)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_navigate_to_thread_skips_reload_when_already_there(self) -> None:
        import asyncio

        from src.gemini.client import GeminiClient

        class _FakePage:
            url = "https://gemini.google.com/app/c0e2ba5d4301f80e"
            goto_calls: list[str] = []

            async def goto(self, url: str, **_kwargs) -> None:
                self.goto_calls.append(url)

        client = GeminiClient.__new__(GeminiClient)
        client._page = _FakePage()
        asyncio.run(client.navigate_to_thread("c0e2ba5d4301f80e"))
        self.assertEqual(client._page.goto_calls, [])

    def test_send_message_accepts_openai_model_kwarg(self) -> None:
        import inspect

        from src.gemini.client import GeminiClient

        params = inspect.signature(GeminiClient.send_message).parameters
        self.assertIn("model", params)

    def test_fast_detector_accepts_stream_completion_without_copy_button(self) -> None:
        snapshots = [
            {
                "found": True,
                "signature": "new:partial",
                "hasCopyButton": False,
                "isStreaming": True,
                "text": "partial",
            },
            {
                "found": True,
                "signature": "new:done",
                "hasCopyButton": False,
                "isStreaming": False,
                "text": "final answer",
            },
        ]
        with (
            patch("src.gemini.detector._latest_assistant_turn_snapshot", new=AsyncMock(side_effect=snapshots)),
            patch.object(Config, "POLL_INTERVAL_MS", 1),
        ):
            completed = asyncio.run(
                _wait_for_fast_completion_signal(object(), 0, 1000, "old:answer")
            )
        self.assertTrue(completed)

    def test_fast_detector_accepts_stable_text_without_stream_or_copy(self) -> None:
        snapshot = {
            "found": True,
            "signature": "new:done",
            "hasCopyButton": False,
            "isStreaming": False,
            "text": "final answer",
        }
        with (
            patch(
                "src.gemini.detector._latest_assistant_turn_snapshot",
                new=AsyncMock(side_effect=[snapshot, snapshot, snapshot]),
            ),
            patch.object(Config, "POLL_INTERVAL_MS", 1),
        ):
            completed = asyncio.run(
                _wait_for_fast_completion_signal(object(), 0, 1000, "old:answer")
            )
        self.assertTrue(completed)

    def test_long_copilot_prompt_uses_attachment_not_composer_paste(self) -> None:
        with patch.object(Config, "CHATGPT_LONG_PROMPT_THRESHOLD", 8000):
            self.assertTrue(_gemini_prompt_too_long_for_composer("x" * 8000))
            self.assertTrue(_gemini_prompt_too_long_for_composer("x" * 1800))
            self.assertFalse(_gemini_prompt_too_long_for_composer("short"))

    def test_compact_prompt_keeps_cursor_user_query(self) -> None:
        text = "huge contract\n<user_query>Animate this hallway shot</user_query>"
        self.assertEqual(_compact_prompt_for_composer(text), "Animate this hallway shot")
        stubby = "<user_query>eee</user_query>\n<user_query>please reasearch about Onwuka Gideon</user_query>"
        self.assertEqual(
            _compact_prompt_for_composer(stubby),
            "please reasearch about Onwuka Gideon",
        )

    def test_gemini_tool_card_skips_browser_tools_and_stays_small(self) -> None:
        tools = [
            ToolDefinition(
                function=FunctionDefinition(
                    name="read_page",
                    description="Get a snapshot of the current browser page state. This is better than screenshot.",
                    parameters={"type": "object", "properties": {"pageId": {"type": "string"}}, "required": ["pageId"]},
                )
            ),
            ToolDefinition(
                function=FunctionDefinition(
                    name="read_file",
                    description="Read a file",
                    parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                )
            ),
        ]
        card = build_gemini_browser_tool_prompt(tools)
        self.assertIn("read_file", card)
        self.assertNotIn("read_page", card)
        self.assertLess(len(card), 1600)

    def test_gemini_refuses_terminal_in_prose(self) -> None:
        self.assertTrue(
            looks_like_workspace_refusal(
                "I am Gemini, an AI assistant, so I don't have direct access to your local machine"
            )
        )
        self.assertFalse(looks_like_workspace_refusal('{"tool_calls":[]}'))

    def test_gemini_tool_prompt_requires_structured_envelope(self) -> None:
        tool = ToolDefinition(
            function=FunctionDefinition(
                name="list_dir",
                parameters={"type": "object", "properties": {}},
            )
        )
        prompt = build_gemini_browser_tool_prompt([tool], user_text="check the files")
        self.assertIn('{"tool_calls"', prompt)
        self.assertIn('{"final"', prompt)
        self.assertIn("Decide whether the request needs a listed function", prompt)

    def test_gemini_final_requires_exact_json_envelope(self) -> None:
        self.assertEqual(
            parse_gemini_final_response('{"final":"The project is ready."}'),
            "The project is ready.",
        )
        self.assertIsNone(parse_gemini_final_response("ls -la ~/Projects"))
        self.assertIsNone(parse_gemini_final_response('{"final":"ok","extra":true}'))


if __name__ == "__main__":
    unittest.main()
