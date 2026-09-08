from __future__ import annotations

import hashlib
import re
from typing import Any

from src.api.cursor_adapter import compact_cursor_followup, extract_actionable_user_request, extract_first_user_request, extract_latest_user_request
from src.api.tool_protocol import build_gemini_browser_tool_prompt
from src.api.openai_schemas import ChatMessage
from src.config import Config


def _extract_content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "\n".join(parts) if parts else ""
    return str(content)


def _first_user_conversation_seed(messages: list[ChatMessage] | None) -> str:
    if not messages:
        return ""
    for message in messages:
        if getattr(message, "role", None) != "user":
            continue
        text = _extract_content_text(message.content)
        seed = extract_first_user_request(text) or text.strip()
        if seed:
            return seed
    return ""


def _latest_user_message(messages: list[ChatMessage]) -> ChatMessage | None:
    for message in reversed(messages):
        if getattr(message, "role", None) == "user":
            return message
    return None


def _latest_turn_messages(messages: list[ChatMessage], *, include_system: bool = True) -> list[ChatMessage]:
    if not messages:
        return []
    systems = [message for message in messages if message.role == "system"] if include_system else []
    latest: list[ChatMessage] = []
    for message in reversed(messages):
        if message.role in {"user", "tool"}:
            latest.insert(0, message)
            if message.role == "user":
                break
    if not latest:
        non_system = [message for message in messages if message.role != "system"]
        latest = non_system[-1:]
    return systems + latest


def _last_user_query(text: str) -> str:
    return extract_latest_user_request(text)


def _compact_followup_user_message(message: ChatMessage) -> ChatMessage:
    text = _extract_content_text(message.content)
    body = compact_cursor_followup(text)
    return message.model_copy(update={"content": body}) if hasattr(message, "model_copy") else message.copy(update={"content": body})


def _compact_tool_result_message(message: ChatMessage, max_chars: int | None = None) -> ChatMessage:
    if message.role != "tool":
        return message
    content = message.content
    if not isinstance(content, str):
        return message
    limit = max_chars if max_chars is not None else Config.API_TOOL_RESULT_MAX_CHARS
    if len(content) <= limit:
        return message

    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    marker = f"\n\n[tool result truncated: original_chars={len(content)} sha256={digest}]\n\n"
    available = max(0, limit - len(marker))
    head_chars = (available * 3) // 5
    tail_chars = available - head_chars
    compacted = content[:head_chars] + marker
    if tail_chars:
        compacted += content[-tail_chars:]
    if hasattr(message, "model_copy"):
        return message.model_copy(update={"content": compacted})
    return message.copy(update={"content": compacted})


def _file_attachment_key(attachment: dict) -> str:
    blob = str(attachment.get("data_b64") or attachment.get("data") or attachment.get("url") or "")
    return f"{attachment.get('filename', '')}:{attachment.get('mime_type', '')}:{blob[:96]}"


def _new_attachments_from_latest_user(messages: list[ChatMessage]) -> tuple[list[str], list[dict]]:
    latest = _latest_user_message(messages)
    if latest is None or not isinstance(latest.content, list):
        return [], []

    prior_urls: set[str] = set()
    prior_files: set[str] = set()
    for message in messages:
        if message is latest or message.role != "user" or not isinstance(message.content, list):
            continue
        prior_urls.update(_extract_image_urls(message.content))
        for attachment in _extract_file_attachments(message.content):
            prior_files.add(_file_attachment_key(attachment))

    image_urls = [url for url in _extract_image_urls(latest.content) if url not in prior_urls]
    files = [
        attachment
        for attachment in _extract_file_attachments(latest.content)
        if _file_attachment_key(attachment) not in prior_files
    ]
    return image_urls, files


def _extract_image_urls(content: Any) -> list[str]:
    if not isinstance(content, list):
        return []
    urls: list[str] = []
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


def _extract_file_attachments(content: Any) -> list[dict]:
    if not isinstance(content, list):
        return []
    files: list[dict] = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "file":
            continue
        file_info = item.get("file", {})
        if not isinstance(file_info, dict):
            continue
        filename = file_info.get("filename", "attachment")
        data_b64 = file_info.get("data")
        mime_type = file_info.get("mime_type", "application/octet-stream")
        url = file_info.get("url", "")
        if not data_b64 and url.startswith("data:"):
            try:
                header, data_b64 = url.split(",", 1)
                if ":" in header and ";" in header:
                    mime_type = header.split(":")[1].split(";")[0]
            except ValueError:
                continue
        if data_b64:
            files.append({"filename": filename, "data_b64": data_b64, "mime_type": mime_type})
    return files


