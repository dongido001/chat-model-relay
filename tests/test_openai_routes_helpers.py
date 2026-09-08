from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sys
import types
import unittest

from starlette.requests import Request
from fastapi import HTTPException

# Provide a minimal patchright stub so helper tests can import API modules
# without requiring browser automation dependencies.
_need_patchright_stub = "patchright" not in sys.modules
if _need_patchright_stub:
    try:
        _need_patchright_stub = importlib.util.find_spec("patchright.async_api") is None
    except ModuleNotFoundError:
        _need_patchright_stub = True
if _need_patchright_stub:
    patchright_mod = types.ModuleType("patchright")
    async_api_mod = types.ModuleType("patchright.async_api")
    async_api_mod.Page = object
    async_api_mod.BrowserContext = object
    async_api_mod.Playwright = object
    async_api_mod.Frame = object
    async_api_mod.Request = object
    async_api_mod.Response = object

    async def _fake_async_playwright():
        return None

    async_api_mod.async_playwright = _fake_async_playwright
    sys.modules["patchright"] = patchright_mod
    sys.modules["patchright.async_api"] = async_api_mod

    impl_mod = types.ModuleType("patchright._impl")
    errors_mod = types.ModuleType("patchright._impl._errors")

    class TargetClosedError(Exception):
        pass

    errors_mod.TargetClosedError = TargetClosedError
    sys.modules["patchright._impl"] = impl_mod
    sys.modules["patchright._impl._errors"] = errors_mod

if "playwright_stealth" not in sys.modules and importlib.util.find_spec("playwright_stealth") is None:
    playwright_stealth_mod = types.ModuleType("playwright_stealth")

    class _FakeStealth:
        script_payload = ""

    playwright_stealth_mod.Stealth = _FakeStealth
    sys.modules["playwright_stealth"] = playwright_stealth_mod

from src.api.openai_routes import (
    _anthropic_messages_to_chat_request,
    _apply_tool_prompt_to_messages,
    _build_page_extraction_note,
    _build_page_extraction_response_format,
    _build_tool_continuation_prompt,
    _build_tool_system_prompt,
    _chat_completion_sse_chunk,
    _compact_followup_user_message,
    _detect_user_prefix_contract,
    _display_app_name,
    _apply_isolated_conversation_id,
    _derive_app_key,
    _extract_content_text,
    _first_user_conversation_seed,
    _extract_image_urls,
    _fresh_thread_from_header,
    _infer_expected_item_count,
    _last_user_query,
    _latest_turn_messages,
    _latest_user_message,
    _new_attachments_from_latest_user,
    _parse_tool_calls,
    _looks_like_instruction_prefix,
    _merge_header_rows_in_array,
    _structured_cardinality_mismatch,
    _should_use_line_cardinality_fallback,
    _slim_gemini_browser_prompt,
    _tab_session_key,
    _validate_chat_request,
    _responses_input_to_messages,
    _responses_request_to_chat_request,
    _responses_response_from_chat,
    _validate_responses_request,
)
from src.api.browser_gate import browser_access_lock
from src.api import routes as native_routes
from src.api import openai_routes as openai_routes_module
from src.api.attachment_expander import AttachmentPageDescriptor
from src.config import Config
from src.api.openai_schemas import (
    ChatCompletionRequest,
    ChatMessage,
    ResponsesRequest,
    ResponsesResponse,
    ChatCompletionResponse,
    UsageInfo,
    Choice,
    ChoiceMessage,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponsesUsageInfo,
    ToolCall,
    ToolDefinition,
    FunctionDefinition,
    FunctionCallInfo,
    ReasoningOptions,
)


def _make_request(headers: dict[str, str] | None = None, client_host: str = "127.0.0.1") -> Request:
    hdrs = []
    for key, value in (headers or {}).items():
        hdrs.append((key.lower().encode("latin-1"), value.encode("latin-1")))

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": hdrs,
        "client": (client_host, 12345),
        "server": ("testserver", 80),
        "scheme": "http",
        "query_string": b"",
    }
    return Request(scope)


async def _collect_stream(stream_response) -> list[bytes]:
    chunks: list[bytes] = []
    async for chunk in stream_response.body_iterator:
        if isinstance(chunk, bytes):
            chunks.append(chunk)
        else:
            chunks.append(chunk.encode("utf-8"))
    return chunks


