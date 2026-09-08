"""Response-format and structured-output shaping helpers for API adapters."""

from __future__ import annotations

import json
import re
from typing import Any

from src.api.attachment_expander import AttachmentPageDescriptor
from src.api.openai_schemas import ChatCompletionRequest, ChatMessage
from src.api.prompt_compaction import _extract_content_text
from src.config import Config

def _build_response_format_system_prompt(response_format: Any) -> str | None:
    """Build a strict JSON-output instruction from OpenAI response_format."""
    if not response_format:
        return None

    if isinstance(response_format, str):
        if response_format == "json_object":
            return (
                "You must respond with valid JSON only. "
                "Return exactly one JSON object and no markdown/code fences."
            )
        return None

    if not isinstance(response_format, dict):
        return None

    rf_type = response_format.get("type")
    if rf_type == "json_object":
        return (
            "You must respond with valid JSON only. "
            "Return exactly one JSON object and no markdown/code fences."
        )

    if rf_type == "json_schema":
        schema_obj = response_format.get("json_schema", {})
        schema = schema_obj.get("schema") if isinstance(schema_obj, dict) else None
        strict = bool(schema_obj.get("strict")) if isinstance(schema_obj, dict) else False
        if schema:
            strict_text = " Follow it strictly." if strict else ""
            return (
                "You must respond with valid JSON only (no markdown/code fences). "
                "The JSON must satisfy this schema:" +
                f"\n{json.dumps(schema, ensure_ascii=False)}" +
                strict_text
            )
        return (
            "You must respond with valid JSON only. "
            "Return exactly one JSON object and no markdown/code fences."
        )

    return None


def _page_extraction_mode(request: ChatCompletionRequest) -> str:
    """Normalize the requested page-extraction mode string."""
    options = getattr(request, "page_extraction", None)
    mode = getattr(options, "mode", "") if options is not None else ""
    return str(mode or "").strip().lower()


def _build_page_extraction_response_format(
    page_descriptors: list[AttachmentPageDescriptor],
) -> dict[str, Any]:
    """Build a strict JSON schema for page-by-page extraction output."""
    if not page_descriptors:
        raise ValueError("page_descriptors cannot be empty")

    return {
        "type": "json_schema",
        "json_schema": {
            "name": "page_extraction",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "pages": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "page_index": {"type": "integer"},
                                "source_name": {"type": "string"},
                                "page_number": {"type": "integer"},
                                "text": {"type": "string"},
                            },
                            "required": ["page_index", "source_name", "page_number", "text"],
                        },
                    }
                },
                "required": ["pages"],
            },
        },
    }


def _build_page_extraction_note(page_descriptors: list[AttachmentPageDescriptor]) -> str:
    """Build a compact prompt prefix describing the required page-map output."""
    if not page_descriptors:
        return ""

    lines = [
        "[Per-page extraction]",
        f"- Return valid JSON only with a top-level `pages` array containing exactly {len(page_descriptors)} item(s).",
        "- Preserve each `page_index`, `source_name`, and `page_number` exactly as listed below.",
        "- Fill `text` with the extracted text for that page in reading order.",
        "- If a page is blank or unreadable, keep the item and return an empty string for `text`.",
        "- Keep the `pages` array in the same order as the numbered page map below.",
        "- If an original document is also attached as a fallback, use it only to help read the listed page map. Do not add extra pages.",
    ]
    for descriptor in page_descriptors:
        lines.append(
            f"- page_index={descriptor.page_index}: '{descriptor.source_name}' page {descriptor.page_number}."
        )
    return "\n".join(lines) + "\n\n"


def _extract_json_payload(text: str) -> Any | None:
    """Extract and parse a JSON object/array from model text."""
    if not text:
        return None

    stripped = text.strip()

    try:
        return json.loads(stripped)
    except Exception:
        pass

    block = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", stripped)
    if block:
        candidate = block.group(1).strip()
        try:
            return json.loads(candidate)
        except Exception:
            pass

    first_obj = stripped.find("{")
    first_arr = stripped.find("[")
    candidates = [i for i in (first_obj, first_arr) if i >= 0]
    if not candidates:
        return None

    start = min(candidates)
    try:
        return json.loads(stripped[start:])
    except Exception:
        return None


