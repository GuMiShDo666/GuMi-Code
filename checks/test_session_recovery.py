"""Completed tool batches must remain resumable without replaying effects."""
import copy

import pytest

from gum_code.session import (
    IN_FLIGHT_MARKER_KEY,
    analyze_tool_batch_tail,
    build_tool_batch_recovery_messages,
    load_session,
    plan_tool_batch_recovery,
    save_session,
)


def batch():
    return [
        {"role": "system", "content": "Synthetic system"},
        {"role": "user", "content": "Inspect and plan"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": name, "type": "function", "function": {
                "name": name, "arguments": "{}"}}
            for name in ("list_files", "git_status")
        ]},
        {"role": "tool", "tool_call_id": "list_files", "content": "README.md"},
        {"role": "tool", "tool_call_id": "git_status", "content": "clean"},
    ]


@pytest.mark.parametrize("role", ["user", "system"])
def test_completed_batch_can_precede_new_host_input(tmp_path, role):
    messages = batch()
    state = analyze_tool_batch_tail(messages)
    assert state.completed_call_ids == ("list_files", "git_status")
    assert not state.pending_call_ids
    messages.append({"role": role, "content": "Continue"})
    assert analyze_tool_batch_tail(messages) is None
    assert plan_tool_batch_recovery(messages) is None
    path = tmp_path / "session.jsonl"
    save_session(path, tmp_path, "synthetic", messages)
    restored = load_session(path, allow_pending_user_tail=True)
    assert restored.messages == messages
    assert restored.pending_user_message == ("Continue" if role == "user" else None)


def test_resume_after_completed_todo_batch_preserves_results(tmp_path):
    # Matches the failed desktop history: inspection batch, completed todo_write,
    # recovery without any remaining calls, then the next host request snapshot.
    messages = batch() + [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "plan", "type": "function", "function": {
                "name": "todo_write", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "plan", "content": "Plan saved"},
    ]
    path = tmp_path / "session.jsonl"
    save_session(path, tmp_path, "synthetic", messages)
    loaded = load_session(path, allow_incomplete_tool_batch=True)
    recovered = build_tool_batch_recovery_messages(loaded.messages)
    assert recovered == messages
    recovered.append({"role": "user", "content": "Continue implementation"})
    save_session(path, tmp_path, "synthetic", recovered, user_request_id="00000000-0000-4000-8000-000000000002")
    restored = load_session(path, allow_pending_user_tail=True)
    assert restored.messages == recovered
    assert restored.tool_batch_state is None
    assert restored.pending_user_message == "Continue implementation"
    assert [m for m in restored.messages if m["role"] == "tool"] == [
        m for m in messages if m["role"] == "tool"
    ]


@pytest.mark.parametrize("tail", [
    [{"role": "user", "content": "Continue"}],
    [{"role": "system", "content": "Notice"}],
])
def test_incomplete_batch_still_rejects_non_tool_tail(tail):
    with pytest.raises(ValueError, match="非 tool"):
        analyze_tool_batch_tail(batch()[:-1] + tail)


def test_inflight_batch_still_rejects_following_request():
    messages = batch()
    messages[-1][IN_FLIGHT_MARKER_KEY] = True
    messages.append({"role": "user", "content": "Continue"})
    with pytest.raises(ValueError, match="in-flight"):
        analyze_tool_batch_tail(messages)


@pytest.mark.parametrize("invalid", ["duplicate", "unknown", "order", "orphan"])
def test_completed_batch_does_not_hide_invalid_results(invalid):
    messages = batch()
    if invalid == "duplicate":
        messages.append(copy.deepcopy(messages[-1]))
    elif invalid == "unknown":
        messages[-1]["tool_call_id"] = "unknown"
    elif invalid == "order":
        messages[-2:] = reversed(messages[-2:])
    else:
        messages += [{"role": "user", "content": "Continue"}, copy.deepcopy(messages[-1])]
    with pytest.raises(ValueError):
        analyze_tool_batch_tail(messages)
