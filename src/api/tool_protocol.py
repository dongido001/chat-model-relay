"""Prompting and parsing for the browser-backed OpenAI tool-call protocol.

The editor owns tool execution and iteration.  This module owns only the
single provider round: describe the available functions, decode the browser
model's JSON, and report enough diagnostics for one bounded repair attempt.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
import uuid
from typing import Any

from src.api.openai_schemas import (
    FunctionCallInfo,
    ToolCall,
    ToolDefinition,
)
from src.api.tool_translation import looks_like_tool_call_intent


log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ToolParseOutcome:
    """Structured result of interpreting one provider response."""

    calls: list[ToolCall] | None
    has_tool_intent: bool
    diagnostics: tuple[str, ...] = ()


def build_tool_system_prompt(
    tools: list[ToolDefinition],
    tool_choice: str | dict[str, Any] | None = None,
) -> str:
    """Describe the request-scoped editor tools to the browser model."""
    if tool_choice == "none":
        return ""

    descriptions = [
        json.dumps(
            {
                "name": tool.function.name,
                "description": tool.function.description,
                "parameters": tool.function.parameters,
            },
            indent=2,
        )
        for tool in tools
    ]
    tools_json = "\n---\n".join(descriptions)
    available_names = {tool.function.name for tool in tools}
    choice_rule = "Producing a record is optional. If no definition applies, answer normally."
    if tool_choice == "required":
        choice_rule = "Your JSON MUST contain at least one listed record. Do not answer with prose."
    elif isinstance(tool_choice, dict):
        selected = tool_choice.get("function")
        selected_name = str(selected.get("name") or "").strip() if isinstance(selected, dict) else ""
        if selected_name and selected_name in available_names:
            choice_rule = f"The JSON name value MUST be {selected_name!r}. Do not answer with prose."

    return f"""Convert the latest request into a JSON data document when it matches one of the
record definitions below. You are the planning side of a Cursor tool loop: Cursor executes
the listed records against the user's local workspace after your response. This is only text
transformation: do not execute anything and do not claim files or tools are unavailable.
Words such as call, invoke, read, write, or use in the latest request mean to select the
matching record definition and fill its argument fields.

When a definition matches, your ENTIRE response must be exactly one JSON object:
{{"tool_calls":[{{"name":"<function_name>","arguments":{{...}}}}]}}

Record definitions:
{tools_json}

