#!/usr/bin/env python3
"""Log inbound OpenAI-shaped traffic, then forward to CatGPT on localhost:8650."""

from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx

UPSTREAM = os.environ.get("CATGPT_UPSTREAM", "http://127.0.0.1:8650")
LISTEN_PORT = int(os.environ.get("CAPTURE_PORT", "8651"))
CAPTURE_DIR = Path(__file__).resolve().parent / "captures"
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
}


def _redact(value: str) -> str:
    if len(value) <= 12:
        return "***"
    return value[:6] + "…" + value[-4:]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[capture] {self.address_string()} {fmt % args}")

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b""
        path = self.path
        stamp = time.strftime("%Y%m%dT%H%M%S")
        rec_id = f"{stamp}-{int(time.time() * 1000) % 100000}"
        headers = {k: v for k, v in self.headers.items()}
        auth = headers.get("Authorization") or headers.get("authorization")
        safe_headers = dict(headers)
        if auth:
            scheme, _, rest = auth.partition(" ")
            safe_headers["Authorization"] = f"{scheme} {_redact(rest or auth)}"
        parsed_body: object
        try:
            parsed_body = json.loads(body.decode("utf-8")) if body else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed_body = {"_raw_b64_len": len(body)}

        record = {
            "id": rec_id,
            "method": self.command,
            "path": path,
            "headers": safe_headers,
            "body": parsed_body,
        }
        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        (CAPTURE_DIR / f"{rec_id}-request.json").write_text(
            json.dumps(record, indent=2, default=str)[:2_000_000],
            encoding="utf-8",
        )

        fwd_headers = {
            k: v
            for k, v in headers.items()
            if k.lower() not in HOP_BY_HOP
        }
        url = f"{UPSTREAM.rstrip('/')}{path}"
        try:
            with httpx.Client(timeout=httpx.Timeout(600.0, connect=10.0)) as client:
                resp = client.request(
                    self.command,
                    url,
                    headers=fwd_headers,
                    content=body if body else None,
                )
        except httpx.HTTPError as exc:
            err = {"error": str(exc), "upstream": url}
            payload = json.dumps(err).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            (CAPTURE_DIR / f"{rec_id}-response.json").write_text(
                json.dumps({"status": 502, **err}, indent=2),
                encoding="utf-8",
            )
            return

        resp_preview: object
        try:
            resp_preview = resp.json()
        except json.JSONDecodeError:
            text = resp.text
            resp_preview = text[:20_000] + ("…" if len(text) > 20_000 else "")

        (CAPTURE_DIR / f"{rec_id}-response.json").write_text(
            json.dumps(
                {
                    "status": resp.status_code,
                    "headers": dict(resp.headers),
                    "body": resp_preview,
                },
                indent=2,
                default=str,
            )[:2_000_000],
            encoding="utf-8",
        )

        self.send_response(resp.status_code)
        for key, value in resp.headers.items():
            if key.lower() in HOP_BY_HOP or key.lower() == "content-length":
                continue
            self.send_header(key, value)
        out = resp.content
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_PUT(self) -> None:
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()

    def do_OPTIONS(self) -> None:
        self._handle()

    def do_HEAD(self) -> None:
        self._handle()


def main() -> None:
    host = "127.0.0.1"
    print(f"capture proxy {host}:{LISTEN_PORT} -> {UPSTREAM}")
    print(f"captures -> {CAPTURE_DIR}")
    ThreadingHTTPServer((host, LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    if urlsplit(UPSTREAM).scheme not in {"http", "https"}:
        raise SystemExit("CATGPT_UPSTREAM must be http(s)")
    main()
