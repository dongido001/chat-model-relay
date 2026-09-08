from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from src.chatgpt.client import ChatGPTClient
from src.chatgpt.errors import PromptAttachmentFallbackError, PromptTooLongError
from src.config import Config


class _FakeKeyboard:
    def __init__(self) -> None:
        self.presses: list[str] = []

    async def press(self, key: str) -> None:
        self.presses.append(key)


class _FakePage:
    def __init__(self, send_state: dict | None = None) -> None:
        self.keyboard = _FakeKeyboard()
        self.send_state = send_state or {}
        self.url = "https://chatgpt.com/c/test-thread"

    def on(self, *_args) -> None:
        return None

    async def evaluate(self, *_args, **_kwargs):
        return self.send_state


class _FlowClient(ChatGPTClient):
    def __init__(self, page: _FakePage, prompt_state: str = "prompt-too-long") -> None:
        super().__init__(page)  # type: ignore[arg-type]
        self.prompt_state = prompt_state
        self.upload_calls: list[list[str]] = []
        self.upload_error: Exception | None = None

    async def _detect_page_error(self) -> str | None:
        return None

    async def _find_selector(self, *_args, **_kwargs) -> str:
        return "#prompt-textarea"

    async def _prompt_submission_state(self, _text: str) -> str:
        return self.prompt_state

    async def _upload_files(self, file_paths: list[str]) -> None:
        self.upload_calls.append(list(file_paths))
        if self.upload_error:
            raise self.upload_error

    async def _click_send(self) -> str:
        return "clicked"

    async def _wait_for_message_submission(self, *_args, **_kwargs) -> bool:
        return True

    async def _reset_composer_for_upload(self) -> None:
        return None

    async def _wait_for_send_ready(self, timeout_s: float = 20) -> str:
        return "ready"


