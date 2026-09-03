import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import vibe_harness.agent as agent_module
from vibe_harness.agent import (
    FINALIZATION_EMPTY_ANSWER,
    STEP_BUDGET_FINALIZATION_PROMPT,
    TODO_SYSTEM_INSTRUCTION,
    UNTRUSTED_TOOL_CONTENT_INSTRUCTION,
    run_agent as _run_agent,
)
from vibe_harness.context import DEFAULT_MAX_CONTEXT_TOKENS
from vibe_harness.hooks import HookEvent, HookRegistry
from vibe_harness.session import IN_FLIGHT_MARKER_KEY
from vibe_harness.todo import MAX_TODO_CONTENT_CHARS


class _TestTokenCounter:
    def count_request(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> int:
        return len(
            json.dumps(
                {"messages": messages, "tools": tools},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )


_TOKEN_COUNTER = _TestTokenCounter()


def run_agent(*args: object, **kwargs: object) -> str:
    kwargs.setdefault("token_counter", _TOKEN_COUNTER)
    return _run_agent(*args, **kwargs)  # type: ignore[arg-type]

class FakeCompletions:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def create(self, **kwargs):
        snapshot = dict(kwargs)
        snapshot["messages"] = list(kwargs["messages"])
        self.requests.append(snapshot)
        return next(self.responses)


def _tool_call(name="list_files", arguments='{"path": "."}', id="call_001"):
    return SimpleNamespace(
        id=id,
        function=SimpleNamespace(
            name=name,
            arguments=arguments,
        ),
    )


def _compact_tool_call(id="call_compact"):
    return SimpleNamespace(
        id=id,
        function=SimpleNamespace(
            name="compact",
            arguments="{}",
        ),
    )


def _todo_write_tool_call(arguments, id="call_todo"):
    return SimpleNamespace(
        id=id,
        function=SimpleNamespace(
            name="todo_write",
            arguments=arguments,
        ),
    )


def _assistant_response(message):
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _assistant_tool_message(tool_calls):
    return SimpleNamespace(content=None, tool_calls=tool_calls)


def _assistant_final_message(content):
    return SimpleNamespace(content=content, tool_calls=None)


def _fake_client(responses):
    fake_completions = FakeCompletions(responses)
    return SimpleNamespace(
        chat=SimpleNamespace(completions=fake_completions)
    ), fake_completions


class ScriptedCompletions:
    def __init__(self, actions):
        self.actions = list(actions)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(dict(kwargs))
        action = self.actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        return action


def _scripted_client(actions):
    completions = ScriptedCompletions(actions)
    return SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    ), completions


def test_run_agent_executes_tool_and_returns_final_answer(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hi")

    tool_call = _tool_call()
    responses = [
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_final_message("工作区检查完成")),
    ]
    client, completions = _fake_client(responses)

    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="看看有什么",
    )

    assert answer == "工作区检查完成"
    assert len(completions.requests) == 2

    second_request = completions.requests[1]
    tool_message = second_request["messages"][-1]
    assert tool_message["role"] == "tool"
    assert "hello.txt" in tool_message["content"]


def test_run_agent_returns_matching_tool_call_id(tmp_path: Path) -> None:
    tool_call = _tool_call(id="call_001")
    responses = [
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_final_message("done")),
    ]
    client, completions = _fake_client(responses)

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="看看有什么",
    )

    second_request = completions.requests[1]
    tool_message = second_request["messages"][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call_001"


def test_run_agent_enforces_max_steps(tmp_path: Path) -> None:
    tool_call = _tool_call()
    responses = [
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_final_message("阶段总结")),
    ]
    client, completions = _fake_client(responses)

    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="看看有什么",
        max_steps=2,
    )

    assert answer == "阶段总结"
    assert len(completions.requests) == 3


def test_run_agent_step_budget_final_request_has_no_tools(tmp_path: Path) -> None:
    tool_call = _tool_call()
    responses = [
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_final_message("阶段总结")),
    ]
    client, completions = _fake_client(responses)

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="看看有什么",
        max_steps=2,
    )

    final_request = completions.requests[2]
    assert "tools" not in final_request
    assert "tool_choice" not in final_request
    final_messages = final_request["messages"]
    assert final_messages[-1]["role"] == "user"
    assert final_messages[-1]["content"] == STEP_BUDGET_FINALIZATION_PROMPT


def test_run_agent_step_budget_keeps_completed_results(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hi")
    tool_call = _tool_call()
    responses = [
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_final_message("阶段总结")),
    ]
    client, completions = _fake_client(responses)

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="看看有什么",
        max_steps=2,
    )

    final_messages = completions.requests[2]["messages"]
    tool_results = [m for m in final_messages if m.get("role") == "tool"]
    assert len(tool_results) == 2
    assert any("hello.txt" in str(m["content"]) for m in tool_results)


def test_run_agent_step_budget_triggers_final_answer_event(tmp_path: Path) -> None:
    tool_call = _tool_call()
    responses = [
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_final_message("阶段总结")),
    ]
    client, _ = _fake_client(responses)
    events: list[tuple[str, dict[str, object]]] = []

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="看看有什么",
        max_steps=2,
        event_callback=lambda t, p: events.append((t, p)),
    )

    final_answers = [e for e in events if e[0] == "final_answer"]
    assert len(final_answers) == 1
    assert final_answers[0][1]["answer"] == "阶段总结"


def test_run_agent_step_budget_session_ends_with_assistant(tmp_path: Path) -> None:
    tool_call = _tool_call()
    responses = [
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_final_message("阶段总结")),
    ]
    client, _ = _fake_client(responses)
    snapshots: list[list[dict[str, object]]] = []

    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="看看有什么",
        max_steps=2,
        message_snapshot_callback=lambda msgs: snapshots.append(
            json.loads(json.dumps(msgs))
        ),
    )

    assert answer == "阶段总结"
    last = snapshots[-1]
    assert last[-1]["role"] == "assistant"
    assert last[-1]["content"] == "阶段总结"


def test_run_agent_step_budget_ignores_final_tool_calls(tmp_path: Path) -> None:
    tool_call = _tool_call()
    write_call = _tool_call(
        name="write_file",
        arguments='{"path": "pwned.txt", "content": "x"}',
    )
    responses = [
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_tool_message([write_call])),
    ]
    client, _ = _fake_client(responses)
    approvals: list[str] = []

    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="看看有什么",
        max_steps=2,
        approval_callback=lambda name, args, reason: approvals.append(name) or True,
    )

    assert answer == FINALIZATION_EMPTY_ANSWER
    assert not (tmp_path / "pwned.txt").exists()
    assert approvals == []


def test_run_agent_step_budget_empty_answer_placeholder(tmp_path: Path) -> None:
    tool_call = _tool_call()
    responses = [
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_final_message("")),
    ]
    client, _ = _fake_client(responses)

    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="看看有什么",
        max_steps=2,
    )

    assert answer == FINALIZATION_EMPTY_ANSWER


def test_run_agent_step_budget_finalization_error_propagates(tmp_path: Path) -> None:
    tool_call = _tool_call()
    actions = [
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_tool_message([tool_call])),
        RuntimeError("boom"),
    ]
    client, _ = _scripted_client(actions)

    with pytest.raises(RuntimeError, match="boom"):
        run_agent(
            client=client,
            model="fake-model",
            workspace=tmp_path,
            user_query="看看有什么",
            max_steps=2,
        )


def test_step_budget_compaction_failure_keeps_last_safe_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_call = _tool_call()
    actions = [
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_final_message("历史摘要")),
        RuntimeError("finalization failed"),
    ]
    client, _ = _scripted_client(actions)
    snapshots: list[list[dict[str, object]]] = []
    monkeypatch.setattr(
        agent_module,
        "execute_tool",
        lambda **kwargs: "x" * 50_000,
    )

    with pytest.raises(RuntimeError, match="finalization failed"):
        run_agent(
            client=client,
            model="fake-model",
            workspace=tmp_path,
            user_query="看看有什么",
            max_steps=1,
            max_context_tokens=10_000,
            message_snapshot_callback=lambda messages: snapshots.append(
                json.loads(json.dumps(messages))
            ),
        )

    assert snapshots[-1][-1]["role"] == "tool"
    assert all(
        STEP_BUDGET_FINALIZATION_PROMPT
        not in str(message.get("content", ""))
        for snapshot in snapshots
        for message in snapshot
    )


def test_run_agent_step_budget_context_error_compacts_once(tmp_path: Path) -> None:
    tool_call = _tool_call()
    actions = [
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_tool_message([tool_call])),
        RuntimeError("context_length_exceeded: prompt too long"),
        _assistant_response(_assistant_final_message("这是摘要")),
        RuntimeError("context_length_exceeded: prompt too long again"),
    ]
    client, completions = _scripted_client(actions)
    events: list[tuple[str, dict[str, object]]] = []

    with pytest.raises(RuntimeError):
        run_agent(
            client=client,
            model="fake-model",
            workspace=tmp_path,
            user_query="看看有什么",
            max_steps=2,
            event_callback=lambda t, p: events.append((t, p)),
        )

    reactive = [
        e
        for e in events
        if e[0] == "context_compacted" and e[1].get("stage") == "reactive_compact"
    ]
    assert len(reactive) == 1
    assert reactive[0][1]["retry"] == 1
    assert len(completions.requests) == 5


