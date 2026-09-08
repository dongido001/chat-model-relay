"""Cursor/VS Code request normalization.

Keep editor-specific envelope knowledge out of the protocol routes.  Cursor
currently embeds the actionable request in one of a few XML-like tags and may
prepend a very large agent contract.  These helpers are deliberately pure so
captured request shapes can be regression-tested without starting FastAPI or a
browser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


_USER_REQUEST_RE = re.compile(
    r"<(?:user_query|user_request|userRequest)>\s*(.*?)\s*"
    r"</(?:user_query|user_request|userRequest)>",
    flags=re.DOTALL | re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CursorEnvelope:
    """Normalized view of a possibly editor-generated user message."""

    original_text: str
    user_request: str
    is_editor_envelope: bool

    def compact(self, max_chars: int = 4000) -> str:
        """Return the actionable request, or a bounded plain-text fallback."""
        if self.user_request:
            return self.user_request
        stripped = self.original_text.strip()
        if len(stripped) <= max_chars:
            return stripped
        return stripped[:max_chars] + "\n\n[truncated]"


def parse_cursor_envelope(text: str) -> CursorEnvelope:
    """Parse the last actionable request from known Cursor/VS Code envelopes."""
    source = text or ""
    matches = _USER_REQUEST_RE.findall(source)
    request = matches[-1].strip() if matches else ""
    return CursorEnvelope(
        original_text=source,
        user_request=request,
        is_editor_envelope=bool(matches),
    )


def extract_latest_user_request(text: str) -> str:
    """Return only the latest tagged user request, if present."""
    return parse_cursor_envelope(text).user_request


def extract_first_user_request(text: str) -> str:
    """Return the first tagged user request, used as a stable conversation seed."""
    matches = _USER_REQUEST_RE.findall(text or "")
    return matches[0].strip() if matches else ""


def extract_actionable_user_request(text: str, *, min_chars: int = 12) -> str:
    """Latest tagged request, skipping placeholder stubs like 'eee' but keeping 'hello'."""
    matches = [item.strip() for item in _USER_REQUEST_RE.findall(text or "") if item.strip()]
    if not matches:
        return ""
    latest = matches[-1]
    if not _is_placeholder_request(latest, min_chars=min_chars):
        return latest
    for request in reversed(matches[:-1]):
        if not _is_placeholder_request(request, min_chars=min_chars):
            return request
    return latest


def _is_placeholder_request(text: str, *, min_chars: int) -> bool:
    cleaned = (text or "").strip()
    if not cleaned:
        return True
    if len(cleaned) >= min_chars:
        return False
    letters = [ch for ch in cleaned.lower() if ch.isalpha()]
    unique = set(letters or cleaned.lower())
    return len(cleaned) <= 3 and len(unique) <= 1


def compact_cursor_followup(text: str, max_chars: int = 4000) -> str:
    """Bound a repeated editor payload while preserving its latest request."""
    return parse_cursor_envelope(text).compact(max_chars=max_chars)
