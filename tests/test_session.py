import json
import stat
from pathlib import Path

import pytest

from vibe_harness.session import (
    IN_FLIGHT_MARKER_KEY,
    MAX_SESSION_FILE_BYTES,
    SESSION_VERSION,
    SessionData,
    analyze_tool_batch_tail,
    build_tool_batch_recovery_messages,
    load_session,
    plan_tool_batch_recovery,
    save_session,
)


def _messages() -> list[dict[str, object]]:
    return [
        {"role": "system", "content": "你是一个本地编程助手。"},
        {"role": "user", "content": "检查这个项目并说明结构"},
    ]


def _resumable_messages() -> list[dict[str, object]]:
    return [
        {"role": "system", "content": "你是一个本地编程助手。"},
        {"role": "user", "content": "检查项目"},
        {"role": "assistant", "content": "检查完成"},
    ]


def test_save_session_writes_expected_json(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    workspace = tmp_path / "proj"

    save_session(path, workspace, "deepseek", _messages())

    data = json.loads(path.read_text(encoding="utf-8"))

    assert data["version"] == SESSION_VERSION == 1
    assert data["workspace"] == str(workspace.resolve())
    assert data["model"] == "deepseek"
    assert data["messages"] == _messages()


def test_save_session_preserves_chinese_content(tmp_path: Path) -> None:
    path = tmp_path / "session.json"

    save_session(path, tmp_path, "deepseek", _messages())

    text = path.read_text(encoding="utf-8")
    assert "检查这个项目并说明结构" in text
    assert json.loads(text)["messages"] == _messages()


def test_save_session_file_permission_is_0600(tmp_path: Path) -> None:
    path = tmp_path / "session.json"

    save_session(path, tmp_path, "deepseek", [])

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_save_session_atomically_replaces_old_content(tmp_path: Path) -> None:
    path = tmp_path / "session.json"

    save_session(
        path,
        tmp_path,
        "model-1",
        [{"role": "user", "content": "旧内容"}],
    )
    save_session(
        path,
        tmp_path,
        "model-2",
        [{"role": "user", "content": "新内容"}],
    )

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["model"] == "model-2"
    assert data["messages"] == [{"role": "user", "content": "新内容"}]


def test_save_session_rejects_symlink_target(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text("{}")
    link = tmp_path / "link.json"
    link.symlink_to(real)

    with pytest.raises(ValueError):
        save_session(link, tmp_path, "deepseek", [])


def test_save_session_rejects_missing_parent(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "session.json"

    with pytest.raises(ValueError):
        save_session(path, tmp_path, "deepseek", [])


def test_save_session_rejects_file_parent(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("hi")

    with pytest.raises(ValueError):
        save_session(f / "session.json", tmp_path, "deepseek", [])


def test_save_session_does_not_include_secrets(tmp_path: Path) -> None:
    path = tmp_path / "session.json"

    save_session(path, tmp_path, "deepseek", [])

    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data.keys()) == {"version", "workspace", "model", "messages"}


def test_save_session_rejects_content_over_load_limit(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    messages = [
        {"role": "system", "content": "s"},
        {"role": "assistant", "content": "x" * MAX_SESSION_FILE_BYTES},
    ]

    with pytest.raises(ValueError):
        save_session(path, tmp_path, "deepseek", messages)

    assert not path.exists()


def test_load_session_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    workspace = tmp_path / "proj"
    save_session(path, workspace, "deepseek", _resumable_messages())

    data = load_session(path)

    assert isinstance(data, SessionData)
    assert data.workspace == workspace.resolve()
    assert data.model == "deepseek"
    assert data.messages == _resumable_messages()


def test_load_session_preserves_chinese_content(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    messages = [
        {"role": "system", "content": "你是一个本地编程助手。"},
        {"role": "user", "content": "检查这个项目并说明结构"},
        {"role": "assistant", "content": "好的，我来检查。"},
    ]
    save_session(path, tmp_path, "deepseek", messages)

    data = load_session(path)

    assert data.messages[1]["content"] == "检查这个项目并说明结构"
    assert data.messages[2]["content"] == "好的，我来检查。"


def test_load_session_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    path.write_text("{ not valid json ", encoding="utf-8")

    with pytest.raises(ValueError):
        load_session(path)


def test_load_session_rejects_non_dict_top_level(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(ValueError):
        load_session(path)


def test_load_session_rejects_wrong_version(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    data = {
        "version": 999,
        "workspace": str(tmp_path),
        "model": "deepseek",
        "messages": _resumable_messages(),
    }
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError):
        load_session(path)


def test_load_session_rejects_missing_workspace(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    data = {
        "version": SESSION_VERSION,
        "model": "deepseek",
        "messages": _resumable_messages(),
    }
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError):
        load_session(path)


def test_load_session_rejects_missing_model(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    data = {
        "version": SESSION_VERSION,
        "workspace": str(tmp_path),
        "messages": _resumable_messages(),
    }
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError):
        load_session(path)


def test_load_session_rejects_empty_messages(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    data = {
        "version": SESSION_VERSION,
        "workspace": str(tmp_path),
        "model": "deepseek",
        "messages": [],
    }
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError):
        load_session(path)


def test_load_session_rejects_first_message_not_system(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    data = {
        "version": SESSION_VERSION,
        "workspace": str(tmp_path),
        "model": "deepseek",
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "ok"},
        ],
    }
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError):
        load_session(path)


def test_load_session_rejects_unfinished_tool_call(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": '{"path": "x.txt", "content": "hi"}',
                    },
                }
            ],
        },
    ]
    save_session(path, tmp_path, "deepseek", messages)

    with pytest.raises(ValueError):
        load_session(path)