def test_run_agent_returns_early_below_default_max_steps(tmp_path: Path) -> None:
    responses = [
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="hi",
    )

    assert answer == "完成"
    assert len(completions.requests) == 1


def test_run_agent_writes_file_when_approved(tmp_path: Path) -> None:
    tool_call = _tool_call(
        name="write_file",
        arguments='{"path": "x.txt", "content": "hello"}',
    )
    responses = [
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_final_message("已写入")),
    ]
    client, completions = _fake_client(responses)

    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="写入文件",
        approval_callback=lambda name, arguments, reason: True,
    )

    assert answer == "已写入"
    assert (tmp_path / "x.txt").read_text() == "hello"
    assert len(completions.requests) == 2


def test_run_agent_denies_write_when_not_approved(tmp_path: Path) -> None:
    tool_call = _tool_call(
        name="write_file",
        arguments='{"path": "x.txt", "content": "hello"}',
    )
    responses = [
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_final_message("已处理")),
    ]
    client, completions = _fake_client(responses)

    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="写入文件",
        approval_callback=lambda name, arguments, reason: False,
    )

    assert answer == "已处理"
    assert not (tmp_path / "x.txt").exists()

    second_request = completions.requests[1]
    tool_message = second_request["messages"][-1]
    assert tool_message["role"] == "tool"
    assert "denied by user" in tool_message["content"]


def test_run_agent_denied_tool_call_not_reasked(tmp_path: Path) -> None:
    write_call_1 = _tool_call(
        name="write_file",
        arguments='{"path": "x.txt", "content": "hi"}',
        id="call_1",
    )
    write_call_2 = _tool_call(
        name="write_file",
        arguments='{"path": "x.txt", "content": "hi"}',
        id="call_2",
    )
    responses = [
        _assistant_response(_assistant_tool_message([write_call_1])),
        _assistant_response(_assistant_tool_message([write_call_2])),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, _ = _fake_client(responses)
    approvals: list[object] = []

    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="写文件",
        approval_callback=lambda name, args, reason: approvals.append(args) or False,
    )

    assert answer == "完成"
    assert len(approvals) == 1
    assert not (tmp_path / "x.txt").exists()


def test_run_agent_denied_tool_call_equivalent_json_not_reasked(
    tmp_path: Path,
) -> None:
    write_call_1 = _tool_call(
        name="write_file",
        arguments='{"path": "x.txt", "content": "hi"}',
        id="call_1",
    )
    write_call_2 = _tool_call(
        name="write_file",
        arguments='{ "content" : "hi", "path" : "x.txt" }',
        id="call_2",
    )
    responses = [
        _assistant_response(_assistant_tool_message([write_call_1])),
        _assistant_response(_assistant_tool_message([write_call_2])),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, _ = _fake_client(responses)
    approvals: list[object] = []

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="写文件",
        approval_callback=lambda name, args, reason: approvals.append(args) or False,
    )

    assert len(approvals) == 1


def test_run_agent_denied_tool_call_different_args_reasked(
    tmp_path: Path,
) -> None:
    write_call_1 = _tool_call(
        name="write_file",
        arguments='{"path": "x.txt", "content": "hi"}',
        id="call_1",
    )
    write_call_2 = _tool_call(
        name="write_file",
        arguments='{"path": "x.txt", "content": "changed"}',
        id="call_2",
    )
    responses = [
        _assistant_response(_assistant_tool_message([write_call_1])),
        _assistant_response(_assistant_tool_message([write_call_2])),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, _ = _fake_client(responses)
    approvals: list[object] = []

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="写文件",
        approval_callback=lambda name, args, reason: approvals.append(args) or False,
    )

    assert len(approvals) == 2


def test_run_agent_validation_failure_not_recorded_as_denied(
    tmp_path: Path,
) -> None:
    bad_call_1 = _tool_call(
        name="write_file",
        arguments='{"path": ".env", "content": "x"}',
        id="call_1",
    )
    bad_call_2 = _tool_call(
        name="write_file",
        arguments='{"path": ".env", "content": "x"}',
        id="call_2",
    )
    responses = [
        _assistant_response(_assistant_tool_message([bad_call_1])),
        _assistant_response(_assistant_tool_message([bad_call_2])),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)
    approvals: list[object] = []

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="写文件",
        approval_callback=lambda name, args, reason: approvals.append(args) or True,
    )

    assert approvals == []
    messages = completions.requests[2]["messages"]
    assert messages[3]["content"].startswith("Error: 禁止访问敏感或内部路径")
    assert messages[5]["content"].startswith("Error: 禁止访问敏感或内部路径")
    assert "denied earlier" not in messages[5]["content"]


def test_run_agent_denial_like_tool_output_not_recorded_as_denied(
    tmp_path: Path,
    monkeypatch,
) -> None:
    call_1 = _tool_call(name="run_command", arguments='{"command": "test"}', id="call_1")
    call_2 = _tool_call(name="run_command", arguments='{"command": "test"}', id="call_2")
    responses = [
        _assistant_response(_assistant_tool_message([call_1])),
        _assistant_response(_assistant_tool_message([call_2])),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)
    executions: list[str] = []

    def fake_execute_tool(**kwargs: object) -> str:
        executions.append(str(kwargs["tool_name"]))
        return "Error: Tool execution denied by user: run_command"

    monkeypatch.setattr("vibe_harness.agent.execute_tool", fake_execute_tool)

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="执行命令",
        allow_run_command=True,
    )

    assert executions == ["run_command", "run_command"]
    messages = completions.requests[2]["messages"]
    assert "denied earlier" not in messages[5]["content"]


def test_run_agent_default_tools_exclude_run_command(tmp_path: Path) -> None:
    responses = [
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="看看有什么",
    )

    tool_names = {
        tool["function"]["name"]
        for tool in completions.requests[0]["tools"]
    }
    assert "run_command" not in tool_names


def test_run_agent_forged_run_command_rejected_without_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = _tool_call(
        name="run_command",
        arguments='{"argv": ["touch", "pwned.txt"]}',
        id="call_forged",
    )
    responses = [
        _assistant_response(_assistant_tool_message([call])),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    executed: list[object] = []
    approvals: list[object] = []
    snapshots: list[list[dict[str, object]]] = []

    def fake_execute_tool(**kwargs: object) -> str:
        executed.append(kwargs)
        return "SHOULD NOT RUN"

    monkeypatch.setattr(agent_module, "execute_tool", fake_execute_tool)

    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="运行命令",
        approval_callback=lambda name, args, reason: approvals.append(name) or True,
        message_snapshot_callback=lambda messages: snapshots.append(
            json.loads(json.dumps(messages))
        ),
    )

    assert answer == "完成"
    assert executed == []
    assert approvals == []
    assert not (tmp_path / "pwned.txt").exists()
    assert not any(
        IN_FLIGHT_MARKER_KEY in message
        for snapshot in snapshots
        for message in snapshot
    )

    tool_message = completions.requests[1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call_forged"
    assert "not enabled for this run" in tool_message["content"]


def test_run_agent_allow_run_command_includes_schema_and_approves(
    tmp_path: Path,
) -> None:
    call = _tool_call(
        name="run_command",
        arguments='{"argv": ["touch", "marker.txt"]}',
        id="call_cmd",
    )
    responses = [
        _assistant_response(_assistant_tool_message([call])),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    approvals: list[object] = []

    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="运行命令",
        allow_run_command=True,
        approval_callback=lambda name, args, reason: approvals.append((name, args)) or False,
    )

    assert answer == "完成"

    tool_names = {
        tool["function"]["name"]
        for tool in completions.requests[0]["tools"]
    }
    assert "run_command" in tool_names

    assert len(approvals) == 1
    assert approvals[0][0] == "run_command"
    assert approvals[0][1]["argv"] == ["touch", "marker.txt"]
    assert not (tmp_path / "marker.txt").exists()

    tool_message = completions.requests[1]["messages"][-1]
    assert "denied by user" in tool_message["content"]


def test_run_agent_denied_skip_produces_matching_tool_messages(
    tmp_path: Path,
) -> None:
    write_call_1 = _tool_call(
        name="write_file",
        arguments='{"path": "x.txt", "content": "hi"}',
        id="call_1",
    )
    write_call_2 = _tool_call(
        name="write_file",
        arguments='{"path": "x.txt", "content": "hi"}',
        id="call_2",
    )
    responses = [
        _assistant_response(_assistant_tool_message([write_call_1])),
        _assistant_response(_assistant_tool_message([write_call_2])),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="写文件",
        approval_callback=lambda name, args, reason: False,
    )

    messages = completions.requests[2]["messages"]
    assert messages[3]["role"] == "tool"
    assert messages[3]["tool_call_id"] == "call_1"
    assert "denied by user" in messages[3]["content"]

    assert messages[5]["role"] == "tool"
    assert messages[5]["tool_call_id"] == "call_2"
    assert "denied earlier" in messages[5]["content"]


def test_run_agent_emits_events_without_tools(tmp_path: Path) -> None:
    responses = [
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, _ = _fake_client(responses)

    events = []
    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="hi",
        event_callback=lambda event_type, payload: events.append(
            (event_type, payload)
        ),
    )

    assert [e[0] for e in events] == [
        "context_usage",
        "model_call",
        "final_answer",
    ]
    assert events[0][1]["step"] == 1
    assert events[0][1]["used_tokens"] > 0
    assert events[0][1]["max_tokens"] == DEFAULT_MAX_CONTEXT_TOKENS
    assert events[1][1] == {"step": 1}
    assert events[2][1]["step"] == 1
    assert events[2][1]["answer"] == "完成"


def test_run_agent_emits_provider_usage_when_available(tmp_path: Path) -> None:
    response = _assistant_response(_assistant_final_message("完成"))
    response.usage = SimpleNamespace(
        prompt_tokens=123,
        completion_tokens=8,
        total_tokens=131,
    )
    client, _ = _fake_client([response])
    events: list[tuple[str, dict[str, object]]] = []

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="hi",
        event_callback=lambda event_type, payload: events.append(
            (event_type, payload)
        ),
    )

    usage = next(payload for name, payload in events if name == "provider_usage")
    assert usage["prompt_tokens"] == 123
    assert usage["completion_tokens"] == 8
    assert usage["total_tokens"] == 131
    assert isinstance(usage["local_prompt_tokens"], int)


