"""A completed answer should not wait for hidden toolbar controls."""
import unittest
from unittest.mock import AsyncMock, patch
from src.chatgpt import detector


class ResponseReadyLatencyTests(unittest.IsolatedAsyncioTestCase):
    async def run_detector(self, snapshot, previous='old', timeout=5000):
        clock = [0.0]
        async def sleep(seconds):
            clock[0] += seconds
        page = AsyncMock()
        with (
            patch.object(detector, '_scroll_to_bottom', new=AsyncMock()),
            patch.object(detector, '_latest_assistant_turn_snapshot', new=AsyncMock(return_value=snapshot)),
            patch.object(detector, '_conversation_snapshot', new=AsyncMock(return_value={})),
            patch.object(detector.time, 'monotonic', side_effect=lambda: clock[0]),
            patch.object(detector.asyncio, 'sleep', side_effect=sleep),
        ):
            result = await detector._wait_for_copy_button_or_image(page, 0, timeout, previous)
        return result, clock[0]

    async def test_complete_text_without_toolbar_finishes_promptly(self):
        result, seconds = await self.run_detector({'signature': 'new', 'text': 'RELAY_OK'})
        self.assertEqual(result, 'text')
        self.assertLess(seconds, 3)

    async def test_does_not_finish_while_streaming(self):
        result, _ = await self.run_detector({'signature': 'new', 'text': 'Partial answer', 'hasStopButton': True})
        self.assertIsNone(result)

    async def test_does_not_return_previous_answer(self):
        result, _ = await self.run_detector({'signature': 'old', 'text': 'Previous answer'})
        self.assertIsNone(result)

    async def test_transient_status_is_not_an_answer(self):
        result, _ = await self.run_detector({'signature': 'new', 'text': 'Searching the web'})
        self.assertIsNone(result)

    async def test_copy_signal_still_finishes_immediately(self):
        result, seconds = await self.run_detector({'signature': 'new', 'text': 'Complete', 'hasCopyButton': True})
        self.assertEqual(result, 'copy')
        self.assertEqual(seconds, 0)