def test_load_session_rejects_tool_result_ending(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "读文件"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "x"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "1 | hi"},
    ]
    save_session(path, tmp_path, "deepseek", messages)

    with pytest.raises(ValueError):
        load_session(path)


def test_load_session_rejects_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    save_session(real, tmp_path, "deepseek", _resumable_messages())
    link = tmp_path / "link.json"
    link.symlink_to(real)

    with pytest.raises(ValueError):
        load_session(link)


def test_load_session_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        load_session(tmp_path / "nope.json")


def test_load_session_rejects_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        load_session(tmp_path)


def test_load_session_rejects_oversized_file(tmp_path: Path) -> None:
    path = tmp_path / "big.json"
    with path.open("wb") as f:
        f.truncate(MAX_SESSION_FILE_BYTES + 1)

    with pytest.raises(ValueError):
        load_session(path)


def test_load_session_returns_deep_copy(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    save_session(path, tmp_path, "deepseek", _resumable_messages())

    data = load_session(path)
    data.messages[0]["content"] = "被篡改"

    reloaded = load_session(path)
    assert reloaded.messages[0]["content"] == "你是一个本地编程助手。"


def _tool_call(call_id: str) -> dict[str, object]:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "write_file",
            "arguments": '{"path": "x.txt", "content": "hi"}',
        },
    }


def _assistant_with_tool_calls(call_ids: list[str]) -> dict[str, object]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [_tool_call(call_id) for call_id in call_ids],
    }


def _tool_result(call_id: str) -> dict[str, object]:
    return {"role": "tool", "tool_call_id": call_id, "content": "ok"}


def test_analyze_tool_batch_tail_normal_end() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "完成"},
    ]

    assert analyze_tool_batch_tail(messages) is None


def test_analyze_tool_batch_tail_no_results() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        _assistant_with_tool_calls(["A", "B", "C"]),
    ]

    state = analyze_tool_batch_tail(messages)

    assert state is not None
    assert state.tool_call_ids == ("A", "B", "C")
    assert state.completed_call_ids == ()
    assert state.pending_call_ids == ("A", "B", "C")


def test_analyze_tool_batch_tail_partial_results() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        _assistant_with_tool_calls(["A", "B", "C"]),
        _tool_result("A"),
        _tool_result("B"),
    ]

    state = analyze_tool_batch_tail(messages)

    assert state is not None
    assert state.tool_call_ids == ("A", "B", "C")
    assert state.completed_call_ids == ("A", "B")
    assert state.pending_call_ids == ("C",)