def test_run_agent_emits_provider_usage_for_summary_and_main_request(
    tmp_path: Path,
) -> None:
    summary_response = _assistant_response(_assistant_final_message("摘要"))
    summary_response.usage = SimpleNamespace(prompt_tokens=101)
    main_response = _assistant_response(_assistant_final_message("完成"))
    main_response.usage = SimpleNamespace(prompt_tokens=202)
    client, _ = _fake_client([summary_response, main_response])
    events: list[tuple[str, dict[str, object]]] = []

    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="继续",
        initial_messages=[
            {"role": "system", "content": "s"},
            {"role": "user", "content": "X" * 50_000},
        ],
        max_context_tokens=20_000,
        event_callback=lambda event_type, payload: events.append(
            (event_type, payload)
        ),
    )

    assert answer == "完成"
    usage_events = [payload for name, payload in events if name == "provider_usage"]
    assert [payload["prompt_tokens"] for payload in usage_events] == [101, 202]
    assert all(
        isinstance(payload["local_prompt_tokens"], int)
        for payload in usage_events
    )


def test_run_agent_emits_events_with_tools(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hi")

    tool_call = _tool_call(name="list_files")
    responses = [
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, _ = _fake_client(responses)

    events = []
    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="看看有什么",
        event_callback=lambda event_type, payload: events.append(
            (event_type, payload)
        ),
    )

    assert [e[0] for e in events] == [
        "context_usage",
        "model_call",
        "tool_call",
        "tool_result",
        "context_usage",
        "model_call",
        "final_answer",
    ]

    assert events[0][1]["step"] == 1

    model_call = events[1][1]
    assert model_call == {"step": 1}

    tool_call_event = events[2][1]
    assert tool_call_event["step"] == 1
    assert tool_call_event["tool_name"] == "list_files"
    assert tool_call_event["arguments"] == '{"path": "."}'

    tool_result_event = events[3][1]
    assert tool_result_event["step"] == 1
    assert tool_result_event["tool_name"] == "list_files"
    assert "hello.txt" in tool_result_event["result"]

    assert events[4][1]["step"] == 2

    second_model_call = events[5][1]
    assert second_model_call == {"step": 2}

    final_answer = events[6][1]
    assert final_answer["step"] == 2
    assert final_answer["answer"] == "完成"


def test_assistant_message_stored_as_plain_dict(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("x = 1")

    tool_call = _tool_call(
        name="read_file",
        arguments='{"path": "x.py"}',
        id="call_001",
    )
    first_message = _assistant_tool_message([tool_call])
    first_message.reasoning_content = "需要先读取文件"
    responses = [
        _assistant_response(first_message),
        _assistant_response(_assistant_final_message("done")),
    ]
    client, completions = _fake_client(responses)

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="读文件",
    )

    second_request = completions.requests[1]
    assistant_message = second_request["messages"][2]

    assert isinstance(assistant_message, dict)
    assert assistant_message["role"] == "assistant"
    assert assistant_message["content"] is None
    assert assistant_message["reasoning_content"] == "需要先读取文件"
    assert assistant_message["tool_calls"] == [
        {
            "id": "call_001",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": '{"path": "x.py"}',
            },
        }
    ]


def test_stop_continuation_preserves_final_reasoning_content(
    tmp_path: Path,
) -> None:
    first_message = _assistant_final_message("需要确认")
    first_message.reasoning_content = "内部推理"
    client, completions = _fake_client(
        [
            _assistant_response(first_message),
            _assistant_response(_assistant_final_message("完成")),
        ]
    )
    hooks = HookRegistry()
    calls = 0

    def continue_once(messages: list[dict[str, object]]) -> str | None:
        nonlocal calls
        calls += 1
        return "继续" if calls == 1 else None

    hooks.register_hook(HookEvent.STOP, continue_once)

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="任务",
        hook_registry=hooks,
    )

    assert completions.requests[1]["messages"][-2] == {
        "role": "assistant",
        "content": "需要确认",
        "reasoning_content": "内部推理",
    }


def test_second_request_messages_are_json_serializable(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hi")

    tool_call = _tool_call()
    responses = [
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_final_message("done")),
    ]
    client, completions = _fake_client(responses)

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="看看有什么",
    )

    second_request = completions.requests[1]

    json.dumps(second_request["messages"])


def test_multiple_tool_calls_are_preserved(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("a")
    (tmp_path / "b.py").write_text("b")

    calls = [
        _tool_call(name="read_file", arguments='{"path": "a.py"}', id="call_1"),
        _tool_call(name="read_file", arguments='{"path": "b.py"}', id="call_2"),
    ]
    responses = [
        _assistant_response(_assistant_tool_message(calls)),
        _assistant_response(_assistant_final_message("done")),
    ]
    client, completions = _fake_client(responses)

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="读两个文件",
    )

    second_request = completions.requests[1]
    assistant_message = second_request["messages"][2]

    assert [c["id"] for c in assistant_message["tool_calls"]] == ["call_1", "call_2"]
    assert [c["function"]["name"] for c in assistant_message["tool_calls"]] == [
        "read_file",
        "read_file",
    ]
    assert [c["function"]["arguments"] for c in assistant_message["tool_calls"]] == [
        '{"path": "a.py"}',
        '{"path": "b.py"}',
    ]


def test_run_agent_sends_message_snapshots_at_key_points(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hi")

    tool_call = _tool_call(name="list_files")
    responses = [
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, _ = _fake_client(responses)

    snapshots: list[list[dict[str, object]]] = []
    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="看看有什么",
        message_snapshot_callback=lambda msgs: snapshots.append(
            json.loads(json.dumps(msgs))
        ),
    )

    assert [m["role"] for m in snapshots[0]] == ["system", "user"]
    assert [m["role"] for m in snapshots[1]] == [
        "system",
        "user",
        "assistant",
    ]
    assert [m["role"] for m in snapshots[2]] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assert snapshots[2][3].get(IN_FLIGHT_MARKER_KEY) is True
    assert [m["role"] for m in snapshots[3]] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assert [m["role"] for m in snapshots[4]] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
    ]

    assert snapshots[1][2]["tool_calls"][0]["function"]["name"] == "list_files"
    assert snapshots[3][3]["role"] == "tool"
    assert snapshots[3][3]["tool_call_id"] == "call_001"
    assert snapshots[4][4]["content"] == "完成"


