"""
Response completion detector for Gemini (gemini.google.com).

Primary strategy: stop-button lifecycle, then copy-button appearance,
then text stability on the latest model response.
"""

from __future__ import annotations

import asyncio
import re
import time

from patchright.async_api import Page

from src.browser.human import idle_mouse_movement
from src.config import Config
from src.log import setup_logging

log = setup_logging("gemini_detector")

_SNAPSHOT_JS = """
() => {
    const stop = document.querySelector(
        'button[aria-label="Stop responding"], button[aria-label="Stop generating"]'
    );
    const copySelector = 'button[aria-label="Copy response"], button[aria-label="Copy"], button[aria-label*="Copy" i]';
    const nodes = Array.from(document.querySelectorAll(
        'model-response, .model-response-text, [data-test-id="model-response-text"], message-content, .response-content, .markdown.markdown-main-panel'
    ));
    let last = null;
    let idx = -1;
    let text = '';
    for (let i = nodes.length - 1; i >= 0; i--) {
        const candidate = (nodes[i].innerText || '').trim();
        if (candidate) {
            last = nodes[i];
            idx = i;
            text = candidate;
            break;
        }
    }
    if (!last) {
        return {
            found: nodes.length > 0,
            index: nodes.length - 1,
            signature: null,
            hasCopyButton: false,
            isStreaming: Boolean(stop),
            text: '',
        };
    }
    const signature = `${idx}:${text.substring(0, 80)}`;
    const hasCopyButton = Boolean(last.querySelector(copySelector));
    return {
        found: true,
        index: idx,
        signature,
        hasCopyButton,
        isStreaming: Boolean(stop),
        text,
    };
}
"""


def normalize_assistant_text(text: str | None) -> str:
    """Normalize extracted assistant text for validation and comparisons."""
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^Gemini said:\s*", "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned


def is_incomplete_response_text(text: str | None) -> bool:
    """Heuristic: true when text looks like transient thinking/status."""
    cleaned = normalize_assistant_text(text)
    if not cleaned:
        return True

    lower = cleaned.lower()
    if re.fullmatch(r"gemini said:?", lower):
        return True
    markers = [
        "thinking",
        "analyzing",
        "searching",
        "working on",
        "please wait",
        "just a sec",
        "generating",
        "defining the task",
        "planning",
        "thought for",
    ]

    if any(marker in lower for marker in markers):
        if len(cleaned) < 240:
            return True
        if lower.startswith(tuple(markers)):
            return True

    if "gemini said" in lower and len(cleaned) < 80:
        return True

    return False


def _empty_snapshot() -> dict:
    return {
        "found": False,
        "index": -1,
        "signature": None,
        "hasCopyButton": False,
        "isStreaming": False,
        "text": "",
    }


async def _latest_assistant_turn_snapshot(page: Page) -> dict:
    """Return metadata for the latest Gemini model response."""
    try:
        snapshot = await asyncio.wait_for(page.evaluate(_SNAPSHOT_JS), 8)
    except Exception:
        return _empty_snapshot()
    if not isinstance(snapshot, dict):
        return _empty_snapshot()

    normalized = _empty_snapshot()
    normalized.update(snapshot)
    return normalized


async def get_latest_assistant_turn_signature(page: Page) -> str | None:
    """Return signature for the latest assistant turn, if available."""
    snapshot = await _latest_assistant_turn_snapshot(page)
    signature = snapshot.get("signature")
    return signature if isinstance(signature, str) and signature else None


async def count_assistant_messages(page: Page) -> int:
    """Count visible model-response nodes."""
    snapshot = await _latest_assistant_turn_snapshot(page)
    if not snapshot.get("found"):
        return 0
    return int(snapshot.get("index") or 0) + 1


async def _count_copy_buttons(page: Page) -> int:
    """Count copy buttons currently in the document."""
    try:
        count = await asyncio.wait_for(
            page.evaluate(
                """
                () => document.querySelectorAll(
                    'button[aria-label="Copy response"], button[aria-label="Copy"], button[aria-label*="Copy" i]'
                ).length
                """
            ),
            8,
        )
    except Exception:
        return 0
    return int(count or 0)


