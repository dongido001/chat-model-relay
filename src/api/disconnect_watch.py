"""Abort an in-flight browser turn when the IDE client disconnects."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from src.log import setup_logging

log = setup_logging("disconnect_watch")

DisconnectProbe = Callable[[], Awaitable[bool]]


async def watch_client_disconnect(
    is_disconnected: DisconnectProbe | None,
    owner_task: asyncio.Task[Any] | None,
    poll_s: float = 0.35,
) -> None:
    """Cancel `owner_task` as soon as the HTTP client is gone (VS Code cancel)."""
    if is_disconnected is None or owner_task is None:
        return
    while not owner_task.done():
        try:
            if await is_disconnected():
                log.warning("Client disconnected; aborting in-flight browser turn")
                owner_task.cancel()
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("Disconnect probe failed: %s", exc)
            return
        await asyncio.sleep(poll_s)


def start_disconnect_watch(http_request: Any | None) -> asyncio.Task[None] | None:
    """Poll FastAPI/Starlette `request.is_disconnected()` on the current task."""
    if http_request is None or not hasattr(http_request, "is_disconnected"):
        return None
    owner = asyncio.current_task()
    if owner is None:
        return None

    async def _probe() -> bool:
        return bool(await http_request.is_disconnected())

    return asyncio.create_task(
        watch_client_disconnect(_probe, owner),
        name="catgpt-disconnect-watch",
    )


async def stop_disconnect_watch(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