def test_run_agent_works_without_snapshot_callback(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hi")

    tool_call = _tool_call(name="list_files")
    responses = [
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="看看有什么",
    )

    assert answer == "完成"
    assert len(completions.requests) == 2


def test_snapshot_callback_cannot_mutate_agent_messages(tmp_path: Path) -> None:
    responses = [
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    def mutate_snapshot(messages: list[dict[str, object]]) -> None:
        messages.clear()

    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="检查项目",
        message_snapshot_callback=mutate_snapshot,
    )

    assert answer == "完成"
    assert [message["role"] for message in completions.requests[0]["messages"]] == [
        "system",
        "user",
    ]


def test_run_agent_with_initial_messages_no_duplicate_system(tmp_path: Path) -> None:
    initial_messages = [
        {"role": "system", "content": "历史系统提示"},
        {"role": "user", "content": "旧任务"},
        {"role": "assistant", "content": "旧回答"},
    ]
    responses = [
        _assistant_response(_assistant_final_message("新回答")),
    ]
    client, completions = _fake_client(responses)

    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="新任务",
        initial_messages=initial_messages,
    )

    assert answer == "新回答"

    sent = completions.requests[0]["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    assert sent[0]["content"].startswith("历史系统提示")
    assert sent[-1] == {"role": "user", "content": "新任务"}
    assert [m["role"] for m in sent].count("system") == 1


def test_run_agent_appends_user_task_to_history(tmp_path: Path) -> None:
    initial_messages = [
        {"role": "system", "content": "s"},
        {"role": "assistant", "content": "旧回答"},
    ]
    responses = [
        _assistant_response(_assistant_final_message("ok")),
    ]
    client, completions = _fake_client(responses)

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="追加的任务",
        initial_messages=initial_messages,
    )

    sent = completions.requests[0]["messages"]
    assert sent[-1] == {"role": "user", "content": "追加的任务"}
    assert len(sent) == len(initial_messages) + 1


def test_run_agent_does_not_mutate_initial_messages(tmp_path: Path) -> None:
    initial_messages = [
        {"role": "system", "content": "s"},
        {"role": "assistant", "content": "旧回答"},
    ]
    original = json.loads(json.dumps(initial_messages))
    responses = [
        _assistant_response(_assistant_final_message("ok")),
    ]
    client, _ = _fake_client(responses)

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="新任务",
        initial_messages=initial_messages,
    )

    assert initial_messages == original


def test_run_agent_does_not_execute_historical_tool_calls(tmp_path: Path) -> None:
    initial_messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写一个文件"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_old",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": '{"path": "x.txt", "content": "hello"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_old", "content": "写入 5 字符"},
        {"role": "assistant", "content": "已写入"},
    ]
    responses = [
        _assistant_response(_assistant_final_message("继续完成")),
    ]
    client, completions = _fake_client(responses)

    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="继续检查",
        initial_messages=initial_messages,
    )

    assert answer == "继续完成"
    assert not (tmp_path / "x.txt").exists()

    sent = completions.requests[0]["messages"]
    assert len(sent) == len(initial_messages) + 1
    assert sent[-1]["role"] == "user"


def test_run_agent_snapshot_after_initial_messages(tmp_path: Path) -> None:
    initial_messages = [
        {"role": "system", "content": "s"},
        {"role": "assistant", "content": "旧回答"},
    ]
    responses = [
        _assistant_response(_assistant_final_message("ok")),
    ]
    client, _ = _fake_client(responses)

    snapshots: list[list[dict[str, object]]] = []
    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="新任务",
        initial_messages=initial_messages,
        message_snapshot_callback=lambda msgs: snapshots.append(
            json.loads(json.dumps(msgs))
        ),
    )

    assert [m["role"] for m in snapshots[0]] == ["system", "assistant", "user"]
    assert snapshots[0][-1]["content"] == "新任务"
    assert [m["role"] for m in snapshots[1]] == [
        "system",
        "assistant",
        "user",
        "assistant",
    ]


def test_run_agent_calls_model_within_budget(tmp_path: Path) -> None:
    responses = [
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="hi",
        max_context_tokens=DEFAULT_MAX_CONTEXT_TOKENS,
    )

    assert answer == "完成"
    assert len(completions.requests) == 1
    assert completions.requests[0]["extra_body"] == {
        "thinking": {"type": "enabled"}
    }


def test_run_agent_raises_when_over_budget_and_never_calls_main_model(
    tmp_path: Path,
) -> None:
    responses = [
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    with pytest.raises(RuntimeError):
        run_agent(
            client=client,
            model="fake-model",
            workspace=tmp_path,
            user_query="hi",
            max_context_tokens=10,
        )

    assert all("tools" not in request for request in completions.requests)


def test_run_agent_over_budget_error_includes_used_and_limit(
    tmp_path: Path,
) -> None:
    responses = [
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, _ = _fake_client(responses)
    budget = 50

    with pytest.raises(RuntimeError) as exc_info:
        run_agent(
            client=client,
            model="fake-model",
            workspace=tmp_path,
            user_query="检查独特项目结构",
            max_context_tokens=budget,
        )

    message = str(exc_info.value)
    numbers = [int(x) for x in re.findall(r"\d+", message)]

    assert budget in numbers
    assert any(n > budget for n in numbers)
    assert "检查独特项目结构" not in message


@pytest.mark.parametrize("budget", [0, -1, 100.5, "lots"])
def test_run_agent_rejects_non_positive_budget(
    tmp_path: Path,
    budget: object,
) -> None:
    responses = [
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    with pytest.raises(RuntimeError):
        run_agent(
            client=client,
            model="fake-model",
            workspace=tmp_path,
            user_query="hi",
            max_context_tokens=budget,
        )

    assert len(completions.requests) == 0


def test_run_agent_rejects_boolean_budget_before_snapshot(tmp_path: Path) -> None:
    responses = [
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)
    snapshots: list[list[dict[str, object]]] = []

    with pytest.raises(RuntimeError):
        run_agent(
            client=client,
            model="fake-model",
            workspace=tmp_path,
            user_query="hi",
            max_context_tokens=True,
            message_snapshot_callback=lambda messages: snapshots.append(messages),
        )

    assert snapshots == []
    assert len(completions.requests) == 0


def test_run_agent_budget_checks_resumed_session(tmp_path: Path) -> None:
    long_history = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "x" * 500},
        {"role": "assistant", "content": "y" * 500},
    ]
    responses = [
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    with pytest.raises(RuntimeError):
        run_agent(
            client=client,
            model="fake-model",
            workspace=tmp_path,
            user_query="继续",
            initial_messages=long_history,
            max_context_tokens=500,
        )

    assert all("tools" not in request for request in completions.requests)


def test_run_agent_compacts_large_tool_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_call = _tool_call(name="list_files", id="call_001")
    responses = [
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, _ = _fake_client(responses)

    big = "x" * 250_000

    def fake_execute_tool(**kwargs: object) -> str:
        return big

    monkeypatch.setattr("vibe_harness.agent.execute_tool", fake_execute_tool)

    events: list[tuple[str, dict[str, object]]] = []
    snapshots: list[list[dict[str, object]]] = []

    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="看看",
        event_callback=lambda t, p: events.append((t, p)),
        message_snapshot_callback=lambda msgs: snapshots.append(
            json.loads(json.dumps(msgs))
        ),
    )

    assert answer == "完成"

    compacted = [e for e in events if e[0] == "context_compacted"]
    assert len(compacted) == 1
    payload = compacted[0][1]
    assert payload["stage"] == "tool_result_budget"
    assert payload["persisted_results"] == 1
    assert payload["before_tokens"] > payload["after_tokens"]

    saved = tmp_path / ".task_outputs" / "tool-results" / "call_001.txt"
    assert saved.read_text(encoding="utf-8") == big

    assert snapshots[3][3]["content"] == big

    compacted_snapshot = snapshots[4][3]
    assert compacted_snapshot["role"] == "tool"
    assert compacted_snapshot["tool_call_id"] == "call_001"
    assert "<persisted-output>" in compacted_snapshot["content"]
    assert compacted_snapshot["content"] != big


def test_run_agent_no_context_compacted_without_large_result(
    tmp_path: Path,
) -> None:
    (tmp_path / "hello.txt").write_text("hi")

    tool_call = _tool_call(name="list_files")
    responses = [
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, _ = _fake_client(responses)

    events = []
    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="看看",
        event_callback=lambda t, p: events.append((t, p)),
    )

    assert "context_compacted" not in [e[0] for e in events]


def test_run_agent_snips_long_history(tmp_path: Path) -> None:
    long_history: list[dict[str, object]] = [{"role": "system", "content": "s"}]
    for i in range(59):
        role = "user" if i % 2 == 0 else "assistant"
        long_history.append({"role": role, "content": f"m{i}"})

    responses = [
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    events = []
    snapshots = []
    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="继续",
        initial_messages=long_history,
        event_callback=lambda t, p: events.append((t, p)),
        message_snapshot_callback=lambda msgs: snapshots.append(
            json.loads(json.dumps(msgs))
        ),
    )

    assert answer == "完成"

    snip_events = [
        e for e in events if e[0] == "context_compacted" and e[1].get("stage") == "snip_compact"
    ]
    assert len(snip_events) == 1
    payload = snip_events[0][1]
    assert payload["archived_messages"] == 12
    assert str(payload["transcript"]).startswith(".transcripts/transcript_")

    sent = completions.requests[0]["messages"]
    assert len(sent) == 50
    assert "messages archived at" in sent[3]["content"]

    assert any(
        len(s) == 50 and any("messages archived at" in str(m.get("content", "")) for m in s)
        for s in snapshots
    )


def test_run_agent_compacts_layers_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_history: list[dict[str, object]] = [{"role": "system", "content": "s"}]
    for i in range(59):
        role = "user" if i % 2 == 0 else "assistant"
        long_history.append({"role": role, "content": f"m{i}"})

    tool_call = _tool_call(name="list_files", id="call_001")
    responses = [
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, _ = _fake_client(responses)

    monkeypatch.setattr(
        "vibe_harness.agent.execute_tool",
        lambda **kwargs: "x" * 250_000,
    )

    events = []
    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="继续",
        initial_messages=long_history,
        event_callback=lambda t, p: events.append((t, p)),
    )

    compacted = [e for e in events if e[0] == "context_compacted"]
    step2 = [e for e in compacted if e[1].get("step") == 2]
    assert [e[1]["stage"] for e in step2] == ["tool_result_budget", "snip_compact"]


def _assistant_tool_call_dict(call_id: str) -> dict[str, object]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "list_files", "arguments": "{}"},
            }
        ],
    }


