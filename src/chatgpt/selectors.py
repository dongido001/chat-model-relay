"""ChatGPT-specific selector compatibility layer.

This centralizes the provider-specific DOM selectors, labels, and fallback text
heuristics used across the ChatGPT browser client and response detector. The
public selector lists remain backwards-compatible with the existing
`src.selectors.Selectors` usage, while exposing a dedicated compatibility layer
that is easier to evolve when ChatGPT changes its UI.
"""

from __future__ import annotations


class ChatGPTSelectors:
    """DOM selectors + compatibility helpers for chatgpt.com."""

    CHAT_INPUT = [
        "#prompt-textarea",
        "div[contenteditable='true'][id='prompt-textarea']",
        "div[contenteditable='true']",
    ]

    SEND_BUTTON = [
        "button[data-testid='send-button']",
        "#composer-submit-button",
        "button[aria-label='Send prompt']",
        "button[aria-label*='Send' i]",
        "#prompt-textarea ~ button",
    ]

    MODEL_PICKER_BUTTON = [
        "button[data-testid='model-switcher-dropdown-button']",
        "button[aria-haspopup='menu']:has-text('Instant')",
        "button[aria-haspopup='menu']:has-text('Thinking')",
        "button[aria-haspopup='menu']:has-text('Medium')",
        "button[aria-haspopup='menu']:has-text('High')",
        "button[aria-haspopup='menu']:has-text('Extra High')",
        "button[aria-haspopup='menu']:has-text('Pro')",
        "button[aria-haspopup='menu']:has-text('Auto')",
        "button[aria-haspopup='menu']:has-text('5.')",
        "button[aria-haspopup='menu']:has-text('GPT')",
    ]

    ASSISTANT_MESSAGE = [
        "div[data-message-author-role='assistant']",
        "[data-message-author-role='assistant']",
        "[data-testid^='conversation-turn-'] [data-message-author-role='assistant']",
        "[data-testid*='conversation-turn' i]",
        ".agent-turn",
        "section[data-turn='assistant']",
        "section[data-testid^='conversation-turn-']",
    ]

    STOP_BUTTON = [
        "button[data-testid='stop-button']",
        "button[aria-label='Stop answering']",
        "button[aria-label='Stop generating']",
        "button[aria-label*='Stop' i]",
    ]

    NEW_CHAT_BUTTON = [
        "a[data-testid='create-new-chat-button']",
        "a[href='/']",
        "nav a[href='/']",
    ]

    SIDEBAR_THREAD_LINKS = [
        "nav a[href^='/c/']",
        "a[href^='/c/']",
    ]

    LOGIN_INDICATORS = [
        "button[data-testid='login-button']",
        "button:has-text('Log in')",
        "[data-testid='login-button']",
    ]

    ASSISTANT_MARKDOWN = [
        "div[data-message-author-role='assistant'] .markdown",
        "div[data-message-author-role='assistant'] .prose",
        "section[data-turn='assistant'] .markdown",
        "section[data-turn='assistant'] .prose",
    ]

    POST_RESPONSE_BUTTONS = [
        "button:has-text('Regenerate')",
        "button:has-text('Continue generating')",
    ]

    COPY_BUTTON = [
        "button[aria-label='Copy response']",
        "button[data-testid='copy-turn-action-button']",
        "button[data-testid*='copy-turn' i]",
        "button[aria-label='Copy message']",
    ]

    ASSISTANT_IMAGE = [
        "img[alt='Generated image']",
        "img[alt*='generated' i]",
        "div[id^='image-'] img",
        "div[class*='imagegen-image'] img",
        "section[data-turn='assistant'] img[alt='Generated image']",
    ]

    IMAGE_CONTAINER = [
        "div[id^='image-']",
        "div[class*='imagegen-image']",
    ]

    IMAGE_DOWNLOAD_BUTTON = [
        "a[aria-label='Download']",
        "a[download]",
    ]

    FILE_UPLOAD_INPUT = [
        "input[type='file']:not([accept*='image'])",
        "input[type='file']",
        "input[data-testid='file-upload']",
        "input#upload-photos",
        "input[accept*='image']",
    ]

    ATTACHMENT_BADGE = [
        "[data-testid*='attachment']",
        "[class*='attachment']",
        "button[aria-label*='Remove']",
        "[data-testid*='file']",
    ]

    ATTACH_BUTTON = [
        "button[data-testid='composer-plus-btn']",
        "button[aria-label='Add files and more']",
        "button[aria-label='Attach files']",
    ]

    SIDEBAR_THREAD_ITEM = [
        "nav a[href^='/c/']",
        "a[href^='/c/']",
    ]

    SIDEBAR_THREAD_MENU_BUTTON = [
        "button[data-testid='thread-menu-button']",
        "button[aria-label='More options']",
        "button[aria-label*='menu' i]",
        "button[aria-label*='actions' i]",
        "button[aria-haspopup='menu']",
    ]

    THREAD_DELETE_OPTION = [
        "button:has-text('Delete')",
        "[role='menuitem']:has-text('Delete')",
        "div[role='menu'] button:has-text('Delete')",
        "div button:has-text('Delete')",
    ]

    THREAD_CONFIRM_DELETE_BUTTON = [
        "button[data-testid='confirm-delete-button']",
        "button[data-testid='delete-confirm-button']",
        "button:has-text('Delete conversation')",
        "button:has-text('Delete chat')",
        "button:has-text('Delete thread')",
        "button[aria-label='Delete conversation']",
        "div[role='alertdialog'] button.bg-red-700",
        "div[role='alertdialog'] button:has-text('Delete')",
        "div[role='dialog'] button:has-text('Delete')",
    ]

    @classmethod
    def send_button_selectors(cls) -> list[str]:
        return list(cls.SEND_BUTTON)

    @classmethod
    def stop_button_selectors(cls) -> list[str]:
        return list(cls.STOP_BUTTON)

    @classmethod
    def copy_button_selectors(cls) -> list[str]:
        return list(cls.COPY_BUTTON)

    @classmethod
    def model_picker_selectors(cls) -> list[str]:
        return list(cls.MODEL_PICKER_BUTTON)

    @classmethod
    def response_completion_selectors(cls) -> list[str]:
        return cls.stop_button_selectors() + cls.copy_button_selectors() + list(cls.POST_RESPONSE_BUTTONS)

    @classmethod
    def compatibility_labels(cls) -> dict[str, tuple[str, ...]]:
        return {
            "copy": tuple(cls.COPY_BUTTON),
            "stop": tuple(cls.STOP_BUTTON),
            "send": tuple(cls.SEND_BUTTON),
            "model-picker": tuple(cls.MODEL_PICKER_BUTTON),
        }

    @classmethod
    def text_heuristics(cls) -> dict[str, tuple[str, ...]]:
        return {
            "response_status": (
                "Generating",
                "Thinking",
                "Searching the web",
                "Creating image",
                "Preparing answer",
                "Working on it",
            ),
            "completion": (
                "Copy response",
                "Copy message",
                "Stop answering",
                "Stop generating",
            ),
        }

    @classmethod
    def selector_fallbacks_for(cls, kind: str) -> list[str]:
        mapping = {
            "copy": cls.copy_button_selectors(),
            "stop": cls.stop_button_selectors(),
            "send": cls.send_button_selectors(),
            "model-picker": cls.model_picker_selectors(),
        }
        return list(mapping.get(kind, ()))


Selectors = ChatGPTSelectors
