"""
Browser lifecycle manager — launch, persist, close.

Uses a persistent Chrome context so the user only signs in once.
Session data (cookies, localStorage, IndexedDB) survives restarts.
"""

from __future__ import annotations

import datetime
import os
import platform
import random
import re
import signal
import socket
import subprocess
from pathlib import Path
from patchright.async_api import async_playwright, BrowserContext, Page, Playwright

from src.config import Config
from src.browser.stealth import apply_stealth
from src.log import setup_logging

log = setup_logging("browser")


def _provider_login_selectors(selectors, claude_selectors, gemini_selectors):
    """Return chat-input, login, and logged-in selector lists for the active provider."""
    if Config.PROVIDER == "claude":
        return (
            claude_selectors.CHAT_INPUT,
            claude_selectors.LOGIN_INDICATORS,
            claude_selectors.LOGGED_IN_INDICATORS,
        )
    if Config.PROVIDER == "gemini":
        return (
            gemini_selectors.CHAT_INPUT,
            gemini_selectors.LOGIN_INDICATORS,
            gemini_selectors.LOGGED_IN_INDICATORS,
        )
    return selectors.CHAT_INPUT, selectors.LOGIN_INDICATORS, []


def _is_docker_runtime() -> bool:
    """Return whether Chrome is running inside the Docker/Xvfb environment."""
    if os.path.exists("/.dockerenv"):
        return True
    return platform.system() != "Windows" and os.environ.get("DISPLAY") == ":99"


def _resolve_domains_for_chrome() -> str:
    """
    Pre-resolve key domains via the OS and return a --host-resolver-rules
    string for Chrome.

    Chrome's built-in DNS client (even with --disable-features=AsyncDns)
    is unreliable — it can return DNS_PROBE_FINISHED_NXDOMAIN for domains
    that the OS resolver handles fine.  By pre-resolving here and passing
    the IPs via --host-resolver-rules, Chrome bypasses its own resolver
    entirely and the problem disappears.

    Returns empty string if all resolutions fail.
    """
    # Only needed in Docker (check for /.dockerenv or DISPLAY=:99).
    if not _is_docker_runtime():
        return ""

    common_domains = [
        "challenges.cloudflare.com",
        "static.cloudflareinsights.com",
    ]
    chatgpt_domains = [
        "chatgpt.com",
        "cdn.oaistatic.com",
        "ab.chatgpt.com",
        "auth.openai.com",
        "auth0.openai.com",
        "openai.com",
        "api.openai.com",
        "platform.openai.com",
        "tcr9i.chat.openai.com",
    ]
    claude_domains = [
        "claude.ai",
        "api.claude.ai",
        "cdn.claude.ai",
        "anthropic.com",
        "www.anthropic.com",
    ]
    gemini_domains = [
        "gemini.google.com",
        "accounts.google.com",
        "www.google.com",
        "ssl.gstatic.com",
        "www.gstatic.com",
        "lh3.googleusercontent.com",
        "apis.google.com",
        "ogs.google.com",
    ]
    if Config.PROVIDER == "claude":
        domains = common_domains + claude_domains
    elif Config.PROVIDER == "gemini":
        domains = common_domains + gemini_domains
    else:
        domains = common_domains + chatgpt_domains
    rules = []
    for domain in domains:
        try:
            ip = socket.gethostbyname(domain)
            rules.append(f"MAP {domain} {ip}")
            log.debug(f"DNS pre-resolve: {domain} -> {ip}")
        except Exception as e:
            log.warning(f"DNS pre-resolve failed: {domain} -> {e}")

    if rules:
        result = ", ".join(rules)
        log.info(f"Chrome host-resolver-rules: {len(rules)} domains mapped")
        return result
    return ""


