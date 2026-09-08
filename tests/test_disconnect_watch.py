from __future__ import annotations

import asyncio
import unittest
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from src.api.disconnect_watch import watch_client_disconnect
from src.config import Config
from src.gemini.detector import wait_for_response_complete


class DisconnectWatchTests(IsolatedAsyncioTestCase):
    async def test_watch_cancels_owner_when_client_disconnects(self) -> None:
        disconnected = False

        async def probe() -> bool:
            return disconnected

        async def work() -> None:
            await asyncio.sleep(8)

        owner = asyncio.create_task(work())
        watch = asyncio.create_task(watch_client_disconnect(probe, owner, poll_s=0.01))
        await asyncio.sleep(0.03)
        disconnected = True
        with self.assertRaises(asyncio.CancelledError):
            await owner
        await asyncio.wait_for(watch, timeout=1)


class GeminiQueueReleaseTests(IsolatedAsyncioTestCase):
    async def test_wait_gives_up_when_no_new_turn_starts(self) -> None:
        snapshot = {
            "found": False,
            "index": -1,
            "signature": "old-turn",
            "hasCopyButton": False,
            "isStreaming": False,
            "text": "",
        }
        with (
            patch("src.gemini.detector._count_copy_buttons", new=AsyncMock(return_value=0)),
            patch(
                "src.gemini.detector._latest_assistant_turn_snapshot",
                new=AsyncMock(return_value=snapshot),
            ),
            patch.object(Config, "POLL_INTERVAL_MS", 1),
            patch.object(Config, "RESPONSE_TIMEOUT", 5000),
        ):
            started = await wait_for_response_complete(
                page=object(),  # type: ignore[arg-type]
                previous_turn_signature="old-turn",
                timeout_ms=800,
            )
        self.assertFalse(started)


if __name__ == "__main__":
    unittest.main()
