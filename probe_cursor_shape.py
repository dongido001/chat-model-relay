#!/usr/bin/env python3
"""Send Chat Completions and Responses probes that resemble Cursor Agent traffic."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

BASE = os.environ.get("PROBE_BASE", "http://127.0.0.1:8651")
KEY = os.environ.get("CATGPT_API_KEY", "dummy123")
MODEL = os.environ.get("PROBE_MODEL", "catgpt-browser")
OUT = Path(__file__).resolve().parent / "captures"

CURSORISH_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "ReadFile",
            "description": "Read a file from the workspace",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "EditFile",
            "description": "Write or patch a file",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "contents": {"type": "string"},
                },
                "required": ["path", "contents"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Shell",
            "description": "Run a terminal command",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "working_directory": {"type": "string"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Grep",
            "description": "Search file contents",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["pattern"],
            },
        },
    },
]


def dump(name: str, payload: object) -> None:
    OUT.mkdir(exist_ok=True)
    (OUT / name).write_text(json.dumps(payload, indent=2, default=str)[:1_000_000], encoding="utf-8")


def main() -> int:
    headers = {
        "Authorization": f"Bearer {KEY}",
        "Content-Type": "application/json",
    }
    client = httpx.Client(timeout=30.0)
    health = client.get(f"{BASE}/healthz", headers=headers)
    dump(
        "probe-health.json",
        {"status": health.status_code, "body": health.text[:5000]},
    )
    models = client.get(f"{BASE}/v1/models", headers=headers)
    dump(
        "probe-models.json",
        {"status": models.status_code, "body": models.text[:20000]},
    )

    chat_body = {
        "model": MODEL,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": 'Create a file called hello.txt containing "hello world".',
            }
        ],
        "tools": CURSORISH_TOOLS,
        "tool_choice": "auto",
    }
    # Short timeout: we only need to see whether CatGPT accepts the shape.
    # A logged-in browser turn can take minutes; 8s is enough to classify 4xx vs hang.
    try:
        chat = client.post(
            f"{BASE}/v1/chat/completions",
            headers=headers,
            json=chat_body,
            timeout=12.0,
        )
        dump(
            "probe-chat-completions.json",
            {"status": chat.status_code, "body": chat.text[:50000]},
        )
    except httpx.TimeoutException:
        dump(
            "probe-chat-completions.json",
            {
                "status": "timeout",
                "note": "Accepted enough to start a browser turn (or hung waiting for login).",
            },
        )

    responses_body = {
        "model": MODEL,
        "input": 'Create a file called hello.txt containing "hello world".',
        "tools": CURSORISH_TOOLS,
        "stream": False,
    }
    try:
        resp = client.post(
            f"{BASE}/v1/responses",
            headers=headers,
            json=responses_body,
            timeout=12.0,
        )
        dump(
            "probe-responses.json",
            {"status": resp.status_code, "body": resp.text[:50000]},
        )
    except httpx.TimeoutException:
        dump(
            "probe-responses.json",
            {"status": "timeout", "note": "Request accepted; waiting on browser/login."},
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
