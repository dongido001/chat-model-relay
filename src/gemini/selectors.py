"""
Centralized DOM selectors for Gemini (gemini.google.com).

All selectors live here so when Google updates the UI, we only
change this one file. Each entry is a list of fallback selectors —
try them in order until one matches.
"""

from __future__ import annotations


class GeminiSelectors:
    """CSS / Playwright selectors for gemini.google.com UI elements."""

    # Gemini's composer is a rich-textarea wrapping a contenteditable region.
    CHAT_INPUT = [
        "rich-textarea .ql-editor[contenteditable='true']",
        "div.ql-editor[contenteditable='true']",
        "div[aria-label='Enter a prompt for Gemini']",
        "div[aria-label='Enter a prompt here']",
        "div[contenteditable='true'][aria-label*='prompt' i]",
        "rich-textarea",
        "div[contenteditable='true'][role='textbox']",
        "textarea[aria-label*='prompt' i]",
        "div[contenteditable='true']",
    ]

    SEND_BUTTON = [
        "button[aria-label='Send message']",
        "button[aria-label='Send prompt']",
        "button[aria-label='Submit prompt']",
        "button[aria-label='Submit']",
        "button[aria-label='Send']",
        "button[aria-label*='Send' i]",
        "button.send-button",
        "button[mattooltip*='Send' i]",
    ]

    ASSISTANT_MESSAGE = [
        "model-response",
        ".model-response-text",
        "[data-test-id='model-response-text']",
        "message-content",
        ".response-content",
        ".markdown.markdown-main-panel",
    ]

    STOP_BUTTON = [
        "button[aria-label='Stop responding']",
        "button[aria-label='Stop generating']",
        "button[aria-label='Stop']",
        "button[aria-label*='Stop' i]",
    ]

    NEW_CHAT_BUTTON = [
        "button[aria-label='New chat']",
        "button[aria-label*='New chat' i]",
        "a[aria-label='New chat']",
    ]

    SIDEBAR_THREAD_LINKS = [
        "a[href*='/app/']",
        "a[href^='/app/']",
    ]

    LOGIN_INDICATORS = [
        "a:has-text('Sign in')",
        "button:has-text('Sign in')",
        "button:has-text('Log in')",
        "a:has-text('Log in')",
        "a[href*='ServiceLogin']",
        "input[type='email'][autocomplete='username']",
    ]

    LOGGED_IN_INDICATORS = [
        "button[aria-label='New chat']",
        "a[aria-label='Google Account']",
        "img[alt*='Google Account']",
        "rich-textarea",
    ]

    ASSISTANT_MARKDOWN = [
        "model-response .markdown",
        ".model-response-text",
        "message-content",
        ".markdown.markdown-main-panel",
    ]

    USER_MESSAGE = [
        "user-query",
        ".user-query-bubble-with-background",
        "[data-test-id='user-query']",
    ]

    COPY_BUTTON = [
        "button[aria-label='Copy response']",
        "button[aria-label='Copy']",
        "button[aria-label*='Copy' i]",
    ]

    POST_RESPONSE_BUTTONS = [
        "button[aria-label='Regenerate']",
        "button[aria-label='Retry']",
        "button[aria-label*='Retry' i]",
    ]

    FILE_UPLOAD_INPUT = [
        "rich-textarea input[type='file']",
        "input-container input[type='file']",
        "main input[type='file'][accept*='image']",
        "main input[type='file']",
        "input[type='file'][accept*='image']",
    ]

    ATTACHMENT_BADGE = [
        "[data-test-id*='attachment']",
        "[class*='attachment']",
        "button[aria-label*='Remove']",
        "[data-test-id*='file']",
    ]

    ATTACH_BUTTON = [
        "button[aria-label='Open file and tools menu']",
        "button[aria-label='Open upload file menu']",
        "button[aria-label='Add files and more']",
        "button[aria-label*='file and tools' i]",
        "button[aria-label*='Add files' i]",
        "button[aria-label*='Add file' i]",
        "button[aria-label*='upload file' i]",
        "button[aria-label*='upload' i]",
        "button[aria-label*='Attach' i]",
        "button[aria-label*='plus' i]",
    ]

    UPLOAD_MENU_ITEMS = [
        "[role='menuitem']:has-text('Upload files')",
        "[role='menuitem']:has-text('Upload file')",
        "[role='menuitem']:has-text('Your files')",
        "button:has-text('Upload files')",
        "div[role='menuitem']:has-text('Upload')",
    ]

    MODEL_SELECTOR = [
        "button[aria-label*='model' i]",
        "button[data-test-id='bard-mode-menu-button']",
    ]

    ASSISTANT_IMAGE: list[str] = []
    IMAGE_CONTAINER: list[str] = []
    IMAGE_DOWNLOAD_BUTTON: list[str] = []
