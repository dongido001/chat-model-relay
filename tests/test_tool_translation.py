from __future__ import annotations

import json
import unittest

from src.api.openai_schemas import FunctionCallInfo, FunctionDefinition, ResponsesRequest, ToolCall, ToolDefinition
from src.api.tool_translation import (
    build_tool_repair_prompt,
    looks_like_tool_call_intent,
    tool_call_expected,
    validate_tool_calls,
)
from src.api.tool_protocol import parse_tool_calls_outcome


def _tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            function=FunctionDefinition(
                name="read_file",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            )
        ),
        ToolDefinition(
            function=FunctionDefinition(
                name="grep_search",
                parameters={
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "paths": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["pattern"],
                },
            )
        ),
    ]


def _call(name: str, arguments: object, call_id: str = "call_1") -> ToolCall:
    encoded = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return ToolCall(id=call_id, function=FunctionCallInfo(name=name, arguments=encoded))


class ToolTranslationTests(unittest.TestCase):
    def test_accepts_flat_responses_api_function_tool(self) -> None:
        tool = ToolDefinition.model_validate({
            "type": "function",
            "name": "exec_command",
            "description": "Run a command.",
            "parameters": {"type": "object", "properties": {}},
        })
        self.assertEqual(tool.function.name, "exec_command")
        self.assertEqual(tool.function.description, "Run a command.")

    def test_expands_responses_api_namespace_tools(self) -> None:
        request = ResponsesRequest.model_validate({
            "input": "hello",
            "tools": [{
                "type": "namespace",
                "name": "multi_agent_v1",
                "tools": [{
                    "type": "function",
                    "name": "close_agent",
                    "parameters": {"type": "object", "properties": {}},
                }],
            }],
        })
        self.assertEqual([tool.function.name for tool in request.tools or []], ["close_agent"])

    def test_ignores_unsupported_responses_builtin_tools(self) -> None:
        request = ResponsesRequest.model_validate({
            "input": "hello",
            "tools": [
                {"type": "web_search", "external_web_access": False},
                {"type": "function", "name": "read_file", "parameters": {"type": "object"}},
            ],
        })
        self.assertEqual([tool.function.name for tool in request.tools or []], ["read_file"])

    def test_detects_explicit_tool_call_intent(self) -> None:
        self.assertTrue(looks_like_tool_call_intent('{"tool_calls":[', _tools()))
        self.assertFalse(looks_like_tool_call_intent("Here is the final answer.", _tools()))

    def test_required_and_specific_choices_expect_calls(self) -> None:
        self.assertTrue(tool_call_expected("required"))
        self.assertTrue(tool_call_expected({"type": "function", "function": {"name": "read_file"}}))
        self.assertFalse(tool_call_expected("auto"))

    def test_validates_required_and_unknown_arguments(self) -> None:
        errors = validate_tool_calls([_call("read_file", {"extra": 1})], _tools(), "auto")
        self.assertIn("tool_calls[0].function.arguments.path is required", errors)
        self.assertIn("tool_calls[0].function.arguments.extra is not allowed", errors)

    def test_validates_nested_array_item_types(self) -> None:
        errors = validate_tool_calls(
            [_call("grep_search", {"pattern": "TODO", "paths": ["src", 3]})],
            _tools(),
            "auto",
        )
        self.assertIn("tool_calls[0].function.arguments.paths[1] must have type string", errors)

    def test_validates_unknown_tool_and_specific_choice(self) -> None:
        unknown = validate_tool_calls([_call("shell", {})], _tools(), "auto")
        self.assertIn("is unknown", unknown[0])
        wrong = validate_tool_calls(
            [_call("grep_search", {"pattern": "x"})],
            _tools(),
            {"type": "function", "function": {"name": "read_file"}},
        )
        self.assertIn("must be 'read_file'", wrong[0])

    def test_rejects_invalid_argument_json_and_duplicate_ids(self) -> None:
        errors = validate_tool_calls(
            [_call("read_file", "{"), _call("read_file", {"path": "b"})],
            _tools(),
            "auto",
        )
        self.assertTrue(any("not valid JSON" in error for error in errors))
        self.assertTrue(any("duplicated" in error for error in errors))

    def test_repair_prompt_is_bounded_and_does_not_execute(self) -> None:
        prompt = build_tool_repair_prompt(
            "x" * 20000,
            ["arguments.path is required"],
            _tools(),
            {"type": "function", "function": {"name": "read_file"}},
        )
        self.assertIn("Do not execute a tool", prompt)
        self.assertIn("Use only 'read_file'", prompt)
        self.assertLess(len(prompt), 16000)

    def test_accepts_captured_cursor_mcp_dynamic_tool_shape(self) -> None:
        dynamic_tool = ToolDefinition(function=FunctionDefinition(
            name="CallDynamicTool",
            parameters={
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "toolName": {"type": "string"},
                    "mcpDetails": {
                        "type": "object",
                        "properties": {"description": {"type": "string"}},
                        "required": ["description"],
                    },
                    "arguments": {"type": "object"},
                },
                "required": ["namespace", "toolName"],
            },
        ))
        call = _call("CallDynamicTool", {
            "namespace": "example-mcp",
            "toolName": "search",
            "mcpDetails": {"description": "Search project records."},
            "arguments": {"query": "release"},
        })
        self.assertEqual(validate_tool_calls([call], [dynamic_tool], "auto"), [])

    def test_parse_outcome_keeps_invalid_tool_diagnostics(self) -> None:
        outcome = parse_tool_calls_outcome(
            '{"tool_calls":[{"name":"missing_tool","arguments":{}}]}',
            _tools(),
        )
        self.assertTrue(outcome.has_tool_intent)
        self.assertIsNone(outcome.calls)
        self.assertIn("is unknown", outcome.diagnostics[0])


    def test_parse_tool_calls_accepts_unwrapped_single_call(self) -> None:
        outcome = parse_tool_calls_outcome(
            '{"name": "read_file", "arguments": {"path": "src/main.py"}}',
            _tools(),
        )
        self.assertTrue(outcome.has_tool_intent)
        self.assertIsNotNone(outcome.calls)
        self.assertEqual(len(outcome.calls), 1)
        self.assertEqual(outcome.calls[0].function.name, "read_file")
        self.assertEqual(json.loads(outcome.calls[0].function.arguments), {"path": "src/main.py"})

    def test_parse_tool_calls_accepts_json_array_of_calls(self) -> None:
        outcome = parse_tool_calls_outcome(
            '[{"name": "read_file", "arguments": {"path": "a.py"}}, {"name": "read_file", "arguments": {"path": "b.py"}}]',
            _tools(),
        )
        self.assertTrue(outcome.has_tool_intent)
        self.assertIsNotNone(outcome.calls)
        self.assertEqual(len(outcome.calls), 2)
        self.assertEqual(outcome.calls[0].function.name, "read_file")
        self.assertEqual(outcome.calls[1].function.name, "read_file")

    def test_parse_tool_calls_accepts_xml_tag_envelopes(self) -> None:
        outcome = parse_tool_calls_outcome(
            'I will read the file now:\n<tool_call>\n{"name": "read_file", "arguments": {"path": "README.md"}}\n</tool_call>',
            _tools(),
        )
        self.assertTrue(outcome.has_tool_intent)
        self.assertIsNotNone(outcome.calls)
        self.assertEqual(len(outcome.calls), 1)
        self.assertEqual(outcome.calls[0].function.name, "read_file")

    def test_parse_tool_calls_accepts_string_encoded_arguments(self) -> None:
        outcome = parse_tool_calls_outcome(
            '{"tool_calls": [{"name": "read_file", "arguments": "{\"path\": \"config.json\"}"}]}',
            _tools(),
        )
        self.assertTrue(outcome.has_tool_intent)
        self.assertIsNotNone(outcome.calls)
        self.assertEqual(len(outcome.calls), 1)
        self.assertEqual(json.loads(outcome.calls[0].function.arguments), {"path": "config.json"})

if __name__ == "__main__":
    unittest.main()
