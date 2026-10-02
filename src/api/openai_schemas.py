"""
OpenAI-compatible Pydantic schemas for /v1/chat/completions and /v1/models.

Mirrors the OpenAI Chat Completions API specification so that any OpenAI SDK
or LangChain client can talk to our browser-backed ChatGPT endpoint.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, List, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator


# ── Tool / Function definitions ─────────────────────────────────


class FunctionDefinition(BaseModel):
    """Schema for a function the model may call."""
    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolDefinition(BaseModel):
    """A function tool in either Chat Completions or Responses API format."""
    type: str = "function"
    function: FunctionDefinition

    @model_validator(mode="before")
    @classmethod
    def normalize_responses_function_tool(cls, value: Any) -> Any:
        """Accept the flat Responses API function-tool shape used by Codex."""
        if not isinstance(value, dict) or "function" in value:
            return value
        if value.get("type") != "function" or not value.get("name"):
            return value
        return {
            "type": "function",
            "function": {
                "name": value["name"],
                "description": value.get("description", ""),
                "parameters": value.get("parameters") or {},
            },
        }


class FunctionCallInfo(BaseModel):
    """Info about a specific function call made by the model."""
    name: str
    arguments: str  # JSON string


class ToolCall(BaseModel):
    """A tool call returned by the model."""
    id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex[:24]}")
    type: str = "function"
    function: FunctionCallInfo


# ── Messages ────────────────────────────────────────────────────


class ChatMessage(BaseModel):
    """A single message in the conversation.
    
    Content can be:
    - A simple string
    - A list of content parts (OpenAI vision format + file attachments):
      [
        {"type": "text", "text": "..."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
        {"type": "file", "file": {"filename": "doc.pdf", "data": "<base64>", "mime_type": "application/pdf"}}
      ]
    """
    role: str  # system | user | assistant | tool
    content: Optional[Union[str, List[Any]]] = None
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[list[ToolCall]] = None


# ── Request ─────────────────────────────────────────────────────


class PageExtractionOptions(BaseModel):
    """CatGPT extension for structured page-by-page document extraction."""
    mode: str = "structured"


class ReasoningOptions(BaseModel):
    """Reasoning options accepted by Chat Completions and Responses."""

    effort: Optional[str] = None
    mode: Optional[str] = None
    summary: Optional[str] = None
    context: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    """OpenAI-compatible chat completion request body."""
    model: str = "catgpt-browser"
    messages: list[ChatMessage]
    tools: Optional[list[ToolDefinition]] = None
    tool_choice: Optional[Union[str, dict]] = None  # "auto" | "none" | {"type":"function","function":{"name":"..."}}
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    stop: Optional[Union[str, List[str]]] = None
    stream: Optional[bool] = False
    n: Optional[int] = 1
    user: Optional[str] = None
    reasoning_effort: Optional[str] = None
    reasoning: Optional[ReasoningOptions] = None
    # Durable logical conversation identity (also accepted via header).
    conversation_id: Optional[str] = None
    # CatGPT extension: group independent editor chats onto one project thread.
    project_id: Optional[str] = None
    # CatGPT extension: explicit thread targeting for app-level isolation.
    thread_id: Optional[str] = None
    response_format: Optional[Any] = None
    # CatGPT extension: force structured page-by-page extraction for attachments.
    page_extraction: Optional[PageExtractionOptions] = None
    read_aloud: Optional[bool] = False


# ── Response ────────────────────────────────────────────────────


class UsageInfo(BaseModel):
    """Token usage (estimated — we don't have real token counts)."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChoiceMessage(BaseModel):
    """The assistant's message in a choice."""
    role: str = "assistant"
    content: Optional[str] = None
    tool_calls: Optional[list[ToolCall]] = None
    audio: Optional["AudioInfo"] = None


class AudioInfo(BaseModel):
    """Metadata for browser-generated read-aloud audio."""
    url: str = ""
    local_path: str = ""
    mime_type: str = ""
    size_bytes: int = 0


class Choice(BaseModel):
    """A single completion choice."""
    index: int = 0
    message: ChoiceMessage
    finish_reason: str = "stop"  # "stop" | "tool_calls"


class ChatCompletionResponse(BaseModel):
    """OpenAI-compatible chat completion response."""
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:24]}")
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = "catgpt-browser"
    choices: list[Choice]
    usage: UsageInfo = Field(default_factory=UsageInfo)


class ChatCompletionAsyncRequest(ChatCompletionRequest):
    """Async chat request; same payload as chat completion."""