def _coerce_payload_to_schema(payload: Any, schema: dict[str, Any]) -> Any:
    """Best-effort payload coercion guided by a JSON schema."""
    schema_type = schema.get("type")

    if schema_type == "object":
        if isinstance(payload, dict):
            return payload

        properties = schema.get("properties", {})
        if not isinstance(properties, dict) or not properties:
            return payload

        if isinstance(payload, list):
            array_keys = [
                key for key, prop in properties.items()
                if isinstance(prop, dict) and prop.get("type") == "array"
            ]
            if len(array_keys) == 1:
                return {array_keys[0]: payload}

            if len(properties) == 1:
                key = next(iter(properties))
                return {key: payload}

            required = schema.get("required", [])
            if isinstance(required, list):
                for key in required:
                    if key in properties:
                        return {key: payload}

        if len(properties) == 1:
            key = next(iter(properties))
            return {key: payload}

        return payload

    if schema_type == "array":
        if isinstance(payload, list):
            return payload

        if isinstance(payload, dict) and len(payload) == 1:
            only_value = next(iter(payload.values()))
            if isinstance(only_value, list):
                return only_value

        return [payload]

    return payload


def _coerce_to_response_schema(payload: Any, response_format: Any) -> Any:
    """Coerce common payload mismatches into the requested response format."""
    if not isinstance(response_format, dict):
        return payload

    rf_type = response_format.get("type")
    if rf_type == "json_object":
        if isinstance(payload, dict):
            return payload
        return {"data": payload}

    if rf_type != "json_schema":
        return payload

    json_schema = response_format.get("json_schema", {})
    if not isinstance(json_schema, dict):
        return payload

    schema = json_schema.get("schema")
    if not isinstance(schema, dict):
        return payload

    return _coerce_payload_to_schema(payload, schema)


