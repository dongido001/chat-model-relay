from __future__ import annotations

import asyncio
import unittest

from src.api.observability import RequestObservabilityMiddleware, _route_family, metrics


class ObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        metrics.reset()

    def test_route_family_drops_app_and_resource_ids(self) -> None:
        self.assertEqual(_route_family("/cline/v1/chat/completions"), "/v1/chat")
        self.assertEqual(_route_family("/api/generate"), "/api/generate")
        self.assertEqual(_route_family("/jobs/private-id"), "/other")

    def test_middleware_adds_request_id_and_metrics(self) -> None:
        events: list[dict] = []

        async def app(scope, receive, send):
            self.assertEqual(scope["state"]["request_id"], "client-request")
            await send({"type": "http.response.start", "status": 201, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/cline/v1/chat/completions",
            "headers": [(b"x-request-id", b"client-request")],
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            events.append(message)

        asyncio.run(RequestObservabilityMiddleware(app)(scope, receive, send))
        headers = dict(events[0]["headers"])
        self.assertEqual(headers[b"x-request-id"], b"client-request")
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["counters"]["http.requests.POST./v1/chat.201"], 1)
        self.assertEqual(snapshot["counters"]["http.duration_seconds.count"], 1)


if __name__ == "__main__":
    unittest.main()