def test_run_agent_skips_micro_and_fit_within_budget(tmp_path: Path) -> None:
    responses = [
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    events: list[tuple[str, dict[str, object]]] = []
    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="hi",
        event_callback=lambda t, p: events.append((t, p)),
    )

    assert answer == "完成"
    assert len(completions.requests) == 1
    stages = [e[1]["stage"] for e in events if e[0] == "context_compacted"]
    assert stages == []


def test_run_agent_runs_micro_and_fit_then_calls_model(tmp_path: Path) -> None:
    initial: list[dict[str, object]] = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
    ]
    for i in range(5):
        call_id = f"c{i}"
        initial.append(_assistant_tool_call_dict(call_id))
        initial.append({"role": "tool", "tool_call_id": call_id, "content": "x" * 5000})
    initial.append(_assistant_tool_call_dict("call_001"))
    initial.append(
        {"role": "tool", "tool_call_id": "call_001", "content": "y" * 5000}
    )

    responses = [
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    events: list[tuple[str, dict[str, object]]] = []
    snapshots: list[list[dict[str, object]]] = []
    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="继续",
        initial_messages=initial,
        max_context_tokens=15_000,
        event_callback=lambda t, p: events.append((t, p)),
        message_snapshot_callback=lambda msgs: snapshots.append(
            json.loads(json.dumps(msgs))
        ),
    )

    assert answer == "完成"
    assert len(completions.requests) == 1

    compacted = [e for e in events if e[0] == "context_compacted"]
    assert [e[1]["stage"] for e in compacted] == ["micro_compact", "fit_tool_results"]

    micro = compacted[0][1]
    assert micro["shortened_results"] == 2
    assert micro["before_tokens"] > micro["after_tokens"]

    fit = compacted[1][1]
    assert fit["fitted_results"] == 4
    assert fit["before_tokens"] > fit["after_tokens"]

    snapshot_text = "\n".join(
        json.dumps(s, ensure_ascii=False) for s in snapshots
    )
    assert "[Earlier tool result saved at" in snapshot_text
    assert "[Fitted tool result saved at" in snapshot_text


def test_run_agent_compacts_history_then_calls_model(tmp_path: Path) -> None:
    initial: list[dict[str, object]] = [
        {"role": "system", "content": "原始系统"},
        {"role": "user", "content": "X" * 50_000},
        {"role": "assistant", "content": "旧回答"},
    ]
    responses = [
        _assistant_response(_assistant_final_message("这是历史摘要")),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    events: list[tuple[str, dict[str, object]]] = []
    snapshots: list[list[dict[str, object]]] = []
    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="继续",
        initial_messages=initial,
        max_context_tokens=20_000,
        event_callback=lambda t, p: events.append((t, p)),
        message_snapshot_callback=lambda msgs: snapshots.append(
            json.loads(json.dumps(msgs))
        ),
    )

    assert answer == "完成"
    assert len(completions.requests) == 2

    summary_request = completions.requests[0]
    assert "tools" not in summary_request
    assert "tool_choice" not in summary_request

    main_request = completions.requests[1]
    assert "tools" in main_request
    main_messages = main_request["messages"]
    assert [m["role"] for m in main_messages] == ["system", "user"]
    assert "原始系统" in main_messages[0]["content"]
    assert "只执行 Current user request" in main_messages[0]["content"]
    assert "[Compacted]" in main_messages[1]["content"]
    assert "Current user request:\n继续" in main_messages[1]["content"]
    assert "这是历史摘要" in main_messages[1]["content"]

    stages = [e[1]["stage"] for e in events if e[0] == "context_compacted"]
    assert stages == ["compact_history"]
    assert any(e[0] == "context_summary" for e in events)

    snapshot_text = "\n".join(
        json.dumps(s, ensure_ascii=False) for s in snapshots
    )
    assert "[Compacted]" in snapshot_text


def test_run_agent_summary_still_over_budget_does_not_call_model(
    tmp_path: Path,
) -> None:
    initial: list[dict[str, object]] = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "X" * 50_000},
    ]
    responses = [
        _assistant_response(_assistant_final_message("Y" * 60_000)),
    ]
    client, completions = _fake_client(responses)

    events: list[tuple[str, dict[str, object]]] = []
    with pytest.raises(RuntimeError):
        run_agent(
            client=client,
            model="fake-model",
            workspace=tmp_path,
            user_query="继续",
            initial_messages=initial,
            max_context_tokens=20_000,
            event_callback=lambda t, p: events.append((t, p)),
        )

    assert len(completions.requests) == 1
    assert "tools" not in completions.requests[0]

    stages = [e[1]["stage"] for e in events if e[0] == "context_compacted"]
    assert stages == ["compact_history"]
    assert any(e[0] == "context_summary" for e in events)


def test_run_agent_summary_failure_propagates(tmp_path: Path) -> None:
    initial: list[dict[str, object]] = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "X" * 50_000},
    ]

    class Boom(Exception):
        pass

    def fail_create(**kwargs: object) -> None:
        raise Boom()

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fail_create))
    )

    with pytest.raises(Boom):
        run_agent(
            client=client,
            model="fake-model",
            workspace=tmp_path,
            user_query="继续",
            initial_messages=initial,
            max_context_tokens=20_000,
        )


def _long_initial_history() -> list[dict[str, object]]:
    messages: list[dict[str, object]] = [{"role": "system", "content": "s"}]
    for i in range(4):
        role = "user" if i % 2 == 0 else "assistant"
        messages.append({"role": role, "content": f"old-{i}-" + "x" * 8000})
    for i in range(5):
        role = "user" if i % 2 == 0 else "assistant"
        messages.append({"role": role, "content": f"recent-{i}"})
    return messages


def test_run_agent_retries_once_on_context_error(tmp_path: Path) -> None:
    actions = [
        RuntimeError("context_length_exceeded: prompt too long"),
        _assistant_response(_assistant_final_message("这是摘要")),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _scripted_client(actions)

    events: list[tuple[str, dict[str, object]]] = []
    snapshots: list[list[dict[str, object]]] = []
    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="继续",
        initial_messages=_long_initial_history(),
        event_callback=lambda t, p: events.append((t, p)),
        message_snapshot_callback=lambda msgs: snapshots.append(
            json.loads(json.dumps(msgs))
        ),
    )

    assert answer == "完成"
    assert len(completions.requests) == 3

    assert "tools" in completions.requests[0]
    assert "tools" not in completions.requests[1]
    assert "tools" in completions.requests[2]

    compacted = [e for e in events if e[0] == "context_compacted"]
    assert len(compacted) == 1
    payload = compacted[0][1]
    assert payload["stage"] == "reactive_compact"
    assert payload["retry"] == 1
    assert str(payload["transcript"]).startswith(".transcripts/transcript_")
    assert payload["before_tokens"] > payload["after_tokens"]
    assert any(e[0] == "context_summary" for e in events)

    snapshot_text = "\n".join(
        json.dumps(s, ensure_ascii=False) for s in snapshots
    )
    assert "[Compacted]" in snapshot_text


def test_run_agent_second_context_error_propagates(tmp_path: Path) -> None:
    actions = [
        RuntimeError("context_length_exceeded: prompt too long"),
        _assistant_response(_assistant_final_message("这是摘要")),
        RuntimeError("context_length_exceeded: prompt too long again"),
    ]
    client, completions = _scripted_client(actions)

    events: list[tuple[str, dict[str, object]]] = []
    with pytest.raises(RuntimeError):
        run_agent(
            client=client,
            model="fake-model",
            workspace=tmp_path,
            user_query="继续",
            initial_messages=_long_initial_history(),
            event_callback=lambda t, p: events.append((t, p)),
        )

    assert len(completions.requests) == 3
    summary_requests = [r for r in completions.requests if "tools" not in r]
    assert len(summary_requests) == 1

    compacted = [e for e in events if e[0] == "context_compacted"]
    assert len(compacted) == 1
    assert compacted[0][1]["stage"] == "reactive_compact"