Rules:
- Use exact names from the definitions.
- Arguments must be a valid JSON object.
- Return multiple records when needed.
- Do not include prose or Markdown around the JSON document.
- {choice_rule}
"""


def build_tool_continuation_prompt(
    tools: list[ToolDefinition],
    tool_choice: str | dict[str, Any] | None = None,
) -> str:
    """Build the small per-round reminder used after a thread is primed."""
    names = ", ".join(tool.function.name for tool in tools)
    rule = "Return a tool call when one is needed; otherwise answer normally."
    if tool_choice == "required":
        rule = "Return at least one tool call. Do not answer with prose."
    elif isinstance(tool_choice, dict):
        function = tool_choice.get("function")
        name = str(function.get("name") or "").strip() if isinstance(function, dict) else ""
        if name:
            rule = f"Return a {name!r} tool call. Do not answer with prose."
    return (
        "Cursor executes tool-call JSON against the local workspace. Do not say files or tools "
        "are unavailable. Use the schemas already established in this chat and return exactly "
        '{"tool_calls":[{"name":"<function_name>","arguments":{...}}]} when calling a tool. '
        f"Available tool names: {names}. {rule}"
    )


_GEMINI_TOOL_SKIP_TOKENS = (
    "browser",
    "read_page",
    "open_browser",
    "screenshot",
    "navigate",
    "click",
    "hover",
    "mcp",
)
_GEMINI_TOOL_PREFER_TOKENS = (
    "run_in_terminal",
    "terminal",
    "run_in",
    "bash",
    "shell",
    "read_file",
    "write_file",
    "replace_file",
    "apply_patch",
    "grep",
    "search",
    "glob",
    "list_dir",
    "list_file",
    "edit_file",
    "create_file",
)
_GEMINI_TOOL_CARD_MAX_CHARS = 1600
_GEMINI_TOOL_CARD_MAX_TOOLS = 12
_GEMINI_REFUSAL_RE = re.compile(
    r"(don'?t|do not|cannot|can't|unable).{0,80}(terminal|permission|access|execut|run command)|"
    r"don'?t have direct access|"
    r"i am gemini|"
    r"run (it|this|the command) (yourself|manually)|"
    r"integrated terminal|"
    r"no (terminal|permission)",
    re.IGNORECASE,
)
_GEMINI_ACTION_RE = re.compile(
    r"\b(start|run|execut|launch|install|build|creat|write|read|open|list|search|"
    r"grep|fix|edit|delet|mkdir|serve|stop|restart|npm|pip|python|git|touch)\b",
    re.IGNORECASE,
)
_TOOL_ARG_ALIASES = (
    ("command", "input"),
    ("input", "command"),
    ("path", "filePath"),
    ("filePath", "path"),
    ("directory", "path"),
    ("cwd", "workingDirectory"),
    ("workingDirectory", "cwd"),
)


def looks_like_workspace_refusal(text: str | None) -> bool:
    """True when Gemini answers as a chatbot that cannot use Copilot tools."""
    return bool(_GEMINI_REFUSAL_RE.search(text or ""))


def _gemini_tool_rank(name: str) -> int:
    lowered = (name or "").lower()
    if any(token in lowered for token in _GEMINI_TOOL_SKIP_TOKENS):
        return 200
    for index, token in enumerate(_GEMINI_TOOL_PREFER_TOKENS):
        if token in lowered:
            return index
    return 80


def _pick_gemini_tools(tools: list[ToolDefinition]) -> list[ToolDefinition]:
    ranked = sorted(tools, key=lambda tool: (_gemini_tool_rank(tool.function.name), tool.function.name))
    preferred = [tool for tool in ranked if _gemini_tool_rank(tool.function.name) < 200]
    forced = [
        tool
        for tool in tools
        if any(token in tool.function.name.lower() for token in ("terminal", "run_in", "shell", "bash"))
    ]
    chosen: list[ToolDefinition] = []
    for tool in [*forced, *preferred]:
        if tool not in chosen:
            chosen.append(tool)
    return chosen[:_GEMINI_TOOL_CARD_MAX_TOOLS]


def _gemini_choice_rule(
    tools: list[ToolDefinition],
    tool_choice: str | dict[str, Any] | None,
    user_text: str = "",
) -> str:
    available_names = {tool.function.name for tool in tools}
    if tool_choice == "required" or _GEMINI_ACTION_RE.search(user_text or ""):
        return (
            "The user asked for a workspace action. You MUST choose tool_calls "
            "and include at least one listed function. Do not use final. "
            "Do not say you are Gemini."
        )
    if isinstance(tool_choice, dict):
        selected = tool_choice.get("function")
        selected_name = str(selected.get("name") or "").strip() if isinstance(selected, dict) else ""
        if selected_name and selected_name in available_names:
            return f"You MUST choose tool_calls and its name MUST be {selected_name!r}."
    return (
        "Decide whether the request needs a listed function. Use tool_calls when it does; "
        "use final only when no function is needed."
    )


def build_gemini_browser_tool_prompt(
    tools: list[ToolDefinition],
    tool_choice: str | dict[str, Any] | None = None,
    *,
    first_turn: bool = True,
    user_text: str = "",
) -> str:
    """Tiny tool card for Gemini's Quill composer. Long Copilot schemas freeze the tab."""
    if tool_choice == "none" or not tools:
        return ""

    picked = _pick_gemini_tools(tools)
    names = ", ".join(tool.function.name for tool in picked)
    choice_rule = _gemini_choice_rule(tools, tool_choice, user_text)
    reminder = (
        "You are Copilot's JSON planner, not a chatbot. VS Code executes functions "
        "on the local machine after your reply. You do not have a terminal; Copilot does. "
        "Never say you lack permissions, a terminal, or files. Never tell the user to run "
        "a command themselves.\n"
        "Your ENTIRE reply must be exactly one of these JSON objects:\n"
        '{"tool_calls":[{"name":"<function_name>","arguments":{...}}]}\n'
        '{"final":"<answer when no function is needed>"}\n'
        "Never put a command for the user to run inside final. "
        f"No markdown. {choice_rule}"
    )
    if not first_turn:
        return f"{reminder} Names: {names}."

    lines: list[str] = []
    for tool in picked:
        fn = tool.function
        params = fn.parameters if isinstance(fn.parameters, dict) else {}
        props = params.get("properties") if isinstance(params.get("properties"), dict) else {}
        required = params.get("required") if isinstance(params.get("required"), list) else []
        args: list[str] = []
        for name, spec in list(props.items())[:6]:
            marker = "!" if name in required else ""
            typ = spec.get("type", "") if isinstance(spec, dict) else ""
            args.append(f"{name}{marker}:{typ}" if typ else f"{name}{marker}")
        desc = " ".join((fn.description or "").split())[:70]
        signature = ", ".join(args)
        line = f"- {fn.name}({signature})"
        if desc:
            line = f"{line}: {desc}"
        lines.append(line)

    card = f"{reminder}\nFunctions:\n" + "\n".join(lines)
    if len(card) <= _GEMINI_TOOL_CARD_MAX_CHARS:
        return card
    return f"{reminder}\nFunctions: {names}."


