from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from src.api import openai_routes
from src.api.openai_schemas import (
    ChatCompletionRequest,
    ChatMessage,
    FunctionCallInfo,
    FunctionDefinition,
    ToolCall,
    ToolDefinition,
)
from src.config import Config
from src.gemini.client import GeminiClient


def _tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(function=FunctionDefinition(
            name="read_file",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        )),
        ToolDefinition(function=FunctionDefinition(
            name="grep_search",
            parameters={
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        )),
    ]


class _FakeProvider:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []
        self.page = None

    async def new_chat(self) -> None:
        return None

    def _extract_thread_id(self) -> str:
        return "thread-test"

    async def send_message(self, prompt: str, **_kwargs):
        self.prompts.append(prompt)
        if not self.responses:
            raise AssertionError("unexpected provider request")
        return SimpleNamespace(
            message=self.responses.pop(0),
            thread_id="thread-test",
            audio=None,
        )


class _FakeGeminiProvider(GeminiClient):
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []
        self._page = None

    async def new_chat(self) -> None:
        return None

    def _extract_thread_id(self) -> str:
        return "gemini-thread-test"

    async def send_message(self, prompt: str, **_kwargs):
        self.prompts.append(prompt)
        if not self.responses:
            raise AssertionError("unexpected provider request")
        return SimpleNamespace(
            message=self.responses.pop(0),
            thread_id="gemini-thread-test",
            audio=None,
        )


class ToolRoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        openai_routes._response_cache.clear()

    def _run(self, client: _FakeProvider, request: ChatCompletionRequest):
        with ExitStack() as stack:
            stack.enter_context(patch.object(Config, "PROVIDER", "chatgpt"))
            stack.enter_context(patch.object(Config, "API_APP_THREAD_MODE", False))
            stack.enter_context(patch.object(openai_routes, "_get_client", return_value=client))
            return asyncio.run(openai_routes._execute_chat_completion(request))

    def _run_gemini(self, client: _FakeGeminiProvider, request: ChatCompletionRequest):
        with ExitStack() as stack:
            stack.enter_context(patch.object(Config, "PROVIDER", "gemini"))
            stack.enter_context(patch.object(Config, "API_APP_THREAD_MODE", False))
            stack.enter_context(patch.object(openai_routes, "_get_client", return_value=client))
            return asyncio.run(openai_routes._execute_chat_completion(request))

    def test_gemini_plain_command_is_repaired_into_tool_call(self) -> None:
        list_dir = ToolDefinition(function=FunctionDefinition(
            name="list_dir",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        ))
        client = _FakeGeminiProvider([
            "ls -la ~/Projects",
            '{"tool_calls":[{"name":"list_dir","arguments":{"path":"Projects"}}]}',
        ])
        response = self._run_gemini(client, ChatCompletionRequest(
            messages=[ChatMessage(role="user", content="check the files in the Projects folder")],
            tools=[list_dir],
            user="cursor-gemini-envelope",
        ))
        self.assertEqual(response.choices[0].finish_reason, "tool_calls")
        self.assertEqual(response.choices[0].message.tool_calls[0].function.name, "list_dir")
        self.assertEqual(len(client.prompts), 2)
        self.assertIn("not usable by Copilot", client.prompts[1])

    def test_gemini_keeps_first_tool_json_without_repair(self) -> None:
        run = ToolDefinition(function=FunctionDefinition(
            name="run_in_terminal",
            parameters={
                "type": "object",
                "properties": {"input": {"type": "string"}},
                "required": ["input"],
            },
        ))
        client = _FakeGeminiProvider([
            '{"tool_calls":[{"name":"run_in_terminal","arguments":{"command":"mkdir -p folder"}}]}',
        ])
        response = self._run_gemini(client, ChatCompletionRequest(
            messages=[ChatMessage(role="user", content="create a folder")],
            tools=[run],
            user="cursor-gemini-keep-json",
        ))
        self.assertEqual(response.choices[0].finish_reason, "tool_calls")
        args = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
        self.assertEqual(args.get("input") or args.get("command"), "mkdir -p folder")
        self.assertEqual(len(client.prompts), 1)

    def test_gemini_valid_final_envelope_is_unwrapped(self) -> None:
        client = _FakeGeminiProvider(['{"final":"No workspace operation is needed."}'])
        response = self._run_gemini(client, ChatCompletionRequest(
            messages=[ChatMessage(role="user", content="hello")],
            tools=_tools(),
            user="cursor-gemini-final",
        ))
        self.assertEqual(response.choices[0].finish_reason, "stop")
        self.assertEqual(response.choices[0].message.content, "No workspace operation is needed.")
        self.assertEqual(len(client.prompts), 1)

    def test_invalid_arguments_are_repaired_once(self) -> None:
        client = _FakeProvider([
            '{"tool_calls":[{"name":"read_file","arguments":{}}]}',
            '{"tool_calls":[{"name":"read_file","arguments":{"path":"README.md"}}]}',
        ])
        response = self._run(client, ChatCompletionRequest(
            messages=[ChatMessage(role="user", content="Read the README")],
            tools=_tools(),
            tool_choice="required",
            user="cursor-repair-test",
        ))
        choice = response.choices[0]
        self.assertEqual(choice.finish_reason, "tool_calls")
        self.assertEqual(choice.message.tool_calls[0].function.name, "read_file")
        self.assertEqual(len(client.prompts), 2)
        self.assertIn("could not be translated", client.prompts[1])

    def test_unrepairable_tool_call_fails_closed(self) -> None:
        client = _FakeProvider([
            '{"tool_calls":[{"name":"read_file","arguments":{}}]}',
            '{"tool_calls":[{"name":"not_available","arguments":{}}]}',
        ])
        with self.assertRaises(HTTPException) as raised:
            self._run(client, ChatCompletionRequest(
                messages=[ChatMessage(role="user", content="Read a file")],
                tools=_tools(),
                tool_choice="required",
                user="cursor-failed-repair-test",
            ))
        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(raised.exception.detail["type"], "tool_call_translation_error")
        self.assertEqual(len(client.prompts), 2)

    def test_separate_rounds_continue_tools_then_finish(self) -> None:
        client = _FakeProvider([
            '{"tool_calls":[{"name":"read_file","arguments":{"path":"src/app.py"}}]}',
            '{"tool_calls":[{"name":"grep_search","arguments":{"pattern":"TODO"}}]}',
            "The requested inspection is complete.",
        ])
        tools = _tools()
        first = self._run(client, ChatCompletionRequest(
            messages=[ChatMessage(role="user", content="Inspect the project")],
            tools=tools,
            user="cursor-multi-round",
        ))
        first_call = first.choices[0].message.tool_calls[0]
        self.assertEqual(first.choices[0].finish_reason, "tool_calls")

        second = self._run(client, ChatCompletionRequest(
            messages=[
                ChatMessage(role="user", content="Inspect the project"),
                ChatMessage(role="assistant", tool_calls=[first_call]),
                ChatMessage(role="tool", tool_call_id=first_call.id, content="ERROR: file was temporarily locked"),
            ],
            tools=tools,
            user="cursor-multi-round",
        ))
        second_call = second.choices[0].message.tool_calls[0]
        self.assertEqual(second.choices[0].finish_reason, "tool_calls")
        self.assertEqual(second_call.function.name, "grep_search")

        third = self._run(client, ChatCompletionRequest(
            messages=[
                ChatMessage(role="user", content="Inspect the project"),
                ChatMessage(role="assistant", tool_calls=[first_call]),
                ChatMessage(role="tool", tool_call_id=first_call.id, content="ERROR: file was temporarily locked"),
                ChatMessage(role="assistant", tool_calls=[second_call]),
                ChatMessage(role="tool", tool_call_id=second_call.id, content="no TODOs"),
            ],
            tools=tools,
            user="cursor-multi-round",
        ))
        self.assertEqual(third.choices[0].finish_reason, "stop")
        self.assertEqual(third.choices[0].message.content, "The requested inspection is complete.")
        self.assertIn("[Tool result for", client.prompts[1])
        self.assertIn("[Tool result for", client.prompts[2])

    def test_cursor_envelope_continuation_preserves_tool_result(self) -> None:
        client = _FakeProvider(["The result was applied."])
        prior_call = ToolCall(
            id="call_cursor_result",
            function=FunctionCallInfo(name="read_file", arguments='{"path":"README.md"}'),
        )
        response = self._run(client, ChatCompletionRequest(
            messages=[
                ChatMessage(
                    role="user",
                    content=(
                        "large editor contract and replayed context\n"
                        "<user_query>Inspect the README</user_query>"
                    ),
                ),
                ChatMessage(role="assistant", tool_calls=[prior_call]),
                ChatMessage(
                    role="tool",
                    tool_call_id=prior_call.id,
                    content="README contains the installation steps.",
                ),
            ],
            tools=_tools(),
            user="cursor-envelope-result",
        ))
        self.assertEqual(response.choices[0].finish_reason, "stop")
        self.assertIn("Inspect the README", client.prompts[0])
        self.assertIn("README contains the installation steps.", client.prompts[0])
        self.assertNotIn("large editor contract", client.prompts[0])

    def test_large_tool_result_is_compacted_for_browser_only(self) -> None:
        large_result = "x" * 150000
        client = _FakeProvider(["Analysis complete."])
        prior_call = ToolCall(
            id="call_large_result",
            function=FunctionCallInfo(name="read_file", arguments='{"path":"large.log"}'),
        )
        request = ChatCompletionRequest(
            messages=[
                ChatMessage(role="user", content="Inspect the log"),
                ChatMessage(role="assistant", tool_calls=[prior_call]),
                ChatMessage(role="tool", tool_call_id=prior_call.id, content=large_result),
            ],
            tools=_tools(),
            user="cursor-large-result",
        )
        with patch.object(openai_routes.Config, "API_TOOL_RESULT_MAX_CHARS", 2000):
            response = self._run(client, request)
        self.assertEqual(response.choices[0].finish_reason, "stop")
        self.assertIn("[Tool result for call_large_result]", client.prompts[0])
        self.assertIn("tool result truncated: original_chars=150000", client.prompts[0])
        self.assertIn("sha256=", client.prompts[0])
        self.assertLess(len(client.prompts[0]), 3000)
        self.assertEqual(request.messages[-1].content, large_result)


if __name__ == "__main__":
    unittest.main()
