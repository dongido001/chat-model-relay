"""Low-cardinality in-process metrics and HTTP request correlation."""

from __future__ import annotations

import time
import uuid
from collections import Counter, defaultdict
from threading import Lock
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send


def _route_family(path: str) -> str:
    """Group paths without retaining user-controlled IDs or app names."""
    if path in {"/healthz", "/metrics"}:
        return path
    if path.startswith("/v1/") or "/v1/" in path:
        suffix = path.split("/v1/", 1)[1].split("/", 1)[0]
        return f"/v1/{suffix}"
    if path.startswith("/api/") or "/api/" in path:
        suffix = path.split("/api/", 1)[1].split("/", 1)[0]
        return f"/api/{suffix}"
    return "/other"


class MetricRegistry:
    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: Counter[str] = Counter()
        self._totals: dict[str, float] = defaultdict(float)

    def increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] += amount

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            self._counters[f"{name}.count"] += 1
            self._totals[f"{name}.total"] += value

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = dict(self._counters)
            totals = dict(self._totals)
        averages = {
            key.removesuffix(".total") + ".average": value / counters.get(key.removesuffix(".total") + ".count", 1)
            for key, value in totals.items()
        }
        return {"counters": counters, "totals": totals, "averages": averages}

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._totals.clear()


metrics = MetricRegistry()


class RequestObservabilityMiddleware:
    """Add a request ID and record method/family/status/duration metrics."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        supplied = headers.get(b"x-request-id", b"").decode("latin-1").strip()
        request_id = supplied[:128] if supplied else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.monotonic()
        status = 500

        async def send_with_request_id(message: dict[str, Any]) -> None:
            nonlocal status
            if message.get("type") == "http.response.start":
                status = int(message.get("status", 500))
                response_headers = list(message.get("headers", []))
                response_headers.append((b"x-request-id", request_id.encode("latin-1")))
                message["headers"] = response_headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            family = _route_family(str(scope.get("path", "")))
            method = str(scope.get("method", "UNKNOWN")).upper()
            metrics.increment(f"http.requests.{method}.{family}.{status}")
            metrics.observe("http.duration_seconds", time.monotonic() - started)