def build_gemini_tool_repair_prompt(
    response_text: str,
    tools: list[ToolDefinition],
    user_text: str = "",
    tool_choice: str | dict[str, Any] | None = None,
) -> str:
    """Repair a Gemini response that violated the structured envelope contract."""
    names = ", ".join(tool.function.name for tool in _pick_gemini_tools(tools))
    asked = (user_text or "").strip()[:400]
    return (
        "Your previous reply was not usable by Copilot. You are not chatting as Gemini. "
        "This is only JSON translation. Do not say you lack access. Do not give the user "
        "a bash snippet. Reply with ONLY:\n"
        '{"tool_calls":[{"name":"<function_name>","arguments":{...}}]}\n'
        f"Use one of: {names}.\n"
        f"User request: {asked or '(see prior turn)'}\n"
        f"Invalid previous reply: {(response_text or '')[:400]}"
    )


def parse_gemini_final_response(response_text: str) -> str | None:
    """Return a final answer only from an exact Gemini JSON response envelope."""
    decoder = json.JSONDecoder(strict=False)
    for candidate in _tool_call_json_candidates(response_text):
        try:
            parsed, end = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            continue
        if candidate[end:].strip() or not isinstance(parsed, dict):
            continue
        if set(parsed) != {"final"}:
            continue
        final = parsed.get("final")
        if isinstance(final, str) and final.strip():
            return final.strip()
    return None


def parse_tool_calls(response_text: str, tools: list[ToolDefinition]) -> list[ToolCall] | None:
    """Compatibility wrapper returning only fully valid decoded calls."""
    return parse_tool_calls_outcome(response_text, tools).calls


def parse_tool_calls_outcome(response_text: str, tools: list[ToolDefinition]) -> ToolParseOutcome:
    """Decode calls while preserving intent and concise parser diagnostics."""
    intended = looks_like_tool_call_intent(response_text, tools)
    parsed = _decode_tool_calls_payload(response_text)
    if parsed is None:
        diagnostics = ("tool-call JSON could not be decoded",) if intended else ()
        return ToolParseOutcome(None, intended, diagnostics)

    raw_calls = parsed.get("tool_calls")
    if not isinstance(raw_calls, list):
        return ToolParseOutcome(None, True, ("tool_calls must be an array",))

    valid_names = {tool.function.name for tool in tools}
    calls: list[ToolCall] = []
    diagnostics: list[str] = []
    for index, raw_call in enumerate(raw_calls):
        prefix = f"tool_calls[{index}]"
        if not isinstance(raw_call, dict):
            diagnostics.append(f"{prefix} must be an object")
            continue
        name = str(raw_call.get("name") or "").strip()
        if not name and isinstance(raw_call.get("function"), dict):
            name = str(raw_call["function"].get("name") or "").strip()
            arguments = raw_call["function"].get("arguments", {})
        else:
            arguments = raw_call.get("arguments", {})
        if name not in valid_names:
            diagnostics.append(f"{prefix}.name is unknown: {name!r}")
            continue
        if isinstance(arguments, dict):
            arguments = _close_unbalanced_quotes_in_args(arguments)
            spec = next((tool.function.parameters for tool in tools if tool.function.name == name), {})
            arguments = _coerce_arguments_dict(arguments, spec if isinstance(spec, dict) else {})
            arguments_text = json.dumps(arguments)
        else:
            arguments_text = str(arguments)
        calls.append(
            ToolCall(
                id=f"call_{uuid.uuid4().hex[:24]}",
                type="function",
                function=FunctionCallInfo(name=name, arguments=arguments_text),
            )
        )

    if diagnostics:
        for diagnostic in diagnostics:
            log.warning("Rejected provider tool call: %s", diagnostic)
        return ToolParseOutcome(None, True, tuple(diagnostics))
    if not calls:
        return ToolParseOutcome(None, True, ("tool_calls is empty",))
    return ToolParseOutcome(calls, True)


def _decode_tool_calls_payload(response_text: str) -> dict[str, Any] | None:
    """Decode a tool-call JSON object, repairing common model mistakes."""
    decoder = json.JSONDecoder(strict=False)
    for candidate in _tool_call_json_candidates(response_text):
        grammar_repaired = _repair_unescaped_json_string_quotes(candidate)
        quote_repaired = _escape_inner_json_string_quotes(candidate)
        for blob in (
            candidate,
            _escape_invalid_json_backslashes(candidate),
            grammar_repaired,
            _escape_invalid_json_backslashes(grammar_repaired),
            quote_repaired,
            _escape_invalid_json_backslashes(quote_repaired),
        ):
            try:
                parsed, _ = decoder.raw_decode(blob)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get("tool_calls"), list):
                return parsed
    return None


