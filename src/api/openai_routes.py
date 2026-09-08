"""
OpenAI-compatible API routes.

Provides:
  POST /v1/chat/completions   - chat completions (with tool/function calling)
  POST /v1/messages           - Anthropic Messages API adapter
  GET  /v1/models             - list available models

Browser work is coordinated through acquire_browser_page so independent
sessions can run in parallel tabs.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from src.api.openai_schemas import (
    ChatCompletionAsyncRequest,
    ChatCompletionJobResponse,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Choice,
    ChoiceMessage,
    AudioInfo,
    FunctionCallInfo,
    FunctionDefinition,
    ImageData,
    ImageGenerationRequest,
    ImagesResponse,
    ModelListResponse,
    ModelObject,
    ResponseInputItem,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseOutputToolCall,
    ResponsesRequest,
    ResponsesResponse,
    ResponsesUsageInfo,
    ToolCall,
    ToolDefinition,
    UsageInfo,)
from src.api.attachment_expander import (
    AttachmentPageDescriptor,
    build_attachment_context_note,
    expand_attachments_for_chatgpt,
)
from src.api.cursor_adapter import (
    compact_cursor_followup,
    extract_actionable_user_request,
    extract_first_user_request,
    extract_latest_user_request,
)
from src.api.prompt_compaction import (
    _apply_tool_prompt_to_messages,
    _build_prompt,
    _compact_followup_user_message,
    _compact_tool_result_message,
    _extract_content_text,
    _extract_file_attachments,
    _extract_image_urls,
    _first_user_conversation_seed,
    _latest_turn_messages,
    _latest_user_message,
    _new_attachments_from_latest_user,
)
from src.api.thread_routing import (
    _conversation_id_from_request,
    _derive_app_key,
    _display_app_name,
    _responses_conversation_id,
    _session_id_from_request,
    _tab_session_key,
)
from src.api.tool_translation import (
    build_tool_repair_prompt,
    tool_call_expected,
    validate_tool_calls,
)
from src.api.tool_protocol import (
    build_gemini_browser_tool_prompt as _build_gemini_browser_tool_prompt,
    build_gemini_tool_repair_prompt as _build_gemini_tool_repair_prompt,
    build_tool_continuation_prompt as _build_tool_continuation_prompt,
    build_tool_system_prompt as _build_tool_system_prompt,
    looks_like_workspace_refusal,
    parse_gemini_final_response,
    parse_tool_calls as _parse_tool_calls,
    parse_tool_calls_outcome as _parse_tool_calls_outcome,
)
from src.api.observability import metrics
from src.api.round_trace import record_round_trace, trace_id_from_request
from src.api.conversation_store import ConversationRoute, ConversationStore
from src.api.browser_gate import (
    CLEANUP_SESSION,
    CONTROL_SESSION,
    acquire_browser_page,
    browser_access_lock,
)
from src.api.disconnect_watch import start_disconnect_watch, stop_disconnect_watch
from src.chatgpt.client import ChatGPTClient
from src.chatgpt.errors import PromptAttachmentFallbackError, PromptTooLongError
from src.claude.client import ClaudeClient
from src.gemini.client import GeminiClient
from src.minimax.client import MiniMaxClient
from src.chatgpt.model_registry import (
    PUBLIC_BROWSER_MODEL_ID,
    is_supported_chat_model,
    list_public_chat_models,
)
from src.config import Config
from src.log import setup_logging

log = setup_logging("openai_routes")

openai_router = APIRouter()

# Global reference - set by server.py at startup
ProviderClient = ChatGPTClient | ClaudeClient | GeminiClient | MiniMaxClient
_client: ProviderClient | None = None

# Kept for compatibility with integrations that reset the legacy route state.
_lock: asyncio.Lock | None = None
_thread_message_count = 0
_last_response_time = 0.0

_jobs_lock = asyncio.Lock()
_jobs: dict[str, ChatCompletionJobResponse] = {}
_job_app_keys: dict[str, str] = {}
_cache_lock = asyncio.Lock()
_response_cache: dict[str, tuple[float, ChatCompletionResponse]] = {}
_contract_lock = asyncio.Lock()
_thread_contracts: dict[str, tuple[float, str]] = {}
_thread_user_contracts: dict[str, tuple[float, str, str]] = {}
_thread_last_user_text: dict[str, tuple[float, str]] = {}
_app_thread_lock = asyncio.Lock()
_conversation_store: ConversationStore | None = None
_conversation_store_path = ""
_completion_route_outcomes: dict[str, ConversationRoute] = {}


@dataclass(slots=True)
class _AppThreadMapping:
    last_used: float
    thread_id: str
    created_by_catgpt: bool = False


_app_threads: dict[str, _AppThreadMapping] = {}
_app_threads_loaded = False
_app_fresh_chats: set[str] = set()


@dataclass(slots=True)
class _ConversationRouting:
    project_key: str
    app_key: str
    conversation_key: str
    previous_route: ConversationRoute | None
    transcript_input: list[dict[str, Any]]
    messages_for_browser: list[ChatMessage]
    contract_hash: str
    action: str

MODEL_ID = PUBLIC_BROWSER_MODEL_ID
_CACHE_TTL_SECONDS = 600
_CACHE_MAX_ENTRIES = 256
_CONTRACT_TTL_SECONDS = max(60, Config.API_THREAD_CONTRACT_TTL_SECONDS)
_APP_THREAD_TTL_SECONDS = max(300, Config.API_APP_THREAD_TTL_SECONDS)
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
_THREAD_MODE_HEADER = "x-catgpt-thread-mode"
_THREAD_TITLE_TTL_SECONDS = 600
_thread_title_lock = asyncio.Lock()
_thread_titles: dict[str, tuple[float, str]] = {}


def set_openai_client(client: ProviderClient) -> None:
    """Called by server.py to inject the active provider client."""
    global _client
    _client = client


def _get_client() -> ProviderClient:
    if _client is None:
        raise HTTPException(status_code=503, detail="Provider client not initialized")
    return _client


def _bind_client(client: ProviderClient, page: Any | None) -> ProviderClient:
    """Bind a provider client to a leased tab when the pool supplied one."""
    bind_page = getattr(client, "bind_page", None)
    if page is None or not callable(bind_page):
        return client
    return bind_page(page)


def _session_id_from_request(http_request: Request | None) -> str:
    from src.api.thread_routing import _session_id_from_request as _impl
    return _impl(http_request)


def _tab_session_key(
    request: Any,
    http_request: Request | None,
    app_key: str = "",
) -> str | None:
    from src.api.thread_routing import _tab_session_key as _impl
    return _impl(request, http_request, app_key)


def _supports_thread_navigation(client: Any) -> bool:
    return all(
        callable(getattr(client, name, None))
        for name in ("new_chat", "navigate_to_thread", "_extract_thread_id")
    )


def _first_user_conversation_seed(messages: list[ChatMessage] | None) -> str:
    from src.api.prompt_compaction import _first_user_conversation_seed as _impl
    return _impl(messages)


def _apply_isolated_conversation_id(
    request: ChatCompletionRequest,
    http_request: Request | None,
    fresh_thread: bool,
) -> ChatCompletionRequest:
    """Give Cursor chats a durable conversation_id when the client omits one.

    Cursor does not send X-CatGPT-Thread-Mode or conversation_id. Without this,
    API_APP_THREAD_MODE maps every Copilot/Cursor chat onto one provider thread.
    """
    if fresh_thread or not Config.API_DERIVE_CONVERSATION_ID:
        return request
    if (getattr(request, "conversation_id", None) or "").strip():
        return request
    if (getattr(request, "thread_id", None) or "").strip():
        return request
    session_id = _session_id_from_request(http_request)
    if session_id:
        if len(session_id) > 256:
            raise HTTPException(status_code=400, detail="conversation id is too long")
        log.info("Using session header as conversation id")
        return _model_copy_compat(request, deep=True, update={"conversation_id": session_id})
    seed = _first_user_conversation_seed(request.messages)
    if not seed:
        return request
    derived = f"derived:{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]}"
    log.info("Derived conversation id %s from first user turn", derived)
    return _model_copy_compat(request, deep=True, update={"conversation_id": derived})


def _latest_turn_messages(
    messages: list[ChatMessage],
    *,
    include_system: bool = True,
) -> list[ChatMessage]:
    from src.api.prompt_compaction import _latest_turn_messages as _impl
    return _impl(messages, include_system=include_system)


def _latest_user_message(messages: list[ChatMessage]) -> ChatMessage | None:
    from src.api.prompt_compaction import _latest_user_message as _impl
    return _impl(messages)


def _last_user_query(text: str) -> str:
    from src.api.prompt_compaction import _last_user_query as _impl
    return _impl(text)


def _compact_followup_user_message(message: ChatMessage) -> ChatMessage:
    from src.api.prompt_compaction import _compact_followup_user_message as _impl
    return _impl(message)


def _compact_tool_result_message(
    message: ChatMessage,
    max_chars: int | None = None,
) -> ChatMessage:
    from src.api.prompt_compaction import _compact_tool_result_message as _impl
    # Resolve the default from this module so callers/tests that override the
    # route configuration affect browser-bound compaction immediately.
    limit = max_chars if max_chars is not None else Config.API_TOOL_RESULT_MAX_CHARS
    return _impl(message, max_chars=limit)


def _file_attachment_key(attachment: dict) -> str:
    from src.api.prompt_compaction import _file_attachment_key as _impl
    return _impl(attachment)


def _new_attachments_from_latest_user(messages: list[ChatMessage]) -> tuple[list[str], list[dict]]:
    from src.api.prompt_compaction import _new_attachments_from_latest_user as _impl
    return _impl(messages)


def _chat_reasoning_effort(request: ChatCompletionRequest) -> str | None:
    """Read the official Chat field, with nested reasoning as a convenience."""
    if request.reasoning_effort:
        return request.reasoning_effort
    return request.reasoning.effort if request.reasoning else None


# -- Helpers -----------------------------------------------------


def _estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars per token)."""
    return max(1, len(text) // 4)


def _model_dump_compat(model: Any, **kwargs) -> dict:
    """Pydantic v1/v2 compatible model dump helper."""
    if hasattr(model, "model_dump"):
        return model.model_dump(**kwargs)
    if hasattr(model, "dict"):
        safe_kwargs = {k: v for k, v in kwargs.items() if k != "mode"}
        return model.dict(**safe_kwargs)
    return dict(getattr(model, "__dict__", {}))


def _model_copy_compat(model: Any, **kwargs):
    """Pydantic v1/v2 compatible model copy helper."""
    if hasattr(model, "model_copy"):
        return model.model_copy(**kwargs)
    if hasattr(model, "copy"):
        return model.copy(**kwargs)
    cloned = copy.deepcopy(model) if kwargs.get("deep") else copy.copy(model)
    for key, value in (kwargs.get("update") or {}).items():
        setattr(cloned, key, value)
    return cloned


def _get_conversation_store() -> ConversationStore:
    global _conversation_store, _conversation_store_path
    path = str(Config.API_CONVERSATION_DB)
    # The configured database may live in a fresh local state directory (for
    # example on first startup or in a test fixture). Ensure its parent exists
    # before SQLite opens the file.
    Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    if _conversation_store is None or _conversation_store_path != path:
        _conversation_store = ConversationStore(path)
        _conversation_store_path = path
    _conversation_store.prune(
        retention_seconds=Config.API_CONVERSATION_RETENTION_SECONDS,
        max_routes=Config.API_CONVERSATION_MAX_ROUTES,
    )
    return _conversation_store


def _project_key() -> str:
    project_getter = getattr(Config, "chatgpt_project_url", None)
    return (project_getter() if callable(project_getter) else "") or "global"


def _canonical_message(message: ChatMessage | dict[str, Any]) -> dict[str, Any]:
    raw = dict(message) if isinstance(message, dict) else _model_dump_compat(
        message, mode="json", exclude_none=True
    )
    return {key: raw[key] for key in sorted(raw) if raw[key] is not None}


def _message_hash(message: ChatMessage | dict[str, Any]) -> str:
    canonical = json.dumps(
        _canonical_message(message), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _messages_from_transcript(
    transcript: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> list[ChatMessage]:
    return [ChatMessage(**dict(item)) for item in transcript]


def _request_contract_hash(request: ChatCompletionRequest) -> str:
    system = [
        _canonical_message(message)
        for message in request.messages
        if message.role in {"system", "developer"}
    ]
    payload = {
        "system": system,
        "tools": _model_dump_compat(request, mode="json", exclude_none=True).get("tools"),
        "tool_choice": _model_dump_compat(request, mode="json", exclude_none=True).get("tool_choice"),
        "response_format": _model_dump_compat(request, mode="json", exclude_none=True).get("response_format"),
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _conversation_id_from_request(
    request: ChatCompletionRequest,
    http_request: Request | None,
) -> str:
    from src.api.thread_routing import _conversation_id_from_request as _impl
    return _impl(request, http_request)


def _responses_conversation_id(value: str | dict[str, Any] | None) -> str:
    from src.api.thread_routing import _responses_conversation_id as _impl
    return _impl(value)


def _shrink_for_cache(value: Any) -> Any:
    """Reduce large strings to a digest so cache-key generation stays cheap."""
    if isinstance(value, str):
        if len(value) > 512:
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
            return f"sha256:{digest}:len:{len(value)}"
        return value
    if isinstance(value, list):
        return [_shrink_for_cache(v) for v in value]
    if isinstance(value, dict):
        return {k: _shrink_for_cache(v) for k, v in value.items()}
    return value


def _cache_key_for_request_with_app(request: ChatCompletionRequest, app_key: str) -> str:
    """
    Build a stable cache key with optional app partitioning.

    When app-thread mode is enabled, app-specific thread context can affect output,
    so app key must be part of the cache identity to avoid cross-app cache reuse.
    """
    payload = _model_dump_compat(request, mode="json", exclude={"stream", "user"})
    if Config.API_APP_THREAD_MODE and app_key:
        payload["_app_key"] = app_key
    compact_payload = _shrink_for_cache(payload)
    canonical = json.dumps(compact_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _host_from_header_url(value: str) -> str:
    """Extract normalized host:port from URL-like header values."""
    raw = (value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    host = (parsed.netloc or parsed.path or "").strip().lower()
    return host


def _normalize_key_part(value: str) -> str:
    """Normalize user/header-derived key parts for stable app routing keys."""
    cleaned = re.sub(r"\s+", " ", (value or "").strip().lower())
    return cleaned[:200]


def _derive_app_key(
    request: ChatCompletionRequest,
    http_request: Request | None,
    endpoint_app_name: str = "",
) -> str:
    from src.api.thread_routing import _derive_app_key as _impl
    return _impl(request, http_request, endpoint_app_name)


def _clone_cached_response(cached: ChatCompletionResponse) -> ChatCompletionResponse:
    """Return a fresh response object so ids/timestamps remain request-specific."""
    return _model_copy_compat(
        cached,
        deep=True,
        update={
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "created": int(time.time()),
        },
    )


def _prune_cache(now: float) -> None:
    """Prune expired entries and enforce size cap."""
    expired_keys = [key for key, (ts, _) in _response_cache.items() if now - ts > _CACHE_TTL_SECONDS]
    for key in expired_keys:
        _response_cache.pop(key, None)

    if len(_response_cache) <= _CACHE_MAX_ENTRIES:
        return

    ordered = sorted(_response_cache.items(), key=lambda item: item[1][0])
    overflow = len(_response_cache) - _CACHE_MAX_ENTRIES
    for key, _ in ordered[:overflow]:
        _response_cache.pop(key, None)


def _contract_hash(system_texts: list[str]) -> str:
    """Stable hash for thread-level system instruction contracts."""
    canonical = json.dumps(
        [_normalize_instruction_text(t) for t in system_texts if t.strip()],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _prune_thread_contracts(now: float) -> None:
    """Drop expired thread contract mappings."""
    expired = [tid for tid, (ts, _) in _thread_contracts.items() if now - ts > _CONTRACT_TTL_SECONDS]
    for tid in expired:
        _thread_contracts.pop(tid, None)
    expired_user = [tid for tid, (ts, _, _) in _thread_user_contracts.items() if now - ts > _CONTRACT_TTL_SECONDS]
    for tid in expired_user:
        _thread_user_contracts.pop(tid, None)
    expired_last = [tid for tid, (ts, _) in _thread_last_user_text.items() if now - ts > _CONTRACT_TTL_SECONDS]
    for tid in expired_last:
        _thread_last_user_text.pop(tid, None)


def _prune_app_threads(now: float) -> list[str]:
    """Drop expired app->thread mappings. Return owned thread ids eligible for deletion."""
    expired = [
        (app, mapping.thread_id, mapping.created_by_catgpt)
        for app, mapping in _app_threads.items()
        if now - mapping.last_used > _APP_THREAD_TTL_SECONDS
    ]
    for app, _, _ in expired:
        _app_threads.pop(app, None)
    # Deduplicate thread ids; one thread may be shared by multiple apps.
    seen: set[str] = set()
    expired_thread_ids: list[str] = []
    for _, tid, created_by_catgpt in expired:
        if created_by_catgpt and tid and tid not in seen:
            seen.add(tid)
            expired_thread_ids.append(tid)
    if expired:
        _save_app_threads()
    return expired_thread_ids


def _app_thread_store_path() -> Path:
    db_path = Path(str(Config.API_CONVERSATION_DB)).expanduser()
    try:
        db_path = db_path.resolve()
    except OSError:
        pass
    return db_path.parent / "app_threads.json"


def _load_app_threads() -> None:
    global _app_threads_loaded
    if _app_threads_loaded:
        return
    _app_threads_loaded = True
    path = _app_thread_store_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(raw, dict):
        return
    now = time.time()
    for app, payload in raw.items():
        if not isinstance(payload, dict):
            continue
        thread_id = str(payload.get("thread_id") or "").strip()
        if not thread_id:
            continue
        _app_threads[str(app)] = _AppThreadMapping(
            last_used=float(payload.get("last_used") or now),
            thread_id=thread_id,
            created_by_catgpt=bool(payload.get("created_by_catgpt")),
        )


def _save_app_threads() -> None:
    path = _app_thread_store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            app: {
                "last_used": mapping.last_used,
                "thread_id": mapping.thread_id,
                "created_by_catgpt": mapping.created_by_catgpt,
            }
            for app, mapping in _app_threads.items()
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        log.debug("Could not persist app-thread mappings: %s", exc)


async def _maybe_delete_expired_app_threads(thread_ids: list[str]) -> None:
    """Best-effort deletion of expired app-tracked ChatGPT threads via the web UI.

    Acquires a cleanup tab (or the process lock when the pool is down) so
    deletion cannot race an in-flight request on the same page.
    """
    if not thread_ids or not Config.API_APP_THREAD_DELETE_EXPIRED:
        return
    try:
        client = _get_client()
    except Exception:
        return

    if not isinstance(client, ChatGPTClient):
        log.debug("App-thread deletion is only supported for ChatGPT provider")
        return

    async with acquire_browser_page(CLEANUP_SESSION) as lease:
        bound = _bind_client(client, lease.page)
        for tid in thread_ids:
            try:
                ok = await bound.delete_thread(tid)
                if ok:
                    log.info(f"Deleted expired app-tracked thread: {tid}")
                else:
                    log.warning(f"Could not delete expired app-tracked thread: {tid}")
            except Exception as e:
                log.warning(f"Failed to delete app-tracked thread {tid}: {e}")


def _prune_thread_titles(now: float) -> None:
    """Drop expired thread-title mappings."""
    expired = [tid for tid, (ts, _) in _thread_titles.items() if now - ts > _THREAD_TITLE_TTL_SECONDS]
    for tid in expired:
        _thread_titles.pop(tid, None)


def _display_app_name(app_key: str) -> str:
    from src.api.thread_routing import _display_app_name as _impl
    return _impl(app_key)


async def _lookup_thread_title(client: ChatGPTClient, thread_id: str) -> str:
    """Best-effort lookup for a conversation title from sidebar threads."""
    if not thread_id:
        return ""

    now = time.time()
    async with _thread_title_lock:
        _prune_thread_titles(now)
        cached = _thread_titles.get(thread_id)
        if cached:
            return cached[1]

    try:
        threads = await client.list_threads()
    except Exception as e:
        log.debug(f"Thread title lookup skipped: {e}")
        return ""

    now = time.time()
    async with _thread_title_lock:
        _prune_thread_titles(now)
        for thread in threads:
            tid = (thread.get("id") or "").strip()
            title = (thread.get("title") or "").strip()
            if tid and title:
                _thread_titles[tid] = (now, title)
        matched = _thread_titles.get(thread_id)
        return matched[1] if matched else ""


def _build_contract_reminder_prompt(user_text: str, contract_id: str) -> str:
    """Compact prompt that reuses previously primed thread instructions."""
    short_id = contract_id[:12]
    return (
        f"[Contract {short_id}] Reuse the established instructions for this thread exactly.\n\n"
        f"{user_text}"
    )


def _build_user_contract_reminder_prompt(user_tail: str, contract_id: str, user_contract_id: str) -> str:
    """Compact prompt that reuses both system and repeated user-prefix instructions."""
    sys_id = contract_id[:12] if contract_id else "none"
    usr_id = user_contract_id[:12]
    return (
        f"[Contract {sys_id}/{usr_id}] Reuse the established instructions for this thread exactly. "
        f"Apply them to the new payload only.\n\n"
        f"{user_tail}"
    )


def _common_prefix_len(a: str, b: str) -> int:
    """Return the length of the common prefix between two strings."""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _looks_like_instruction_prefix(prefix: str) -> bool:
    """
    Heuristic: detect instruction-heavy prefix text.

    Keeps optimization conservative so we avoid compressing arbitrary repeated prose.
    """
    normalized = _normalize_instruction_text(prefix)
    markers = (
        "[system instruction",
        "you must respond",
        "respond in json",
        "follow it strictly",
        "rules are",
        "json-schema",
        "$schema",
        "<text_content>",
    )
    return any(m in normalized for m in markers)


def _detect_user_prefix_contract(prev_text: str, curr_text: str) -> tuple[str, str] | None:
    """
    Detect a repeated leading user instruction block.

    Returns (prefix, tail) when confident; otherwise None.
    """
    if not prev_text or not curr_text:
        return None

    # First, try marker-aware split for instruction + payload formats.
    # This handles cases where payload body changes heavily while instructions remain fixed.
    for marker in ("<TEXT_CONTENT>", "<INPUT>", "INPUT:"):
        i_prev = prev_text.find(marker)
        i_curr = curr_text.find(marker)
        if i_prev < 0 or i_curr < 0:
            continue
        e_prev = prev_text.find("\n", i_prev)
        e_curr = curr_text.find("\n", i_curr)
        if e_prev < 0 or e_curr < 0:
            continue
        prefix_prev = prev_text[: e_prev + 1]
        prefix_curr = curr_text[: e_curr + 1]
        if prefix_prev != prefix_curr:
            continue
        if not _looks_like_instruction_prefix(prefix_prev):
            continue
        tail = curr_text[e_curr + 1 :].strip()
        if len(tail) >= 20:
            return prefix_prev, tail

    lcp_len = _common_prefix_len(prev_text, curr_text)
    if lcp_len < 400:
        return None

    min_len = min(len(prev_text), len(curr_text))
    if min_len <= 0:
        return None
    # Keep conservative ratio by default, but allow low-ratio cases when the
    # shared prefix itself is very large and instruction-like.
    ratio = lcp_len / min_len
    if ratio < 0.5:
        if lcp_len < 1200:
            return None
        if not _looks_like_instruction_prefix(prev_text[:lcp_len]):
            return None

    candidate = prev_text[:lcp_len]
    if "\n" in candidate:
        newline_idx = candidate.rfind("\n")
        if newline_idx >= 200:
            candidate = candidate[: newline_idx + 1]

    tail = curr_text[len(candidate) :].strip()
    if len(tail) < 20:
        return None
    if not _looks_like_instruction_prefix(candidate):
        return None

    return candidate, tail


def _user_contract_hash(system_texts: list[str], user_prefix: str) -> str:
    """Stable hash for combined system+user-prefix instruction contract."""
    canonical = json.dumps(
        {
            "system": [_normalize_instruction_text(t) for t in system_texts if t.strip()],
            "user_prefix": _normalize_instruction_text(user_prefix),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _extract_content_text(content) -> str:
    from src.api.prompt_compaction import _extract_content_text as _impl
    return _impl(content)


def _normalize_instruction_text(text: str) -> str:
    """Normalize instruction text for duplicate/equivalence checks."""
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _collect_system_texts(messages: list[ChatMessage]) -> list[str]:
    """Collect non-empty system message texts."""
    texts: list[str] = []
    for msg in messages:
        if msg.role != "system":
            continue
        text = _extract_content_text(msg.content).strip()
        if text:
            texts.append(text)
    return texts


def _dedupe_system_messages(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Remove duplicate system messages while preserving order."""
    deduped: list[ChatMessage] = []
    seen: set[str] = set()

    for msg in messages:
        if msg.role != "system":
            deduped.append(msg)
            continue

        text = _extract_content_text(msg.content).strip()
        key = _normalize_instruction_text(text)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        deduped.append(msg)

    return deduped


def _looks_like_json_only_instruction(text: str) -> bool:
    """Heuristic detection for a generic JSON-only system instruction."""
    normalized = _normalize_instruction_text(text)
    return (
        "valid json only" in normalized
        and "markdown/code fences" in normalized
    )


def _has_equivalent_response_instruction(
    messages: list[ChatMessage], response_format_system: str
) -> bool:
    """Check if an equivalent structured-output instruction already exists."""
    target = _normalize_instruction_text(response_format_system)
    if not target:
        return False

    for text in _collect_system_texts(messages):
        normalized = _normalize_instruction_text(text)
        if normalized == target:
            return True

    # Generic json_object instruction can be considered equivalent even if phrasing differs.
    if "return exactly one json object" in target:
        return any(_looks_like_json_only_instruction(text) for text in _collect_system_texts(messages))

    return False


def _extract_image_urls(content) -> list[str]:
    """Extract image URLs from message content (OpenAI vision format)."""
    if not isinstance(content, list):
        return []
    urls = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "image_url":
            image_url = item.get("image_url", {})
            if isinstance(image_url, dict):
                url = image_url.get("url", "")
            else:
                url = str(image_url)
            if url:
                urls.append(url)
    return urls


def _extract_file_attachments(content) -> list[dict]:
    """
    Extract file attachments from message content.

    Supported content part format:
      {"type": "file", "file": {"filename": "test.pdf", "data": "base64...", "mime_type": "application/pdf"}}

    Also supports a shorthand data-URL style:
      {"type": "file", "file": {"filename": "test.pdf", "url": "data:application/pdf;base64,..."}}

    Returns list of dicts: [{"filename": str, "data_b64": str, "mime_type": str}, ...]
    """
    if not isinstance(content, list):
        return []
    files = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "file":
            continue
        file_info = item.get("file", {})
        if not isinstance(file_info, dict):
            continue
        filename = file_info.get("filename", "attachment")
        # Two ways to supply file data:
        # 1. data + mime_type  2. url (data-URL)
        data_b64 = file_info.get("data")
        mime_type = file_info.get("mime_type", "application/octet-stream")
        url = file_info.get("url", "")
        if not data_b64 and url.startswith("data:"):
            # Parse data URL
            try:
                header, data_b64 = url.split(",", 1)
                # header = "data:application/pdf;base64"
                if ":" in header and ";" in header:
                    mime_type = header.split(":")[1].split(";")[0]
            except ValueError:
                continue
        if data_b64:
            files.append({"filename": filename, "data_b64": data_b64, "mime_type": mime_type})
    return files


def _contains_attachment(content) -> bool:
    """Return whether OpenAI chat/responses content contains an attachment."""
    if isinstance(content, list):
        return any(_contains_attachment(item) for item in content)
    if not isinstance(content, dict):
        return False
    if content.get("type") in {"image_url", "file", "input_image", "input_file"}:
        return True
    return any(_contains_attachment(value) for value in content.values())


async def _download_file(url_or_data: str | dict, download_dir: str = "/tmp/catgpt_files") -> str | None:
    """
    Download / decode a file (image, PDF, etc.) from URL, base64 data URL,
    or a file attachment dict. Returns the local file path.
    """
    import base64
    import hashlib
    import os

    os.makedirs(download_dir, exist_ok=True)

    # -- Dict form (from _extract_file_attachments) --
    if isinstance(url_or_data, dict):
        try:
            filename = url_or_data.get("filename", "file")
            data_b64 = url_or_data["data_b64"]
            # Sanitize filename
            safe_name = re.sub(r"[^\w.\-]", "_", filename)
            hash_suffix = hashlib.md5(data_b64[:60].encode()).hexdigest()[:8]
            filepath = os.path.join(download_dir, f"{hash_suffix}_{safe_name}")
            with open(filepath, "wb") as f:
                f.write(base64.b64decode(data_b64))
            log.info(f"Decoded file attachment: {filepath}")
            return filepath
        except Exception as e:
            log.error(f"Failed to decode file attachment: {e}")
            return None

    # -- String forms --
    url = str(url_or_data)

    if url.startswith("data:"):
        # Base64 data URL: data:image/png;base64,iVBOR... or data:application/pdf;base64,...
        try:
            header, b64data = url.split(",", 1)
            # Detect extension from MIME type
            ext = "bin"
            mime = ""
            if ":" in header and ";" in header:
                mime = header.split(":")[1].split(";")[0]
            ext_map = {
                "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp",
                "image/gif": "gif", "image/tiff": "tiff", "application/pdf": "pdf",
                "text/plain": "txt", "text/csv": "csv",
                "application/json": "json",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
            }
            ext = ext_map.get(mime, mime.split("/")[-1] if "/" in mime else "bin")
            filename = f"file_{hashlib.md5(b64data[:100].encode()).hexdigest()[:12]}.{ext}"
            filepath = os.path.join(download_dir, filename)
            with open(filepath, "wb") as f:
                f.write(base64.b64decode(b64data))
            log.info(f"Decoded base64 file: {filepath}")
            return filepath
        except Exception as e:
            log.error(f"Failed to decode base64 data URL: {e}")
            return None
    elif url.startswith(("http://", "https://")):
        # HTTP URL - download it
        try:
            import urllib.request
            ext = "bin"
            for e in ["jpg", "jpeg", "webp", "gif", "png", "tif", "tiff", "pdf", "txt", "csv", "docx", "xlsx"]:
                if e in url.lower():
                    ext = e
                    break
            filename = f"file_{hashlib.md5(url.encode()).hexdigest()[:12]}.{ext}"
            filepath = os.path.join(download_dir, filename)
            urllib.request.urlretrieve(url, filepath)
            log.info(f"Downloaded file: {filepath}")
            return filepath
        except Exception as e:
            log.error(f"Failed to download file from {url}: {e}")
            return None
    elif os.path.isfile(url):
        # Local file path
        return url
    else:
        log.warning(f"Unknown file URL format: {url[:80]}")
        return None


def _build_prompt(messages: list[ChatMessage]) -> str:
    from src.api.prompt_compaction import _build_prompt as _impl
    return _impl(messages)


_GEMINI_TOOL_TRANSFORM_MARKER = "Latest request to transform:\n"


def _latest_user_text_for_gemini(messages: list[ChatMessage]) -> str:
    latest = _latest_user_message(messages)
    if latest is None:
        return ""
    raw = _extract_content_text(latest.content)
    if _GEMINI_TOOL_TRANSFORM_MARKER in raw:
        raw = raw.split(_GEMINI_TOOL_TRANSFORM_MARKER, 1)[-1]
    return extract_actionable_user_request(raw, min_chars=12) or raw.strip()


def _slim_gemini_browser_prompt(request: ChatCompletionRequest, messages: list[ChatMessage]) -> str:
    from src.api.prompt_compaction import _slim_gemini_browser_prompt as _impl
    return _impl(request, messages)


def _apply_tool_prompt_to_messages(
    messages: list[ChatMessage],
    tool_prompt: str,
) -> list[ChatMessage]:
    from src.api.prompt_compaction import _apply_tool_prompt_to_messages as _impl
    return _impl(messages, tool_prompt)


def _build_response_format_system_prompt(*args, **kwargs):
    from src.api.response_shaping import _build_response_format_system_prompt as _impl
    return _impl(*args, **kwargs)

def _page_extraction_mode(*args, **kwargs):
    from src.api.response_shaping import _page_extraction_mode as _impl
    return _impl(*args, **kwargs)

def _build_page_extraction_response_format(*args, **kwargs):
    from src.api.response_shaping import _build_page_extraction_response_format as _impl
    return _impl(*args, **kwargs)

def _build_page_extraction_note(*args, **kwargs):
    from src.api.response_shaping import _build_page_extraction_note as _impl
    return _impl(*args, **kwargs)

def _extract_json_payload(*args, **kwargs):
    from src.api.response_shaping import _extract_json_payload as _impl
    return _impl(*args, **kwargs)

def _coerce_payload_to_schema(*args, **kwargs):
    from src.api.response_shaping import _coerce_payload_to_schema as _impl
    return _impl(*args, **kwargs)

def _coerce_to_response_schema(*args, **kwargs):
    from src.api.response_shaping import _coerce_to_response_schema as _impl
    return _impl(*args, **kwargs)

def _is_effectively_empty_value(*args, **kwargs):
    from src.api.response_shaping import _is_effectively_empty_value as _impl
    return _impl(*args, **kwargs)

def _pick_note_field_name(*args, **kwargs):
    from src.api.response_shaping import _pick_note_field_name as _impl
    return _impl(*args, **kwargs)

def _header_text_from_row(*args, **kwargs):
    from src.api.response_shaping import _header_text_from_row as _impl
    return _impl(*args, **kwargs)

def _append_note(*args, **kwargs):
    from src.api.response_shaping import _append_note as _impl
    return _impl(*args, **kwargs)

def _merge_header_rows_in_array(*args, **kwargs):
    from src.api.response_shaping import _merge_header_rows_in_array as _impl
    return _impl(*args, **kwargs)

def _merge_header_rows(*args, **kwargs):
    from src.api.response_shaping import _merge_header_rows as _impl
    return _impl(*args, **kwargs)

def _normalize_structured_content(*args, **kwargs):
    from src.api.response_shaping import _normalize_structured_content as _impl
    return _impl(*args, **kwargs)

def _latest_user_text(*args, **kwargs):
    from src.api.response_shaping import _latest_user_text as _impl
    return _impl(*args, **kwargs)

def _infer_expected_item_count(*args, **kwargs):
    from src.api.response_shaping import _infer_expected_item_count as _impl
    return _impl(*args, **kwargs)

def _should_use_line_cardinality_fallback(*args, **kwargs):
    from src.api.response_shaping import _should_use_line_cardinality_fallback as _impl
    return _impl(*args, **kwargs)

def _infer_primary_array_count(*args, **kwargs):
    from src.api.response_shaping import _infer_primary_array_count as _impl
    return _impl(*args, **kwargs)

def _structured_cardinality_mismatch(*args, **kwargs):
    from src.api.response_shaping import _structured_cardinality_mismatch as _impl
    return _impl(*args, **kwargs)

def _build_cardinality_retry_prompt(*args, **kwargs):
    from src.api.response_shaping import _build_cardinality_retry_prompt as _impl
    return _impl(*args, **kwargs)



def _validate_chat_request(
    request: ChatCompletionRequest,
    *,
    fresh_thread: bool = False,
) -> None:
    """Shared validation for chat completion request payloads."""
    if not request.messages:
        raise HTTPException(status_code=400, detail="messages array cannot be empty")

    if request.thread_id and request.conversation_id:
        raise HTTPException(
            status_code=400,
            detail="thread_id and conversation_id are mutually exclusive",
        )
    if fresh_thread and (request.thread_id or request.conversation_id):
        raise HTTPException(
            status_code=400,
            detail="X-CatGPT-Thread-Mode: fresh cannot be combined with thread_id or conversation_id",
        )

    page_extraction_mode = _page_extraction_mode(request)
    if page_extraction_mode and page_extraction_mode != "structured":
        raise HTTPException(
            status_code=400,
            detail="Unsupported page_extraction.mode. Supported modes: structured",
        )

    if page_extraction_mode and request.response_format:
        raise HTTPException(
            status_code=400,
            detail="page_extraction.mode='structured' manages response_format automatically. Omit response_format.",
        )

    if Config.PROVIDER == "minimax" and any(
        _contains_attachment(message.content) for message in request.messages
    ):
        raise HTTPException(
            status_code=501,
            detail="Attachments are not supported by the MiniMax provider.",
        )

    _resolve_model_id(request.model)


def _resolve_model_id(requested: str | None) -> str:
    """Resolve and validate a model ID for the active provider."""
    if Config.PROVIDER != "chatgpt":
        try:
            return Config.resolve_model_id(requested)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not is_supported_chat_model(requested):
        supported = ", ".join(list_public_chat_models())
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported model '{requested}'. Supported models: {supported}",
        )
    return requested or Config.default_model_id()


def _resolve_app_key(
    request: ChatCompletionRequest,
    http_request: Request | None,
    endpoint_app_name: str = "",
) -> str:
    """Resolve app key when app-thread mode is enabled."""
    if not Config.API_APP_THREAD_MODE:
        return ""
    if http_request is None:
        return (endpoint_app_name or "").strip()
    return _derive_app_key(request, http_request, endpoint_app_name=endpoint_app_name)


def _resolve_image_app_key(
    request: ImageGenerationRequest,
    http_request: Request,
    endpoint_app_name: str = "",
) -> str:
    """Resolve app key for image generation when app-thread mode is enabled."""
    if not Config.API_APP_THREAD_MODE:
        return ""
    return _derive_app_key(request, http_request, endpoint_app_name=endpoint_app_name)  # type: ignore[arg-type]


async def _execute_image_generation(
    request: ImageGenerationRequest,
    app_key_override: str = "",
    http_request: Request | None = None,
) -> ImagesResponse:
    """Shared executor for generic and app-scoped image generation."""
    import base64

    if not request.prompt or not request.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt cannot be empty")

    response_format = (request.response_format or "b64_json").strip()
    if response_format not in {"b64_json", "url"}:
        raise HTTPException(status_code=400, detail="response_format must be 'b64_json' or 'url'")

    client = _get_client()
    if not hasattr(client, "generate_image"):
        raise HTTPException(status_code=422, detail="Image generation is only supported by the ChatGPT provider")

    _deletion_pending: list[str] = []
    app_key = (app_key_override or "").strip()
    session_key = _tab_session_key(request, http_request, app_key)

    try:
        async with acquire_browser_page(session_key) as lease:
            client = _bind_client(client, lease.page)
            start_time = time.time()
            app_name = _display_app_name(app_key)
            app_thread_created_by_catgpt = False

            if Config.API_APP_THREAD_MODE and app_key:
                log.info("OpenAI image app-thread key: %s", app_key)
                now_app = time.time()
                mapped_thread = ""
                expired_tids: list[str] = []
                async with _app_thread_lock:
                    _load_app_threads()
                    expired_tids = _prune_app_threads(now_app)
                    mapped = _app_threads.get(app_key)
                    if mapped:
                        mapped_thread = mapped.thread_id
                        app_thread_created_by_catgpt = mapped.created_by_catgpt
                if expired_tids:
                    _deletion_pending.extend(expired_tids)
                if mapped_thread:
                    current_tid = client._extract_thread_id()
                    if current_tid != mapped_thread:
                        log.info(f"OpenAI image app-thread mode: app='{app_key}' -> thread {mapped_thread}")
                        await client.navigate_to_thread(mapped_thread)
                else:
                    log.info(
                        "OpenAI image app-thread mode: app='%s' has no mapped thread. New chat will be created.",
                        app_name,
                    )
                    await client.new_chat()
                    app_thread_created_by_catgpt = True
            elif Config.uses_browser() and not (session_key and not lease.is_first_turn):
                new_chat = getattr(client, "new_chat", None)
                if callable(new_chat):
                    await new_chat()

            log.info(
                "POST /v1/images/generations - model=%s, prompt=%r, n=%s, size=%s, response_format=%s",
                request.model,
                request.prompt[:80],
                request.n,
                request.size,
                response_format,
            )

            try:
                result = await client.generate_image(
                    request.prompt,
                    n=int(request.n or 1),
                    size=request.size or "1024x1024",
                    quality=request.quality or "standard",
                    style=request.style or "vivid",
                )
            except Exception as e:
                log.error(f"ChatGPT error during image generation: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=f"ChatGPT error: {str(e)}")

            elapsed_ms = int((time.time() - start_time) * 1000)

            if Config.API_APP_THREAD_MODE and app_key:
                thread_for_app = result.thread_id or client._extract_thread_id()
                if thread_for_app:
                    post_prune_expired: list[str] = []
                    async with _app_thread_lock:
                        post_prune_expired = _prune_app_threads(time.time())
                        _app_threads[app_key] = _AppThreadMapping(
                            time.time(),
                            thread_for_app,
                            created_by_catgpt=app_thread_created_by_catgpt,
                        )
                        _save_app_threads()
                    if post_prune_expired:
                        _deletion_pending.extend(post_prune_expired)
                    log.info("Image app-thread mapping updated: app=%s -> thread=%s", app_name, thread_for_app)

            if not result.images:
                log.warning(
                    f"No images detected in response ({elapsed_ms}ms). "
                    f"ChatGPT replied: {result.message[:200]}"
                )
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "ChatGPT did not generate an image. "
                        f"Model response: {result.message[:500]}"
                    ),
                )

            image_data_list: list[ImageData] = []
            for img_info in result.images:
                revised_prompt = img_info.prompt_title or img_info.alt or request.prompt

                if response_format == "b64_json":
                    if img_info.local_path:
                        try:
                            with open(img_info.local_path, "rb") as f:
                                img_bytes = f.read()
                            b64 = base64.b64encode(img_bytes).decode("utf-8")
                            image_data_list.append(
                                ImageData(
                                    b64_json=b64,
                                    revised_prompt=revised_prompt,
                                )
                            )
                        except Exception as e:
                            log.error(f"Failed to read image file {img_info.local_path}: {e}")
                    else:
                        log.warning(f"Image has no local_path: {img_info.url[:80]}")
                else:
                    image_data_list.append(
                        ImageData(
                            url=img_info.local_path or img_info.url,
                            revised_prompt=revised_prompt,
                        )
                    )

            if not image_data_list:
                raise HTTPException(
                    status_code=500,
                    detail="Images were detected but could not be processed.",
                )

            log.info(
                f"Image generation complete: {len(image_data_list)} image(s), "
                f"{elapsed_ms}ms, format={response_format}"
            )

            return ImagesResponse(data=image_data_list)
    finally:
        if _deletion_pending:
            asyncio.create_task(_maybe_delete_expired_app_threads(_deletion_pending))


# -- Routes ------------------------------------------------------


@openai_router.get("/v1/models", response_model=ModelListResponse)
async def list_models() -> ModelListResponse:
    """List model IDs exposed by the active provider."""
    if Config.PROVIDER != "chatgpt":
        return ModelListResponse(
            data=[
                ModelObject(id=model_id, owned_by=Config.provider_owner())
                for model_id in Config.provider_model_ids()
            ]
        )
    if isinstance(_client, ChatGPTClient):
        try:
            async with acquire_browser_page(CONTROL_SESSION) as lease:
                bound = _bind_client(_client, lease.page)
                await bound.discover_available_models()
        except Exception as exc:
            log.warning("Could not refresh models from the live ChatGPT picker: %s", exc)
    return ModelListResponse(
        data=[ModelObject(id=model_id, owned_by="catgpt") for model_id in list_public_chat_models()]
    )


@openai_router.get("/{app_name}/v1/models", response_model=ModelListResponse)
async def list_models_scoped(app_name: str) -> ModelListResponse:
    """App-scoped alias for model listing."""
    _ = app_name
    return await list_models()


@openai_router.post("/v1/images/generations", response_model=ImagesResponse)
async def create_image(
    request: ImageGenerationRequest,
    http_request: Request,
) -> ImagesResponse:
    """OpenAI-compatible image generation endpoint."""
    app_key = _resolve_image_app_key(request, http_request)
    return await _execute_image_generation(
        request,
        app_key_override=app_key,
        http_request=http_request,
    )


@openai_router.post("/{app_name}/v1/images/generations", response_model=ImagesResponse)
async def create_image_scoped(
    app_name: str,
    request: ImageGenerationRequest,
    http_request: Request,
) -> ImagesResponse:
    """App-scoped alias for image generation."""
    app_key = _resolve_image_app_key(request, http_request, endpoint_app_name=app_name)
    return await _execute_image_generation(
        request,
        app_key_override=app_key,
        http_request=http_request,
    )


@openai_router.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def create_chat_completion(
    request: ChatCompletionRequest,
    http_request: Request = None,
) -> ChatCompletionResponse:
    """
    OpenAI-compatible chat completions endpoint.

    Converts the message array into a single prompt, sends it to ChatGPT
    via browser automation, and returns an OpenAI-formatted response.
    Supports tool/function calling via prompt injection.
    """
    fresh_thread = _fresh_thread_from_header(http_request)
    _validate_chat_request(request, fresh_thread=fresh_thread)
    app_key = _resolve_app_key(request, http_request)
    if request.stream:
        return await _stream_chat_completion(
            request,
            app_key_override=app_key,
            http_request=http_request,
            fresh_thread=fresh_thread,
        )
    return await _execute_chat_completion(
        request,
        app_key_override=app_key,
        http_request=http_request,
        fresh_thread=fresh_thread,
    )




@openai_router.post("/v1/responses", response_model=ResponsesResponse)
async def create_responses(
    request: ResponsesRequest,
    http_request: Request,
) -> ResponsesResponse:
    """OpenAI Responses API endpoint.

    Translates the request to a chat completion, executes it, and returns
    a Responses API format response. Reuses existing browser automation flow.
    """
    fresh_thread = _fresh_thread_from_header(http_request)
    _validate_responses_request(request, fresh_thread=fresh_thread)
    app_key = _resolve_app_key(request, http_request)
    if request.stream:
        return await _stream_responses(
            request,
            app_key_override=app_key,
            http_request=http_request,
            fresh_thread=fresh_thread,
        )
    return await _execute_responses(
        request,
        app_key_override=app_key,
        http_request=http_request,
        fresh_thread=fresh_thread,
    )


@openai_router.post("/{app_name}/v1/responses", response_model=ResponsesResponse)
async def create_responses_scoped(
    app_name: str,
    request: ResponsesRequest,
    http_request: Request,
) -> ResponsesResponse:
    """App-scoped alias for Responses API (maps app name from URL path)."""
    fresh_thread = _fresh_thread_from_header(http_request)
    _validate_responses_request(request, fresh_thread=fresh_thread)
    app_key = _resolve_app_key(request, http_request, endpoint_app_name=app_name)
    if request.stream:
        return await _stream_responses(
            request,
            app_key_override=app_key,
            http_request=http_request,
            fresh_thread=fresh_thread,
        )
    return await _execute_responses(
        request,
        app_key_override=app_key,
        http_request=http_request,
        fresh_thread=fresh_thread,
    )

@openai_router.post("/{app_name}/v1/chat/completions", response_model=ChatCompletionResponse)
async def create_chat_completion_scoped(
    app_name: str,
    request: ChatCompletionRequest,
    http_request: Request,
) -> ChatCompletionResponse:
    """App-scoped alias for chat completions (maps app name from URL path)."""
    fresh_thread = _fresh_thread_from_header(http_request)
    _validate_chat_request(request, fresh_thread=fresh_thread)
    app_key = _resolve_app_key(request, http_request, endpoint_app_name=app_name)
    if request.stream:
        return await _stream_chat_completion(
            request,
            app_key_override=app_key,
            http_request=http_request,
            fresh_thread=fresh_thread,
        )
    return await _execute_chat_completion(
        request,
        app_key_override=app_key,
        http_request=http_request,
        fresh_thread=fresh_thread,
    )


def _anthropic_content_to_text(content: Any) -> str:
    """Flatten Anthropic message content blocks to a prompt string."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
        return "\n".join(part for part in parts if part)
    return str(content)


def _anthropic_messages_to_chat_request(body: dict[str, Any]) -> ChatCompletionRequest:
    """Translate an Anthropic Messages body into a chat completion request."""
    messages: list[ChatMessage] = []
    system = body.get("system")
    if system:
        if isinstance(system, list):
            system_text = _anthropic_content_to_text(system)
        else:
            system_text = str(system)
        if system_text.strip():
            messages.append(ChatMessage(role="system", content=system_text))

    for item in body.get("messages") or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user")
        if role not in {"user", "assistant"}:
            role = "user"
        messages.append(ChatMessage(role=role, content=_anthropic_content_to_text(item.get("content"))))

    if not messages:
        raise HTTPException(status_code=400, detail="messages array cannot be empty")

    return ChatCompletionRequest(
        model=str(body.get("model") or Config.default_model_id()),
        messages=messages,
        max_tokens=body.get("max_tokens"),
        temperature=body.get("temperature"),
        top_p=body.get("top_p"),
        stream=bool(body.get("stream")),
        user=(body.get("user") or body.get("session_id") or None),
        thread_id=body.get("thread_id"),
    )


def _anthropic_message_payload(
    chat_response: ChatCompletionResponse,
    model_id: str,
) -> dict[str, Any]:
    text = chat_response.choices[0].message.content or ""
    prompt_tokens = chat_response.usage.prompt_tokens if chat_response.usage else 1
    completion_tokens = chat_response.usage.completion_tokens if chat_response.usage else _estimate_tokens(text)
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model_id,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
        },
    }


@openai_router.post("/v1/messages")
async def create_anthropic_message(http_request: Request):
    """Anthropic Messages API adapter for Claude Code CLI and similar clients."""
    try:
        body = await http_request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Request body must be JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")

    chat_request = _anthropic_messages_to_chat_request(body)
    _validate_chat_request(chat_request)
    app_key = _resolve_app_key(chat_request, http_request)
    chat_response = await _execute_chat_completion(
        chat_request,
        app_key_override=app_key,
        http_request=http_request,
    )
    payload = _anthropic_message_payload(chat_response, chat_response.model)

    if not chat_request.stream:
        return payload

    text = payload["content"][0]["text"]

    async def _events():
        yield (
            "event: message_start\n"
            f"data: {json.dumps({'type': 'message_start', 'message': {**payload, 'content': [], 'stop_reason': None}}, ensure_ascii=False)}\n\n"
        )
        yield (
            "event: content_block_start\n"
            f"data: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}}, ensure_ascii=False)}\n\n"
        )
        if text:
            yield (
                "event: content_block_delta\n"
                f"data: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': text}}, ensure_ascii=False)}\n\n"
            )
        yield (
            "event: content_block_stop\n"
            f"data: {json.dumps({'type': 'content_block_stop', 'index': 0}, ensure_ascii=False)}\n\n"
        )
        yield (
            "event: message_delta\n"
            f"data: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': payload['usage']['output_tokens']}}, ensure_ascii=False)}\n\n"
        )
        yield (
            "event: message_stop\n"
            f"data: {json.dumps({'type': 'message_stop'}, ensure_ascii=False)}\n\n"
        )

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# -- Responses API translation helpers --------------------------


def _responses_input_to_messages(
    input_data: str | list,
    instructions: str | None = None,
) -> list:
    """Convert Responses API input to ChatCompletionRequest messages list.

    Supports both string and list-of-input-item formats.
    Instructions field is prepended as a system message when present.
    """
    from src.api.openai_schemas import ChatMessage

    messages = []

    if instructions:
        messages.append(ChatMessage(role="system", content=instructions))

    def _normalize_content(content: Any):
        if isinstance(content, list):
            normalized_parts: list[dict[str, Any]] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type")
                if part_type in {"input_text", "text"}:
                    normalized_parts.append({"type": "text", "text": part.get("text", "")})
                elif part_type in {"input_image", "image"}:
                    image_url = part.get("image_url") or part.get("url")
                    image_b64 = part.get("image_base64") or part.get("b64_json")
                    if image_b64 and not image_url:
                        image_url = f"data:image/png;base64,{image_b64}"
                    if image_url:
                        normalized_parts.append({"type": "image_url", "image_url": {"url": image_url}})
            return normalized_parts if normalized_parts else content
        return content

    if isinstance(input_data, str):
        messages.append(ChatMessage(role="user", content=input_data))
    elif isinstance(input_data, list):
        for item in input_data:
            if isinstance(item, dict):
                role = item.get("role") or "user"
                content = _normalize_content(item.get("content"))
            else:
                role = getattr(item, "role", "user") or "user"
                content = _normalize_content(getattr(item, "content", None))
            messages.append(ChatMessage(role=role, content=content))
    else:
        messages.append(ChatMessage(role="user", content=str(input_data)))

    return messages


def _responses_request_to_chat_request(
    resp_req: ResponsesRequest,
    conversation_id: str = "",
) -> ChatCompletionRequest:
    """Translate a ResponsesRequest into a ChatCompletionRequest for execution."""
    messages = _responses_input_to_messages(resp_req.input, resp_req.instructions)

    return ChatCompletionRequest(
        model=resp_req.model,
        messages=messages,
        tools=resp_req.tools,
        tool_choice=resp_req.tool_choice,
        temperature=resp_req.temperature,
        max_tokens=resp_req.max_output_tokens,
        top_p=resp_req.top_p,
        stream=resp_req.stream if resp_req.stream is not None else False,
        user=resp_req.user,
        reasoning_effort=resp_req.reasoning.effort if resp_req.reasoning else None,
        conversation_id=conversation_id or None,
        read_aloud=bool(resp_req.read_aloud),
    )


def _responses_response_from_chat(
    chat_response: ChatCompletionResponse,
    model: str,
) -> ResponsesResponse:
    """Translate a ChatCompletionResponse into a Responses API response envelope.

    Converts choices[0].message.content into response output items.
    """
    output_items: list[ResponseOutputMessage | ResponseOutputToolCall] = []

    for choice in chat_response.choices:
        msg_text = choice.message.content or ""
        output_items.append(
            ResponseOutputMessage(
                content=[
                    ResponseOutputText(text=msg_text),
                ],
            )
        )
        tool_calls = choice.message.tool_calls or []
        for call in tool_calls:
            output_items.append(
                ResponseOutputToolCall(
                    id=call.id,
                    name=call.function.name,
                    arguments=call.function.arguments,
                )
            )

    return ResponsesResponse(
        id=f"resp_{chat_response.id.split('-', 1)[-1]}" if "-" in chat_response.id else chat_response.id,
        model=model,
        output=output_items,
        usage=ResponsesUsageInfo(
            input_tokens=chat_response.usage.prompt_tokens,
            output_tokens=chat_response.usage.completion_tokens,
            total_tokens=chat_response.usage.total_tokens,
        ),
    )


def _responses_sse_event(event: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


def _chat_completion_sse_chunk(
    response: ChatCompletionResponse,
    delta: dict[str, Any],
    *,
    finish_reason: str | None = None,
) -> str:
    """Format one OpenAI chat.completion.chunk SSE data line."""
    chunk = {
        "id": response.id,
        "object": "chat.completion.chunk",
        "created": response.created,
        "model": response.model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    payload = json.dumps(chunk, ensure_ascii=False, separators=(",", ":"))
    return f"data: {payload}\n\n"


async def _stream_chat_completion(
    request: ChatCompletionRequest,
    app_key_override: str = "",
    http_request: Request | None = None,
    fresh_thread: bool = False,
) -> StreamingResponse:
    """Return a Chat Completions SSE stream after the browser response completes.

    Browser automation cannot provide token deltas, but IDE clients (OpenCode,
    Cline, etc.) send stream=true and require text/event-stream. Emit the full
    assistant message as one or two chunks plus a terminal finish chunk.

    Run the browser call before opening the SSE body so failures return HTTP
    errors instead of a truncated chunked stream (Copilot: ERR_INCOMPLETE_CHUNKED_ENCODING).
    """
    non_stream_request = request.model_copy(update={"stream": False})
    response = await _execute_chat_completion(
        non_stream_request,
        app_key_override=app_key_override,
        http_request=http_request,
        fresh_thread=fresh_thread,
    )

    async def _events():
        choice = response.choices[0]
        message = choice.message

        yield _chat_completion_sse_chunk(
            response,
            {"role": "assistant", "content": ""},
        )

        if message.content:
            yield _chat_completion_sse_chunk(response, {"content": message.content})

        if message.tool_calls:
            tool_deltas: list[dict[str, Any]] = []
            for index, call in enumerate(message.tool_calls):
                tool_deltas.append(
                    {
                        "index": index,
                        "id": call.id,
                        "type": call.type,
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                )
            yield _chat_completion_sse_chunk(response, {"tool_calls": tool_deltas})

        yield _chat_completion_sse_chunk(
            response,
            {},
            finish_reason=choice.finish_reason,
        )
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _stream_responses(
    request: ResponsesRequest,
    app_key_override: str = "",
    http_request: Request | None = None,
    fresh_thread: bool = False,
) -> StreamingResponse:
    """Return a Responses API SSE stream after the browser response completes.

    Browser automation cannot provide token deltas, but some clients (notably chat
    UIs) send stream=true and require an event-stream response. Emit one full-text
    delta plus the completed response so those clients can consume the result.

    Run the browser call before opening the SSE body so failures return HTTP errors.
    """
    response = await _execute_responses(
        request,
        app_key_override=app_key_override,
        http_request=http_request,
        fresh_thread=fresh_thread,
    )

    async def _events():
        response_dict = _model_dump_compat(response, mode="json")
        text = ""
        for item in response.output:
            if isinstance(item, ResponseOutputMessage):
                text += "".join(part.text for part in item.content)

        if text:
            yield _responses_sse_event(
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "response_id": response.id,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": text,
                },
            )

        yield _responses_sse_event(
            "response.completed",
            {
                "type": "response.completed",
                "response": response_dict,
            },
        )
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _validate_responses_request(
    request: ResponsesRequest,
    *,
    fresh_thread: bool = False,
) -> None:
    """Validate a Responses API request."""
    if not request.input:
        raise HTTPException(status_code=400, detail="input cannot be empty")

    if request.conversation and request.previous_response_id:
        raise HTTPException(
            status_code=400,
            detail="conversation and previous_response_id are mutually exclusive",
        )

    if fresh_thread and (request.conversation or request.previous_response_id):
        raise HTTPException(
            status_code=400,
            detail="X-CatGPT-Thread-Mode: fresh cannot be combined with conversation or previous_response_id",
        )

    if request.conversation is not None and not _responses_conversation_id(request.conversation):
        raise HTTPException(status_code=400, detail="conversation must contain a non-empty id")

    _resolve_model_id(request.model)


def _fresh_thread_from_header(http_request: Request | None) -> bool:
    if http_request is None:
        return False
    value = (http_request.headers.get(_THREAD_MODE_HEADER) or "").strip().lower()
    if not value:
        return False
    if value != "fresh":
        raise HTTPException(
            status_code=400,
            detail="Unsupported X-CatGPT-Thread-Mode. Supported value: fresh",
        )
    return True


async def _prepare_conversation_routing(
    client: Any,
    request: ChatCompletionRequest,
    *,
    app_key: str,
    conversation_key: str,
    seed_transcript: tuple[dict[str, Any], ...] | None = None,
) -> _ConversationRouting:
    """Resolve one logical conversation to a verified browser thread."""
    store = _get_conversation_store()
    project_key = _project_key()
    namespace = app_key or "default"
    existing = store.get_route(project_key, namespace, conversation_key)
    incoming = [_canonical_message(message) for message in request.messages]
    incoming_hashes = [_message_hash(message) for message in incoming]
    contract_hash = _request_contract_hash(request)
    messages_for_browser = list(request.messages)
    transcript_input = list(incoming)
    action = "new-conversation"

    if existing is None and seed_transcript:
        transcript_input = [*map(dict, seed_transcript), *incoming]
        messages_for_browser = _messages_from_transcript(transcript_input)
        await client.new_chat()
        action = "new-response-branch"
    elif existing is None:
        await client.new_chat()
    else:
        existing_hashes = list(existing.message_hashes)
        prefix_match = (
            len(incoming_hashes) >= len(existing_hashes)
            and incoming_hashes[: len(existing_hashes)] == existing_hashes
        )
        non_instruction = [
            message for message in request.messages
            if message.role not in {"system", "developer"}
        ]
        delta_shaped = bool(
            0 < len(non_instruction) <= 1
            and non_instruction[-1].role in {"user", "tool"}
        )
        has_contract = bool(
            any(message.role in {"system", "developer"} for message in request.messages)
            or request.tools or request.response_format
        )
        if delta_shaped and not has_contract:
            contract_hash = existing.contract_hash

        client_fresh = not any(message.role in {"assistant", "tool"} for message in request.messages)
        provider_has_history = any(
            str(item.get("role") or "") in {"assistant", "tool"}
            for item in existing.transcript
        )
        if (
            conversation_key.startswith("derived:")
            and client_fresh
            and provider_has_history
        ):
            # Copilot "New chat" often starts with the same first words (e.g. hello),
            # so the derived id matches. The client history has no assistant turns.
            await client.new_chat()
            action = "new-chat-client-reset"
            existing = None
        elif (
            existing.contract_hash
            and existing.contract_hash != contract_hash
            and not conversation_key.startswith("derived:")
        ):
            reconstructed = incoming if len(incoming) > 2 else [
                dict(message) for message in existing.transcript
                if message.get("role") not in {"system", "developer"}
            ] + incoming
            transcript_input = reconstructed
            messages_for_browser = _messages_from_transcript(reconstructed)
            await client.new_chat()
            action = "new-chat-contract-change"
            existing = None
        elif prefix_match:
            additions = incoming[len(existing_hashes):]
            if not additions:
                raise HTTPException(
                    status_code=400,
                    detail="conversation request does not contain a new message",
                )
            transcript_input = incoming
            messages_for_browser = _messages_from_transcript(additions)
            action = "verified-prefix-delta"
        elif delta_shaped:
            additions = [_canonical_message(message) for message in non_instruction]
            transcript_input = [*map(dict, existing.transcript), *additions]
            messages_for_browser = list(non_instruction)
            action = "single-turn-delta"
        elif (
            conversation_key.startswith("derived:")
            and non_instruction
            and non_instruction[-1].role in {"user", "tool"}
        ):
            # Copilot resends the full agent dump every turn; hashes never prefix-match.
            latest = non_instruction[-1]
            additions = [_canonical_message(latest)]
            transcript_input = [*map(dict, existing.transcript), *additions]
            messages_for_browser = [latest]
            action = "derived-replay-delta"
        else:
            await client.new_chat()
            action = "new-chat-history-diverged"
            existing = None

        if existing is not None:
            current_thread = client._extract_thread_id()
            if current_thread != existing.thread_id:
                try:
                    await client.navigate_to_thread(existing.thread_id)
                except Exception as exc:
                    log.warning("Discarding stale conversation mapping %s: %s", conversation_key, exc)
                    store.delete_route(project_key, namespace, conversation_key)
                    await client.new_chat()
                    messages_for_browser = _messages_from_transcript(transcript_input)
                    action = "new-chat-stale-mapping"
                    existing = None

    return _ConversationRouting(
        project_key=project_key, app_key=namespace, conversation_key=conversation_key,
        previous_route=existing, transcript_input=transcript_input,
        messages_for_browser=messages_for_browser, contract_hash=contract_hash,
        action=action,
    )


async def _execute_responses(
    request: ResponsesRequest,
    app_key_override: str = "",
    http_request: Request | None = None,
    fresh_thread: bool = False,
) -> ResponsesResponse:
    """Shared executor for Responses API requests.

    Translates to ChatCompletionRequest, delegates to _execute_chat_completion,
    and translates back to Responses API format.
    """
    app_key = (app_key_override or "").strip() or "default"
    conversation_id = _responses_conversation_id(request.conversation)
    seed_transcript: tuple[dict[str, Any], ...] | None = None
    if request.previous_response_id:
        previous = _get_conversation_store().get_response(request.previous_response_id)
        if previous is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown previous_response_id '{request.previous_response_id}'",
            )
        if previous.project_key != _project_key() or previous.app_key != app_key:
            raise HTTPException(
                status_code=400,
                detail="previous_response_id belongs to a different app or ChatGPT project",
            )
        current = _get_conversation_store().get_route(
            previous.project_key, previous.app_key, previous.conversation_key
        )
        if current and current.revision == previous.revision and current.message_hashes == previous.message_hashes:
            conversation_id = previous.conversation_key
        else:
            conversation_id = f"response-branch:{request.previous_response_id}:{uuid.uuid4().hex[:12]}"
            seed_transcript = previous.transcript
    if not conversation_id:
        conversation_id = f"response-chain:{uuid.uuid4().hex}"

    chat_request = _responses_request_to_chat_request(request, conversation_id=conversation_id)
    chat_request.stream = False
    chat_response = await _execute_chat_completion(
        chat_request,
        app_key_override=app_key_override,
        http_request=http_request,
        seed_transcript=seed_transcript,
        capture_route_outcome=True,
        fresh_thread=fresh_thread,
    )
    response = _responses_response_from_chat(chat_response, request.model)
    route = _completion_route_outcomes.pop(chat_response.id, None)
    if route is not None and request.store is not False:
        _get_conversation_store().save_response(response.id, route)
    elif route is not None and request.store is False and request.conversation is None and not request.previous_response_id:
        _get_conversation_store().delete_route(route.project_key, route.app_key, route.conversation_key)
    return response

async def _execute_chat_completion(
    request: ChatCompletionRequest,
    app_key_override: str = "",
    http_request: Request | None = None,
    seed_transcript: tuple[dict[str, Any], ...] | None = None,
    capture_route_outcome: bool = False,
    fresh_thread: bool = False,
) -> ChatCompletionResponse:
    """Shared sync/async executor for chat completions."""
    trace_id = trace_id_from_request(http_request, f"round-{uuid.uuid4().hex}")
    record_round_trace(trace_id, "request", {
        "app_key": app_key_override,
        "fresh_thread": fresh_thread,
        "request": _model_dump_compat(request),
    })
    client = _get_client()
    model_id = _resolve_model_id(request.model)
    app_key = (app_key_override or "").strip()
    header_conversation_id = _conversation_id_from_request(request, http_request)
    if header_conversation_id and request.thread_id:
        raise HTTPException(
            status_code=400,
            detail="thread_id and conversation_id are mutually exclusive",
        )
    if fresh_thread and header_conversation_id:
        raise HTTPException(
            status_code=400,
            detail="X-CatGPT-Thread-Mode: fresh cannot be combined with conversation_id",
        )
    if header_conversation_id and header_conversation_id != (request.conversation_id or ""):
        request = _model_copy_compat(
            request, deep=True, update={"conversation_id": header_conversation_id}
        )
    request = _apply_isolated_conversation_id(request, http_request, fresh_thread)
    session_key = None if fresh_thread else _tab_session_key(request, http_request, app_key)

    # Track expired thread ids to delete after this request releases its tab.
    _deletion_pending: list[str] = []
    disconnect_watch = start_disconnect_watch(http_request)
    lease_held = False

    try:
        async with acquire_browser_page(session_key) as lease:
            lease_held = True
            client = _bind_client(client, lease.page)
            start_time = time.time()
            app_name = _display_app_name(app_key)
            explicit_thread_id = (request.thread_id or "").strip() if getattr(request, "thread_id", None) else ""
            conversation_key = (request.conversation_id or "").strip()
            conversation_routing: _ConversationRouting | None = None
            routing_action = "reuse-current"
            continuing_thread = False
            app_thread_created_by_catgpt = False
            if Config.API_APP_THREAD_MODE and app_key:
                log.info("OpenAI app-thread key: %s", app_key)

            if fresh_thread:
                await client.new_chat()
                routing_action = "new-chat-fresh-request"
                app_thread_created_by_catgpt = True
            elif explicit_thread_id:
                current_tid = client._extract_thread_id()
                if current_tid != explicit_thread_id:
                    log.info(f"OpenAI route: navigating to explicit thread {explicit_thread_id}")
                    await client.navigate_to_thread(explicit_thread_id)
                routing_action = "explicit-thread"
                continuing_thread = True
            elif conversation_key and _supports_thread_navigation(client):
                conversation_routing = await _prepare_conversation_routing(
                    client,
                    request,
                    app_key=app_key,
                    conversation_key=conversation_key,
                    seed_transcript=seed_transcript,
                )
                routing_action = conversation_routing.action
                continuing_thread = conversation_routing.previous_route is not None
                request = _model_copy_compat(
                    request,
                    deep=True,
                    update={"messages": conversation_routing.messages_for_browser},
                )
                log.info(
                    "Conversation route: app=%s conversation=%s action=%s",
                    app_name,
                    conversation_key,
                    routing_action,
                )
            elif Config.API_APP_THREAD_MODE and app_key:
                now_app = time.time()
                mapped_thread = ""
                expired_tids: list[str] = []
                async with _app_thread_lock:
                    _load_app_threads()
                    expired_tids = _prune_app_threads(now_app)
                    mapped = _app_threads.get(app_key)
                    if mapped:
                        mapped_thread = mapped.thread_id
                        app_thread_created_by_catgpt = mapped.created_by_catgpt
                if expired_tids:
                    _deletion_pending.extend(expired_tids)
                if mapped_thread:
                    current_tid = client._extract_thread_id()
                    if current_tid != mapped_thread:
                        log.info(f"OpenAI app-thread mode: app='{app_key}' -> thread {mapped_thread}")
                        await client.navigate_to_thread(mapped_thread)
                    routing_action = "mapped-thread"
                    continuing_thread = True
                    _app_fresh_chats.discard(app_key)
                elif app_key in _app_fresh_chats:
                    log.info(
                        "OpenAI app-thread mode: app='%s' reusing the in-progress new chat "
                        "(system prompt still included until the thread is mapped)",
                        app_name,
                    )
                    routing_action = "reuse-new-chat-for-app"
                    continuing_thread = False
                else:
                    log.info(
                        "OpenAI app-thread mode: app='%s' has no mapped thread. New chat will be created before prompt send.",
                        app_name,
                    )
                    await client.new_chat()
                    routing_action = "new-chat-for-app"
                    app_thread_created_by_catgpt = True
                    _app_fresh_chats.add(app_key)
                    early_tid = client._extract_thread_id()
                    if early_tid:
                        async with _app_thread_lock:
                            _app_threads[app_key] = _AppThreadMapping(
                                time.time(),
                                early_tid,
                                created_by_catgpt=True,
                            )
                            _save_app_threads()
                        routing_action = "mapped-thread"
                        continuing_thread = False
                        _app_fresh_chats.discard(app_key)
            elif session_key and not lease.is_first_turn:
                routing_action = "persistent-session"
                continuing_thread = True
            elif Config.uses_browser():
                log.info("No session identity: starting a fresh ChatGPT thread")
                await client.new_chat()
                routing_action = "new-chat-stateless"
            else:
                routing_action = "stateless-api"

            # -- Extract attachments that are new on this user turn --
            image_paths: list[str] = []
            file_paths: list[str] = []
            image_urls, file_attachments = _new_attachments_from_latest_user(list(request.messages))
            for url in image_urls:
                local_path = await _download_file(url)
                if local_path:
                    image_paths.append(local_path)
            for fa in file_attachments:
                local_path = await _download_file(fa)
                if local_path:
                    file_paths.append(local_path)

            all_attachment_paths = image_paths + file_paths
            if all_attachment_paths:
                log.info(
                    "Extracted %d new image(s) and %d new file(s) from the latest user turn",
                    len(image_paths),
                    len(file_paths),
                )

            expansion = expand_attachments_for_chatgpt(image_paths, file_paths)
            image_paths = expansion.image_paths
            file_paths = expansion.file_paths
            page_extraction_mode = _page_extraction_mode(request)
            page_extraction_expected_count: int | None = None
            effective_response_format = request.response_format
            prompt_prefixes: list[str] = []

            if expansion.notes:
                prompt_prefixes.append(build_attachment_context_note(expansion.notes))
                log.info(
                    "Attachment expansion: rendered %d page image(s), upload set now has %d image(s) and %d file(s)",
                    expansion.total_rendered_pages,
                    len(image_paths),
                    len(file_paths),
                )

            if page_extraction_mode == "structured":
                if not expansion.page_descriptors:
                    raise HTTPException(
                        status_code=400,
                        detail="page_extraction.mode='structured' requires at least one image or file attachment.",
                    )
                page_extraction_expected_count = len(expansion.page_descriptors)
                effective_response_format = _build_page_extraction_response_format(expansion.page_descriptors)
                prompt_prefixes.append(_build_page_extraction_note(expansion.page_descriptors))
                log.info(
                    "Per-page extraction mode enabled: %d logical page item(s)",
                    page_extraction_expected_count,
                )

            attachment_prefix = "".join(prefix for prefix in prompt_prefixes if prefix)

            # -- Build the prompt --------------------------------
            messages = list(request.messages)

            # If structured output is requested, force strict JSON response
            response_format_system = _build_response_format_system_prompt(effective_response_format)
            if response_format_system and not _has_equivalent_response_instruction(messages, response_format_system):
                messages.insert(0, ChatMessage(role="system", content=response_format_system))

            messages = _dedupe_system_messages(messages)
            if continuing_thread:
                pruned = _latest_turn_messages(messages, include_system=False)
                if len(pruned) < len(messages):
                    log.info(
                        "Pruned conversation history for existing thread: %d -> %d messages (system omitted)",
                        len(messages),
                        len(pruned),
                    )
                    messages = pruned
                messages = [
                    _compact_followup_user_message(message) if message.role == "user" else message
                    for message in messages
                ]
                log.info(
                    "Follow-up prompt compacted for existing thread: %d chars",
                    sum(len(_extract_content_text(m.content)) for m in messages),
                )

            if Config.uses_browser():
                messages = [_compact_tool_result_message(message) for message in messages]

            # Tool definitions are request-scoped. Apply them after pruning and
            # compaction so every turn sees the current Cursor tool contract.
            # Applying them earlier lets _compact_followup_user_message discard
            # the wrapper while extracting Cursor's <user_query> payload.
            if request.tools and request.tool_choice != "none":
                tool_system = (
                    _build_tool_continuation_prompt(request.tools, request.tool_choice)
                    if continuing_thread
                    else _build_tool_system_prompt(request.tools, request.tool_choice)
                )
                if tool_system:
                    messages = _apply_tool_prompt_to_messages(messages, tool_system)
            system_texts = [_extract_content_text(m.content) for m in messages if m.role == "system"]
            system_lengths = [len(t) for t in system_texts]
            system_previews = [_normalize_instruction_text(t)[:120] for t in system_texts]
            non_system = [m for m in messages if m.role != "system"]
            full_prompt = _build_prompt(messages)
            prompt = full_prompt
            used_thread_contract = False
            used_user_contract = False
            extract_thread_id = getattr(client, "_extract_thread_id", None)
            current_thread_id = extract_thread_id() if callable(extract_thread_id) else ""
            current_chat_name = await _lookup_thread_title(client, current_thread_id) if current_thread_id else ""
            chat_display = current_chat_name or ("New chat" if not current_thread_id else "Unknown")
            log.info(
                "Request context: app=%s, route=%s, thread=%s, chat_name=%s",
                app_name,
                routing_action,
                current_thread_id or "<new>",
                chat_display,
            )
            contract_id = ""
            user_contract_id = ""
            user_text = _extract_content_text(non_system[0].content) if len(non_system) == 1 else ""

            if (
                Config.API_THREAD_CONTRACT_MODE
                and system_texts
                and len(non_system) == 1
                and non_system[0].role == "user"
            ):
                contract_id = _contract_hash(system_texts)
                if current_thread_id:
                    now_contract = time.time()
                    async with _contract_lock:
                        _prune_thread_contracts(now_contract)
                        known = _thread_contracts.get(current_thread_id)
                        if known and known[1] == contract_id:
                            prompt = _build_contract_reminder_prompt(user_text, contract_id)
                            used_thread_contract = True
                            _thread_contracts[current_thread_id] = (now_contract, contract_id)

                        # Learn repeated user-instruction prefixes across turns.
                        prev_user = _thread_last_user_text.get(current_thread_id)
                        detected_prefix = None
                        if prev_user:
                            detected_prefix = _detect_user_prefix_contract(prev_user[1], user_text)
                        if detected_prefix:
                            prefix, tail = detected_prefix
                            candidate_user_contract_id = _user_contract_hash(system_texts, prefix)
                            known_user = _thread_user_contracts.get(current_thread_id)
                            if known_user and known_user[1] == candidate_user_contract_id and effective_response_format:
                                prompt = _build_user_contract_reminder_prompt(
                                    tail,
                                    contract_id,
                                    candidate_user_contract_id,
                                )
                                used_user_contract = True
                                used_thread_contract = False
                            # Store/refresh learned prefix contract for future turns.
                            _thread_user_contracts[current_thread_id] = (
                                now_contract,
                                candidate_user_contract_id,
                                prefix,
                            )
                            user_contract_id = candidate_user_contract_id

                        _thread_last_user_text[current_thread_id] = (now_contract, user_text)

            system_chars = sum(len(_extract_content_text(m.content)) for m in messages if m.role == "system")
            user_chars = sum(len(_extract_content_text(m.content)) for m in messages if m.role == "user")
            assistant_chars = sum(len(_extract_content_text(m.content)) for m in messages if m.role == "assistant")
            tool_chars = sum(len(_extract_content_text(m.content)) for m in messages if m.role == "tool")
            log.debug(
                "System prompt breakdown: count=%d lengths=%s previews=%s",
                len(system_texts),
                system_lengths,
                system_previews,
            )
            log.info(
                f"POST /v1/chat/completions - model={request.model}, "
                f"{len(request.messages)} messages, prompt={len(prompt)} chars "
                f"(system={system_chars}, user={user_chars}, assistant={assistant_chars}, tool={tool_chars})"
            )
            if isinstance(client, GeminiClient):
                slim = _slim_gemini_browser_prompt(request, messages)
                if slim and slim != prompt:
                    log.info("Gemini browser prompt slimmed %d -> %d chars", len(prompt), len(slim))
                    prompt = slim
            elif (
                isinstance(client, ChatGPTClient)
                and continuing_thread
                and Config.CHATGPT_LONG_PROMPT_THRESHOLD
                and len(prompt) >= Config.CHATGPT_LONG_PROMPT_THRESHOLD
            ):
                slim = _slim_gemini_browser_prompt(request, messages)
                if slim and slim != prompt:
                    log.info(
                        "ChatGPT follow-up slimmed %d -> %d chars (thread already primed)",
                        len(prompt),
                        len(slim),
                    )
                    prompt = slim
            if used_thread_contract:
                log.info("Thread contract mode: compact prompt used for thread=%s", current_thread_id or "unknown")
            if used_user_contract:
                log.info(
                    "User prefix contract mode: compact prompt used for thread=%s contract=%s",
                    current_thread_id or "unknown",
                    user_contract_id[:12] if user_contract_id else "unknown",
                )

            if attachment_prefix:
                prompt = f"{attachment_prefix}{prompt}" if prompt else attachment_prefix.strip()
                full_prompt = f"{attachment_prefix}{full_prompt}" if full_prompt else attachment_prefix.strip()
            record_round_trace(trace_id, "composer_prompt", {
                "routing_action": routing_action,
                "prompt": prompt,
                "attachment_names": [Path(path).name for path in [*image_paths, *file_paths]],
            })

            cache_key = _cache_key_for_request_with_app(request, app_key)
            stateful_request = bool(
                fresh_thread
                or conversation_routing
                or explicit_thread_id
                or (Config.API_APP_THREAD_MODE and app_key)
                or session_key
            )
            if not stateful_request:
                now = time.time()
                async with _cache_lock:
                    _prune_cache(now)
                    cached_entry = _response_cache.get(cache_key)
                    if cached_entry and now - cached_entry[0] <= _CACHE_TTL_SECONDS:
                        log.info("Response cache hit: returning cached completion")
                        return _clone_cached_response(cached_entry[1])

            # -- Send to ChatGPT --------------------------------
            try:
                reasoning_kwargs = (
                    {"reasoning_effort": _chat_reasoning_effort(request)}
                    if isinstance(client, ChatGPTClient)
                    else {}
                )
                send_kwargs = {
                    "image_paths": image_paths or None,
                    "file_paths": file_paths or None,
                    "model": model_id,
                    **reasoning_kwargs,
                }
                if Config.uses_browser():
                    send_kwargs["read_aloud"] = bool(request.read_aloud)
                else:
                    send_kwargs["stateless"] = True
                result = await client.send_message(prompt, **send_kwargs)
            except PromptTooLongError as e:
                log.warning("ChatGPT rejected an oversized prompt: %s", e)
                raise HTTPException(status_code=413, detail=str(e)) from e
            except PromptAttachmentFallbackError as e:
                log.error("Long-prompt attachment fallback failed: %s", e, exc_info=True)
                raise HTTPException(status_code=502, detail=str(e)) from e
            except Exception as e:
                log.error(f"ChatGPT error: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=f"ChatGPT error: {str(e)}")

            if Config.API_THREAD_CONTRACT_MODE and contract_id:
                thread_for_contract = result.thread_id or current_thread_id or client._extract_thread_id()
                if thread_for_contract:
                    async with _contract_lock:
                        _prune_thread_contracts(time.time())
                        _thread_contracts[thread_for_contract] = (time.time(), contract_id)
                        # Refresh user text tracking on resolved thread id too.
                        if user_text:
                            _thread_last_user_text[thread_for_contract] = (time.time(), user_text)

            if Config.API_APP_THREAD_MODE and app_key:
                thread_for_app = result.thread_id or client._extract_thread_id()
                if thread_for_app:
                    post_prune_expired: list[str] = []
                    async with _app_thread_lock:
                        post_prune_expired = _prune_app_threads(time.time())
                        _app_threads[app_key] = _AppThreadMapping(
                            time.time(),
                            thread_for_app,
                            created_by_catgpt=app_thread_created_by_catgpt,
                        )
                        _save_app_threads()
                    _app_fresh_chats.discard(app_key)
                    # Best-effort deletion is scheduled after this request releases
                    # its tab lease, so cleanup cannot navigate mid-request.
                    if post_prune_expired:
                        _deletion_pending.extend(post_prune_expired)
                    thread_title = await _lookup_thread_title(client, thread_for_app)
                    log.info(
                        "App-thread mapping updated: app=%s -> thread=%s (%s)",
                        app_name,
                        thread_for_app,
                        thread_title or "title unavailable",
                    )

            response_text = result.message
            record_round_trace(trace_id, "provider_response", {"text": response_text})
            elapsed_ms = int((time.time() - start_time) * 1000)

            if (used_thread_contract or used_user_contract) and effective_response_format and _extract_json_payload(response_text) is None:
                mode_name = "user-prefix contract" if used_user_contract else "thread contract"
                log.warning("%s mode produced non-JSON content. Retrying once with full prompt.", mode_name.capitalize())
                try:
                    full_retry = await client.send_message(
                        full_prompt,
                        image_paths=image_paths or None,
                        file_paths=file_paths or None,
                        model=model_id,
                        **reasoning_kwargs,
                    )
                    response_text = full_retry.message
                    elapsed_ms = int((time.time() - start_time) * 1000)
                except Exception as e:
                    log.warning(f"Full-prompt fallback after contract mode failed: {e}")

            # -- Detect echo (extraction grabbed sent prompt instead of reply) --
            if response_text and "[System instruction:" in response_text and request.tools:
                log.warning("Response appears to echo the sent prompt - retrying extraction")
                try:
                    await asyncio.sleep(max(0.2, Config.RESPONSE_SETTLE_MS / 1000))
                    from src.chatgpt.detector import extract_last_response_via_copy

                    retry_text = await extract_last_response_via_copy(client.page)
                    if retry_text and "[System instruction:" not in retry_text:
                        response_text = retry_text
                        log.info(f"Retry extraction succeeded: {len(response_text)} chars")
                    else:
                        log.warning("Retry extraction still echoed - stripping system prefix")
                        idx = response_text.rfind("\n\n")
                        if idx > 0:
                            tail = response_text[idx:].strip()
                            if tail and not tail.startswith("["):
                                response_text = tail
                except Exception as e:
                    log.warning(f"Retry extraction failed: {e}")

            # -- Check for tool calls ----------------------------
            tool_calls = None
            finish_reason = "stop"

            if effective_response_format and response_text:
                response_text = _normalize_structured_content(response_text, effective_response_format)
                mismatch = _structured_cardinality_mismatch(
                    request.messages,
                    response_text,
                    expected_count=page_extraction_expected_count,
                )
                if mismatch:
                    expected, actual = mismatch
                    log.warning(
                        "Structured cardinality mismatch detected (expected=%d, actual=%d). Retrying once.",
                        expected,
                        actual,
                    )
                    retry_prompt = _build_cardinality_retry_prompt(
                        prompt,
                        expected,
                        actual,
                        expectation_reason="the numbered page map" if page_extraction_expected_count is not None else "input line count",
                        correction_rule=(
                            "Return valid JSON only, with exactly one output item per numbered page-map entry, "
                            "preserving `page_index`, `source_name`, and `page_number` exactly and keeping the same order. "
                            "Do not merge, drop, or add pages."
                        )
                        if page_extraction_expected_count is not None
                        else None,
                    )
                    try:
                        retry_result = await client.send_message(
                            retry_prompt,
                            image_paths=image_paths or None,
                            file_paths=file_paths or None,
                            model=model_id,
                            **reasoning_kwargs,
                        )
                        retry_text = retry_result.message
                        retry_text = _normalize_structured_content(retry_text, effective_response_format)
                        retry_mismatch = _structured_cardinality_mismatch(
                            request.messages,
                            retry_text,
                            expected_count=page_extraction_expected_count,
                        )
                        if not retry_mismatch:
                            response_text = retry_text
                            elapsed_ms = int((time.time() - start_time) * 1000)
                            log.info("Structured cardinality retry succeeded")
                        else:
                            log.warning(
                                "Structured cardinality retry still mismatched (expected=%d, actual=%d)",
                                retry_mismatch[0],
                                retry_mismatch[1],
                            )
                    except Exception as e:
                        log.warning(f"Structured cardinality retry failed: {e}")

            if request.tools and request.tool_choice != "none":
                parse_outcome = _parse_tool_calls_outcome(response_text, request.tools)
                tool_calls = parse_outcome.calls
                intended = parse_outcome.has_tool_intent
                validation_errors: list[str] = []
                gemini_final: str | None = None
                if tool_calls:
                    validation_errors.extend(
                        validate_tool_calls(tool_calls, request.tools, request.tool_choice)
                    )
                    if isinstance(client, GeminiClient) and not any(
                        "unknown" in error for error in validation_errors
                    ):
                        if validation_errors:
                            log.info(
                                "Gemini tool_calls accepted despite schema warnings: %s",
                                "; ".join(validation_errors[:6]),
                            )
                        validation_errors = []
                elif isinstance(client, GeminiClient):
                    gemini_final = parse_gemini_final_response(response_text)
                    if gemini_final is not None and not tool_call_expected(request.tool_choice):
                        response_text = gemini_final
                    else:
                        validation_errors.extend(parse_outcome.diagnostics)
                        if looks_like_workspace_refusal(response_text):
                            validation_errors.append("replied with a workspace-access refusal")
                        if not validation_errors:
                            validation_errors.append(
                                "response was neither valid tool_calls nor a valid final JSON envelope"
                            )
                else:
                    # Providers may echo the shared tool protocol envelope even
                    # when they are not Gemini. Unwrap an exact {"final": ...}
                    # response before returning it to editor clients such as VS
                    # Code; otherwise the JSON transport envelope is rendered as
                    # the assistant's visible message.
                    generic_final = parse_gemini_final_response(response_text)
                    if generic_final is not None and not tool_call_expected(request.tool_choice):
                        response_text = generic_final
                    else:
                        validation_errors.extend(parse_outcome.diagnostics)
                        if (intended or tool_call_expected(request.tool_choice)) and not validation_errors:
                            validation_errors.append("no valid tool calls were decoded")
                if validation_errors:
                    metrics.increment("tool_translation.repair_attempted")
                    if isinstance(client, GeminiClient):
                        repair_prompt = _build_gemini_tool_repair_prompt(
                            response_text,
                            request.tools,
                            user_text=_latest_user_text_for_gemini(list(request.messages)),
                            tool_choice=request.tool_choice,
                        )
                    else:
                        repair_prompt = build_tool_repair_prompt(
                            response_text,
                            validation_errors,
                            request.tools,
                            request.tool_choice,
                        )
                    record_round_trace(trace_id, "tool_translation", {
                        "status": "repairing",
                        "errors": validation_errors,
                    })
                    try:
                        repair_result = await client.send_message(
                            repair_prompt,
                            model=model_id,
                            **reasoning_kwargs,
                        )
                        repaired_text = repair_result.message
                        repair_outcome = _parse_tool_calls_outcome(repaired_text, request.tools)
                        repaired_calls = repair_outcome.calls
                        repaired_final = (
                            parse_gemini_final_response(repaired_text)
                            if isinstance(client, GeminiClient)
                            else None
                        )
                        repair_errors: list[str] = []
                        if repaired_calls:
                            repair_errors.extend(validate_tool_calls(
                                repaired_calls,
                                request.tools,
                                request.tool_choice,
                            ))
                        elif repaired_final is not None and not tool_call_expected(request.tool_choice):
                            pass
                        else:
                            repair_errors.extend(repair_outcome.diagnostics)
                        if not repaired_calls and repaired_final is None and not repair_errors:
                            repair_errors.append("no valid tool calls were decoded")
                    except Exception as exc:
                        repaired_calls = None
                        repaired_final = None
                        repair_errors = [f"provider repair request failed: {exc}"]
                        repaired_text = ""
                    record_round_trace(trace_id, "tool_repair_response", {
                        "text": repaired_text,
                        "errors": repair_errors,
                    })
                    if repair_errors:
                        if isinstance(client, GeminiClient) and tool_calls:
                            log.warning(
                                "Gemini repair failed; keeping the original tool_calls (%s)",
                                "; ".join(repair_errors[:6]),
                            )
                        else:
                            metrics.increment("tool_translation.repair_failed")
                            log.error("Tool-call translation failed after repair: %s", "; ".join(repair_errors))
                            raise HTTPException(
                                status_code=502,
                                detail={
                                    "type": "tool_call_translation_error",
                                    "message": "The browser provider produced an invalid tool call after one repair attempt.",
                                    "errors": repair_errors[:12],
                                },
                            )
                    else:
                        metrics.increment("tool_translation.repair_succeeded")
                        tool_calls = repaired_calls
                        response_text = repaired_final if repaired_final is not None else repaired_text
                if tool_calls:
                    finish_reason = "tool_calls"
                    response_text = None

            # -- Build response ----------------------------------
            prompt_tokens = _estimate_tokens(prompt)
            completion_tokens = _estimate_tokens(response_text or "")

            response = ChatCompletionResponse(
                model=model_id,
                choices=[
                    Choice(
                        index=0,
                        message=ChoiceMessage(
                            role="assistant",
                            content=response_text,
                            tool_calls=tool_calls,
                            audio=(
                                AudioInfo(
                                    url=result.audio.url,
                                    local_path=result.audio.local_path,
                                    mime_type=result.audio.mime_type,
                                    size_bytes=result.audio.size_bytes,
                                )
                                if result.audio
                                else None
                            ),
                        ),
                        finish_reason=finish_reason,
                    )
                ],
                usage=UsageInfo(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                ),
            )
            record_round_trace(trace_id, "response", _model_dump_compat(response))

            log.info(
                f"Response: {elapsed_ms}ms, finish_reason={finish_reason}, "
                f"tokens~{response.usage.total_tokens}"
            )

            if conversation_routing is not None:
                assistant_message = ChatMessage(
                    role="assistant",
                    content=response_text,
                    tool_calls=tool_calls,
                )
                transcript = [
                    *conversation_routing.transcript_input,
                    _canonical_message(assistant_message),
                ]
                route = _get_conversation_store().save_route(
                    project_key=conversation_routing.project_key,
                    app_key=conversation_routing.app_key,
                    conversation_key=conversation_routing.conversation_key,
                    thread_id=result.thread_id or client._extract_thread_id(),
                    transcript=transcript,
                    message_hashes=[_message_hash(message) for message in transcript],
                    contract_hash=conversation_routing.contract_hash,
                )
                if capture_route_outcome:
                    _completion_route_outcomes[response.id] = route

            if not stateful_request:
                async with _cache_lock:
                    _prune_cache(time.time())
                    _response_cache[cache_key] = (time.time(), _model_copy_compat(response, deep=True))

            return response
    except asyncio.CancelledError:
        if lease_held:
            interrupt = getattr(client, "interrupt_generation", None)
            if callable(interrupt):
                try:
                    await interrupt()
                except Exception as exc:
                    log.debug("Could not interrupt provider generation: %s", exc)
        log.warning("Browser turn aborted after client cancel; queue slot released")
        raise
    finally:
        await stop_disconnect_watch(disconnect_watch)
        if _deletion_pending:
            asyncio.create_task(_maybe_delete_expired_app_threads(_deletion_pending))


async def _run_async_chat_job(
    job_id: str,
    request: ChatCompletionRequest,
    app_key: str = "",
    fresh_thread: bool = False,
) -> None:
    """Background runner for async chat completion jobs."""
    async with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job.status = "running"

    try:
        response = await _execute_chat_completion(
            request,
            app_key_override=app_key,
            fresh_thread=fresh_thread,
        )
        async with _jobs_lock:
            job = _jobs.get(job_id)
            if job is not None:
                job.status = "completed"
                job.response = response
    except Exception as e:
        log.error(f"Async chat job failed ({job_id}): {e}", exc_info=True)
        async with _jobs_lock:
            job = _jobs.get(job_id)
            if job is not None:
                job.status = "failed"
                job.error = str(e)
                job.error_status_code = int(getattr(e, "status_code", 500))


async def _submit_async_chat_job(
    request: ChatCompletionAsyncRequest,
    http_request: Request,
    endpoint_app_name: str = "",
) -> ChatCompletionJobResponse:
    """Shared async chat submit logic for generic and app-scoped routes."""
    fresh_thread = _fresh_thread_from_header(http_request)
    _validate_chat_request(request, fresh_thread=fresh_thread)
    _get_client()

    app_key = _resolve_app_key(request, http_request, endpoint_app_name=endpoint_app_name)

    job_id = f"chatjob-{uuid.uuid4().hex[:24]}"
    job = ChatCompletionJobResponse(
        id=job_id,
        status="queued",
        model=request.model,
    )

    async with _jobs_lock:
        _jobs[job_id] = job
        _job_app_keys[job_id] = app_key

    asyncio.create_task(_run_async_chat_job(job_id, request, app_key, fresh_thread))
    return job


@openai_router.post("/v1/chat/completions/async", response_model=ChatCompletionJobResponse)
async def create_chat_completion_async(
    request: ChatCompletionAsyncRequest,
    http_request: Request,
) -> ChatCompletionJobResponse:
    """Submit an async chat completion job and return the job handle."""
    return await _submit_async_chat_job(request, http_request)


@openai_router.post("/{app_name}/v1/chat/completions/async", response_model=ChatCompletionJobResponse)
async def create_chat_completion_async_scoped(
    app_name: str,
    request: ChatCompletionAsyncRequest,
    http_request: Request,
) -> ChatCompletionJobResponse:
    """App-scoped alias for async chat submit."""
    return await _submit_async_chat_job(request, http_request, endpoint_app_name=app_name)


@openai_router.get("/v1/chat/completions/async/{job_id}", response_model=ChatCompletionJobResponse)
async def get_chat_completion_async_job(job_id: str) -> ChatCompletionJobResponse:
    """Get async chat completion job state and result."""
    async with _jobs_lock:
        job = _jobs.get(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    return job


@openai_router.get("/{app_name}/v1/chat/completions/async/{job_id}", response_model=ChatCompletionJobResponse)
async def get_chat_completion_async_job_scoped(
    app_name: str,
    job_id: str,
) -> ChatCompletionJobResponse:
    """
    App-scoped alias for async chat status/result.

    Enforces app/job ownership so parallel apps cannot read each other's jobs.
    """
    expected_key = f"endpoint:{_normalize_key_part(app_name)}"

    async with _jobs_lock:
        job = _jobs.get(job_id)
        job_key = _job_app_keys.get(job_id, "")

    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    if job_key and job_key != expected_key:
        raise HTTPException(status_code=404, detail="Job not found")

    return job