class OpenAIRoutesHelpersTests(unittest.TestCase):
    def test_parse_tool_calls_accepts_markdown_with_closing_brackets(self) -> None:
        """Large replacement payloads must remain structured tool calls."""
        tools = [
            ToolDefinition(
                function=FunctionDefinition(name="StrReplace", parameters={})
            )
        ]
        response_text = (
            '{"tool_calls":[{"name":"StrReplace","arguments":'
            '{"path":"README.md","old_string":"- [x] done ] \\n ```json\\n{}",'
            '"new_string":"updated"}}]}'
        )

        calls = _parse_tool_calls(response_text, tools)

        self.assertIsNotNone(calls)
        assert calls is not None
        self.assertEqual(calls[0].function.name, "StrReplace")
        self.assertEqual(
            calls[0].function.arguments,
            '{"path": "README.md", "old_string": "- [x] done ] \\n ```json\\n{}", "new_string": "updated"}',
        )

    def test_parse_tool_calls_accepts_literal_newlines_in_write_content(self) -> None:
        """Full-file writes can contain literal Markdown line breaks from the model."""
        tools = [ToolDefinition(function=FunctionDefinition(name="Write", parameters={}))]
        response_text = '''{"tool_calls":[{"name":"Write","arguments":{"path":"README.md","contents":"# Title

Paragraph with [a link](https://example.com)."}}]}'''

        calls = _parse_tool_calls(response_text, tools)

        self.assertIsNotNone(calls)
        assert calls is not None
        arguments = json.loads(calls[0].function.arguments)
        self.assertEqual(arguments["contents"], "# Title\n\nParagraph with [a link](https://example.com).")

    def test_parse_tool_calls_repairs_unescaped_quotes_in_shell_command(self) -> None:
        tools = [ToolDefinition(function=FunctionDefinition(name="Shell", parameters={}))]
        response_text = (
            '{"tool_calls":[{"name":"Shell","arguments":'
            '{"command":"python3 -c "from docx import Document; p=\'cv.docx\'; print(1)"}}]}'
        )
        calls = _parse_tool_calls(response_text, tools)
        self.assertIsNotNone(calls)
        assert calls is not None
        arguments = json.loads(calls[0].function.arguments)
        self.assertIn("from docx import Document", arguments["command"])
        self.assertIn("cv.docx", arguments["command"])
        self.assertTrue(arguments["command"].endswith('"'))

    def test_parse_tool_calls_repairs_bracket_adjacent_shell_quotes(self) -> None:
        """A quote before `]]` is shell syntax, not the end of the JSON string."""
        tools = [
            ToolDefinition(
                function=FunctionDefinition(name="run_in_terminal", parameters={})
            )
        ]
        response_text = (
            '{"tool_calls":[{"name":"run_in_terminal","arguments":'
            '{"command":"skills_root="$HOME/.copilot/skills"\\n'
            'if [[ -z "$pdf_skill" ]]; then\\necho \'missing\'\\nfi",'
            '"explanation":"Check skills","mode":"sync"}}]}'
        )

        calls = _parse_tool_calls(response_text, tools)

        self.assertIsNotNone(calls)
        assert calls is not None
        arguments = json.loads(calls[0].function.arguments)
        self.assertEqual(
            arguments["command"],
            'skills_root="$HOME/.copilot/skills"\n'
            'if [[ -z "$pdf_skill" ]]; then\necho \'missing\'\nfi',
        )
        self.assertEqual(arguments["explanation"], "Check skills")
        self.assertEqual(arguments["mode"], "sync")

    def test_parse_tool_calls_repairs_regex_escapes_in_grep_pattern(self) -> None:
        tools = [ToolDefinition(function=FunctionDefinition(name="Grep", parameters={}))]
        response_text = (
            '{"tool_calls":[{"name":"Grep","arguments":'
            '{"pattern":"@router.post(\\(/v1\\/(chat|responses)\\)",'
            '"path":"src/api"}}]}'
        )

        calls = _parse_tool_calls(response_text, tools)

        self.assertIsNotNone(calls)
        assert calls is not None
        arguments = json.loads(calls[0].function.arguments)
        self.assertEqual(arguments["pattern"], "@router.post(\\(/v1\\/(chat|responses)\\)")

    def test_parse_tool_calls_accepts_markdown_fence(self) -> None:
        tools = [ToolDefinition(function=FunctionDefinition(name="Read", parameters={}))]
        response_text = (
            '```json\n{"tool_calls":[{"name":"Read","arguments":{"path":"/tmp/a.txt"}}]}\n```'
        )
        calls = _parse_tool_calls(response_text, tools)
        self.assertIsNotNone(calls)
        assert calls is not None
        self.assertEqual(calls[0].function.name, "Read")

    def test_parse_tool_calls_rejects_mixed_known_and_unknown_calls(self) -> None:
        tools = [ToolDefinition(function=FunctionDefinition(name="Read", parameters={}))]
        response_text = json.dumps({"tool_calls": [
            {"name": "Read", "arguments": {"path": "README.md"}},
            {"name": "Unknown", "arguments": {}},
        ]})
        self.assertIsNone(_parse_tool_calls(response_text, tools))

    def test_fresh_thread_header_validation(self) -> None:
        self.assertTrue(_fresh_thread_from_header(_make_request({"x-catgpt-thread-mode": "fresh"})))
        self.assertFalse(_fresh_thread_from_header(_make_request()))
        with self.assertRaises(HTTPException):
            _fresh_thread_from_header(_make_request({"x-catgpt-thread-mode": "reuse"}))

    def test_fresh_thread_rejects_explicit_routing(self) -> None:
        for field in ("conversation_id", "thread_id"):
            request = ChatCompletionRequest(
                messages=[ChatMessage(role="user", content="hello")],
                **{field: "route-1"},
            )
            with self.subTest(field=field), self.assertRaises(HTTPException):
                _validate_chat_request(request, fresh_thread=True)

    def test_tool_prompt_honors_none_required_and_specific_choices(self) -> None:
        tools = [ToolDefinition(function=FunctionDefinition(name="add_numbers"))]
        self.assertEqual(_build_tool_system_prompt(tools, "none"), "")

        required = _build_tool_system_prompt(tools, "required")
        specific = _build_tool_system_prompt(
            tools,
            {"type": "function", "function": {"name": "add_numbers"}},
        )
        self.assertIn("MUST contain at least one", required)
        self.assertIn("JSON name value MUST be 'add_numbers'", specific)
        self.assertIn("only text\ntransformation", specific)
        self.assertIn("Cursor executes", specific)
        self.assertIn("do not claim files or tools are unavailable", specific)

    def test_tool_continuation_prompt_uses_names_not_full_schema(self) -> None:
        tools = [ToolDefinition(function=FunctionDefinition(name="Read", parameters={"type": "object", "properties": {"path": {"type": "string"}}}))]
        prompt = _build_tool_continuation_prompt(tools)
        self.assertIn("Available tool names: Read", prompt)
        self.assertNotIn('"properties"', prompt)

    def test_tool_prompt_prefixes_latest_text_user_turn_without_mutation(self) -> None:
        messages = [
            ChatMessage(role="assistant", content="Earlier answer"),
            ChatMessage(role="user", content="Call add_numbers"),
        ]
        updated = _apply_tool_prompt_to_messages(messages, "Return JSON")
        self.assertEqual(updated[0], messages[0])
        self.assertIn("Return JSON", updated[1].content)
        self.assertIn("Latest request to transform:\nCall add_numbers", updated[1].content)
        self.assertEqual(messages[1].content, "Call add_numbers")

    def test_tool_prompt_preserves_multimodal_content(self) -> None:
        original_parts = [
            {"type": "text", "text": "Describe this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
        ]
        updated = _apply_tool_prompt_to_messages(
            [ChatMessage(role="user", content=original_parts)],
            "Return JSON",
        )
        self.assertEqual(updated[0].content[1:], original_parts)
        self.assertIn("Return JSON", updated[0].content[0]["text"])

    def test_route_families_share_browser_access_lock(self) -> None:
        self.assertIs(native_routes.browser_access_lock, browser_access_lock)
        self.assertIs(openai_routes_module.browser_access_lock, browser_access_lock)

    def test_derive_app_key_prefers_user(self) -> None:
        req = ChatCompletionRequest(
            messages=[ChatMessage(role="user", content="hello")],
            user="KaraKeep",
        )
        http_req = _make_request({"x-app-name": "mealie"})
        app_key = _derive_app_key(req, http_req)
        self.assertEqual(app_key, "user:karakeep")

    def test_derive_app_key_uses_app_header(self) -> None:
        req = ChatCompletionRequest(messages=[ChatMessage(role="user", content="hello")])
        http_req = _make_request({"x-app-name": "KaraKeep"})
        app_key = _derive_app_key(req, http_req)
        self.assertEqual(app_key, "hdr:x-app-name:karakeep")

    def test_derive_app_key_falls_back_to_origin(self) -> None:
        req = ChatCompletionRequest(messages=[ChatMessage(role="user", content="hello")])
        http_req = _make_request({"origin": "https://app.example.com/path"})
        app_key = _derive_app_key(req, http_req)
        self.assertEqual(app_key, "origin:app.example.com")

    def test_derive_app_key_prefers_endpoint_name(self) -> None:
        req = ChatCompletionRequest(
            messages=[ChatMessage(role="user", content="hello")],
            user="karakeep",
        )
        http_req = _make_request({"x-app-name": "mealie"})
        app_key = _derive_app_key(req, http_req, endpoint_app_name="linkwarden")
        self.assertEqual(app_key, "endpoint:linkwarden")

    def test_display_app_name_from_user_key(self) -> None:
        self.assertEqual(_display_app_name("user:karakeep"), "karakeep")

    def test_display_app_name_from_header_key(self) -> None:
        self.assertEqual(_display_app_name("hdr:x-app-name:mealie"), "mealie")

    def test_line_fallback_rejects_instruction_heavy_prompt(self) -> None:
        text = (
            "[System instructions]\n"
            "You must respond with valid JSON only.\n"
            "$schema: http://json-schema.org/draft-07/schema#\n"
            "<TEXT_CONTENT>\n"
            "TABLE OF CONTENTS\n"
        )
        self.assertFalse(_should_use_line_cardinality_fallback(text))

    def test_line_fallback_accepts_compact_item_list(self) -> None:
        text = "one line\nsecond line\nthird line"
        self.assertTrue(_should_use_line_cardinality_fallback(text))

    def test_infer_expected_item_count_from_json_array(self) -> None:
        messages = [
            ChatMessage(
                role="user",
                content='{"ingredients":[{"food":"salt"},{"food":"pepper"},{"food":"oil"}]}',
            )
        ]
        self.assertEqual(_infer_expected_item_count(messages), 3)

    def test_infer_expected_item_count_skips_instruction_prompt(self) -> None:
        prompt = (
            "[System instructions]\n"
            "You must respond with valid JSON only.\n"
            "$schema\n"
            "<TEXT_CONTENT>\n"
            "line A\nline B\nline C\n"
        )
        messages = [ChatMessage(role="user", content=prompt)]
        self.assertIsNone(_infer_expected_item_count(messages))

    def test_merge_header_rows_moves_header_into_next_note(self) -> None:
        items = [
            {"quantity": None, "unit": None, "food": None, "note": "TO SERVE"},
            {"quantity": 8, "unit": None, "food": "chapattis", "note": None},
        ]
        merged = _merge_header_rows_in_array(items)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["food"], "chapattis")
        self.assertEqual(merged[0]["note"], "TO SERVE")

    def test_instruction_prefix_heuristic_detects_prompt_markers(self) -> None:
        text = (
            "[System instruction: You must respond with valid JSON only]\n"
            "Follow it strictly.\n"
            "<TEXT_CONTENT>\n"
        )
        self.assertTrue(_looks_like_instruction_prefix(text))

    def test_detect_user_prefix_contract_finds_large_shared_prefix(self) -> None:
        prefix = (
            "[System instruction: You must respond with valid JSON only]\n"
            "You are an expert tagger.\n"
            "Follow it strictly.\n"
            "<TEXT_CONTENT>\n"
            + ("A" * 500)
            + "\n"
        )
        prev_text = prefix + "URL: one\nTitle: alpha article"
        curr_text = prefix + "URL: two\nTitle: beta article"
        detected = _detect_user_prefix_contract(prev_text, curr_text)
        self.assertIsNotNone(detected)
        assert detected is not None
        found_prefix, tail = detected
        self.assertTrue(found_prefix.startswith("[System instruction"))
        self.assertTrue(tail.endswith("URL: two\nTitle: beta article"))

    def test_detect_user_prefix_contract_rejects_short_or_non_instruction_prefix(self) -> None:
        prev_text = ("hello world\n" * 20) + "tail one"
        curr_text = ("hello world\n" * 20) + "tail two"
        self.assertIsNone(_detect_user_prefix_contract(prev_text, curr_text))

    def test_detect_user_prefix_contract_with_text_content_marker(self) -> None:
        fixed = (
            "[System instruction: You must respond with valid JSON only]\n"
            "You are an expert tagger.\n"
            "Rules apply.\n"
            "<TEXT_CONTENT>\n"
        )
        prev_text = fixed + ("A" * 1500)
        curr_text = fixed + ("B" * 1500)
        detected = _detect_user_prefix_contract(prev_text, curr_text)
        self.assertIsNotNone(detected)
        assert detected is not None
        _, tail = detected
        self.assertEqual(tail, "B" * 1500)

    def test_validate_chat_request_rejects_unsupported_model(self) -> None:
        req = ChatCompletionRequest(
            model="not-a-real-model",
            messages=[ChatMessage(role="user", content="hello")],
        )
        with self.assertRaises(HTTPException) as ctx:
            _validate_chat_request(req)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Unsupported model", ctx.exception.detail)

    def test_validate_chat_request_rejects_unknown_page_extraction_mode(self) -> None:
        req = ChatCompletionRequest(
            messages=[ChatMessage(role="user", content="hello")],
            page_extraction={"mode": "table"},
        )
        with self.assertRaises(HTTPException) as ctx:
            _validate_chat_request(req)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Unsupported page_extraction.mode", ctx.exception.detail)

    def test_validate_chat_request_rejects_page_extraction_with_response_format(self) -> None:
        req = ChatCompletionRequest(
            messages=[ChatMessage(role="user", content="hello")],
            response_format="json_object",
            page_extraction={"mode": "structured"},
        )
        with self.assertRaises(HTTPException) as ctx:
            _validate_chat_request(req)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("manages response_format automatically", ctx.exception.detail)

    def test_build_page_extraction_note_lists_pages(self) -> None:
        note = _build_page_extraction_note(
            [
                AttachmentPageDescriptor(source_name="contract.pdf", page_number=1, page_index=1, source_kind="pdf"),
                AttachmentPageDescriptor(source_name="contract.pdf", page_number=2, page_index=2, source_kind="pdf"),
            ]
        )
        self.assertIn("[Per-page extraction]", note)
        self.assertIn("exactly 2 item(s)", note)
        self.assertIn("page_index=1", note)
        self.assertIn("page 2", note)

    def test_build_page_extraction_response_format_requires_pages_array(self) -> None:
        response_format = _build_page_extraction_response_format(
            [AttachmentPageDescriptor(source_name="doc.pdf", page_number=1, page_index=1, source_kind="pdf")]
        )
        self.assertEqual(response_format["type"], "json_schema")
        schema = response_format["json_schema"]["schema"]
        self.assertIn("pages", schema["properties"])
        self.assertEqual(schema["required"], ["pages"])

    def test_structured_cardinality_mismatch_uses_explicit_expected_count(self) -> None:
        messages = [ChatMessage(role="user", content="single page")]
        response_text = '{"pages":[{"page_index":1,"source_name":"a.pdf","page_number":1,"text":"a"}]}'
        self.assertIsNone(_structured_cardinality_mismatch(messages, response_text, expected_count=1))
        mismatch = _structured_cardinality_mismatch(messages, '{"pages":[]}', expected_count=1)
        self.assertEqual(mismatch, (1, 0))

    def test_slim_gemini_prompt_keeps_tools_and_user_query(self) -> None:
        req = ChatCompletionRequest(
            messages=[
                ChatMessage(
                    role="user",
                    content="agent contract dump\n<user_query>read README.md</user_query>",
                )
            ],
            tools=[
                ToolDefinition(
                    function=FunctionDefinition(
                        name="read_file",
                        description="Read a file from the workspace",
                        parameters={
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                    )
                )
            ],
        )
        slim = _slim_gemini_browser_prompt(req, list(req.messages))
        self.assertIn("read README.md", slim)
        self.assertIn("read_file", slim)
        self.assertIn("tool_calls", slim)
        self.assertNotIn("agent contract dump", slim)
        self.assertLess(len(slim), 2500)

    def test_slim_gemini_start_it_keeps_structured_envelope(self) -> None:
        req = ChatCompletionRequest(
            messages=[ChatMessage(role="user", content="<user_query>start it</user_query>")],
            tools=[
                ToolDefinition(
                    function=FunctionDefinition(
                        name="run_in_terminal",
                        parameters={
                            "type": "object",
                            "properties": {"command": {"type": "string"}},
                            "required": ["command"],
                        },
                    )
                )
            ],
        )
        slim = _slim_gemini_browser_prompt(req, list(req.messages))
        self.assertIn("start it", slim)
        self.assertIn("run_in_terminal", slim)
        self.assertIn('{"tool_calls"', slim)
        self.assertIn('{"final"', slim)

    def test_slim_gemini_tool_continuation_keeps_result(self) -> None:
        req = ChatCompletionRequest(
            messages=[
                ChatMessage(
                    role="tool",
                    tool_call_id="call_list",
                    content="README.md\nsrc\ntests",
                )
            ],
            tools=[
                ToolDefinition(
                    function=FunctionDefinition(
                        name="list_dir",
                        parameters={
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                    )
                )
            ],
        )
        injected = _apply_tool_prompt_to_messages(
            list(req.messages),
            _build_tool_continuation_prompt(req.tools, req.tool_choice),
        )
        slim = _slim_gemini_browser_prompt(req, injected)
        self.assertIn("README.md\nsrc\ntests", slim)
        self.assertIn("Continue the original user request", slim)
        self.assertNotIn("Latest request to transform", slim)
        self.assertLess(len(slim), 2500)


class ResponsesAPITests(unittest.TestCase):
    def test_responses_input_to_messages_string_form(self) -> None:
        """String input becomes a single user message."""
        messages = _responses_input_to_messages("Hello")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].role, "user")
        self.assertEqual(messages[0].content, "Hello")

    def test_responses_input_to_messages_with_instructions(self) -> None:
        """Instructions prepended as system message."""
        messages = _responses_input_to_messages("Hello", instructions="Be concise")
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].role, "system")
        self.assertEqual(messages[0].content, "Be concise")
        self.assertEqual(messages[1].role, "user")
        self.assertEqual(messages[1].content, "Hello")

    def test_responses_input_to_messages_list_form(self) -> None:
        """List of input items maps by role/content."""
        from src.api.openai_schemas import ResponseInputItem
        items = [
            ResponseInputItem(role="user", content="Hi"),
            ResponseInputItem(role="assistant", content="Hello!"),
            ResponseInputItem(role="user", content="How are you?"),
        ]
        messages = _responses_input_to_messages(items)
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[0].role, "user")
        self.assertEqual(messages[0].content, "Hi")
        self.assertEqual(messages[1].role, "assistant")
        self.assertEqual(messages[1].content, "Hello!")

    def test_responses_input_to_messages_content_parts(self) -> None:
        """Input content parts map to OpenAI chat content parts."""
        items = [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Hello"},
                ],
            }
        ]
        messages = _responses_input_to_messages(items)
        self.assertEqual(len(messages), 1)
        self.assertIsInstance(messages[0].content, list)
        assert isinstance(messages[0].content, list)
        self.assertEqual(messages[0].content[0]["type"], "text")
        self.assertEqual(messages[0].content[0]["text"], "Hello")

    def test_responses_request_to_chat_request_basic(self) -> None:
        """ResponsesRequest translates to ChatCompletionRequest."""
        req = ResponsesRequest(
            model="catgpt-browser",
            input="Hello",
            instructions="Be concise",
            temperature=0.5,
            max_output_tokens=100,
        )
        chat_req = _responses_request_to_chat_request(req)
        self.assertEqual(chat_req.model, "catgpt-browser")
        self.assertEqual(len(chat_req.messages), 2)
        self.assertEqual(chat_req.messages[0].role, "system")
        self.assertEqual(chat_req.temperature, 0.5)
        self.assertEqual(chat_req.max_tokens, 100)

    def test_responses_request_to_chat_request_with_tools(self) -> None:
        """Tools are forwarded to ChatCompletionRequest."""
        req = ResponsesRequest(
            model="catgpt-browser",
            input="What's the weather?",
            tools=[
                {"type": "function", "function": {"name": "get_weather", "description": "Get weather", "parameters": {}}}
            ],
            tool_choice="auto",
        )
        chat_req = _responses_request_to_chat_request(req)
        self.assertEqual(len(chat_req.tools), 1)
        self.assertEqual(chat_req.tool_choice, "auto")

    def test_responses_reasoning_effort_translates_to_chat_field(self) -> None:
        req = ResponsesRequest(
            model="gpt-5.6-sol",
            input="Hello",
            reasoning=ReasoningOptions(effort="high"),
        )
        chat_req = _responses_request_to_chat_request(req)
        self.assertEqual(chat_req.reasoning_effort, "high")

    def test_validate_chat_request_accepts_stream(self) -> None:
        """Stream=true is allowed; route handlers emit pseudo-SSE after completion."""
        req = ChatCompletionRequest(
            model="catgpt-browser",
            messages=[ChatMessage(role="user", content="hello")],
            stream=True,
        )
        _validate_chat_request(req)

    def test_chat_completion_sse_chunk_uses_openai_shape(self) -> None:
        response = ChatCompletionResponse(
            model="catgpt-browser",
            choices=[Choice(message=ChoiceMessage(role="assistant", content="ok"))],
            usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )
        chunk = _chat_completion_sse_chunk(response, {"content": "ok"})
        self.assertIn('"object":"chat.completion.chunk"', chunk.replace(" ", ""))
        self.assertIn('"content":"ok"', chunk.replace(" ", ""))

    def test_responses_request_to_chat_request_preserves_stream_flag(self) -> None:
        """Responses stream flag is forwarded for route-level SSE handling."""
        req = ResponsesRequest(
            model="catgpt-browser",
            input="Hello",
            stream=True,
        )
        chat_req = _responses_request_to_chat_request(req)
        self.assertTrue(chat_req.stream)

    def test_responses_response_from_chat_converts_content(self) -> None:
        """Chat completion response converts to Responses API format."""
        chat_response = ChatCompletionResponse(
            model="catgpt-browser",
            choices=[
                Choice(
                    message=ChoiceMessage(
                        role="assistant",
                        content="Hello! How can I help?",
                    ),
                )
            ],
            usage=UsageInfo(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )
        resp = _responses_response_from_chat(chat_response, "catgpt-browser")
        self.assertEqual(resp.object, "response")
        self.assertEqual(len(resp.output), 1)
        self.assertEqual(resp.output[0].role, "assistant")
        self.assertEqual(len(resp.output[0].content), 1)
        self.assertEqual(resp.output[0].content[0].text, "Hello! How can I help?")
        self.assertEqual(resp.usage.input_tokens, 10)
        self.assertEqual(resp.usage.output_tokens, 5)
        self.assertEqual(resp.usage.total_tokens, 15)

    def test_responses_response_from_chat_includes_tool_calls(self) -> None:
        """Tool calls are added to Responses output items."""
        chat_response = ChatCompletionResponse(
            model="catgpt-browser",
            choices=[
                Choice(
                    message=ChoiceMessage(
                        role="assistant",
                        content="Calling tool",
                        tool_calls=[
                            ToolCall(
                                id="call_123",
                                function=FunctionCallInfo(
                                    name="get_weather",
                                    arguments='{"city":"Paris"}',
                                ),
                            )
                        ],
                    ),
                )
            ],
            usage=UsageInfo(prompt_tokens=3, completion_tokens=2, total_tokens=5),
        )
        resp = _responses_response_from_chat(chat_response, "catgpt-browser")
        self.assertEqual(len(resp.output), 2)
        self.assertEqual(resp.output[1].type, "tool_call")

    def test_execute_responses_forwards_app_key_override(self) -> None:
        """Responses execution preserves app-scoped routing keys."""
        captured: dict[str, str] = {}

        async def fake_execute_chat_completion(
            request: ChatCompletionRequest,
            app_key_override: str = "",
            http_request=None,
            **_kwargs,
        ) -> ChatCompletionResponse:
            captured["app_key_override"] = app_key_override
            return ChatCompletionResponse(
                model=request.model,
                choices=[Choice(message=ChoiceMessage(role="assistant", content="ok"))],
                usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )

        original = openai_routes_module._execute_chat_completion
        openai_routes_module._execute_chat_completion = fake_execute_chat_completion
        try:
            req = ResponsesRequest(model="catgpt-browser", input="Hello")
            resp = asyncio.run(
                openai_routes_module._execute_responses(
                    req,
                    app_key_override="endpoint:n8n",
                )
            )
        finally:
            openai_routes_module._execute_chat_completion = original

        self.assertEqual(captured["app_key_override"], "endpoint:n8n")
        self.assertEqual(resp.output[0].content[0].text, "ok")

    def test_execute_chat_streaming_uses_non_stream_browser_call(self) -> None:
        """Chat stream requests execute the browser call without stream=true."""
        captured: dict[str, bool] = {}

        async def fake_execute_chat_completion(
            request: ChatCompletionRequest,
            app_key_override: str = "",
            http_request=None,
            **_kwargs,
        ) -> ChatCompletionResponse:
            captured["stream"] = bool(request.stream)
            return ChatCompletionResponse(
                model=request.model,
                choices=[Choice(message=ChoiceMessage(role="assistant", content="ok"))],
                usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )

        original = openai_routes_module._execute_chat_completion
        openai_routes_module._execute_chat_completion = fake_execute_chat_completion
        try:
            req = ChatCompletionRequest(
                model="catgpt-browser",
                messages=[ChatMessage(role="user", content="Hello")],
                stream=True,
            )
            _validate_chat_request(req)

            async def _run_stream_test() -> bytes:
                stream_response = await openai_routes_module._stream_chat_completion(req)
                chunks = await _collect_stream(stream_response)
                return b"".join(chunks)

            body = asyncio.run(_run_stream_test())
        finally:
            openai_routes_module._execute_chat_completion = original

        self.assertFalse(captured["stream"])
        self.assertIn(b"chat.completion.chunk", body)
        self.assertIn(b"[DONE]", body)

    def test_streaming_tool_call_has_no_text_delta(self) -> None:
        """Cursor must receive a structured call, never the model's JSON as text."""
        async def fake_execute_chat_completion(
            request: ChatCompletionRequest,
            app_key_override: str = "",
            http_request=None,
            **_kwargs,
        ) -> ChatCompletionResponse:
            return ChatCompletionResponse(
                model=request.model,
                choices=[
                    Choice(
                        message=ChoiceMessage(
                            role="assistant",
                            content=None,
                            tool_calls=[
                                ToolCall(
                                    id="call_123",
                                    function=FunctionCallInfo(
                                        name="StrReplace",
                                        arguments='{"path":"README.md"}',
                                    ),
                                )
                            ],
                        ),
                        finish_reason="tool_calls",
                    )
                ],
            )

        original = openai_routes_module._execute_chat_completion
        openai_routes_module._execute_chat_completion = fake_execute_chat_completion
        try:
            req = ChatCompletionRequest(
                model="catgpt-browser",
                messages=[ChatMessage(role="user", content="Update the README")],
                stream=True,
            )

            async def _run_stream_test() -> bytes:
                stream_response = await openai_routes_module._stream_chat_completion(req)
                return b"".join(await _collect_stream(stream_response))

            body = asyncio.run(_run_stream_test())
        finally:
            openai_routes_module._execute_chat_completion = original

        self.assertIn(b'"tool_calls"', body)
        self.assertIn(b'"finish_reason":"tool_calls"', body)
        self.assertNotIn(b'"content":"{\\"tool_calls', body)

    def test_execute_responses_accepts_streaming_clients_without_streaming_browser(self) -> None:
        """Responses stream requests are executed as non-stream browser calls."""
        captured: dict[str, bool] = {}

        async def fake_execute_chat_completion(
            request: ChatCompletionRequest,
            app_key_override: str = "",
            http_request=None,
            **_kwargs,
        ) -> ChatCompletionResponse:
            captured["stream"] = bool(request.stream)
            return ChatCompletionResponse(
                model=request.model,
                choices=[Choice(message=ChoiceMessage(role="assistant", content="ok"))],
                usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )

        original = openai_routes_module._execute_chat_completion
        openai_routes_module._execute_chat_completion = fake_execute_chat_completion
        try:
            req = ResponsesRequest(model="catgpt-browser", input="Hello", stream=True)
            _validate_responses_request(req)
            resp = asyncio.run(openai_routes_module._execute_responses(req))
        finally:
            openai_routes_module._execute_chat_completion = original

        self.assertFalse(captured["stream"])
        self.assertEqual(resp.output[0].content[0].text, "ok")

    def test_validate_responses_request_rejects_empty_input(self) -> None:
        """Empty input raises HTTPException."""
        req = ResponsesRequest(model="catgpt-browser", input="")
        with self.assertRaises(HTTPException) as ctx:
            _validate_responses_request(req)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_validate_responses_request_rejects_unsupported_model(self) -> None:
        """Unsupported model raises HTTPException."""
        req = ResponsesRequest(model="not-a-model", input="Hello")
        with self.assertRaises(HTTPException) as ctx:
            _validate_responses_request(req)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Unsupported model", ctx.exception.detail)

    def test_latest_turn_messages_keeps_system_and_latest_user_tools(self) -> None:
        messages = [
            ChatMessage(role="system", content="Be brief"),
            ChatMessage(role="user", content="first"),
            ChatMessage(role="assistant", content="ok"),
            ChatMessage(role="user", content="second"),
            ChatMessage(role="tool", content="tool-result", tool_call_id="call_1"),
        ]
        pruned = _latest_turn_messages(messages)
        self.assertEqual([m.role for m in pruned], ["system", "user", "tool"])
        self.assertEqual(pruned[1].content, "second")

    def test_latest_turn_messages_can_omit_system_on_existing_thread(self) -> None:
        messages = [
            ChatMessage(role="system", content="Be brief"),
            ChatMessage(role="user", content="first"),
            ChatMessage(role="assistant", content="ok"),
            ChatMessage(role="user", content="second"),
        ]
        pruned = _latest_turn_messages(messages, include_system=False)
        self.assertEqual([m.role for m in pruned], ["user"])
        self.assertEqual(pruned[0].content, "second")

    def test_latest_user_message_skips_older_turns(self) -> None:
        first = ChatMessage(
            role="user",
            content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,aaa"}}],
        )
        second = ChatMessage(role="user", content="hello")
        messages = [first, ChatMessage(role="assistant", content="ok"), second]
        latest = _latest_user_message(messages)
        self.assertIs(latest, second)
        self.assertEqual(_extract_image_urls(latest.content), [])

    def test_compact_followup_keeps_only_user_query(self) -> None:
        message = ChatMessage(
            role="user",
            content="skills dump\n<user_query>how are you?</user_query>",
        )
        compact = _compact_followup_user_message(message)
        self.assertEqual(compact.content, "how are you?")
        self.assertEqual(_last_user_query(message.content), "how are you?")

    def test_compact_followup_keeps_vscode_user_request(self) -> None:
        message = ChatMessage(
            role="user",
            content="agent context\n<userRequest>Change the chat theme.</userRequest>",
        )

        compact = _compact_followup_user_message(message)

        self.assertEqual(_extract_content_text(compact.content), "Change the chat theme.")

    def test_tool_contract_survives_followup_compaction(self) -> None:
        message = ChatMessage(
            role="user",
            content="large Cursor payload\n<user_query>read the compose file</user_query>",
        )
        compacted = _compact_followup_user_message(message)
        updated = _apply_tool_prompt_to_messages([compacted], "Return a Read tool call.")

        self.assertIn("Return a Read tool call.", updated[0].content)
        self.assertIn("read the compose file", updated[0].content)

    def test_new_attachments_ignore_images_already_in_history(self) -> None:
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,aaa"}}
        first = ChatMessage(role="user", content=[image, {"type": "text", "text": "see screenshot"}])
        second = ChatMessage(
            role="user",
            content=[image, {"type": "text", "text": "<user_query>hello</user_query>"}],
        )
        urls, files = _new_attachments_from_latest_user(
            [first, ChatMessage(role="assistant", content="ok"), second]
        )
        self.assertEqual(urls, [])
        self.assertEqual(files, [])

    def test_new_attachments_keep_first_turn_image(self) -> None:
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,aaa"}}
        first = ChatMessage(role="user", content=[image, {"type": "text", "text": "see screenshot"}])
        urls, files = _new_attachments_from_latest_user([first])
        self.assertEqual(urls, ["data:image/png;base64,aaa"])
        self.assertEqual(files, [])

    def test_tab_session_key_prefers_session_header(self) -> None:
        req = ChatCompletionRequest(
            messages=[ChatMessage(role="user", content="hello")],
            user="alice",
            thread_id="thread-1",
        )
        http_req = _make_request({"x-session-id": "sess-9"})
        self.assertEqual(_tab_session_key(req, http_req, app_key="user:alice"), "sess-9")

    def test_tab_session_key_falls_back_to_app_key(self) -> None:
        req = ChatCompletionRequest(messages=[ChatMessage(role="user", content="hello")])
        self.assertEqual(_tab_session_key(req, None, app_key="endpoint:mealie"), "app:endpoint:mealie")

    def test_isolated_conversation_id_hashes_first_cursor_query(self) -> None:
        first = ChatMessage(
            role="user",
            content="contract\n<user_query>Read pyproject.toml</user_query>",
        )
        follow_up = ChatMessage(
            role="user",
            content="contract\n<user_query>Read pyproject.toml</user_query>\n"
            "<user_query>also add tests</user_query>",
        )
        req1 = ChatCompletionRequest(messages=[first], user="githubcopilotchat")
        req2 = ChatCompletionRequest(
            messages=[first, ChatMessage(role="assistant", content="ok"), follow_up],
            user="githubcopilotchat",
        )
        with unittest.mock.patch.object(Config, "API_DERIVE_CONVERSATION_ID", True):
            isolated1 = _apply_isolated_conversation_id(req1, None, False)
            isolated2 = _apply_isolated_conversation_id(req2, None, False)
        seed = _first_user_conversation_seed(req1.messages)
        expected = "derived:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
        self.assertEqual(isolated1.conversation_id, expected)
        self.assertEqual(isolated2.conversation_id, expected)
        self.assertEqual(seed, "Read pyproject.toml")

    def test_isolated_conversation_id_uses_session_header(self) -> None:
        req = ChatCompletionRequest(messages=[ChatMessage(role="user", content="hello")])
        http_req = _make_request({"x-session-id": "cursor-thread-42"})
        with unittest.mock.patch.object(Config, "API_DERIVE_CONVERSATION_ID", True):
            isolated = _apply_isolated_conversation_id(req, http_req, False)
        self.assertEqual(isolated.conversation_id, "cursor-thread-42")

    def test_isolated_conversation_id_skips_explicit_and_fresh(self) -> None:
        req = ChatCompletionRequest(
            messages=[ChatMessage(role="user", content="hello")],
            conversation_id="keep-me",
        )
        with unittest.mock.patch.object(Config, "API_DERIVE_CONVERSATION_ID", True):
            same = _apply_isolated_conversation_id(req, None, False)
            fresh = _apply_isolated_conversation_id(
                ChatCompletionRequest(messages=[ChatMessage(role="user", content="hello")]),
                None,
                True,
            )
        self.assertEqual(same.conversation_id, "keep-me")
        self.assertFalse(fresh.conversation_id)

    def test_isolated_conversation_id_can_be_disabled(self) -> None:
        req = ChatCompletionRequest(messages=[ChatMessage(role="user", content="hello")])
        with unittest.mock.patch.object(Config, "API_DERIVE_CONVERSATION_ID", False):
            isolated = _apply_isolated_conversation_id(req, None, False)
        self.assertFalse(isolated.conversation_id)

    def test_anthropic_messages_to_chat_request(self) -> None:
        body = {
            "model": "catgpt-browser",
            "system": "You are helpful",
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "Hello from Claude Code"}],
                }
            ],
            "session_id": "cli-1",
        }
        converted = _anthropic_messages_to_chat_request(body)
        self.assertEqual(converted.user, "cli-1")
        self.assertEqual(converted.messages[0].role, "system")
        self.assertEqual(converted.messages[1].content, "Hello from Claude Code")


if __name__ == "__main__":
    unittest.main()