def _tool_call_json_candidates(response_text: str) -> list[str]:
    text = (response_text or "").strip()
    if not text:
        return []
    fenced = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    fenced = re.sub(r"\s*```$", "", fenced)
    candidates: list[str] = []
    seen: set[str] = set()
    for source in (text, fenced):
        for match in re.finditer(r'\{\s*"tool_calls"\s*:', source):
            snippet = source[match.start():].strip()
            if snippet and snippet not in seen:
                seen.add(snippet)
                candidates.append(snippet)
        if source.startswith("{") and source not in seen:
            seen.add(source)
            candidates.append(source)
    return candidates


_JSON_KEY_AHEAD = re.compile(r'\s*"(?:[^"\\]|\\.)*"\s*:')
_JSON_VALUE_AHEAD = re.compile(r'\s*(?:"|\{|\[|-?\d|true\b|false\b|null\b)')


def _repair_unescaped_json_string_quotes(text: str) -> str:
    out: list[str] = []
    stack: list[str] = []
    expect_key = False
    in_string = False
    escaped = False
    role = "value"
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                out.append(char)
                escaped = False
            elif char == "\\":
                out.append(char)
                escaped = True
            elif char == '"':
                if _json_string_ends_at(text, index, role):
                    in_string = False
                    out.append(char)
                else:
                    out.append('\\"')
            else:
                out.append(char)
            continue
        if char == "{":
            stack.append("obj")
            expect_key = True
        elif char == "[":
            stack.append("arr")
        elif char in "}]":
            if stack:
                stack.pop()
            expect_key = False
        elif char == ":":
            expect_key = False
        elif char == ",":
            expect_key = bool(stack) and stack[-1] == "obj"
        elif char == '"':
            in_string = True
            role = "element" if stack and stack[-1] == "arr" else "key" if expect_key else "value"
        out.append(char)
    return "".join(out)


def _json_string_ends_at(text: str, quote_index: int, role: str) -> bool:
    rest = text[quote_index + 1:].lstrip(" \t\r\n")
    if not rest:
        return True
    if role == "key":
        return rest[0] == ":"
    if rest[0] == ",":
        ahead = _JSON_VALUE_AHEAD if role == "element" else _JSON_KEY_AHEAD
        return ahead.match(rest[1:]) is not None
    if rest[0] == ("]" if role == "element" else "}"):
        return _json_closers_then_boundary(rest)
    return False


def _json_closers_then_boundary(rest: str) -> bool:
    for char in rest:
        if char in "}] \t\r\n":
            continue
        return char == ","
    return True


def _escape_inner_json_string_quotes(text: str) -> str:
    out: list[str] = []
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if not in_string:
            if char == '"':
                in_string = True
            out.append(char)
            continue
        if escaped:
            out.append(char)
            escaped = False
            continue
        if char == "\\":
            out.append(char)
            escaped = True
            continue
        if char == '"':
            next_index = index + 1
            while next_index < len(text) and text[next_index] in " \t\r\n":
                next_index += 1
            nxt = text[next_index] if next_index < len(text) else ""
            if nxt in {",", "}", "]", ":"} or not nxt:
                in_string = False
                out.append(char)
            else:
                out.append('\\"')
            continue
        out.append(char)
    return "".join(out)


def _escape_invalid_json_backslashes(text: str) -> str:
    out: list[str] = []
    in_string = False
    escaped = False
    valid_escapes = {'"', "\\", "b", "f", "n", "r", "t", "u"}
    for char in text:
        if not in_string:
            if char == '"':
                in_string = True
            out.append(char)
            continue
        if escaped:
            if char not in valid_escapes:
                out.append("\\")
            out.append(char)
            escaped = False
            continue
        if char == "\\":
            out.append(char)
            escaped = True
            continue
        if char == '"':
            in_string = False
        out.append(char)
    return "".join(out)


def _coerce_arguments_dict(arguments: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
    """Copy common Copilot/Gemini argument aliases into the schema's property names."""
    props = parameters.get("properties") if isinstance(parameters.get("properties"), dict) else {}
    if not props:
        return arguments
    out = dict(arguments)
    for source, dest in _TOOL_ARG_ALIASES:
        if source in out and dest in props and dest not in out:
            out[dest] = out[source]
    return out


def _close_unbalanced_quotes_in_args(arguments: dict[str, Any]) -> dict[str, Any]:
    repaired = dict(arguments)
    for key, value in arguments.items():
        if isinstance(value, str) and value.count('"') % 2 == 1:
            repaired[key] = value + '"'
    return repaired