class ChatCompletionJobResponse(BaseModel):
    """Job status/result for async chat completion."""
    id: str
    object: str = "chat.completion.job"
    created: int = Field(default_factory=lambda: int(time.time()))
    status: str  # queued | running | completed | failed
    model: str = "catgpt-browser"
    response: Optional[ChatCompletionResponse] = None
    error: Optional[str] = None
    error_status_code: Optional[int] = None


# ── Models endpoint ─────────────────────────────────────────────


class ModelObject(BaseModel):
    """A model object for /v1/models."""
    id: str
    object: str = "model"
    created: int = 1700000000
    owned_by: str = "catgpt"


class ModelListResponse(BaseModel):
    """Response for GET /v1/models."""
    object: str = "list"
    data: list[ModelObject]


# ── Image Generation ────────────────────────────────────────────


class ImageGenerationRequest(BaseModel):
    """OpenAI-compatible image generation request (POST /v1/images/generations)."""
    prompt: str
    model: Optional[str] = "dall-e-3"
    n: Optional[int] = Field(default=1, ge=1, le=4)
    size: Optional[str] = "1024x1024"
    quality: Optional[str] = "standard"
    style: Optional[str] = "vivid"
    response_format: Optional[str] = "b64_json"  # "url" or "b64_json"
    user: Optional[str] = None


class ImageData(BaseModel):
    """A single generated image in the response."""
    url: Optional[str] = None
    b64_json: Optional[str] = None
    revised_prompt: Optional[str] = None


class ImagesResponse(BaseModel):
    """OpenAI-compatible image generation response."""
    created: int = Field(default_factory=lambda: int(time.time()))
    data: List[ImageData]


# -- Responses API (OpenAI Responses /v1/responses) ----------------


class ResponseInputItem(BaseModel):
    """An input item for the Responses API.

    Can be a message with role+content or other input types.
    """
    type: str = "message"
    role: str = "user"
    content: Optional[Union[str, List[Any]]] = None
    call_id: Optional[str] = None
    output: Optional[Union[str, List[Any]]] = None


class ResponsesRequest(BaseModel):
    """OpenAI Responses API request body (POST /v1/responses).

    Minimal subset needed by Codex CLI/Desktop compatibility.
    """
    model: str = "catgpt-browser"
    input: Union[str, List[ResponseInputItem]]
    instructions: Optional[str] = None
    max_output_tokens: Optional[int] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    tools: Optional[list[ToolDefinition]] = None
    tool_choice: Optional[Union[str, dict]] = None
    stream: Optional[bool] = False
    metadata: Optional[dict[str, Any]] = None
    user: Optional[str] = None
    reasoning: Optional[ReasoningOptions] = None
    conversation: Optional[Union[str, dict[str, Any]]] = None
    previous_response_id: Optional[str] = None
    # CatGPT extension: group independent Responses API sessions by workspace.
    project_id: Optional[str] = None
    store: Optional[bool] = True
    # CatGPT extension
    read_aloud: Optional[bool] = False

    @field_validator("tools", mode="before")
    @classmethod
    def expand_namespace_tools(cls, value: Any) -> Any:
        """Flatten Codex's Responses API namespace tool groups into functions."""
        if not isinstance(value, list):
            return value
        flattened: list[Any] = []
        for tool in value:
            if isinstance(tool, dict) and tool.get("type") == "namespace":
                nested = tool.get("tools")
                if isinstance(nested, list):
                    flattened.extend(nested)
                    continue
            if isinstance(tool, dict) and tool.get("type") == "function":
                flattened.append(tool)
        return flattened


class ResponseOutputText(BaseModel):
    """A text content part in a Responses API output message."""
    type: str = "output_text"
    text: str = ""
    annotations: list[Any] = Field(default_factory=list)


class ResponseOutputMessage(BaseModel):
    """An output message item in the Responses API response."""
    type: str = "message"
    id: str = Field(default_factory=lambda: f"msg_{uuid.uuid4().hex[:24]}")
    role: str = "assistant"
    content: list[ResponseOutputText] = Field(default_factory=list)


class ResponseOutputToolCall(BaseModel):
    """A tool call output item in the Responses API response."""
    type: str = "function_call"
    id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex[:24]}")
    call_id: str = ""
    name: str = ""
    arguments: str = ""


class ResponsesUsageInfo(BaseModel):
    """Token usage in Responses API format."""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class ResponsesResponse(BaseModel):
    """OpenAI Responses API response envelope."""
    id: str = Field(default_factory=lambda: f"resp_{uuid.uuid4().hex[:24]}")
    object: str = "response"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = "catgpt-browser"
    output: list[Union[ResponseOutputMessage, ResponseOutputToolCall]] = Field(default_factory=list)
    usage: ResponsesUsageInfo = Field(default_factory=ResponsesUsageInfo)
