import pytest

from vibe_harness.hooks import HookEvent, HookRegistry


@pytest.mark.parametrize("event", list(HookEvent))
def test_registry_supports_every_hook_event(event: HookEvent) -> None:
    registry = HookRegistry()
    calls: list[str] = []

    registry.register_hook(event, lambda: calls.append(event.value))

    assert registry.trigger_hooks(event) is None
    assert calls == [event.value]


def test_hooks_run_in_registration_order_and_stop_on_result() -> None:
    registry = HookRegistry()
    calls: list[str] = []

    registry.register_hook(
        "PreToolUse",
        lambda: calls.append("first"),
    )
    registry.register_hook(
        "PreToolUse",
        lambda: calls.append("second") or "blocked",
    )
    registry.register_hook(
        "PreToolUse",
        lambda: calls.append("should-not-run"),
    )

    assert registry.trigger_hooks("PreToolUse") == "blocked"
    assert calls == ["first", "second"]


def test_registry_instances_are_isolated() -> None:
    first = HookRegistry()
    second = HookRegistry()
    calls: list[str] = []

    first.register_hook("Stop", lambda: calls.append("first"))

    assert second.trigger_hooks("Stop") is None
    assert calls == []


def test_registry_rejects_unknown_event() -> None:
    registry = HookRegistry()

    with pytest.raises(ValueError, match="Unknown hook event"):
        registry.register_hook("BeforeEverything", lambda: None)


def test_registry_rejects_non_callable_callback() -> None:
    registry = HookRegistry()

    with pytest.raises(TypeError, match="callback must be callable"):
        registry.register_hook("Stop", "not-callable")  # type: ignore[arg-type]


def test_registry_rejects_invalid_callback_result() -> None:
    registry = HookRegistry()
    registry.register_hook("Stop", lambda: 1)  # type: ignore[return-value]

    with pytest.raises(TypeError, match="str or None"):
        registry.trigger_hooks("Stop")


def test_registration_during_trigger_applies_next_time() -> None:
    registry = HookRegistry()
    calls: list[str] = []

    def register_late() -> None:
        calls.append("current")
        registry.register_hook("Stop", lambda: calls.append("late"))

    registry.register_hook("Stop", register_late)

    registry.trigger_hooks("Stop")
    assert calls == ["current"]

    registry.trigger_hooks("Stop")
    assert calls == ["current", "current", "late"]