def _is_effectively_empty_value(value: Any) -> bool:
    """Return True when a value is effectively empty for header-row detection."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return False


def _pick_note_field_name(item: dict[str, Any]) -> str | None:
    """Pick the best note/context field key in an item dict."""
    preferred = ("note", "notes", "context", "header", "section", "group")
    for key in preferred:
        if key in item:
            return key
    return None


def _header_text_from_row(item: Any) -> str | None:
    """
    Detect a header-only row, e.g. {"food": null, "quantity": null, "note": "TO SERVE"}.
    Returns header text if matched, else None.
    """
    if not isinstance(item, dict) or not item:
        return None

    note_key = _pick_note_field_name(item)
    if not note_key:
        return None

    note_val = item.get(note_key)
    if not isinstance(note_val, str) or not note_val.strip():
        return None

    for key, value in item.items():
        if key == note_key:
            continue
        if not _is_effectively_empty_value(value):
            return None

    return note_val.strip()


def _append_note(item: dict[str, Any], text: str) -> None:
    """Append note/context text to an item."""
    key = _pick_note_field_name(item) or "note"
    existing = item.get(key)
    if isinstance(existing, str) and existing.strip():
        item[key] = f"{text}; {existing.strip()}"
    else:
        item[key] = text


def _merge_header_rows_in_array(items: list[Any]) -> list[Any]:
    """Merge header-only rows into the next real row in an array."""
    merged: list[Any] = []
    pending_header: str | None = None

    for raw in items:
        header_text = _header_text_from_row(raw)
        if header_text:
            pending_header = f"{pending_header}; {header_text}" if pending_header else header_text
            continue

        if isinstance(raw, dict) and pending_header:
            item = dict(raw)
            _append_note(item, pending_header)
            merged.append(item)
            pending_header = None
            continue

        merged.append(raw)

    if pending_header:
        # No real row after header; preserve as standalone note row.
        merged.append({"note": pending_header})

    return merged


def _merge_header_rows(payload: Any) -> Any:
    """
    Merge header-only rows into next row for structured payloads.

    Supports:
    - root array of objects
    - root object with exactly one array field
    """
    if isinstance(payload, list):
        return _merge_header_rows_in_array(payload)

    if isinstance(payload, dict):
        array_keys = [k for k, v in payload.items() if isinstance(v, list)]
        if len(array_keys) == 1:
            key = array_keys[0]
            out = dict(payload)
            out[key] = _merge_header_rows_in_array(out[key])
            return out

    return payload


def _normalize_structured_content(response_text: str, response_format: Any) -> str:
    """Best-effort normalization to JSON string for structured output calls."""
    payload = _extract_json_payload(response_text)
    if payload is None:
        return response_text

    payload = _coerce_to_response_schema(payload, response_format)
    if Config.API_HEADER_ROW_MERGE_MODE:
        payload = _merge_header_rows(payload)
    return json.dumps(payload, ensure_ascii=False)


def _latest_user_text(messages: list[ChatMessage]) -> str:
    """Return the latest user text content from request messages."""
    for msg in reversed(messages):
        if msg.role == "user":
            return _extract_content_text(msg.content).strip()
    return ""


def _infer_expected_item_count(messages: list[ChatMessage]) -> int | None:
    """
    Infer expected item count from the latest user text.

    Used as a best-effort guard for structured extraction tasks where output
    cardinality should match input rows.

    Strategy:
    1) If user content is JSON, infer from its primary array cardinality.
    2) Otherwise, fallback to line counting only when input appears to be
       a compact line-item list (not an instruction-heavy prompt).
    """
    text = _latest_user_text(messages)
    if not text:
        return None

    # Preferred: JSON payloads (e.g., [...], {"items":[...]}, {"ingredients":[...]}).
    payload = _extract_json_payload(text)
    if payload is not None:
        json_count = _infer_primary_array_count(payload)
        if json_count is not None and json_count >= 1:
            return json_count

    # Heuristic fallback: only for line-item style prompts.
    if not _should_use_line_cardinality_fallback(text):
        return None

    count = 0
    for line in text.splitlines():
        cleaned = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line).strip()
        if cleaned:
            count += 1

    return count if count >= 2 else None


def _should_use_line_cardinality_fallback(text: str) -> bool:
    """
    Decide whether plain-text line-count cardinality fallback is safe.

    Avoids instruction-heavy prompts (schemas, long docs, embedded templates)
    where line count does not represent expected output cardinality.
    """
    lower = (text or "").lower()
    if not lower:
        return False

    # Strong signals this is an instruction/template payload, not line items.
    instruction_markers = (
        "[system instruction",
        "[system instructions",
        "you must respond with valid json only",
        "$schema",
        "json-schema.org",
        "<text_content>",
        "table of contents",
        "project structure",
        "architecture",
    )
    if any(marker in lower for marker in instruction_markers):
        return False

    lines: list[str] = []
    short_lines = 0
    for line in text.splitlines():
        cleaned = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line).strip()
        if not cleaned:
            continue
        lines.append(cleaned)
        if len(cleaned) <= 120:
            short_lines += 1

    if len(lines) < 2:
        return False

    # Very large/verbose payloads are likely instructions or article content.
    if len(text) > 2000 or len(lines) > 40:
        return False

    # Require mostly short item-like lines.
    return (short_lines / len(lines)) >= 0.8


def _infer_primary_array_count(payload: Any) -> int | None:
    """Infer the cardinality of the primary array in a structured payload."""
    if isinstance(payload, list):
        return len(payload)

    if isinstance(payload, dict):
        array_values = [v for v in payload.values() if isinstance(v, list)]
        if len(array_values) == 1:
            return len(array_values[0])

    return None


def _structured_cardinality_mismatch(
    messages: list[ChatMessage],
    response_text: str,
    expected_count: int | None = None,
) -> tuple[int, int] | None:
    """Return (expected, actual) if a clear structured cardinality mismatch exists."""
    expected = expected_count if expected_count is not None else _infer_expected_item_count(messages)
    if expected is None:
        return None

    payload = _extract_json_payload(response_text)
    if payload is None:
        return None

    actual = _infer_primary_array_count(payload)
    if actual is None:
        return None

    if expected != actual:
        return expected, actual
    return None


def _build_cardinality_retry_prompt(
    base_prompt: str,
    expected: int,
    actual: int,
    expectation_reason: str = "input line count",
    correction_rule: str | None = None,
) -> str:
    """Build a corrective retry prompt for structured cardinality mismatch."""
    if not correction_rule:
        correction_rule = (
            "Return valid JSON only, with exactly one output item per non-empty input line, "
            "preserving input order. Do not merge or drop lines."
        )
    return (
        f"{base_prompt}\n\n"
        "Correction: The previous JSON had the wrong number of items.\n"
        f"Expected output count is {expected} based on {expectation_reason}, but output count was {actual}.\n"
        f"{correction_rule}"
    )
