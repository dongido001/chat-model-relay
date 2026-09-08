"""Opt-in, sanitized JSONL traces for one gateway translation round."""
from __future__ import annotations

import json
import hashlib
import re
import time
from pathlib import Path
from typing import Any

from src.config import Config

_SECRET_KEYS = {
    "authorization", "api_key", "apikey", "token", "access_token",
    "refresh_token", "cookie", "set_cookie", "password", "secret",
}
_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+\-/]+=*"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
)
_CONTENT_KEYS = {
    "arguments", "body", "content", "input", "prompt", "response_text", "text",
}


def _content_fingerprint(value: Any) -> str:
    """Describe sensitive free-form content without persisting the content."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        encoded = repr(value)
    digest = hashlib.sha256(encoded.encode("utf-8", errors="replace")).hexdigest()
    return f"[CONTENT REDACTED chars={len(encoded)} sha256={digest}]"


def sanitize_trace_value(value: Any, *, max_chars: int | None = None) -> Any:
    """Recursively redact credentials and bound strings and collections."""
    limit = max_chars or Config.API_TRACE_MAX_CHARS
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for raw_key, child in list(value.items())[:500]:
            key = str(raw_key)
            normalized_key = key.lower().replace("-", "_")
            if normalized_key in _SECRET_KEYS:
                sanitized[key] = "[REDACTED]"
            elif normalized_key in _CONTENT_KEYS and not Config.API_TRACE_INCLUDE_CONTENT:
                sanitized[key] = _content_fingerprint(child)
            else:
                sanitized[key] = sanitize_trace_value(child, max_chars=limit)
        return sanitized
    if isinstance(value, (list, tuple)):
        return [sanitize_trace_value(child, max_chars=limit) for child in list(value)[:500]]
    if isinstance(value, bytes):
        return f"[BYTES {len(value)}]"
    if isinstance(value, Path):
        return value.name
    if isinstance(value, str):
        text = value
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub(lambda match: (match.group(1) if match.lastindex else "") + "[REDACTED]", text)
        if len(text) > limit:
            return text[:limit] + f"\n[TRUNCATED {len(text) - limit} CHARS]"
        return text
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:limit]


def trace_id_from_request(http_request: Any | None, fallback: str) -> str:
    """Reuse middleware correlation ID while restricting filesystem characters."""
    candidate = ""
    if http_request is not None:
        candidate = str(getattr(getattr(http_request, "state", None), "request_id", "") or "")
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", candidate or fallback)[:128]
    return safe or "unknown"


def record_round_trace(trace_id: str, stage: str, payload: Any) -> None:
    """Append one sanitized event. Tracing is disabled unless explicitly enabled."""
    if not Config.API_TRACE_ENABLED:
        return
    try:
        trace_dir = Path(Config.API_TRACE_DIR)
        trace_dir.mkdir(parents=True, exist_ok=True)
        event = {"timestamp": time.time(), "stage": stage, "payload": sanitize_trace_value(payload)}
        with (trace_dir / f"{trace_id}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    except (OSError, TypeError, ValueError):
        # Diagnostics must never turn a valid completion into a failed request.
        return