def test_analyze_tool_batch_tail_all_results() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        _assistant_with_tool_calls(["A", "B", "C"]),
        _tool_result("A"),
        _tool_result("B"),
        _tool_result("C"),
    ]

    state = analyze_tool_batch_tail(messages)

    assert state is not None
    assert state.tool_call_ids == ("A", "B", "C")
    assert state.completed_call_ids == ("A", "B", "C")
    assert state.pending_call_ids == ()


@pytest.mark.parametrize(
    "results",
    [
        [_tool_result("UNKNOWN")],
        [_tool_result("B"), _tool_result("A")],
        [_tool_result("A"), _tool_result("A")],
    ],
)
def test_analyze_tool_batch_tail_rejects_bad_results(
    results: list[dict[str, object]],
) -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        _assistant_with_tool_calls(["A", "B"]),
        *results,
    ]

    with pytest.raises(ValueError):
        analyze_tool_batch_tail(messages)


def test_analyze_tool_batch_tail_rejects_duplicate_tool_call_ids() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        _assistant_with_tool_calls(["A", "A"]),
    ]

    with pytest.raises(ValueError):
        analyze_tool_batch_tail(messages)


def test_analyze_tool_batch_tail_rejects_marker_after_all_results() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        _assistant_with_tool_calls(["A", "B"]),
        _tool_result("A"),
        _tool_result("B"),
        _tool_result_in_flight("B"),
    ]

    with pytest.raises(ValueError):
        analyze_tool_batch_tail(messages)


def _tool_result_in_flight(call_id: str) -> dict[str, object]:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": "",
        IN_FLIGHT_MARKER_KEY: True,
    }


def test_analyze_tool_batch_tail_in_flight() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        _assistant_with_tool_calls(["A", "B", "C"]),
        _tool_result("A"),
        _tool_result_in_flight("B"),
    ]

    state = analyze_tool_batch_tail(messages)

    assert state is not None
    assert state.tool_call_ids == ("A", "B", "C")
    assert state.completed_call_ids == ("A",)
    assert state.in_flight_call_id == "B"
    assert state.pending_call_ids == ("C",)


def test_analyze_tool_batch_tail_no_in_flight_keeps_current_behavior() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        _assistant_with_tool_calls(["A", "B", "C"]),
        _tool_result("A"),
        _tool_result("B"),
    ]

    state = analyze_tool_batch_tail(messages)

    assert state is not None
    assert state.completed_call_ids == ("A", "B")
    assert state.in_flight_call_id is None
    assert state.pending_call_ids == ("C",)


@pytest.mark.parametrize(
    "tail",
    [
        [_tool_result_in_flight("B")],
        [_tool_result("A"), _tool_result_in_flight("C")],
        [_tool_result("A"), _tool_result_in_flight("A")],
        [_tool_result_in_flight("A"), _tool_result("B")],
    ],
)
def test_analyze_tool_batch_tail_rejects_bad_in_flight(
    tail: list[dict[str, object]],
) -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        _assistant_with_tool_calls(["A", "B", "C"]),
        *tail,
    ]

    with pytest.raises(ValueError):
        analyze_tool_batch_tail(messages)


def test_load_session_default_rejects_unfinished_batch(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        _assistant_with_tool_calls(["A", "B"]),
    ]
    save_session(path, tmp_path, "deepseek", messages)

    with pytest.raises(ValueError):
        load_session(path)


def test_load_session_explicit_loads_zero_results(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        _assistant_with_tool_calls(["A", "B", "C"]),
    ]
    save_session(path, tmp_path, "deepseek", messages)

    data = load_session(path, allow_incomplete_tool_batch=True)

    assert data.tool_batch_state is not None
    assert data.tool_batch_state.completed_call_ids == ()
    assert data.tool_batch_state.pending_call_ids == ("A", "B", "C")
    assert data.tool_batch_state.in_flight_call_id is None