async def _wait_for_new_turn_signature(
    page: Page,
    previous_turn_signature: str,
    timeout_ms: int,
) -> bool:
    """Wait until latest assistant-turn signature differs from previous one."""
    elapsed = 0
    poll_interval = Config.POLL_INTERVAL_MS / 1000
    heartbeat = 10

    while elapsed * 1000 < timeout_ms:
        snapshot = await _latest_assistant_turn_snapshot(page)
        signature = snapshot.get("signature")
        if isinstance(signature, str) and signature and signature != previous_turn_signature:
            log.debug(f"New assistant turn detected: {signature} (prev: {previous_turn_signature})")
            return True

        if elapsed > 0 and elapsed % heartbeat == 0:
            log.debug(f"Still waiting for new assistant turn... ({int(elapsed)}s)")

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    log.debug("Timed out waiting for a new assistant-turn signature")
    return False


def _remaining_timeout_ms(start: float, timeout_ms: int) -> int:
    elapsed_ms = int((time.monotonic() - start) * 1000)
    return max(0, timeout_ms - elapsed_ms)


async def wait_for_response_complete(
    page: Page,
    expected_msg_count: int | None = None,
    timeout_ms: int | None = None,
    previous_turn_signature: str | None = None,
) -> bool:
    """Wait until Gemini finishes. One timeout budget, like ChatGPT."""
    timeout = timeout_ms or Config.RESPONSE_TIMEOUT
    start = time.monotonic()
    log.info(f"Waiting for response (timeout: {timeout}ms)...")

    pre_copy_count = await _count_copy_buttons(page)
    log.debug(f"Copy buttons before send: {pre_copy_count}")

    if previous_turn_signature:
        wait_ms = min(20000, _remaining_timeout_ms(start, timeout))
        if wait_ms > 0:
            log.debug(f"Previous assistant turn signature: {previous_turn_signature}")
            started = await _wait_for_new_turn_signature(
                page, previous_turn_signature, timeout_ms=wait_ms
            )
            if not started:
                log.warning(
                    "Gemini did not start a new reply; releasing the queue slot"
                )
                return False
    elif expected_msg_count is not None:
        log.debug(f"Waiting for assistant message #{expected_msg_count}...")
        waited = 0
        wait_ms = min(15000, _remaining_timeout_ms(start, timeout))
        while waited < wait_ms:
            current_count = await count_assistant_messages(page)
            if current_count >= expected_msg_count:
                log.debug(f"Assistant message target reached (count: {current_count})")
                break
            await asyncio.sleep(0.4)
            waited += 400
        current_count = await count_assistant_messages(page)
        if current_count < expected_msg_count:
            snapshot = await _latest_assistant_turn_snapshot(page)
            if not snapshot.get("isStreaming"):
                log.warning(
                    "Gemini never produced assistant message #%s; releasing the queue slot",
                    expected_msg_count,
                )
                return False

    remaining = _remaining_timeout_ms(start, timeout)
    if remaining <= 0:
        return False

    log.debug("Waiting for Gemini completion signals...")
    quick_ms = min(max(8000, timeout // 5), remaining)
    if await _wait_for_fast_completion_signal(page, pre_copy_count, quick_ms, previous_turn_signature):
        log.info("Response complete — fast DOM completion signal")
        return True

    remaining = _remaining_timeout_ms(start, timeout)
    if remaining <= 0:
        return False

    log.debug("Waiting for streaming to complete (stop button gone)...")
    if await _wait_for_streaming_complete(page, remaining, previous_turn_signature):
        log.info("Response complete — streaming finished")
        return True

    remaining = _remaining_timeout_ms(start, timeout)
    if remaining <= 0:
        return False

    log.info("Falling back to text-stability detection...")
    try:
        return await _wait_via_text_stability(page, remaining, previous_turn_signature)
    except Exception as e:
        log.error(f"All strategies failed: {e}")
        return False


async def _wait_for_streaming_complete(
    page: Page,
    timeout_ms: int,
    previous_turn_signature: str | None = None,
) -> bool:
    """Wait until Gemini's stop button disappears after a new turn starts."""
    elapsed = 0
    poll_interval = Config.POLL_INTERVAL_MS / 1000
    heartbeat = 10
    saw_stream = False

    while elapsed * 1000 < timeout_ms:
        snapshot = await _latest_assistant_turn_snapshot(page)
        signature = snapshot.get("signature")
        is_new_turn = previous_turn_signature is None or (
            isinstance(signature, str) and signature != previous_turn_signature
        )
        if snapshot.get("isStreaming"):
            saw_stream = True

        if is_new_turn and snapshot.get("found") and not snapshot.get("isStreaming") and (
            saw_stream or (snapshot.get("text") or "").strip()
        ):
            if is_incomplete_response_text(snapshot.get("text")):
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval
                continue
            await asyncio.sleep(0.5)
            elapsed += 0.5
            verify = await _latest_assistant_turn_snapshot(page)
            if not verify.get("isStreaming") and not is_incomplete_response_text(verify.get("text")):
                log.debug(f"Streaming complete on turn {signature}")
                return True

        if elapsed > 0 and elapsed % heartbeat == 0:
            log.debug(f"Still streaming... ({int(elapsed)}s)")
            await idle_mouse_movement(page)

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    log.warning(f"Streaming did not complete after {int(elapsed)}s")
    return False


async def _wait_for_fast_completion_signal(
    page: Page,
    pre_count: int,
    timeout_ms: int,
    previous_turn_signature: str | None = None,
) -> bool:
    """Accept the first reliable copy, stream-finished, or stable-text signal."""
    elapsed = 0
    poll_interval = Config.POLL_INTERVAL_MS / 1000
    saw_stream = False
    stable_count = 0
    last_text = ""

    while elapsed * 1000 < timeout_ms:
        snapshot = await _latest_assistant_turn_snapshot(page)
        signature = snapshot.get("signature")
        text = snapshot.get("text") if isinstance(snapshot.get("text"), str) else ""
        is_new_turn = previous_turn_signature is None or (
            isinstance(signature, str) and signature != previous_turn_signature
        )

        if is_new_turn and snapshot.get("hasCopyButton") and text.strip():
            current_count = await _count_copy_buttons(page)
            log.debug(
                f"Copy button detected on latest turn {signature} "
                f"(copy-buttons: {pre_count} -> {current_count})"
            )
            return True

        if is_new_turn and snapshot.get("isStreaming"):
            saw_stream = True
            stable_count = 0
            last_text = text
        elif is_new_turn and snapshot.get("found") and text.strip() and not is_incomplete_response_text(text):
            if saw_stream:
                log.debug("Gemini stop button disappeared on latest turn %s", signature)
                return True
            if text == last_text:
                stable_count += 1
            else:
                stable_count = 0
                last_text = text
            if stable_count >= 2:
                log.debug("Gemini latest-turn text stabilized without a copy button")
                return True
        else:
            stable_count = 0
            if not is_new_turn:
                last_text = ""

        if elapsed > 0 and int(elapsed) % 10 == 0:
            await idle_mouse_movement(page)

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    return False


async def _wait_via_text_stability(
    page: Page,
    timeout_ms: int,
    previous_turn_signature: str | None = None,
) -> bool:
    """Last resort: poll latest assistant-turn text and wait until stable."""
    stable_count = 0
    required_stable = 3
    last_text = ""
    elapsed = 0
    poll_interval = Config.POLL_INTERVAL_MS / 1000

    while elapsed * 1000 < timeout_ms:
        snapshot = await _latest_assistant_turn_snapshot(page)
        signature = snapshot.get("signature")
        text = snapshot.get("text") if isinstance(snapshot.get("text"), str) else ""

        if previous_turn_signature and signature == previous_turn_signature:
            stable_count = 0
            last_text = ""
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            continue

        if snapshot.get("isStreaming"):
            stable_count = 0
            last_text = text
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            continue

        if text and text == last_text:
            stable_count += 1
            log.debug(f"Text stable ({stable_count}/{required_stable})")
            if stable_count >= required_stable:
                if is_incomplete_response_text(text):
                    log.debug("Stable text looks like transient status; continuing wait")
                    stable_count = 0
                    last_text = text
                    await asyncio.sleep(poll_interval)
                    elapsed += poll_interval
                    continue
                log.info("Response text stabilized — complete")
                return True
        else:
            stable_count = 0
            last_text = text

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    log.warning(f"Text stability timed out after {int(elapsed)}s")
    return False


async def extract_last_response_via_copy(
    page: Page,
    previous_turn_signature: str | None = None,
) -> str:
    """Extract latest assistant response by clicking copy, then DOM fallback."""
    log.debug("Attempting extraction via latest-turn copy button...")

    try:
        await page.context.grant_permissions(["clipboard-read", "clipboard-write"])

        if previous_turn_signature:
            await _wait_for_new_turn_signature(page, previous_turn_signature, timeout_ms=2000)

        pre_clipboard = await page.evaluate("navigator.clipboard.readText().catch(() => '')")
        await page.evaluate("navigator.clipboard.writeText('').catch(() => {})")

        click_result = await page.evaluate(
            """
            (previousSignature) => {
                const copySelector = 'button[aria-label="Copy response"], button[aria-label="Copy"], button[aria-label*="Copy" i]';
                const nodes = Array.from(document.querySelectorAll(
                    'model-response, .model-response-text, [data-test-id="model-response-text"], message-content, .response-content, .markdown.markdown-main-panel'
                ));
                for (let idx = nodes.length - 1; idx >= 0; idx--) {
                    const turn = nodes[idx];
                    const text = (turn.innerText || '').trim();
                    if (!text) {
                        continue;
                    }
                    const signature = `${idx}:${text.substring(0, 80)}`;
                    if (previousSignature && signature === previousSignature) {
                        continue;
                    }
                    const btn = turn.querySelector(copySelector);
                    if (!btn) {
                        continue;
                    }
                    btn.click();
                    return { clicked: true, reason: 'ok', signature };
                }
                return { clicked: false, reason: 'no-assistant-turn', signature: null };
            }
            """,
            previous_turn_signature,
        )

        if isinstance(click_result, dict) and click_result.get("clicked"):
            await asyncio.sleep(0.8)
            content = await page.evaluate("navigator.clipboard.readText().catch(() => '')")
            if content and content.strip() and content.strip() != str(pre_clipboard).strip():
                log.info(
                    "Extracted via copy button (latest-turn): "
                    f"{len(content)} chars, turn={click_result.get('signature')}"
                )
                return content.strip()
            log.debug("Clipboard unchanged/empty after latest-turn copy click")
        else:
            reason = click_result.get("reason") if isinstance(click_result, dict) else "unknown"
            log.debug(f"Latest-turn copy click not used: {reason}")

    except Exception as e:
        log.warning(f"Copy button extraction failed: {e}")

    log.info("Falling back to latest-turn DOM extraction...")
    return await _extract_via_dom(page, previous_turn_signature)


async def _extract_via_dom(
    page: Page,
    previous_turn_signature: str | None = None,
) -> str:
    """Fallback extraction: innerText from latest model response only."""
    text = await page.evaluate(
        """
        (previousSignature) => {
            const nodes = Array.from(document.querySelectorAll(
                'model-response, .model-response-text, [data-test-id="model-response-text"], message-content, .response-content, .markdown.markdown-main-panel'
            ));
            for (let idx = nodes.length - 1; idx >= 0; idx--) {
                const turn = nodes[idx];
                const full = (turn.innerText || '').trim();
                if (!full) {
                    continue;
                }
                const signature = `${idx}:${full.substring(0, 80)}`;
                if (previousSignature && signature === previousSignature) {
                    continue;
                }
                return full;
            }
            return '';
        }
        """,
        previous_turn_signature,
    )
    cleaned = normalize_assistant_text(text or "")
    if cleaned:
        log.info(f"Extracted via DOM: {len(cleaned)} chars")
    else:
        log.warning("DOM extraction returned empty text")
    return cleaned