def test_run_agent_plain_error_propagates_without_compaction(tmp_path: Path) -> None:
    actions = [ValueError("some unrelated error")]
    client, completions = _scripted_client(actions)

    events: list[tuple[str, dict[str, object]]] = []
    with pytest.raises(ValueError):
        run_agent(
            client=client,
            model="fake-model",
            workspace=tmp_path,
            user_query="继续",
            initial_messages=_long_initial_history(),
            event_callback=lambda t, p: events.append((t, p)),
        )

    assert len(completions.requests) == 1
    assert not any(e[0] == "context_compacted" for e in events)
    assert not any(e[0] == "context_summary" for e in events)


def test_run_agent_model_request_includes_compact_schema(tmp_path: Path) -> None:
    responses = [
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="hi",
    )

    tool_names = [
        tool["function"]["name"]
        for tool in completions.requests[0]["tools"]
    ]
    assert "compact" in tool_names
    assert "list_files" in tool_names


def test_run_agent_compact_does_not_execute_or_approve(
    tmp_path: Path,
) -> None:
    write_call = _tool_call(
        name="write_file",
        arguments='{"path": "x.txt", "content": "hello"}',
        id="call_001",
    )
    compact_call = _compact_tool_call(id="call_compact")
    responses = [
        _assistant_response(_assistant_tool_message([write_call, compact_call])),
        _assistant_response(_assistant_final_message("这是摘要")),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, _ = _fake_client(responses)

    approvals: list[str] = []
    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="写文件",
        approval_callback=lambda name, arguments, reason: (
            approvals.append(name),
            True,
        )[1],
    )

    assert answer == "完成"
    assert (tmp_path / "x.txt").read_text(encoding="utf-8") == "hello"
    assert approvals == ["write_file"]


def test_run_agent_compacts_after_full_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "hello.txt").write_text("hi")
    tool_call = _tool_call(name="list_files", id="call_001")
    compact_call = _compact_tool_call(id="call_compact")
    responses = [
        _assistant_response(_assistant_tool_message([tool_call, compact_call])),
        _assistant_response(_assistant_final_message("这是摘要")),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    executed: list[str] = []
    monkeypatch.setattr(
        "vibe_harness.agent.execute_tool",
        lambda **kwargs: executed.append(kwargs["tool_name"]) or "real-result",
    )

    events: list[tuple[str, dict[str, object]]] = []
    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="看看",
        event_callback=lambda t, p: events.append((t, p)),
    )

    assert answer == "完成"
    assert executed == ["list_files"]
    assert len(completions.requests) == 3

    assert "tools" not in completions.requests[1]

    summary_user = completions.requests[1]["messages"][-1]["content"]
    assert "real-result" in summary_user
    assert "Compaction requested after this tool batch." in summary_user

    next_messages = completions.requests[2]["messages"]
    assert "[Compacted]" in next_messages[1]["content"]

    compacted = [e for e in events if e[0] == "context_compacted"]
    assert len(compacted) == 1
    payload = compacted[0][1]
    assert payload["stage"] == "active_compact"
    assert str(payload["transcript"]).startswith(".transcripts/transcript_")
    assert payload["before_tokens"] > payload["after_tokens"]
    assert any(e[0] == "context_summary" for e in events)


def test_run_agent_model_request_includes_todo_write_schema(tmp_path: Path) -> None:
    responses = [
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="hi",
    )

    tool_names = [
        tool["function"]["name"]
        for tool in completions.requests[0]["tools"]
    ]
    assert "todo_write" in tool_names
    assert "compact" in tool_names
    assert "list_files" in tool_names

    todo_schema = next(
        tool for tool in completions.requests[0]["tools"]
        if tool["function"]["name"] == "todo_write"
    )
    todos_schema = todo_schema["function"]["parameters"]["properties"]["todos"]
    assert todos_schema["maxItems"] == 20
    content_schema = todos_schema["items"]["properties"]["content"]
    assert content_schema["minLength"] == 1
    assert content_schema["maxLength"] == MAX_TODO_CONTENT_CHARS