def _matching_browser_processes(data_dir: Path, ps_output: str | None = None) -> set[int]:
    """Return exact browser PIDs tied to this project's persistent profile dir."""
    if not data_dir:
        return set()
    resolved = data_dir.resolve() if data_dir.exists() else data_dir
    profile_dir = str(resolved).replace("\\", "/").rstrip("/")
    if not profile_dir:
        return set()

    if ps_output is None:
        try:
            result = subprocess.run(
                ["ps", "-eo", "pid,ppid,comm,args", "--no-headers"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            ps_output = result.stdout or ""
        except Exception:
            return set()

    matches: set[int] = set()
    for line in (ps_output or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
            _ppid = int(parts[1])
        except ValueError:
            continue
        cmd = parts[3] if len(parts) > 3 else ""
        if not cmd:
            continue

        if not any(marker in cmd for marker in ("--user-data-dir", "user-data-dir", "--profile-directory", "profile-directory")):
            continue

        path_tokens = []
        for marker in ("--user-data-dir=", "--profile-directory=", "user-data-dir=", "profile-directory="):
            idx = cmd.find(marker)
            if idx >= 0:
                rest = cmd[idx + len(marker):]
                value = rest.split()[0].strip("\"'")
                path_tokens.append(value)

        if not path_tokens:
            continue

        normalized = [str(token).replace("\\", "/").rstrip("/") for token in path_tokens]
        if profile_dir not in normalized and not any(
            os.path.normpath(value) == os.path.normpath(profile_dir) for value in normalized
        ):
            continue

        matches.add(pid)
    return matches


def _browser_process_tree(data_dir: Path) -> list[int]:
    """Return the full process tree for the CatGPT-managed profile, including children."""
    profile_pids = _matching_browser_processes(data_dir)
    if not profile_pids:
        return []

    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,ppid,comm,args", "--no-headers"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        rows = result.stdout or ""
    except Exception:
        rows = ""

    by_parent: dict[int, list[int]] = {}
    all_pids: set[int] = set()
    for line in rows.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        if pid <= 0:
            continue
        all_pids.add(pid)
        by_parent.setdefault(ppid, []).append(pid)

    seen: set[int] = set()
    stack = list(profile_pids)
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        stack.extend(by_parent.get(pid, []))

    return sorted(seen)


def _terminate_browser_processes(data_dir: Path) -> None:
    """Terminate only exact CatGPT-owned browser PIDs and their child processes."""
    pids = _browser_process_tree(data_dir)
    for pid in sorted(pids, reverse=True):
        try:
            os.kill(pid, signal.SIGTERM)
            log.info("Terminated CatGPT-owned browser process PID %s", pid)
        except ProcessLookupError:
            continue
        except PermissionError:
            continue
        except OSError:
            continue

    # Safe fallback: if the process was already gone or the command lines were
    # unavailable, the stale lock cleanup below still recovers the local profile.


def _cleanup_stale_locks(data_dir: Path) -> None:
    """
    Remove stale lock / journal / WAL files that prevent browser launch.

    After a crash, Chromium leaves behind:
    - SingletonLock/Socket/Cookie — prevents new instance from using data dir.
    - *-journal, *-wal, *-shm — SQLite journal/WAL files that cause
      "database is locked" errors (UKM, Top Sites, History, etc.)

    We also terminate only the exact CatGPT-owned browser process tree that
    matches this profile's user-data-dir rather than generic Chrome/Chromium.
    """
    _terminate_browser_processes(data_dir)

    # 1. Remove singleton lock files
    lock_files = ["SingletonLock", "SingletonSocket", "SingletonCookie"]
    for name in lock_files:
        path = data_dir / name
        if path.exists():
            try:
                path.unlink()
                log.info(f"Removed stale lock file: {name}")
            except Exception as e:
                log.warning(f"Could not remove {name}: {e}")

    # 3. Remove SQLite journal/WAL/SHM files that cause "database is locked"
    #    but leave the rest of the persistent profile untouched.
    import glob as _glob
    patterns = ["**/*-journal", "**/*-wal", "**/*-shm"]
    removed = 0
    for pattern in patterns:
        for path_str in _glob.glob(str(data_dir / pattern), recursive=True):
            try:
                Path(path_str).unlink()
                removed += 1
            except Exception:
                pass
    if removed:
        log.info(f"Removed {removed} stale SQLite journal/WAL/SHM files")

    log.info("Profile cleanup limited to stale Chromium lock and SQLite state for %s", data_dir)


def _env_int(name: str, default: int) -> int:
    """Read a positive integer from the environment."""
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


class BrowserManager:
    """Manages a single persistent Chromium browser context."""

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def start(self) -> Page:
        """
        Launch a persistent Chrome context with stealth and human-like settings.

        Automatically cleans up stale lock files from previous crashed sessions.
        Returns the active page ready for navigation.
        """
        Config.ensure_dirs()

        # Clean up stale locks from previous sessions
        _cleanup_stale_locks(Config.BROWSER_DATA_DIR)

        log.info("Launching browser...")
        self._playwright = await async_playwright().start()

        in_docker = _is_docker_runtime()
        display_width = _env_int("DISPLAY_WIDTH", Config.VIEWPORT_WIDTH)
        display_height = _env_int("DISPLAY_HEIGHT", Config.VIEWPORT_HEIGHT)

        # Randomize local headed launches slightly to avoid fingerprint consistency.
        # In Docker/VNC, use the Xvfb size so Chrome fills the visible remote desktop.
        if in_docker:
            width = display_width
            height = display_height
        else:
            width = Config.VIEWPORT_WIDTH + random.randint(-20, 20)
            height = Config.VIEWPORT_HEIGHT + random.randint(-20, 20)

        chrome_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            # Disable Chrome's built-in DNS client entirely.  Even with
            # AsyncDns off, Chrome's stub resolver can return NXDOMAIN for
            # domains the OS resolves fine.  We also pre-resolve domains
            # via --host-resolver-rules (see _resolve_domains_for_chrome).
            "--disable-features=AsyncDns,DnsOverHttps",
            "--dns-prefetch-disable",
            # Local system extensions/ad blockers can break OpenAI auth flows
            # even with a fresh user-data-dir.
            "--disable-extensions",
            "--disable-component-extensions-with-background-pages",
            "--hide-crash-restore-bubble",
            "--disable-session-crashed-bubble",
        ]

        # Docker-specific flags
        if in_docker:
            chrome_args.extend([
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--start-maximized",
                "--window-position=0,0",
                f"--window-size={width},{height}",
            ])

        # Pre-resolve domains via the OS and hardcode the IPs for Chrome.
        # This prevents Chrome's built-in DNS client from ever being used.
        resolver_rules = _resolve_domains_for_chrome()
        if resolver_rules:
            chrome_args.append(f"--host-resolver-rules={resolver_rules}")

        launch_kwargs = dict(
            user_data_dir=str(Config.BROWSER_DATA_DIR),
            headless=Config.HEADLESS,
            slow_mo=Config.SLOW_MO,
            locale="en-US",
            timezone_id="America/Los_Angeles",
            args=chrome_args,
        )
        if in_docker:
            launch_kwargs["no_viewport"] = True
        else:
            launch_kwargs["viewport"] = {"width": width, "height": height}

        browser_channel = Config.BROWSER_CHANNEL
        try:
            if browser_channel in {"", "chromium", "bundled"}:
                self._context = await self._playwright.chromium.launch_persistent_context(
                    **launch_kwargs
                )
                log.info("Launched with bundled Chromium")
            else:
                self._context = await self._playwright.chromium.launch_persistent_context(
                    channel=browser_channel, **launch_kwargs
                )
                log.info("Launched with browser channel: %s", browser_channel)
        except Exception:
            if browser_channel in {"", "chromium", "bundled"}:
                raise
            log.info("Browser channel %r not available, using bundled Chromium", browser_channel)
            self._context = await self._playwright.chromium.launch_persistent_context(
                **launch_kwargs
            )

        # NOTE: Stealth patches are applied AFTER the first navigation.
        # In Docker, applying stealth init scripts before navigation
        # causes Chrome's DNS resolver to fail (ERR_NAME_NOT_RESOLVED).
        # Call apply_stealth_patches() after navigating to the target page.

        # Use existing page or create one
        if self._context.pages:
            self._page = self._context.pages[0]
        else:
            self._page = await self._context.new_page()

        # NOTE: We intentionally do NOT flush Chrome's DNS cache here.
        # The --host-resolver-rules flag handles DNS resolution for all
        # mapped domains.  Previously, _clear_dns_cache() would navigate
        # to chrome://net-internals and flush the host cache + socket
        # pools — but this destroyed working connection state and caused
        # DNS_PROBE_FINISHED_NXDOMAIN on subsequent navigations.

        log.info(f"Browser ready — viewport {width}x{height}")
        return self._page

    async def new_page(self) -> Page:
        """Open an extra tab in the persistent context for concurrent requests."""
        if self._context is None:
            raise RuntimeError("Browser not started. Call start() first.")
        page = await self._context.new_page()
        log.info("Opened additional browser tab")
        return page

    async def _clear_dns_cache(self) -> None:
        """Clear Chrome's in-memory DNS host cache via chrome://net-internals."""
        import asyncio as _asyncio

        if self._page is None:
            return

        try:
            await self._page.goto(
                "chrome://net-internals/#dns",
                wait_until="domcontentloaded",
                timeout=10000,
            )
            await _asyncio.sleep(0.5)

            # The "Clear host cache" button ID in chrome://net-internals/#dns
            cleared = await self._page.evaluate(
                """
                () => {
                    // Try the standard button
                    const btn = document.getElementById('dns-view-clear-cache');
                    if (btn) { btn.click(); return 'clicked-dns-view-clear-cache'; }
                    // Newer Chrome: look for any button that says "Clear"
                    const buttons = Array.from(document.querySelectorAll('button'));
                    for (const b of buttons) {
                        if (b.textContent.toLowerCase().includes('clear')) {
                            b.click();
                            return 'clicked-' + b.textContent.trim();
                        }
                    }
                    return 'no-clear-button-found';
                }
                """
            )
            log.info(f"Chrome DNS cache flush: {cleared}")
            await _asyncio.sleep(0.3)

            # Also try to flush socket pools
            try:
                await self._page.goto(
                    "chrome://net-internals/#sockets",
                    wait_until="domcontentloaded",
                    timeout=5000,
                )
                await _asyncio.sleep(0.3)
                await self._page.evaluate(
                    """
                    () => {
                        const buttons = Array.from(document.querySelectorAll('button'));
                        for (const b of buttons) {
                            if (b.textContent.toLowerCase().includes('flush') ||
                                b.textContent.toLowerCase().includes('close')) {
                                b.click();
                            }
                        }
                    }
                    """
                )
                log.info("Chrome socket pools flushed")
            except Exception:
                pass  # Best-effort

        except Exception as e:
            log.warning(f"Could not clear Chrome DNS cache: {e}")

    async def apply_stealth_patches(self) -> None:
        """
        Apply stealth patches to the browser context.

        Must be called AFTER the first page navigation, not before.
        In Docker containers, applying stealth init scripts before any
        navigation causes Chrome's DNS resolver to fail.
        """
        if self._context is None:
            raise RuntimeError("Browser not started. Call start() first.")
        await apply_stealth(self._context)

    @property
    def page(self) -> Page:
        """Get the active page. Raises if browser not started."""
        if self._page is None:
            raise RuntimeError("Browser not started. Call start() first.")
        return self._page

    @property
    def context(self) -> BrowserContext:
        """Get the browser context."""
        if self._context is None:
            raise RuntimeError("Browser not started. Call start() first.")
        return self._context

    async def navigate(self, url: str) -> None:
        """Navigate to a URL and wait for page load."""
        log.info(f"Navigating to {url}")
        await self.page.goto(url, wait_until="domcontentloaded")
        log.info("Page loaded")

    async def get_session_info(self) -> dict:
        """
        Read ChatGPT session cookies from the browser context.

        Returns a dict with:
          exists       (bool)            — True if a session cookie was found
          expires      (datetime | None) — expiry of the most important cookie
          cookie_count (int)             — number of session cookies found
          email        (str | None)      — masked account email (from JWT)
        """
        if self._context is None:
            return {"exists": False, "expires": None, "cookie_count": 0, "email": None}

        try:
            cookies = await self._context.cookies("https://chatgpt.com")
        except Exception as e:
            log.debug(f"Could not read session cookies: {e}")
            return {"exists": False, "expires": None, "cookie_count": 0, "email": None}

        # Key session cookies OpenAI uses
        session_cookie_names = {"__Secure-next-auth.session-token", "__cf_bm", "cf_clearance", "oai-did"}
        found = [c for c in cookies if c.get("name") in session_cookie_names]

        if not found:
            return {"exists": False, "expires": None, "cookie_count": 0, "email": None}

        # Find the latest expiry among session cookies (most meaningful)
        latest_expiry: datetime.datetime | None = None
        for c in found:
            exp = c.get("expires")
            if exp and exp > 0:
                dt = datetime.datetime.fromtimestamp(exp, tz=datetime.timezone.utc)
                if latest_expiry is None or dt > latest_expiry:
                    latest_expiry = dt

        # Try to extract email from the next-auth session JWT
        email: str | None = None
        try:
            import base64, json as _json
            session_cookie = next(
                (c for c in cookies if c.get("name") == "__Secure-next-auth.session-token"), None
            )
            if session_cookie:
                token = session_cookie.get("value", "")
                parts = token.split(".")
                if len(parts) >= 2:
                    payload_b64 = parts[1]
                    # Fix padding
                    payload_b64 += "=" * (4 - len(payload_b64) % 4)
                    payload = _json.loads(base64.urlsafe_b64decode(payload_b64))
                    raw_email = payload.get("email") or payload.get("user", {}).get("email")
                    if raw_email and "@" in raw_email:
                        local, domain = raw_email.split("@", 1)
                        masked_local = local[0] + "***" if len(local) > 1 else "***"
                        email = f"{masked_local}@{domain}"
        except Exception as e:
            log.debug(f"Could not decode session JWT for email: {e}")

        return {"exists": True, "expires": latest_expiry, "cookie_count": len(found), "email": email}


    async def is_logged_in(self) -> bool:
        """
        Check if user is logged in by looking for chat input vs login indicators.

        Returns True if the chat interface is visible, False if login page detected.
        """
        from src.selectors import Selectors
        from src.claude.selectors import ClaudeSelectors
        from src.gemini.selectors import GeminiSelectors

        chat_inputs, login_indicators, logged_in_indicators = _provider_login_selectors(
            Selectors, ClaudeSelectors, GeminiSelectors
        )

        try:
            # Logged-out ChatGPT can still show a guest composer, so visible
            # login controls must win over chat-input detection.
            for selector in login_indicators:
                try:
                    el = await self.page.wait_for_selector(selector, timeout=2000)
                    if el:
                        log.warning("Login check: NOT LOGGED IN (login button found)")
                        return False
                except Exception:
                    continue

            # Try to find the chat input
            for selector in chat_inputs:
                try:
                    el = await self.page.wait_for_selector(selector, timeout=3000)
                    if el:
                        log.info("Login check: LOGGED IN (chat input found)")
                        return True
                except Exception:
                    continue

            # Claude: also check for user-menu-button as a logged-in signal
            for selector in logged_in_indicators:
                try:
                    el = await self.page.wait_for_selector(selector, timeout=2000)
                    if el:
                        log.info("Login check: LOGGED IN (user menu found)")
                        return True
                except Exception:
                    continue

            log.warning("Login check: UNCERTAIN — no chat input or login button found")
            return False

        except Exception as e:
            log.error(f"Login check error: {e}")
            return False

    async def close(self) -> None:
        """Gracefully close the browser context and playwright instance."""
        log.info("Closing browser...")
        try:
            if self._context:
                await self._context.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            log.error(f"Error closing browser: {e}")
        finally:
            self._context = None
            self._page = None
            self._playwright = None
            log.info("Browser closed")