def test_load_session_explicit_loads_partial_results(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        _assistant_with_tool_calls(["A", "B", "C"]),
        _tool_result("A"),
        _tool_result("B"),
    ]
    save_session(path, tmp_path, "deepseek", messages)

    data = load_session(path, allow_incomplete_tool_batch=True)

    assert data.tool_batch_state is not None
    assert data.tool_batch_state.completed_call_ids == ("A", "B")
    assert data.tool_batch_state.pending_call_ids == ("C",)
    assert data.tool_batch_state.in_flight_call_id is None


def test_load_session_explicit_loads_all_results(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        _assistant_with_tool_calls(["A", "B"]),
        _tool_result("A"),
        _tool_result("B"),
    ]
    save_session(path, tmp_path, "deepseek", messages)

    data = load_session(path, allow_incomplete_tool_batch=True)

    assert data.tool_batch_state is not None
    assert data.tool_batch_state.completed_call_ids == ("A", "B")
    assert data.tool_batch_state.pending_call_ids == ()
    assert data.tool_batch_state.in_flight_call_id is None


def test_load_session_explicit_loads_in_flight(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        _assistant_with_tool_calls(["A", "B", "C"]),
        _tool_result("A"),
        _tool_result_in_flight("B"),
    ]
    save_session(path, tmp_path, "deepseek", messages)

    data = load_session(path, allow_incomplete_tool_batch=True)

    assert data.tool_batch_state is not None
    assert data.tool_batch_state.completed_call_ids == ("A",)
    assert data.tool_batch_state.in_flight_call_id == "B"
    assert data.tool_batch_state.pending_call_ids == ("C",)


@pytest.mark.parametrize(
    "tail",
    [
        [_tool_result("UNKNOWN")],
        [_tool_result("A"), _tool_result("A")],
        [_tool_result("B"), _tool_result("A")],
    ],
)
def test_load_session_explicit_rejects_illegal_batch(
    tmp_path: Path,
    tail: list[dict[str, object]],
) -> None:
    path = tmp_path / "session.json"
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        _assistant_with_tool_calls(["A", "B"]),
        *tail,
    ]
    save_session(path, tmp_path, "deepseek", messages)

    with pytest.raises(ValueError):
        load_session(path, allow_incomplete_tool_batch=True)


def test_load_session_normal_session_tool_batch_state_none(
    tmp_path: Path,
) -> None:
    path = tmp_path / "session.json"
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        {"role": "assistant", "content": "完成"},
    ]
    save_session(path, tmp_path, "deepseek", messages)

    data = load_session(path)

    assert data.tool_batch_state is None


def test_plan_tool_batch_recovery_normal_session() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "完成"},
    ]

    assert plan_tool_batch_recovery(messages) is None


def test_plan_tool_batch_recovery_zero_results() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        _assistant_with_tool_calls(["A", "B", "C"]),
    ]

    plan = plan_tool_batch_recovery(messages)

    assert plan is not None
    assert plan.completed_call_ids == ()
    assert [tool_call.id for tool_call in plan.pending_tool_calls] == [
        "A",
        "B",
        "C",
    ]


def test_plan_tool_batch_recovery_partial_results() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        _assistant_with_tool_calls(["A", "B", "C"]),
        _tool_result("A"),
        _tool_result("B"),
    ]

    plan = plan_tool_batch_recovery(messages)

    assert plan is not None
    assert plan.completed_call_ids == ("A", "B")
    assert [tool_call.id for tool_call in plan.pending_tool_calls] == ["C"]


def test_plan_tool_batch_recovery_all_results() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        _assistant_with_tool_calls(["A", "B"]),
        _tool_result("A"),
        _tool_result("B"),
    ]

    plan = plan_tool_batch_recovery(messages)

    assert plan is not None
    assert plan.completed_call_ids == ("A", "B")
    assert plan.pending_tool_calls == ()


def test_plan_tool_batch_recovery_in_flight_blocks() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        _assistant_with_tool_calls(["A", "B", "C"]),
        _tool_result("A"),
        _tool_result_in_flight("B"),
    ]

    with pytest.raises(ValueError) as exc_info:
        plan_tool_batch_recovery(messages)

    assert "B" in str(exc_info.value)


