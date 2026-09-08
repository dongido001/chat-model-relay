"""Backward-compatible import shim to the provider-specific ChatGPT selector layer."""

from __future__ import annotations

from src.chatgpt.selectors import ChatGPTSelectors as Selectors

__all__ = ["Selectors"]