def test_run_agent_todo_write_valid_call(tmp_path: Path) -> None:
    arguments = json.dumps(
        {
            "todos": [
                {"content": "  step one  ", "status": "pending"},
                {"content": "step two", "status": "in_progress"},
            ]
        }
    )
    todo_call = _todo_write_tool_call(arguments, id="call_todo")
    responses = [
        _assistant_response(_assistant_tool_message([todo_call])),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="计划任务",
    )

    assert answer == "完成"
    tool_message = completions.requests[1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call_todo"
    assert "[ ] step one" in tool_message["content"]
    assert "[>] step two" in tool_message["content"]
    assert "(0/2 completed)" in tool_message["content"]


def test_run_agent_todo_write_does_not_execute_or_approve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = json.dumps({"todos": [{"content": "a", "status": "pending"}]})
    todo_call = _todo_write_tool_call(arguments, id="call_todo")
    responses = [
        _assistant_response(_assistant_tool_message([todo_call])),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, _ = _fake_client(responses)

    executed: list[str] = []
    monkeypatch.setattr(
        "vibe_harness.agent.execute_tool",
        lambda **kwargs: executed.append(kwargs["tool_name"]) or "x",
    )

    approvals: list[str] = []
    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="计划",
        approval_callback=lambda name, arguments, reason: (
            approvals.append(name),
            True,
        )[1],
    )

    assert answer == "完成"
    assert executed == []
    assert approvals == []


def test_run_agent_todo_write_invalid_json_continues(tmp_path: Path) -> None:
    todo_call = _todo_write_tool_call("not valid json", id="call_todo")
    responses = [
        _assistant_response(_assistant_tool_message([todo_call])),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="计划",
    )

    assert answer == "完成"
    tool_message = completions.requests[1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call_todo"
    assert tool_message["content"].startswith("Error:")


def test_run_agent_todo_write_invalid_todo_continues(tmp_path: Path) -> None:
    arguments = json.dumps(
        {"todos": [{"content": "a", "status": "invalid-status"}]}
    )
    todo_call = _todo_write_tool_call(arguments, id="call_todo")
    responses = [
        _assistant_response(_assistant_tool_message([todo_call])),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="计划",
    )

    assert answer == "完成"
    tool_message = completions.requests[1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call_todo"
    assert tool_message["content"].startswith("Error:")


def test_run_agent_todo_write_overlong_content_continues(tmp_path: Path) -> None:
    arguments = json.dumps(
        {
            "todos": [
                {"content": "valid", "status": "pending"},
                {
                    "content": "x" * (MAX_TODO_CONTENT_CHARS + 1),
                    "status": "pending",
                },
            ]
        }
    )
    todo_call = _todo_write_tool_call(arguments, id="call_todo")
    responses = [
        _assistant_response(_assistant_tool_message([todo_call])),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="计划",
    )

    assert answer == "完成"
    tool_message = completions.requests[1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call_todo"
    assert tool_message["content"].startswith("Error:")


def test_run_agent_todo_reminder_after_three_rounds(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hi")
    responses = [
        _assistant_response(_assistant_tool_message([_tool_call(id="c1")])),
        _assistant_response(_assistant_tool_message([_tool_call(id="c2")])),
        _assistant_response(_assistant_tool_message([_tool_call(id="c3")])),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="看看",
    )

    assert answer == "完成"
    reminder = "<reminder>Update your todos.</reminder>"

    assert reminder not in completions.requests[1]["messages"][-1]["content"]
    assert reminder not in completions.requests[2]["messages"][-1]["content"]
    assert reminder in completions.requests[3]["messages"][-1]["content"]
    assert completions.requests[3]["messages"][-1]["content"].endswith(reminder)

    for request in completions.requests[1:]:
        assistant = request["messages"][-2]
        tool = request["messages"][-1]
        assert assistant["role"] == "assistant"
        assert tool["role"] == "tool"
        assert assistant["tool_calls"][0]["id"] == tool["tool_call_id"]


def test_run_agent_todo_reminder_counts_batches_not_tools(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")

    def _batch(prefix: str) -> list[object]:
        return [
            _tool_call(name="read_file", arguments='{"path": "a.txt"}', id=f"{prefix}a"),
            _tool_call(name="read_file", arguments='{"path": "b.txt"}', id=f"{prefix}b"),
        ]

    responses = [
        _assistant_response(_assistant_tool_message(_batch("r1"))),
        _assistant_response(_assistant_tool_message(_batch("r2"))),
        _assistant_response(_assistant_tool_message(_batch("r3"))),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="读文件",
    )

    assert answer == "完成"
    reminder = "<reminder>Update your todos.</reminder>"

    round1 = completions.requests[1]["messages"]
    assert reminder not in round1[-1]["content"]
    assert reminder not in round1[-2]["content"]

    round2 = completions.requests[2]["messages"]
    assert reminder not in round2[-1]["content"]
    assert reminder not in round2[-2]["content"]

    round3 = completions.requests[3]["messages"]
    assert reminder in round3[-1]["content"]
    assert reminder not in round3[-2]["content"]


def test_run_agent_todo_reminder_reset_on_todo_write(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hi")
    todo_arguments = json.dumps(
        {"todos": [{"content": "do it", "status": "pending"}]}
    )
    responses = [
        _assistant_response(_assistant_tool_message([_tool_call(id="c1")])),
        _assistant_response(
            _assistant_tool_message([_todo_write_tool_call(todo_arguments, id="t1")])
        ),
        _assistant_response(_assistant_tool_message([_tool_call(id="c2")])),
        _assistant_response(_assistant_tool_message([_tool_call(id="c3")])),
        _assistant_response(_assistant_tool_message([_tool_call(id="c4")])),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="看看",
    )

    assert answer == "完成"
    reminder = "<reminder>Update your todos.</reminder>"

    assert reminder not in completions.requests[1]["messages"][-1]["content"]
    assert reminder not in completions.requests[2]["messages"][-1]["content"]
    assert reminder not in completions.requests[3]["messages"][-1]["content"]
    assert reminder not in completions.requests[4]["messages"][-1]["content"]
    assert reminder in completions.requests[5]["messages"][-1]["content"]


def test_run_agent_todo_reminder_preserves_tool_call_ids(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")

    def _batch(prefix: str) -> list[object]:
        return [
            _tool_call(name="read_file", arguments='{"path": "a.txt"}', id=f"{prefix}a"),
            _tool_call(name="read_file", arguments='{"path": "b.txt"}', id=f"{prefix}b"),
        ]

    responses = [
        _assistant_response(_assistant_tool_message(_batch("r1"))),
        _assistant_response(_assistant_tool_message(_batch("r2"))),
        _assistant_response(_assistant_tool_message(_batch("r3"))),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="读文件",
    )

    assert answer == "完成"

    for request in completions.requests[1:]:
        assistant = request["messages"][-3]
        tool_a = request["messages"][-2]
        tool_b = request["messages"][-1]
        assert assistant["role"] == "assistant"
        assert [tc["id"] for tc in assistant["tool_calls"]] == [
            tool_a["tool_call_id"],
            tool_b["tool_call_id"],
        ]
        assert tool_a["role"] == "tool"
        assert tool_b["role"] == "tool"


def test_run_agent_new_session_includes_todo_instruction(tmp_path: Path) -> None:
    responses = [
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="hi",
    )

    system_content = completions.requests[0]["messages"][0]["content"]
    assert TODO_SYSTEM_INSTRUCTION in system_content


def test_run_agent_restored_session_appends_todo_instruction(
    tmp_path: Path,
) -> None:
    initial_messages = [
        {"role": "system", "content": "旧系统提示"},
        {"role": "user", "content": "旧任务"},
    ]
    responses = [
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="继续",
        initial_messages=initial_messages,
    )

    system_content = completions.requests[0]["messages"][0]["content"]
    assert system_content.startswith("旧系统提示")
    assert TODO_SYSTEM_INSTRUCTION in system_content


def test_run_agent_does_not_duplicate_todo_instruction(tmp_path: Path) -> None:
    initial_messages = [
        {"role": "system", "content": f"旧系统提示\n\n{TODO_SYSTEM_INSTRUCTION}"},
        {"role": "user", "content": "旧任务"},
    ]
    responses = [
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="继续",
        initial_messages=initial_messages,
    )

    system_content = completions.requests[0]["messages"][0]["content"]
    assert system_content.count(TODO_SYSTEM_INSTRUCTION) == 1


def test_run_agent_todo_instruction_does_not_mutate_input(tmp_path: Path) -> None:
    initial_messages = [
        {"role": "system", "content": "旧系统提示"},
        {"role": "user", "content": "旧任务"},
    ]
    original = json.loads(json.dumps(initial_messages))
    responses = [
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, _ = _fake_client(responses)

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="继续",
        initial_messages=initial_messages,
    )

    assert initial_messages == original


def test_run_agent_new_session_includes_untrusted_instruction(
    tmp_path: Path,
) -> None:
    responses = [
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="hi",
    )

    messages = completions.requests[0]["messages"]
    assert [m["role"] for m in messages].count("system") == 1

    system_content = messages[0]["content"]
    assert UNTRUSTED_TOOL_CONTENT_INSTRUCTION in system_content
    assert TODO_SYSTEM_INSTRUCTION in system_content
    assert system_content.count(UNTRUSTED_TOOL_CONTENT_INSTRUCTION) == 1
    assert system_content.count(TODO_SYSTEM_INSTRUCTION) == 1


def test_run_agent_restored_session_appends_untrusted_instruction(
    tmp_path: Path,
) -> None:
    initial_messages = [
        {"role": "system", "content": "旧系统提示"},
        {"role": "user", "content": "旧任务"},
    ]
    responses = [
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="继续",
        initial_messages=initial_messages,
    )

    system_content = completions.requests[0]["messages"][0]["content"]
    assert system_content.startswith("旧系统提示")
    assert UNTRUSTED_TOOL_CONTENT_INSTRUCTION in system_content
    assert TODO_SYSTEM_INSTRUCTION in system_content


def test_run_agent_does_not_duplicate_untrusted_instruction(
    tmp_path: Path,
) -> None:
    initial_messages = [
        {
            "role": "system",
            "content": f"旧系统提示\n\n{UNTRUSTED_TOOL_CONTENT_INSTRUCTION}",
        },
        {"role": "user", "content": "旧任务"},
    ]
    responses = [
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="继续",
        initial_messages=initial_messages,
    )

    system_content = completions.requests[0]["messages"][0]["content"]
    assert system_content.count(UNTRUSTED_TOOL_CONTENT_INSTRUCTION) == 1


def test_run_agent_tool_result_not_wrapped(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hi")
    tool_call = _tool_call(id="call_001")
    responses = [
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="看看",
    )

    tool_message = completions.requests[1]["messages"][-1]
    assert set(tool_message.keys()) == {"role", "tool_call_id", "content"}
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call_001"
    assert tool_message["content"] == "[FILE] hello.txt"
    assert UNTRUSTED_TOOL_CONTENT_INSTRUCTION not in tool_message["content"]
    assert TODO_SYSTEM_INSTRUCTION not in tool_message["content"]


def test_run_agent_compaction_preserves_untrusted_instruction(
    tmp_path: Path,
) -> None:
    initial: list[dict[str, object]] = [
        {"role": "system", "content": "原始系统"},
        {"role": "user", "content": "X" * 50_000},
        {"role": "assistant", "content": "旧回答"},
    ]
    responses = [
        _assistant_response(_assistant_final_message("这是历史摘要")),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="继续",
        initial_messages=initial,
        max_context_tokens=20_000,
    )

    main_request = completions.requests[1]
    system_content = main_request["messages"][0]["content"]
    assert UNTRUSTED_TOOL_CONTENT_INSTRUCTION in system_content
    assert TODO_SYSTEM_INSTRUCTION in system_content


@pytest.mark.parametrize("bad", [0, -1, False, True, 3.5, "lots"])
def test_run_agent_rejects_invalid_max_steps(tmp_path: Path, bad: object) -> None:
    responses = [
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    with pytest.raises(RuntimeError):
        run_agent(
            client=client,
            model="fake-model",
            workspace=tmp_path,
            user_query="hi",
            max_steps=bad,
        )

    assert len(completions.requests) == 0


def test_run_agent_in_flight_marker_not_in_model_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_call = _tool_call(name="list_files", id="call_001")
    responses = [
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)

    monkeypatch.setattr(
        "vibe_harness.agent.execute_tool",
        lambda **kwargs: "ok",
    )

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="看看",
    )

    second_request = completions.requests[1]
    for message in second_request["messages"]:
        assert IN_FLIGHT_MARKER_KEY not in message

    tool_message = second_request["messages"][-1]
    assert tool_message["tool_call_id"] == "call_001"
    assert tool_message["content"] == "ok"


def test_run_agent_in_flight_marker_snapshot_before_execute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_call = _tool_call(name="list_files", id="call_001")
    responses = [
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, _ = _fake_client(responses)

    events: list[object] = []

    def fake_execute_tool(**kwargs: object) -> str:
        events.append("execute")
        return "ok"

    def snapshot_callback(messages: list[dict[str, object]]) -> None:
        has_marker = any(
            IN_FLIGHT_MARKER_KEY in message for message in messages
        )
        events.append(("snapshot", has_marker))

    monkeypatch.setattr("vibe_harness.agent.execute_tool", fake_execute_tool)

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="看看",
        message_snapshot_callback=snapshot_callback,
    )

    execute_index = events.index("execute")
    assert execute_index > 0
    assert events[execute_index - 1] == ("snapshot", True)


def test_run_agent_in_flight_marker_save_failure_blocks_execute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_call = _tool_call(name="list_files", id="call_001")
    responses = [
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, _ = _fake_client(responses)

    executed: list[object] = []

    def fake_execute_tool(**kwargs: object) -> str:
        executed.append(kwargs)
        return "ok"

    def failing_snapshot(messages: list[dict[str, object]]) -> None:
        if any(IN_FLIGHT_MARKER_KEY in message for message in messages):
            raise OSError("磁盘写入失败")

    monkeypatch.setattr("vibe_harness.agent.execute_tool", fake_execute_tool)

    with pytest.raises(OSError):
        run_agent(
            client=client,
            model="fake-model",
            workspace=tmp_path,
            user_query="看看",
            message_snapshot_callback=failing_snapshot,
        )

    assert executed == []


def test_run_agent_final_session_has_no_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_call = _tool_call(name="list_files", id="call_001")
    responses = [
        _assistant_response(_assistant_tool_message([tool_call])),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, _ = _fake_client(responses)

    monkeypatch.setattr(
        "vibe_harness.agent.execute_tool",
        lambda **kwargs: "ok",
    )

    snapshots: list[list[dict[str, object]]] = []

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="看看",
        message_snapshot_callback=lambda msgs: snapshots.append(
            json.loads(json.dumps(msgs))
        ),
    )

    final = snapshots[-1]
    assert not any(IN_FLIGHT_MARKER_KEY in message for message in final)
    assert final[-1]["role"] == "assistant"
    assert final[-1]["content"] == "完成"
    tool_result = final[-2]
    assert tool_result["role"] == "tool"
    assert tool_result["tool_call_id"] == "call_001"
    assert tool_result["content"] == "ok"


def test_run_agent_marks_internal_model_tools_in_flight(
    tmp_path: Path,
) -> None:
    todo_arguments = json.dumps(
        {"todos": [{"content": "检查任务", "status": "in_progress"}]}
    )
    todo_call = _todo_write_tool_call(todo_arguments, id="call_todo")
    compact_call = _compact_tool_call(id="call_compact")
    responses = [
        _assistant_response(
            _assistant_tool_message([todo_call, compact_call])
        ),
        _assistant_response(_assistant_final_message("压缩摘要")),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, _ = _fake_client(responses)
    snapshots: list[list[dict[str, object]]] = []

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="规划并压缩",
        message_snapshot_callback=lambda messages: snapshots.append(
            json.loads(json.dumps(messages))
        ),
    )

    marked_ids = [
        snapshot[-1]["tool_call_id"]
        for snapshot in snapshots
        if snapshot[-1].get(IN_FLIGHT_MARKER_KEY) is True
    ]
    assert marked_ids == ["call_todo", "call_compact"]


# --- Hooks ---


def test_user_prompt_submit_runs_before_model_and_cannot_rewrite_query(
    tmp_path: Path,
) -> None:
    responses = [_assistant_response(_assistant_final_message("完成"))]
    client, completions = _fake_client(responses)
    hooks = HookRegistry()
    observations: list[tuple[str, int]] = []

    def observe(query: str) -> str:
        observations.append((query, len(completions.requests)))
        return "ignored replacement"

    hooks.register_hook(HookEvent.USER_PROMPT_SUBMIT, observe)

    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="original query",
        hook_registry=hooks,
    )

    assert answer == "完成"
    assert observations == [("original query", 0)]
    assert completions.requests[0]["messages"][1] == {
        "role": "user",
        "content": "original query",
    }


def test_run_agent_triggers_pre_and_post_tool_hooks(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hi")
    call = _tool_call(name="list_files", id="call_hook")
    responses = [
        _assistant_response(_assistant_tool_message([call])),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)
    hooks = HookRegistry()
    lifecycle: list[object] = []

    hooks.register_hook(
        HookEvent.PRE_TOOL_USE,
        lambda name, args: lifecycle.append(("pre", name, args)),
    )
    hooks.register_hook(
        HookEvent.POST_TOOL_USE,
        lambda name, args, result: lifecycle.append(
            ("post", name, args, result)
        ),
    )

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="看看",
        hook_registry=hooks,
    )

    assert lifecycle[0] == ("pre", "list_files", {"path": "."})
    assert lifecycle[1][:3] == ("post", "list_files", {"path": "."})
    assert "hello.txt" in lifecycle[1][3]
    tool_result = completions.requests[1]["messages"][-1]
    assert tool_result["role"] == "tool"
    assert tool_result["tool_call_id"] == "call_hook"


def test_run_agent_pre_tool_hook_veto_closes_tool_call(
    tmp_path: Path,
) -> None:
    call = _tool_call(
        name="write_file",
        arguments='{"path": "blocked.txt", "content": "hi"}',
        id="call_blocked",
    )
    responses = [
        _assistant_response(_assistant_tool_message([call])),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)
    hooks = HookRegistry()
    approvals: list[str] = []
    hooks.register_hook(
        HookEvent.PRE_TOOL_USE,
        lambda name, args: "read-only project policy",
    )

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="写文件",
        approval_callback=lambda name, args, reason: approvals.append(name) or True,
        hook_registry=hooks,
    )

    assert approvals == []
    assert not (tmp_path / "blocked.txt").exists()
    tool_result = completions.requests[1]["messages"][-1]
    assert tool_result["role"] == "tool"
    assert tool_result["tool_call_id"] == "call_blocked"
    assert "blocked by PreToolUse hook" in tool_result["content"]


def test_stop_hook_can_request_bounded_continuation(tmp_path: Path) -> None:
    responses = [
        _assistant_response(_assistant_final_message("还没完成")),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, completions = _fake_client(responses)
    hooks = HookRegistry()
    stop_calls: list[list[dict[str, object]]] = []

    def continue_once(messages: list[dict[str, object]]) -> str | None:
        stop_calls.append(messages)
        messages.clear()
        if len(stop_calls) == 1:
            return "继续完成任务"
        return None

    hooks.register_hook(HookEvent.STOP, continue_once)

    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="完成任务",
        hook_registry=hooks,
    )

    assert answer == "完成"
    assert len(stop_calls) == 2
    assert [message["role"] for message in completions.requests[1]["messages"]] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert completions.requests[1]["messages"][-1]["content"] == "继续完成任务"


def test_stop_hook_cannot_exceed_step_budget(tmp_path: Path) -> None:
    responses = [
        _assistant_response(_assistant_final_message("还没完成")),
        _assistant_response(_assistant_final_message("阶段总结")),
    ]
    client, completions = _fake_client(responses)
    hooks = HookRegistry()
    hooks.register_hook(HookEvent.STOP, lambda messages: "继续")

    answer = run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="完成任务",
        max_steps=1,
        hook_registry=hooks,
    )

    assert answer == "阶段总结"
    assert len(completions.requests) == 2
    assert "tools" not in completions.requests[1]
    assert completions.requests[1]["messages"][-2]["content"] == "继续"
    assert (
        completions.requests[1]["messages"][-1]["content"]
        == STEP_BUDGET_FINALIZATION_PROMPT
    )


def test_agent_control_tools_do_not_trigger_tool_hooks(tmp_path: Path) -> None:
    todo_call = _todo_write_tool_call(
        json.dumps(
            {"todos": [{"content": "检查任务", "status": "in_progress"}]}
        ),
        id="call_todo_hook",
    )
    responses = [
        _assistant_response(_assistant_tool_message([todo_call])),
        _assistant_response(_assistant_final_message("完成")),
    ]
    client, _ = _fake_client(responses)
    hooks = HookRegistry()
    lifecycle: list[str] = []
    hooks.register_hook(
        HookEvent.PRE_TOOL_USE,
        lambda name, args: lifecycle.append("pre"),
    )
    hooks.register_hook(
        HookEvent.POST_TOOL_USE,
        lambda name, args, result: lifecycle.append("post"),
    )

    run_agent(
        client=client,
        model="fake-model",
        workspace=tmp_path,
        user_query="规划",
        hook_registry=hooks,
    )

    assert lifecycle == []


def test_post_tool_hook_failure_keeps_in_flight_snapshot(
    tmp_path: Path,
) -> None:
    call = _tool_call(
        name="write_file",
        arguments='{"path": "created.txt", "content": "hi"}',
        id="call_post_failure",
    )
    responses = [_assistant_response(_assistant_tool_message([call]))]
    client, _ = _fake_client(responses)
    hooks = HookRegistry()
    snapshots: list[list[dict[str, object]]] = []

    def fail_after_tool(name: str, arguments: dict[str, object], result: str) -> None:
        raise RuntimeError("post hook failed")

    hooks.register_hook(HookEvent.POST_TOOL_USE, fail_after_tool)

    with pytest.raises(RuntimeError, match="post hook failed"):
        run_agent(
            client=client,
            model="fake-model",
            workspace=tmp_path,
            user_query="写文件",
            approval_callback=lambda name, args, reason: True,
            hook_registry=hooks,
            message_snapshot_callback=lambda messages: snapshots.append(
                json.loads(json.dumps(messages))
            ),
        )

    assert (tmp_path / "created.txt").read_text() == "hi"
    assert snapshots[-1][-1] == {
        "role": "tool",
        "tool_call_id": "call_post_failure",
        "content": "",
        IN_FLIGHT_MARKER_KEY: True,
    }
