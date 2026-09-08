"""
Gemini client — core interaction logic for gemini.google.com.

Sends messages, waits for responses, manages conversations.
Same interface as ChatGPTClient / ClaudeClient so the API layer is
provider-agnostic.
"""

from __future__ import annotations

import asyncio
import copy
import html
import os
import re
import tempfile
import time
from pathlib import Path

import fitz
from patchright.async_api import Page

from src.browser.human import human_click, random_delay
from src.api.cursor_adapter import extract_actionable_user_request
from src.chatgpt.models import ChatResponse
from src.config import Config
from src.gemini.detector import (
    count_assistant_messages,
    extract_last_response_via_copy,
    get_latest_assistant_turn_signature,
    is_incomplete_response_text,
    wait_for_response_complete,
)
from src.gemini.selectors import GeminiSelectors
from src.log import setup_logging

log = setup_logging("gemini_client")

_GEMINI_RESERVED_APP_SEGMENTS = {
    "about",
    "extensions",
    "prompt",
    "settings",
    "updates",
}


def thread_id_from_gemini_url(url: str) -> str:
    """Parse a Gemini conversation id from a gemini.google.com URL."""
    if not url:
        return ""
    match = re.search(r"/app/([a-zA-Z0-9_-]+)", url)
    if not match:
        return ""
    token = match.group(1)
    if token.lower() in _GEMINI_RESERVED_APP_SEGMENTS:
        return ""
    return token


_GEMINI_COMPOSER_SAFE_CHARS = 1800
_GEMINI_INSERT_CHUNK = 240


def _gemini_prompt_too_long_for_composer(text: str) -> bool:
    """Gemini's Quill composer freezes well below ChatGPT's paste limit."""
    threshold = min(
        _GEMINI_COMPOSER_SAFE_CHARS,
        Config.CHATGPT_LONG_PROMPT_THRESHOLD or _GEMINI_COMPOSER_SAFE_CHARS,
    )
    return len(text) >= threshold


def _composer_text_js() -> str:
    return """(selector) => {
        const el = document.querySelector(selector);
        return el ? (el.innerText || el.textContent || '').trim() : '';
    }"""


async def _read_composer_text_raw(page: Page, selector: str) -> str | None:
    """Return composer text, or None when the DOM read failed."""
    try:
        value = await asyncio.wait_for(page.evaluate(_composer_text_js(), selector), 5)
    except Exception:
        return None
    return value if isinstance(value, str) else None


async def _read_composer_text(page: Page, selector: str) -> str:
    value = await _read_composer_text_raw(page, selector)
    return value or ""


async def _clear_composer(page: Page, selector: str) -> None:
    try:
        await asyncio.wait_for(
            page.evaluate(
                """(selector) => {
                    const el = document.querySelector(selector);
                    if (!el) return;
                    el.focus();
                    document.execCommand('selectAll', false, null);
                    document.execCommand('delete', false, null);
                }""",
                selector,
            ),
            5,
        )
    except Exception as exc:
        log.debug("Could not clear Gemini composer: %s", exc)


async def _insert_composer_chunk(page: Page, selector: str, chunk: str) -> None:
    await asyncio.wait_for(
        page.evaluate(
            """([selector, text]) => {
                const el = document.querySelector(selector);
                if (!el) return 'no-element';
                el.focus();
                document.execCommand('insertText', false, text);
                return 'ok';
            }""",
            [selector, chunk],
        ),
        8,
    )


async def fill_gemini_composer(page: Page, selector: str, text: str) -> None:
    """Insert in small chunks so Gemini's editor does not freeze mid-paste."""
    locator = page.locator(selector).first
    await locator.click()
    await asyncio.sleep(0.12)
    await _clear_composer(page, selector)
    log.info("Inserting %s chars into Gemini composer in chunks", len(text))
    for start in range(0, len(text), _GEMINI_INSERT_CHUNK):
        chunk = text[start : start + _GEMINI_INSERT_CHUNK]
        await _insert_composer_chunk(page, selector, chunk)
        await asyncio.sleep(0.03)
    actual = await _read_composer_text(page, selector)
    if len(actual) < max(12, int(len(text) * 0.6)):
        log.warning(
            "Gemini composer insert looks truncated (%s of %s chars)",
            len(actual),
            len(text),
        )