def _named_tool_call(call_id: str, name: str, arguments: str) -> dict[str, object]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def test_plan_tool_batch_recovery_preserves_order() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                _named_tool_call("c1", "alpha", '{"n": 1}'),
                _named_tool_call("c2", "beta", '{"n": 2}'),
                _named_tool_call("c3", "gamma", '{"n": 3}'),
                _named_tool_call("c4", "delta", '{"n": 4}'),
            ],
        },
        _tool_result("c1"),
        _tool_result("c2"),
    ]

    plan = plan_tool_batch_recovery(messages)

    assert plan is not None
    assert plan.completed_call_ids == ("c1", "c2")
    pending = plan.pending_tool_calls
    assert [(p.id, p.name, p.arguments) for p in pending] == [
        ("c3", "gamma", '{"n": 3}'),
        ("c4", "delta", '{"n": 4}'),
    ]


def test_plan_tool_batch_recovery_does_not_mutate_input() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        _assistant_with_tool_calls(["A", "B"]),
        _tool_result("A"),
    ]
    original = json.loads(json.dumps(messages))

    plan_tool_batch_recovery(messages)

    assert messages == original


@pytest.mark.parametrize(
    "tool_calls",
    [
        "not-a-list",
        ["not-an-object"],
        [{"type": "function", "function": {"name": "f", "arguments": "{}"}}],
        [{"id": "A", "type": "function", "function": {"arguments": "{}"}}],
        [{"id": "A", "type": "function", "function": {"name": "f"}}],
    ],
)
def test_plan_tool_batch_recovery_rejects_bad_structure(
    tool_calls: list[dict[str, object]],
) -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        {"role": "assistant", "content": None, "tool_calls": tool_calls},
    ]

    with pytest.raises(ValueError):
        plan_tool_batch_recovery(messages)


def test_build_recovery_messages_zero_results() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        _assistant_with_tool_calls(["A", "B", "C"]),
    ]

    result = build_tool_batch_recovery_messages(messages)

    appended = result[3:]
    assert [m["role"] for m in appended] == ["tool", "tool", "tool"]
    assert [m["tool_call_id"] for m in appended] == ["A", "B", "C"]
    assert all("尚未执行" in str(m["content"]) for m in appended)


def test_build_recovery_messages_partial_results() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        _assistant_with_tool_calls(["A", "B", "C"]),
        _tool_result("A"),
        _tool_result("B"),
    ]

    result = build_tool_batch_recovery_messages(messages)

    assert result[3]["tool_call_id"] == "A"
    assert result[4]["tool_call_id"] == "B"
    assert result[3]["content"] == "ok"
    assert result[4]["content"] == "ok"

    appended = result[5:]
    assert [m["tool_call_id"] for m in appended] == ["C"]
    assert "尚未执行" in str(appended[0]["content"])


def test_build_recovery_messages_all_complete() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        _assistant_with_tool_calls(["A", "B"]),
        _tool_result("A"),
        _tool_result("B"),
    ]

    result = build_tool_batch_recovery_messages(messages)

    assert result == messages
    assert result is not messages


def test_build_recovery_messages_one_result_per_call() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        _assistant_with_tool_calls(["A", "B", "C"]),
        _tool_result("A"),
    ]

    result = build_tool_batch_recovery_messages(messages)

    tool_call_ids = [tc["id"] for tc in result[2]["tool_calls"]]
    result_ids = [
        m["tool_call_id"] for m in result if m.get("role") == "tool"
    ]
    assert sorted(tool_call_ids) == sorted(result_ids)
    assert len(result_ids) == len(set(result_ids)) == len(tool_call_ids)


def test_build_recovery_messages_in_flight_blocks() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        _assistant_with_tool_calls(["A", "B", "C"]),
        _tool_result("A"),
        _tool_result_in_flight("B"),
    ]

    with pytest.raises(ValueError):
        build_tool_batch_recovery_messages(messages)


