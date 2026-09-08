from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest

if "patchright" not in sys.modules and importlib.util.find_spec("patchright.async_api") is None:
    patchright_mod = types.ModuleType("patchright")
    async_api_mod = types.ModuleType("patchright.async_api")
    async_api_mod.Page = object
    async_api_mod.BrowserContext = object
    async_api_mod.Playwright = object
    async_api_mod.Frame = object
    async_api_mod.Request = object
    async_api_mod.Response = object
    async_api_mod.async_playwright = lambda: None
    sys.modules["patchright"] = patchright_mod
    sys.modules["patchright.async_api"] = async_api_mod

from src.chatgpt.detector import _CONVERSATION_SNAPSHOT_JS
from src.chatgpt.selectors import ChatGPTSelectors


class ChatGPTSelectorCompatibilityTests(unittest.TestCase):
    def test_copy_stop_and_send_have_provider_specific_fallbacks(self) -> None:
        self.assertIn("button[aria-label='Copy response']", ChatGPTSelectors.copy_button_selectors())
        self.assertIn("button[aria-label='Stop answering']", ChatGPTSelectors.stop_button_selectors())
        self.assertIn("button[data-testid='send-button']", ChatGPTSelectors.send_button_selectors())

    def test_model_picker_compatibility_keeps_legacy_labels(self) -> None:
        options = ChatGPTSelectors.model_picker_selectors()
        self.assertIn("button[data-testid='model-switcher-dropdown-button']", options)
        self.assertTrue(any("has-text('GPT')" in selector for selector in options))

    def test_response_completion_selectors_merge_stop_and_copy_heuristics(self) -> None:
        selectors = ChatGPTSelectors.response_completion_selectors()
        self.assertTrue(any("Stop" in selector for selector in selectors))
        self.assertTrue(any("Copy" in selector for selector in selectors))

    def test_detector_snapshot_uses_centralized_selector_lists(self) -> None:
        for kind in ("COPY_BUTTON", "STOP_BUTTON", "SEND_BUTTON"):
            value = getattr(ChatGPTSelectors, kind)
            self.assertIn(json.dumps(value), _CONVERSATION_SNAPSHOT_JS)
        self.assertIn(json.dumps(ChatGPTSelectors.ASSISTANT_MESSAGE[0]), _CONVERSATION_SNAPSHOT_JS)


if __name__ == "__main__":
    unittest.main()
