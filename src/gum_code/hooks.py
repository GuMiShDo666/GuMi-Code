from collections.abc import Callable
from enum import StrEnum


class HookEvent(StrEnum):
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    STOP = "Stop"


HookCallback = Callable[..., str | None]


class HookRegistry:
    """Store synchronous lifecycle callbacks for one Agent integration."""

    def __init__(self) -> None:
        self._hooks: dict[HookEvent, list[HookCallback]] = {
            event: [] for event in HookEvent
        }

    def register_hook(
        self,
        event: HookEvent | str,
        callback: HookCallback,
    ) -> None:
        if not callable(callback):
            raise TypeError("callback must be callable")
        self._hooks[self._normalize_event(event)].append(callback)

    def trigger_hooks(
        self,
        event: HookEvent | str,
        *args: object,
    ) -> str | None:
        callbacks = tuple(self._hooks[self._normalize_event(event)])
        for callback in callbacks:
            result = callback(*args)
            if result is not None:
                if not isinstance(result, str):
                    raise TypeError("hook callback result must be str or None")
                return result
        return None

    @staticmethod
    def _normalize_event(event: HookEvent | str) -> HookEvent:
        try:
            return HookEvent(event)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Unknown hook event: {event!r}") from error