def test_build_recovery_messages_does_not_mutate_input() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        _assistant_with_tool_calls(["A", "B"]),
        _tool_result("A"),
    ]
    original = json.loads(json.dumps(messages))

    build_tool_batch_recovery_messages(messages)

    assert messages == original


def test_build_recovery_messages_no_marker() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        _assistant_with_tool_calls(["A", "B"]),
        _tool_result("A"),
    ]

    result = build_tool_batch_recovery_messages(messages)

    assert all(IN_FLIGHT_MARKER_KEY not in message for message in result)


def _read_only_batch_in_flight() -> list[dict[str, object]]:
    return [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "读文件"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                _named_tool_call("A", "read_file", '{"path": "a.txt"}'),
                _named_tool_call("B", "list_files", '{"path": "."}'),
                _named_tool_call("C", "read_file", '{"path": "c.txt"}'),
            ],
        },
        _tool_result("A"),
        _tool_result_in_flight("B"),
    ]


def test_plan_tool_batch_recovery_read_only_in_flight_allows() -> None:
    plan = plan_tool_batch_recovery(_read_only_batch_in_flight())

    assert plan is not None
    assert plan.completed_call_ids == ("A",)
    pending = plan.pending_tool_calls
    assert [p.id for p in pending] == ["B", "C"]
    assert pending[0].name == "list_files"
    assert pending[0].interrupted is True
    assert pending[1].interrupted is False


@pytest.mark.parametrize(
    "name",
    [
        "read_file",
        "list_files",
        "find_files",
        "search_text",
        "git_status",
        "git_diff",
        "read_tool_output",
    ],
)
def test_plan_tool_batch_recovery_read_only_tools_all_allowed(name: str) -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "读文件"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                _named_tool_call("A", name, "{}"),
                _named_tool_call("B", "list_files", "{}"),
            ],
        },
        _tool_result_in_flight("A"),
    ]

    plan = plan_tool_batch_recovery(messages)

    assert plan is not None
    assert plan.pending_tool_calls[0].id == "A"
    assert plan.pending_tool_calls[0].interrupted is True
    assert plan.pending_tool_calls[1].id == "B"
    assert plan.pending_tool_calls[1].interrupted is False


@pytest.mark.parametrize(
    "name",
    [
        "write_file",
        "edit_file",
        "delete_file",
        "run_command",
        "compact",
        "todo_write",
        "unknown_tool",
    ],
)
def test_plan_tool_batch_recovery_non_read_only_in_flight_blocks(
    name: str,
) -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "写文件"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_named_tool_call("A", name, "{}")],
        },
        _tool_result_in_flight("A"),
    ]

    with pytest.raises(ValueError):
        plan_tool_batch_recovery(messages)


def test_build_recovery_messages_read_only_in_flight_removes_marker() -> None:
    result = build_tool_batch_recovery_messages(_read_only_batch_in_flight())

    assert all(IN_FLIGHT_MARKER_KEY not in message for message in result)

    assert result[3]["tool_call_id"] == "A"
    assert result[3]["content"] == "ok"

    assert result[4]["tool_call_id"] == "B"
    assert "被中断" in result[4]["content"]
    assert "尚未执行" not in result[4]["content"]

    assert result[5]["tool_call_id"] == "C"
    assert "尚未执行" in result[5]["content"]


def test_build_recovery_messages_read_only_one_result_per_call() -> None:
    result = build_tool_batch_recovery_messages(_read_only_batch_in_flight())

    tool_call_ids = [tc["id"] for tc in result[2]["tool_calls"]]
    result_ids = [
        m["tool_call_id"] for m in result if m.get("role") == "tool"
    ]
    assert sorted(tool_call_ids) == sorted(result_ids)
    assert len(result_ids) == len(set(result_ids)) == len(tool_call_ids)


def test_build_recovery_messages_read_only_does_not_mutate_input() -> None:
    messages = _read_only_batch_in_flight()
    original = json.loads(json.dumps(messages))

    build_tool_batch_recovery_messages(messages)

    assert messages == original