def _build_prompt(messages: list[ChatMessage]) -> str:
    non_system = [m for m in messages if m.role != "system"]
    system_msgs = [m for m in messages if m.role == "system"]
    if len(non_system) == 1 and non_system[0].role == "user":
        prefix = ""
        if system_msgs:
            sys_texts = []
            for msg in system_msgs:
                text = _extract_content_text(msg.content).strip()
                if text:
                    sys_texts.append(text)
            if len(sys_texts) == 1:
                prefix = f"[System instruction: {sys_texts[0]}]\n\n"
            elif sys_texts:
                combined = "\n".join(f"{idx + 1}. {text}" for idx, text in enumerate(sys_texts))
                prefix = f"[System instructions]\n{combined}\n\n"
        user_text = _extract_content_text(non_system[0].content)
        return prefix + (user_text or "")

    parts: list[str] = []
    for msg in messages:
        role = msg.role.capitalize()
        if msg.role == "tool":
            parts.append(f"[Tool result for {msg.tool_call_id or 'unknown'}]: {_extract_content_text(msg.content)}")
        elif msg.role == "assistant" and msg.tool_calls:
            calls_desc = [f'{tc.function.name}({tc.function.arguments})' for tc in msg.tool_calls]
            parts.append(f"Assistant called tools: {', '.join(calls_desc)}")
        elif msg.content:
            text = _extract_content_text(msg.content)
            if text:
                parts.append(f"{role}: {text}")
    return "\n\n".join(parts)


def _latest_user_text_for_gemini(messages: list[ChatMessage]) -> str:
    latest = _latest_user_message(messages)
    if latest is None:
        return ""
    raw = _extract_content_text(latest.content)
    if "Latest request to transform:\n" in raw:
        raw = raw.split("Latest request to transform:\n", 1)[-1]
    return extract_actionable_user_request(raw, min_chars=12) or raw.strip()


def _slim_gemini_browser_prompt(request: Any, messages: list[ChatMessage]) -> str:
    source_messages = list(request.messages)
    user_text = _latest_user_text_for_gemini(source_messages)
    tool_block = ""
    if request.tools and request.tool_choice != "none":
        first_turn = not any(message.role in {"assistant", "tool"} for message in source_messages)
        tool_block = build_gemini_browser_tool_prompt(
            request.tools,
            request.tool_choice,
            first_turn=first_turn,
            user_text=user_text,
        )
    labeled = f"VS Code user message:\n{user_text}" if user_text else ""

    tool_context = ""
    tool_messages = [message for message in source_messages if message.role == "tool"]
    if tool_messages:
        latest = _compact_tool_result_message(tool_messages[-1], max_chars=900)
        result_text = _extract_content_text(latest.content).strip()
        tool_context = (
            "VS Code executed your previous tool call.\n"
            f"Tool result ({latest.tool_call_id or latest.name or 'unknown'}):\n{result_text}\n\n"
            "Continue the original user request using this result. Call another function only if needed; otherwise answer the user with the result."
        )
    return "\n\n".join(part for part in (tool_block, labeled, tool_context) if part)


def _apply_tool_prompt_to_messages(messages: list[ChatMessage], tool_prompt: str) -> list[ChatMessage]:
    updated = list(messages)
    for index in range(len(updated) - 1, -1, -1):
        message = updated[index]
        if message.role != "user":
            continue
        prefix = f"{tool_prompt}\n\nLatest request to transform:\n"
        if isinstance(message.content, str):
            updated[index] = message.model_copy(update={"content": prefix + message.content}) if hasattr(message, "model_copy") else message.copy(update={"content": prefix + message.content})
            return updated
        if isinstance(message.content, list):
            updated[index] = message.model_copy(update={"content": [{"type": "text", "text": prefix.rstrip()}, *message.content]}) if hasattr(message, "model_copy") else message.copy(update={"content": [{"type": "text", "text": prefix.rstrip()}, *message.content]})
            return updated
    updated.insert(0, ChatMessage(role="user", content=tool_prompt))
    return updated


__all__ = [
    "_extract_content_text",
    "_first_user_conversation_seed",
    "_latest_user_message",
    "_latest_turn_messages",
    "_last_user_query",
    "_compact_followup_user_message",
    "_compact_tool_result_message",
    "_file_attachment_key",
    "_new_attachments_from_latest_user",
    "_extract_image_urls",
    "_extract_file_attachments",
    "_build_prompt",
    "_latest_user_text_for_gemini",
    "_slim_gemini_browser_prompt",
    "_apply_tool_prompt_to_messages",
]
