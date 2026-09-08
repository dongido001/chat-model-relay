from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

from fastapi import HTTPException
from starlette.requests import Request

from src.api.openai_schemas import ChatCompletionRequest
from src.config import Config


_APP_KEY_HEADERS = (
    "x-session-id",
    "session-id",
    "x-catgpt-app-key",
    "x-app-name",
    "x-client-name",
    "x-service-name",
    "x-application-name",
    "x-requested-with",
)
_CONVERSATION_ID_HEADER = "x-catgpt-conversation-id"


def _host_from_header_url(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    host = (parsed.netloc or parsed.path or "").strip().lower()
    return host


def _normalize_key_part(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", (value or "").strip().lower())
    return cleaned[:200]


def _session_id_from_request(http_request: Request | None) -> str:
    if http_request is None:
        return ""
    for header_name in ("x-session-id", "session-id"):
        value = (http_request.headers.get(header_name) or "").strip()
        if value:
            return value
    return ""


def _tab_session_key(request: Any, http_request: Request | None, app_key: str = "") -> str | None:
    session_id = _session_id_from_request(http_request)
    if session_id:
        return session_id
    conversation_id = (getattr(request, "conversation_id", None) or "").strip()
    if conversation_id:
        return f"conversation:{conversation_id}"
    thread_id = (getattr(request, "thread_id", None) or "").strip()
    if thread_id:
        return f"thread:{thread_id}"
    if app_key:
        return f"app:{app_key}"
    user = (getattr(request, "user", None) or "").strip()
    if user:
        return f"user:{user}"
    return None


def _project_key() -> str:
    project_getter = getattr(Config, "chatgpt_project_url", None)
    return (project_getter() if callable(project_getter) else "") or "global"


def _conversation_id_from_request(request: ChatCompletionRequest, http_request: Request | None) -> str:
    value = (request.conversation_id or "").strip()
    if not value and http_request is not None:
        value = (http_request.headers.get(_CONVERSATION_ID_HEADER) or "").strip()
    if len(value) > 256:
        raise HTTPException(status_code=400, detail="conversation id is too long")
    return value


def _responses_conversation_id(value: str | dict[str, Any] | None) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("id") or "").strip()
    return ""


def _derive_app_key(request: ChatCompletionRequest, http_request: Request | None, endpoint_app_name: str = "") -> str:
    endpoint_name = (endpoint_app_name or "").strip()
    if endpoint_name:
        return f"endpoint:{_normalize_key_part(endpoint_name)}"

    explicit_user = (request.user or "").strip() if getattr(request, "user", None) else ""
    if explicit_user:
        return f"user:{_normalize_key_part(explicit_user)}"

    if http_request is None:
        return ""

    headers = http_request.headers
    for header_name in _APP_KEY_HEADERS:
        value = (headers.get(header_name) or "").strip()
        if value:
            return f"hdr:{header_name}:{_normalize_key_part(value)}"

    origin_host = _host_from_header_url(headers.get("origin", ""))
    if origin_host:
        return f"origin:{origin_host}"

    referer_host = _host_from_header_url(headers.get("referer", ""))
    if referer_host:
        return f"referer:{referer_host}"

    user_agent = (headers.get("user-agent") or "").strip()
    if user_agent:
        first_token = user_agent.split()[0].strip()
        product = first_token.split("/", 1)[0].strip().lower()
        if product and product != "mozilla":
            return f"ua:{_normalize_key_part(product)}"
        ua_hash = hashlib.sha256(user_agent.encode("utf-8")).hexdigest()[:16]
        return f"ua_hash:{ua_hash}"

    client_host = (http_request.client.host if http_request.client else "") or ""
    client_host = client_host.strip()
    if client_host:
        return f"ip:{client_host}"
    return ""


def _display_app_name(app_key: str) -> str:
    if not app_key:
        return "unknown"
    if app_key.startswith("user:"):
        return app_key.split(":", 1)[1]
    if app_key.startswith("hdr:"):
        parts = app_key.split(":", 2)
        if len(parts) == 3:
            return parts[2]
    if app_key.startswith("endpoint:"):
        return app_key.split(":", 1)[1]
    if ":" in app_key:
        return app_key.split(":", 1)[1]
    return app_key


def _fresh_thread_from_header(http_request: Request | None) -> bool:
    if http_request is None:
        return False
    value = (http_request.headers.get("x-catgpt-thread-mode") or "").strip().lower()
    return value == "fresh"


def _request_contract_hash(request: Any) -> str:
    payload = {
        "system": [
            {"role": msg.role, "content": str(getattr(msg, "content", ""))}
            for msg in getattr(request, "messages", [])
            if getattr(msg, "role", "") in {"system", "developer"}
        ],
        "tools": getattr(request, "tools", None),
        "tool_choice": getattr(request, "tool_choice", None),
        "response_format": getattr(request, "response_format", None),
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _cache_key_for_request_with_app(request: Any, app_key: str) -> str:
    payload = getattr(request, "model_dump", lambda **kwargs: request.__dict__)()
    if Config.API_APP_THREAD_MODE and app_key:
        payload["_app_key"] = app_key
    compact_payload = payload
    canonical = json.dumps(compact_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "_session_id_from_request",
    "_tab_session_key",
    "_project_key",
    "_conversation_id_from_request",
    "_responses_conversation_id",
    "_derive_app_key",
    "_display_app_name",
    "_fresh_thread_from_header",
    "_request_contract_hash",
    "_cache_key_for_request_with_app",
]