class LongPromptFallbackTests(IsolatedAsyncioTestCase):
    async def test_prompt_state_detects_ui_error(self) -> None:
        client = ChatGPTClient(_FakePage())  # type: ignore[arg-type]
        client._composer_state = AsyncMock(
            return_value={
                "promptTooLong": True,
                "sendButton": {"disabled": True, "ariaDisabled": "true"},
            }
        )

        with patch("src.chatgpt.client.asyncio.sleep", new=AsyncMock()):
            state = await client._prompt_submission_state("long prompt")

        self.assertEqual(state, "prompt-too-long")

    async def test_prompt_state_detects_populated_aria_disabled_composer(self) -> None:
        client = ChatGPTClient(_FakePage())  # type: ignore[arg-type]
        client._composer_state = AsyncMock(
            return_value={
                "composerText": "oversized prompt",
                "hasStopButton": False,
                "promptTooLong": False,
                "sendButton": {"disabled": False, "ariaDisabled": "true"},
            }
        )

        with patch("src.chatgpt.client.asyncio.sleep", new=AsyncMock()):
            state = await client._prompt_submission_state("x" * 5000)

        self.assertEqual(state, "prompt-too-long")

    async def test_short_prompt_with_disabled_send_is_not_too_long(self) -> None:
        client = ChatGPTClient(_FakePage())  # type: ignore[arg-type]
        client._composer_state = AsyncMock(
            return_value={
                "composerText": "hey",
                "hasStopButton": False,
                "promptTooLong": False,
                "sendButton": {"disabled": False, "ariaDisabled": "true"},
            }
        )

        with (
            patch.object(Config, "CHATGPT_LONG_PROMPT_THRESHOLD", 8000),
            patch("src.chatgpt.client.asyncio.sleep", new=AsyncMock()),
        ):
            state = await client._prompt_submission_state("hey")

        self.assertEqual(state, "disabled")

    async def test_prompt_state_does_not_treat_active_generation_as_too_long(self) -> None:
        client = ChatGPTClient(_FakePage())  # type: ignore[arg-type]
        client._composer_state = AsyncMock(
            return_value={
                "composerText": "queued prompt",
                "hasStopButton": True,
                "promptTooLong": False,
                "sendButton": {"disabled": False, "ariaDisabled": "true"},
            }
        )

        with patch("src.chatgpt.client.asyncio.sleep", new=AsyncMock()):
            state = await client._prompt_submission_state("queued prompt")

        self.assertEqual(state, "disabled")

    async def test_aria_disabled_without_long_prompt_is_not_enter_fallback(self) -> None:
        client = ChatGPTClient(
            _FakePage({"disabled": False, "ariaDisabled": "true", "visible": True})
        )  # type: ignore[arg-type]
        client._find_selector = AsyncMock(return_value="#composer-submit-button")

        with patch("src.chatgpt.client.human_click", new=AsyncMock()) as click:
            state = await client._click_send()

        self.assertEqual(state, "disabled")
        click.assert_not_awaited()
        self.assertEqual(client.page.keyboard.presses, [])

    async def test_native_disabled_without_long_prompt_is_not_enter_fallback(self) -> None:
        client = ChatGPTClient(
            _FakePage({"disabled": True, "ariaDisabled": None, "visible": True})
        )  # type: ignore[arg-type]
        client._find_selector = AsyncMock(return_value="#composer-submit-button")

        state = await client._click_send()

        self.assertEqual(state, "disabled")
        self.assertEqual(client.page.keyboard.presses, [])

    async def test_prompt_attachment_is_utf8_and_cleaned_after_success(self) -> None:
        client = _FlowClient(_FakePage())
        typed: list[str] = []

        async def record_type(_page, _selector, text: str) -> None:
            typed.append(text)

        with (
            patch.object(Config, "CHATGPT_LONG_PROMPT_FALLBACK", "attachment"),
            patch("src.chatgpt.client.human_type", new=record_type),
            patch("src.chatgpt.client.random_delay", new=AsyncMock()),
            patch("src.chatgpt.client.count_assistant_messages", new=AsyncMock(return_value=0)),
            patch("src.chatgpt.client.get_latest_assistant_turn_signature", new=AsyncMock(return_value=None)),
            patch("src.chatgpt.client.get_latest_user_turn_signature", new=AsyncMock(return_value=None)),
            patch("src.chatgpt.client.wait_for_response_complete", new=AsyncMock(return_value=True)),
            patch("src.chatgpt.client.extract_images_from_response", new=AsyncMock(return_value=[])),
            patch("src.chatgpt.client.extract_last_response_via_copy", new=AsyncMock(return_value="LONG_PROMPT_OK")),
            patch("src.chatgpt.client.asyncio.sleep", new=AsyncMock()),
        ):
            result = await client.send_message(
                "héllo\n" + ("x" * 5000),
                image_paths=["screenshot.png"],
                file_paths=["existing.pdf"],
            )

        self.assertEqual(result.message, "LONG_PROMPT_OK")
        self.assertEqual(len(client.upload_calls), 1)
        uploaded = client.upload_calls[0]
        self.assertEqual(uploaded[:2], ["screenshot.png", "existing.pdf"])
        self.assertTrue(uploaded[-1].endswith(".txt"))
        self.assertEqual(Path(uploaded[-1]).exists(), False)
        self.assertIn("Read the attached file", typed[1])

    async def test_prompt_too_long_can_be_rejected_without_fallback(self) -> None:
        client = _FlowClient(_FakePage())

        with (
            patch.object(Config, "CHATGPT_LONG_PROMPT_FALLBACK", "error"),
            patch("src.chatgpt.client.human_type", new=AsyncMock()),
            patch("src.chatgpt.client.random_delay", new=AsyncMock()),
            patch("src.chatgpt.client.count_assistant_messages", new=AsyncMock(return_value=0)),
            patch("src.chatgpt.client.get_latest_assistant_turn_signature", new=AsyncMock(return_value=None)),
            patch("src.chatgpt.client.get_latest_user_turn_signature", new=AsyncMock(return_value=None)),
        ):
            with self.assertRaises(PromptTooLongError):
                await client.send_message("too long")

        self.assertEqual(client.upload_calls, [])

    async def test_attachment_cleanup_runs_when_upload_fails(self) -> None:
        client = _FlowClient(_FakePage())
        client.upload_error = PromptAttachmentFallbackError("upload failed")

        with (
            patch.object(Config, "CHATGPT_LONG_PROMPT_FALLBACK", "attachment"),
            patch("src.chatgpt.client.human_type", new=AsyncMock()),
            patch("src.chatgpt.client.random_delay", new=AsyncMock()),
            patch("src.chatgpt.client.count_assistant_messages", new=AsyncMock(return_value=0)),
            patch("src.chatgpt.client.get_latest_assistant_turn_signature", new=AsyncMock(return_value=None)),
            patch("src.chatgpt.client.get_latest_user_turn_signature", new=AsyncMock(return_value=None)),
        ):
            with self.assertRaises(PromptAttachmentFallbackError):
                await client.send_message("too long")

        self.assertEqual(len(client.upload_calls), 1)
        self.assertFalse(Path(client.upload_calls[0][-1]).exists())

    async def test_threshold_forces_attachment_fallback(self) -> None:
        client = _FlowClient(_FakePage(), prompt_state="ready")
        client._prompt_submission_state = ChatGPTClient._prompt_submission_state.__get__(client)  # type: ignore[method-assign]

        with (
            patch.object(Config, "CHATGPT_LONG_PROMPT_FALLBACK", "attachment"),
            patch.object(Config, "CHATGPT_LONG_PROMPT_THRESHOLD", 3),
            patch("src.chatgpt.client.human_type", new=AsyncMock()),
            patch("src.chatgpt.client.random_delay", new=AsyncMock()),
            patch("src.chatgpt.client.count_assistant_messages", new=AsyncMock(return_value=0)),
            patch("src.chatgpt.client.get_latest_assistant_turn_signature", new=AsyncMock(return_value=None)),
            patch("src.chatgpt.client.get_latest_user_turn_signature", new=AsyncMock(return_value=None)),
            patch("src.chatgpt.client.wait_for_response_complete", new=AsyncMock(return_value=True)),
            patch("src.chatgpt.client.extract_images_from_response", new=AsyncMock(return_value=[])),
            patch("src.chatgpt.client.extract_last_response_via_copy", new=AsyncMock(return_value="ok")),
            patch("src.chatgpt.client.asyncio.sleep", new=AsyncMock()),
        ):
            await client.send_message("abcd")

        self.assertEqual(len(client.upload_calls), 1)

    async def test_threshold_skips_pasting_the_full_prompt(self) -> None:
        client = _FlowClient(_FakePage())
        typed: list[str] = []

        async def record_type(_page, _selector, text: str) -> None:
            typed.append(text)

        with (
            patch.object(Config, "CHATGPT_LONG_PROMPT_FALLBACK", "attachment"),
            patch.object(Config, "CHATGPT_LONG_PROMPT_THRESHOLD", 8),
            patch("src.chatgpt.client.human_type", new=record_type),
            patch("src.chatgpt.client.random_delay", new=AsyncMock()),
            patch("src.chatgpt.client.count_assistant_messages", new=AsyncMock(return_value=0)),
            patch("src.chatgpt.client.get_latest_assistant_turn_signature", new=AsyncMock(return_value=None)),
            patch("src.chatgpt.client.get_latest_user_turn_signature", new=AsyncMock(return_value=None)),
            patch("src.chatgpt.client.wait_for_response_complete", new=AsyncMock(return_value=True)),
            patch("src.chatgpt.client.extract_images_from_response", new=AsyncMock(return_value=[])),
            patch("src.chatgpt.client.extract_last_response_via_copy", new=AsyncMock(return_value="ok")),
            patch("src.chatgpt.client.asyncio.sleep", new=AsyncMock()),
        ):
            await client.send_message("this prompt is over the threshold")

        self.assertEqual(len(typed), 1)
        self.assertIn("Read the attached file", typed[0])
        self.assertEqual(len(client.upload_calls), 1)

    def test_compact_prompt_uses_last_user_query(self) -> None:
        text = (
            "System: huge\n"
            "<user_query>first</user_query>\n"
            "<user_query>hello world</user_query>"
        )
        compact = ChatGPTClient._compact_prompt_for_composer(text)
        self.assertIn("hello world", compact)

    def test_compact_prompt_uses_vscode_user_request(self) -> None:
        prompt = (
            "agent context\n"
            "<userRequest>first</userRequest>\n"
            "<userRequest>change the theme</userRequest>"
        )

        compact = ChatGPTClient._compact_prompt_for_composer(prompt)

        self.assertIn("change the theme", compact)
        self.assertNotIn("first", compact)

    def test_non_image_paths_drop_screenshots(self) -> None:
        paths = ["/tmp/a.png", "/tmp/note.pdf", "/tmp/b.jpg"]
        self.assertEqual(ChatGPTClient._non_image_paths(paths), ["/tmp/note.pdf"])


