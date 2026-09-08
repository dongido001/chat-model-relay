"""Validation helpers for translating browser text into OpenAI tool calls."""

from __future__ import annotations

import json
import re
from typing import Any

from src.api.openai_schemas import ToolCall, ToolDefinition


def tool_call_expected(tool_choice: str | dict[str, Any] | None) -> bool:
    """Return whether the request contract requires a tool call."""
    return tool_choice == "required" or isinstance(tool_choice, dict)


def looks_like_tool_call_intent(text: str, tools: list[ToolDefinition]) -> bool:
    """Conservatively identify output that was intended as a tool call."""
    source = (text or "").strip()
    if not source:
        return False
    lowered = source.lower()
    if '"tool_calls"' in lowered or "'tool_calls'" in lowered:
        return True
    if re.search(r"```(?:json)?\s*\{", source, flags=re.IGNORECASE):
        names = {tool.function.name.lower() for tool in tools}
        return any(re.search(rf'["\']name["\']\s*:\s*["\']{re.escape(name)}["\']', lowered) for name in names)
    return False


def validate_tool_calls(
    calls: list[ToolCall] | None,
    tools: list[ToolDefinition],
    tool_choice: str | dict[str, Any] | None,
) -> list[str]:
    """Validate decoded calls against names, tool choice, and JSON Schemas."""
    if not calls:
        return ["no valid tool calls were decoded"]

    definitions = {tool.function.name: tool.function for tool in tools}
    selected_name = _selected_tool_name(tool_choice)
    errors: list[str] = []
    seen_ids: set[str] = set()
    for index, call in enumerate(calls):
        prefix = f"tool_calls[{index}]"
        if call.id in seen_ids:
            errors.append(f"{prefix}.id is duplicated")
        seen_ids.add(call.id)
        name = call.function.name
        definition = definitions.get(name)
        if definition is None:
            errors.append(f"{prefix}.function.name is unknown: {name!r}")
            continue
        if selected_name and name != selected_name:
            errors.append(f"{prefix}.function.name must be {selected_name!r}")
        try:
            arguments = json.loads(call.function.arguments)
        except (TypeError, json.JSONDecodeError) as exc:
            errors.append(f"{prefix}.function.arguments is not valid JSON: {exc}")
            continue
        if not isinstance(arguments, dict):
            errors.append(f"{prefix}.function.arguments must be an object")
            continue
        errors.extend(_validate_value(arguments, definition.parameters or {}, f"{prefix}.function.arguments"))
    return errors


def build_tool_repair_prompt(
    response_text: str,
    errors: list[str],
    tools: list[ToolDefinition],
    tool_choice: str | dict[str, Any] | None,
) -> str:
    """Build one tightly-scoped provider protocol repair request."""
    schemas = [
        {
            "name": tool.function.name,
            "parameters": tool.function.parameters or {},
        }
        for tool in tools
    ]
    selected = _selected_tool_name(tool_choice)
    selected_rule = f" Use only {selected!r}." if selected else ""
    error_text = "\n".join(f"- {error}" for error in errors[:12])
    malformed = (response_text or "")[:12000]
    return (
        "Your previous response was intended as a tool call but could not be translated.\n\n"
        f"Validation errors:\n{error_text}\n\n"
        "Return only one corrected JSON object in this exact outer form:\n"
        '{"tool_calls":[{"name":"<function_name>","arguments":{}}]}\n'
        f"Do not execute a tool or answer the user yet.{selected_rule}\n\n"
        f"Available definitions:\n{json.dumps(schemas, ensure_ascii=False)}\n\n"
        f"Previous malformed response:\n{malformed}"
    )


def _selected_tool_name(tool_choice: str | dict[str, Any] | None) -> str:
    if not isinstance(tool_choice, dict):
        return ""
    function = tool_choice.get("function")
    return str(function.get("name") or "").strip() if isinstance(function, dict) else ""


def _validate_value(value: Any, schema: dict[str, Any], path: str) -> list[str]:
    if not isinstance(schema, dict):
        return []
    errors: list[str] = []
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path} must equal {schema['const']!r}")
    for keyword, require_one in (("anyOf", True), ("oneOf", False)):
        branches = schema.get(keyword)
        if isinstance(branches, list) and branches:
            outcomes = [
                _validate_value(value, branch, path)
                for branch in branches
                if isinstance(branch, dict)
            ]
            matches = sum(not outcome for outcome in outcomes)
            valid = matches >= 1 if require_one else matches == 1
            if not valid:
                errors.append(f"{path} does not satisfy {keyword}")
            return errors
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} must be one of {schema['enum']!r}")

    expected = schema.get("type")
    allowed = set(expected) if isinstance(expected, list) else {expected} if expected else set()
    if allowed and not _matches_type(value, allowed):
        errors.append(f"{path} must have type {' or '.join(sorted(str(item) for item in allowed))}")
        return errors

    if isinstance(value, dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        for name in required:
            if name not in value:
                errors.append(f"{path}.{name} is required")
        if isinstance(schema.get("minProperties"), int) and len(value) < schema["minProperties"]:
            errors.append(f"{path} must contain at least {schema['minProperties']} properties")
        if isinstance(schema.get("maxProperties"), int) and len(value) > schema["maxProperties"]:
            errors.append(f"{path} must contain at most {schema['maxProperties']} properties")
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in properties:
                    errors.append(f"{path}.{name} is not allowed")
        for name, child in value.items():
            child_schema = properties.get(name)
            if isinstance(child_schema, dict):
                errors.extend(_validate_value(child, child_schema, f"{path}.{name}"))
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        if isinstance(schema.get("minItems"), int) and len(value) < schema["minItems"]:
            errors.append(f"{path} must contain at least {schema['minItems']} items")
        if isinstance(schema.get("maxItems"), int) and len(value) > schema["maxItems"]:
            errors.append(f"{path} must contain at most {schema['maxItems']} items")
        for index, child in enumerate(value):
            errors.extend(_validate_value(child, schema["items"], f"{path}[{index}]"))
    elif isinstance(value, str):
        if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
            errors.append(f"{path} is shorter than minLength {schema['minLength']}")
        if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
            errors.append(f"{path} is longer than maxLength {schema['maxLength']}")
    return errors


def _matches_type(value: Any, allowed: set[Any]) -> bool:
    checks = {
        "null": value is None,
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
    }
    return any(checks.get(str(kind), True) for kind in allowed)