def _compact_prompt_for_composer(text: str) -> str:
    """Fallback when a huge editor dump still reaches the Gemini client."""
    query = extract_actionable_user_request(text, min_chars=12)
    if not query:
        stripped = (text or "").strip()
        query = stripped if len(stripped) <= 4000 else stripped[:4000] + "\n\n[truncated]"
    if "tool_calls" in (text or "") or '"final"' in (text or ""):
        reminder = (
            "Reply with ONLY one JSON object: "
            '{"tool_calls":[{"name":"<function_name>","arguments":{...}}]} '
            'when a workspace function is needed, or {"final":"<answer>"} otherwise. '
            "Never put a command for the user to run inside final. No markdown.\n\n"
        )
        return reminder + query
    return query


def _write_prompt_pdf(text: str, path: str) -> None:
    """Write the Copilot dump as a PDF Gemini's document uploader will accept.

    Plain `.txt` chips often fail in the gemini.google.com composer (red error
    icon, Send stays disabled). PDF goes through the document pipeline.
    """
    doc = fitz.open()
    try:
        chunk_size = 3500
        payload = text or " "
        for start in range(0, len(payload), chunk_size):
            chunk = payload[start : start + chunk_size]
            page = doc.new_page(width=612, height=792)
            escaped = html.escape(chunk).replace("\n", "<br/>")
            page.insert_htmlbox(
                fitz.Rect(28, 28, 584, 764),
                (
                    "<div style='font-family:monospace;font-size:8pt;"
                    f"line-height:1.25'>{escaped}</div>"
                ),
            )
        doc.save(path)
    finally:
        doc.close()


_ATTACHMENT_STATE_JS = """() => {
    const send = Array.from(document.querySelectorAll('button')).find((button) => {
        const label = (button.getAttribute('aria-label') || '').toLowerCase();
        return label.includes('send message') || label.includes('send prompt');
    });
    const sendDisabled = !send || send.disabled || send.getAttribute('aria-disabled') === 'true';
    const root =
        document.querySelector('input-container, .input-area, rich-textarea, bard-mode-query-box')
        || document.body;
    const blob = (root.innerText || '') + ' ' + (document.body.innerText || '').slice(-2500);
    if (/couldn.?t upload|upload failed|couldn.?t process|failed to (upload|analyze)|not a supported/i.test(blob)) {
        return 'error';
    }
    const fileBits = Array.from(root.querySelectorAll('span, div, p, a, button'));
    for (const node of fileBits) {
        const label = (node.textContent || '').trim();
        if (!/catgpt-long|\\.pdf$|\\.txt$/i.test(label)) continue;
        const color = getComputedStyle(node).color || '';
        const match = color.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
        if (match) {
            const r = Number(match[1]);
            const g = Number(match[2]);
            const b = Number(match[3]);
            if (r > 160 && g < 110 && b < 110) return 'error';
        }
    }
    if (root.querySelector('[class*="error"], [aria-label*="error" i], [aria-label*="failed" i], [aria-label*="Couldn" i]')) {
        if (/catgpt-long|Remove/i.test(root.innerText || '')) return 'error';
    }
    const hasChip = /catgpt-long/i.test(root.innerText || '')
        || !!root.querySelector('button[aria-label*="Remove" i]');
    if (!hasChip) return 'missing';
    if (sendDisabled) return 'pending';
    return 'ready';
}"""