class GeminiLongPromptTests(IsolatedAsyncioTestCase):
    async def test_long_prompt_sends_compact_composer_text_without_attach(self) -> None:
        from src.gemini.client import GeminiClient

        typed: list[str] = []
        uploaded: list[list[str]] = []

        async def record_fill(_page, _selector, text: str) -> None:
            typed.append(text)

        async def record_upload(file_paths: list[str]) -> None:
            uploaded.append(list(file_paths))

        page = _FakePage()
        page.url = "https://gemini.google.com/app/test"
        client = GeminiClient(page)  # type: ignore[arg-type]
        client._find_selector = AsyncMock(return_value="div.ql-editor")  # type: ignore[method-assign]
        client._upload_files = record_upload  # type: ignore[method-assign]
        client._click_send = AsyncMock(return_value=True)  # type: ignore[method-assign]
        client._composer_submitted = AsyncMock(return_value=True)  # type: ignore[method-assign]
        long_prompt = "<user_query>list Desktop</user_query>\n" + ("x" * 5000)

        with (
            patch("src.gemini.client.fill_gemini_composer", new=record_fill),
            patch("src.gemini.client.random_delay", new=AsyncMock()),
            patch("src.gemini.client.count_assistant_messages", new=AsyncMock(return_value=0)),
            patch(
                "src.gemini.client.get_latest_assistant_turn_signature",
                new=AsyncMock(return_value=None),
            ),
            patch("src.gemini.client.wait_for_response_complete", new=AsyncMock(return_value=True)),
            patch(
                "src.gemini.client.extract_last_response_via_copy",
                new=AsyncMock(return_value="COMPACT_OK"),
            ),
            patch("src.gemini.client.is_incomplete_response_text", return_value=False),
            patch("src.gemini.client.asyncio.sleep", new=AsyncMock()),
        ):
            result = await client.send_message(long_prompt)

        self.assertEqual(result.message, "COMPACT_OK")
        self.assertEqual(uploaded, [])
        self.assertEqual(len(typed), 1)
        self.assertIn("list Desktop", typed[0])
        self.assertNotIn("Read the attached file", typed[0])
        self.assertNotIn("x" * 100, typed[0])


if __name__ == "__main__":
    unittest.main()