class GeminiClient:
    """High-level client for the Gemini web interface."""

    def __init__(self, page: Page) -> None:
        self._page = page

    @property
    def page(self) -> Page:
        return self._page

    def bind_page(self, page: Page | None) -> GeminiClient:
        """Return a client bound to a specific tab without mutating this instance."""
        if page is None or page is self._page:
            return self
        bound = copy.copy(self)
        bound._page = page
        return bound

    async def send_message(
        self,
        text: str,
        image_paths: list[str] | None = None,
        file_paths: list[str] | None = None,
        model: str | None = None,
        read_aloud: bool = False,
    ) -> ChatResponse:
        """Send a message to Gemini and wait for the complete response.

        `model` is accepted for OpenAI-route compatibility; Gemini's web UI
        does not expose a model picker in this client.
        """
        _ = model
        all_attachments = list((image_paths or []) + (file_paths or []))
        log.info(
            f"Sending message ({len(text)} chars, {len(all_attachments)} attachments): "
            f"{text[:80]}..."
        )
        start_time = time.time()

        pre_count = await count_assistant_messages(self._page)
        pre_turn_signature = await get_latest_assistant_turn_signature(self._page)
        log.debug(f"Assistant messages before send: {pre_count}")
        log.debug(f"Latest assistant turn before send: {pre_turn_signature}")

        await random_delay()

        input_selector = await self._find_selector(GeminiSelectors.CHAT_INPUT, "chat input")
        if not input_selector:
            raise RuntimeError("Could not find Gemini chat input element")

        submitted_text = text
        if _gemini_prompt_too_long_for_composer(text):
            submitted_text = _compact_prompt_for_composer(text)
            log.info(
                "Prompt is too long for the Gemini composer; sending compact request (%s chars)",
                len(submitted_text),
            )

        await fill_gemini_composer(self._page, input_selector, submitted_text)

        if all_attachments:
            try:
                await self._upload_files(all_attachments)
            except Exception as exc:
                log.warning("Gemini file upload failed; sending text only: %s", exc)

        await random_delay(80, 180)

        sent = await self._click_send()
        if not sent:
            log.info("Send button not found, trying Enter key")
            await self._page.keyboard.press("Enter")
        if not await self._composer_submitted(input_selector):
            log.info("Gemini composer still has text after send; pressing Enter")
            await self._page.keyboard.press("Enter")
            if not await self._composer_submitted(input_selector):
                raise RuntimeError(
                    "Gemini did not accept the prompt (composer still filled). "
                    "Reload http://localhost:5801 and try a new chat."
                )

        log.info("Waiting for Gemini response...")
        expected_count = pre_count + 1
        completed = await wait_for_response_complete(
            self._page,
            expected_msg_count=expected_count,
            previous_turn_signature=pre_turn_signature,
        )

        if not completed:
            log.warning("Response may not be complete (timeout)")

        await asyncio.sleep(0.25)

        response_text = await extract_last_response_via_copy(
            self._page,
            previous_turn_signature=pre_turn_signature,
        )

        if not completed and is_incomplete_response_text(response_text):
            log.warning(
                "Gemini produced no usable reply; skipping extra wait so the queue can proceed"
            )
        elif is_incomplete_response_text(response_text):
            log.warning("Extracted text looks incomplete/transient; retrying for final answer")
            await asyncio.sleep(1.5)
            await wait_for_response_complete(
                self._page,
                timeout_ms=60000,
                previous_turn_signature=pre_turn_signature,
            )
            retry_text = await extract_last_response_via_copy(
                self._page,
                previous_turn_signature=pre_turn_signature,
            )
            if retry_text:
                response_text = retry_text

        elapsed_ms = int((time.time() - start_time) * 1000)
        thread_id = self._extract_thread_id()
        if read_aloud:
            log.warning("read_aloud=True requested, but Gemini read-aloud audio is not implemented")

        log.info(
            f"Response received ({elapsed_ms}ms, {len(response_text)} chars): "
            f"{response_text[:80]}..."
        )

        return ChatResponse(
            message=response_text,
            thread_id=thread_id,
            response_time_ms=elapsed_ms,
            images=[],
            has_images=False,
            audio=None,
            has_audio=False,
        )

    async def new_chat(self) -> None:
        """Start a new conversation."""
        log.info("Starting new Gemini chat...")
        clicked = False
        selector = await self._find_selector(GeminiSelectors.NEW_CHAT_BUTTON, "new chat")
        if selector:
            await human_click(self._page, selector)
            clicked = True

        if not clicked:
            current = self._page.url or ""
            if "gemini.google.com" in current:
                log.warning(
                    "New chat control not found; staying on %s instead of a full page reload",
                    current,
                )
            else:
                url = Config.GEMINI_URL.rstrip("/")
                await self._page.goto(url, wait_until="domcontentloaded")

        await asyncio.sleep(Config.NAVIGATION_SETTLE_MS / 1000)
        input_selector = await self._find_selector(GeminiSelectors.CHAT_INPUT, "chat input")
        if input_selector:
            log.debug(f"Chat input ready: {input_selector}")
        else:
            log.warning("Chat input not visible after starting a new Gemini chat")

        await random_delay()
        log.info("New Gemini chat started")

    async def navigate_to_thread(self, thread_id: str) -> None:
        """Navigate to an existing Gemini conversation."""
        thread_id = (thread_id or "").strip()
        if not thread_id:
            return
        current = self._page.url or ""
        if thread_id == thread_id_from_gemini_url(current) or thread_id in current:
            log.info("Already on Gemini thread %s; skipping reload", thread_id)
            return
        base = Config.GEMINI_URL.rstrip("/")
        if base.endswith("/app"):
            url = f"{base}/{thread_id}"
        else:
            url = f"{base}/app/{thread_id}"
        log.info(f"Navigating to thread: {thread_id}")
        await self._page.goto(url, wait_until="domcontentloaded")
        await asyncio.sleep(Config.NAVIGATION_SETTLE_MS / 1000)
        log.info(f"Thread {thread_id} loaded")

    async def get_current_thread_url(self) -> str:
        """Get the current page URL (contains thread ID if in a conversation)."""
        return self._page.url

    async def list_threads(self) -> list[dict]:
        """Scrape the sidebar for recent conversation threads."""
        threads: list[dict] = []
        seen: set[str] = set()
        for selector in GeminiSelectors.SIDEBAR_THREAD_LINKS:
            try:
                elements = await self._page.query_selector_all(selector)
                for el in elements:
                    href = await el.get_attribute("href") or ""
                    title = (await el.inner_text()).strip()
                    match = re.search(r"/app/([a-zA-Z0-9_-]+)", href)
                    if not match:
                        continue
                    thread_id = match.group(1)
                    if thread_id in seen:
                        continue
                    seen.add(thread_id)
                    threads.append(
                        {
                            "id": thread_id,
                            "title": title,
                            "url": href if href.startswith("http") else (
                                f"{Config.GEMINI_URL.rstrip('/').rsplit('/app', 1)[0]}{href}"
                                if href.startswith("/")
                                else href
                            ),
                        }
                    )
                if threads:
                    break
            except Exception as e:
                log.debug(f"Sidebar scrape with {selector} failed: {e}")

        log.info(f"Found {len(threads)} threads in sidebar")
        return threads

    async def interrupt_generation(self) -> None:
        """Click Gemini Stop so a cancelled VS Code turn does not keep the tab busy."""
        selector = await self._find_selector(GeminiSelectors.STOP_BUTTON, "stop")
        if not selector:
            return
        try:
            await self._page.locator(selector).first.click(timeout=1200)
            log.info("Clicked Gemini Stop after client cancel")
        except Exception as exc:
            log.debug("Could not click Gemini Stop: %s", exc)

    async def _find_selector(self, selectors: list[str], name: str) -> str | None:
        """Return the first visible selector without waiting 10s on every miss."""
        for selector in selectors:
            try:
                locator = self._page.locator(selector).first
                if await locator.count() == 0:
                    continue
                if await locator.is_visible():
                    log.debug(f"Found {name} via: {selector}")
                    return selector
            except Exception:
                log.debug(f"Selector miss for {name}: {selector}")

        probe_ms = min(600, max(200, Config.SELECTOR_TIMEOUT // 8))
        for selector in selectors:
            try:
                el = await self._page.wait_for_selector(
                    selector,
                    timeout=probe_ms,
                    state="visible",
                )
                if el:
                    log.debug(f"Found {name} via: {selector}")
                    return selector
            except Exception:
                continue

        if name == "chat input" and selectors:
            try:
                el = await self._page.wait_for_selector(
                    selectors[0],
                    timeout=Config.SELECTOR_TIMEOUT,
                    state="visible",
                )
                if el:
                    log.debug(f"Found {name} via: {selectors[0]}")
                    return selectors[0]
            except Exception:
                pass

        log.warning(f"No working selector found for: {name}")
        return None

    async def _composer_submitted(self, input_selector: str) -> bool:
        """True once Gemini clears the composer after a successful send.

        A failed DOM read must not count as empty — Gemini's Quill often
        returns blank snapshots while the prompt is still sitting in the box.
        """
        saw_text = False
        empty_hits = 0
        for _ in range(20):
            remaining = await _read_composer_text_raw(self._page, input_selector)
            if remaining is None:
                await asyncio.sleep(0.25)
                continue
            if len(remaining.strip()) < 8:
                empty_hits += 1
                if saw_text or empty_hits >= 2:
                    return True
            else:
                saw_text = True
                empty_hits = 0
            await asyncio.sleep(0.25)
        return False

    async def _prompt_attachment_state(self) -> str:
        try:
            value = await asyncio.wait_for(self._page.evaluate(_ATTACHMENT_STATE_JS), 5)
        except Exception:
            return "missing"
        if isinstance(value, str) and value in {"error", "pending", "ready", "missing"}:
            return value
        return "missing"

    async def _wait_for_prompt_attachment(self, timeout_s: float = 20) -> str:
        """Wait until Gemini accepts the chip (Send enabled) or rejects it."""
        deadline = time.monotonic() + timeout_s
        last = "missing"
        while time.monotonic() < deadline:
            last = await self._prompt_attachment_state()
            if last in {"ready", "error"}:
                log.info("Gemini attachment state: %s", last)
                return last
            await asyncio.sleep(0.4)
        log.warning("Gemini attachment still %s after %.0fs", last, timeout_s)
        return last if last != "missing" else "error"

    async def _dismiss_composer_attachments(self) -> None:
        for selector in (
            "button[aria-label*='Remove attachment' i]",
            "button[aria-label*='Remove file' i]",
            "button[aria-label*='Remove' i]",
        ):
            for _ in range(6):
                locator = self._page.locator(selector).first
                try:
                    if await locator.count() == 0:
                        break
                    if not await locator.is_visible():
                        break
                    await locator.click(timeout=800)
                    await asyncio.sleep(0.15)
                except Exception:
                    break

    async def _click_send(self) -> bool:
        """Try to click the send button using selector fallbacks."""
        for _ in range(20):
            try:
                enabled = await asyncio.wait_for(
                    self._page.evaluate(
                        """
                        () => {
                            const buttons = Array.from(document.querySelectorAll('button'));
                            const match = buttons.find((button) => {
                                const label = (
                                    (button.getAttribute('aria-label') || '') + ' ' +
                                    (button.getAttribute('mattooltip') || '')
                                ).toLowerCase();
                                return /send message|send prompt|submit prompt/.test(label);
                            });
                            if (!match) return true;
                            return !(match.disabled || match.getAttribute('aria-disabled') === 'true');
                        }
                        """
                    ),
                    5,
                )
            except Exception:
                enabled = True
            if enabled:
                break
            await asyncio.sleep(0.15)
        selector = await self._find_selector(GeminiSelectors.SEND_BUTTON, "send button")
        if selector:
            try:
                await human_click(self._page, selector)
                log.debug("Send button clicked")
                return True
            except Exception as exc:
                log.debug("Gemini send hover/click failed (%s); trying DOM click", exc)
        clicked = await self._page.evaluate(
            """
            () => {
                const buttons = Array.from(document.querySelectorAll('button'));
                const match = buttons.find((button) => {
                    const label = (
                        (button.getAttribute('aria-label') || '') + ' ' +
                        (button.getAttribute('mattooltip') || '')
                    ).toLowerCase();
                    return /send|submit prompt|submit/.test(label);
                });
                if (match) {
                    match.click();
                    return true;
                }
                return false;
            }
            """
        )
        if clicked:
            log.debug("Send button clicked via aria-label scan")
            return True
        return False

    async def _click_upload_menu_item(self) -> bool:
        for selector in GeminiSelectors.UPLOAD_MENU_ITEMS:
            try:
                locator = self._page.locator(selector).first
                if await locator.is_visible(timeout=800):
                    await locator.click()
                    log.debug("Clicked Gemini upload menu item: %s", selector)
                    return True
            except Exception:
                continue
        return False

    async def _pick_composer_file_input(self):
        """Prefer a composer file input over Google-account / header widgets."""
        for selector in GeminiSelectors.FILE_UPLOAD_INPUT:
            try:
                elements = await self._page.query_selector_all(selector)
            except Exception:
                continue
            for element in elements:
                try:
                    name = (await element.get_attribute("name") or "").lower()
                    if any(token in name for token in ("avatar", "profile", "photo")):
                        continue
                    return element
                except Exception:
                    continue
        return None

    async def _upload_files(self, file_paths: list[str]) -> None:
        """Attach files through Gemini's composer, not a random page file input."""
        valid_paths = []
        for path_str in file_paths:
            path = Path(path_str)
            if path.exists() and path.is_file():
                valid_paths.append(str(path.resolve()))
            else:
                log.warning(f"File not found, skipping: {path_str}")

        if not valid_paths:
            log.warning("No valid files to upload")
            return

        log.info(f"Uploading {len(valid_paths)} file(s)...")
        uploaded = False

        attach_selector = await self._find_selector(GeminiSelectors.ATTACH_BUTTON, "attach")
        if attach_selector:
            try:
                async with self._page.expect_file_chooser(timeout=6000) as chooser_info:
                    await human_click(self._page, attach_selector)
                    await self._click_upload_menu_item()
                chooser = await chooser_info.value
                await chooser.set_files(valid_paths)
                uploaded = True
                log.info("Set %d file(s) via Gemini file chooser", len(valid_paths))
            except Exception as exc:
                log.debug("Gemini file-chooser upload failed: %s", exc)

        if not uploaded:
            file_input = await self._pick_composer_file_input()
            if file_input:
                await file_input.set_input_files(valid_paths)
                uploaded = True
                log.info("Set %d file(s) on composer file input", len(valid_paths))

        if not uploaded:
            raise RuntimeError(
                "Could not upload files: no Gemini composer file input or attach control"
            )

        badge_selector = ", ".join(GeminiSelectors.ATTACHMENT_BADGE)
        try:
            await self._page.wait_for_selector(
                badge_selector,
                timeout=8000,
                state="attached",
            )
            log.info("Attachment badge detected in composer")
        except Exception:
            log.debug("Attachment badge wait timed out, using fallback sleep")
            await asyncio.sleep(max(0.5, Config.RESPONSE_SETTLE_MS / 1000))
            if valid_paths:
                await asyncio.sleep(min(1.0, 0.25 * len(valid_paths)))
        log.info("File upload complete")

    @staticmethod
    def _create_prompt_attachment(text: str) -> str:
        """Persist a prompt as a PDF Gemini's document uploader will accept.

        Never paste this blob into the Quill composer; that freezes the tab.
        Plain `.txt` often fails in the composer with a red error chip.
        """
        file_descriptor, filename = tempfile.mkstemp(
            prefix="catgpt-long-prompt-",
            suffix=".pdf",
        )
        os.close(file_descriptor)
        try:
            _write_prompt_pdf(text, filename)
        except Exception as exc:
            Path(filename).unlink(missing_ok=True)
            raise RuntimeError(f"Could not create the temporary Gemini prompt attachment: {exc}") from exc
        return filename

    def _extract_thread_id(self) -> str:
        """Extract the conversation ID from the current Gemini URL."""
        return thread_id_from_gemini_url(self._page.url or "")
