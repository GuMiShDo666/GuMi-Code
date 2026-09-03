import pytest

from vibe_harness.events import print_terminal_event


def test_context_compacted_event_shows_counts_not_content(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_terminal_event(
        "context_compacted",
        {
            "step": 2,
            "stage": "tool_result_budget",
            "persisted_results": 1,
            "before_tokens": 250000,
            "after_tokens": 52000,
            "content": "secret-tool-output",
        },
    )

    out = capsys.readouterr().out

    assert "tool_result_budget" in out
    assert "1" in out
    assert "250000" in out
    assert "52000" in out
    assert "secret-tool-output" not in out


def test_context_compacted_snip_compact_stage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_terminal_event(
        "context_compacted",
        {
            "step": 2,
            "stage": "snip_compact",
            "archived_messages": 12,
            "transcript": ".transcripts/transcript_abc.jsonl",
            "before_tokens": 90000,
            "after_tokens": 60000,
            "content": "secret-archived-history",
        },
    )

    out = capsys.readouterr().out

    assert "snip_compact" in out
    assert "12" in out
    assert ".transcripts/transcript_abc.jsonl" in out
    assert "secret-archived-history" not in out


def test_context_compacted_micro_compact_stage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_terminal_event(
        "context_compacted",
        {
            "step": 2,
            "stage": "micro_compact",
            "shortened_results": 2,
            "before_tokens": 35000,
            "after_tokens": 25000,
            "content": "secret-tool-output",
        },
    )

    out = capsys.readouterr().out

    assert "micro_compact" in out
    assert "2" in out
    assert "35000" in out
    assert "25000" in out
    assert "secret-tool-output" not in out


def test_context_compacted_fit_tool_results_stage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_terminal_event(
        "context_compacted",
        {
            "step": 2,
            "stage": "fit_tool_results",
            "fitted_results": 4,
            "before_tokens": 25000,
            "after_tokens": 9000,
            "content": "secret-tool-output",
        },
    )

    out = capsys.readouterr().out

    assert "fit_tool_results" in out
    assert "4" in out
    assert "25000" in out
    assert "9000" in out
    assert "secret-tool-output" not in out


def test_context_compacted_compact_history_stage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_terminal_event(
        "context_compacted",
        {
            "step": 2,
            "stage": "compact_history",
            "transcript": ".transcripts/transcript_abc.jsonl",
            "before_tokens": 90000,
            "after_tokens": 6000,
            "content": "secret-history-content",
        },
    )

    out = capsys.readouterr().out

    assert "compact_history" in out
    assert ".transcripts/transcript_abc.jsonl" in out
    assert "90000" in out
    assert "6000" in out
    assert "secret-history-content" not in out


def test_context_compacted_reactive_compact_stage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_terminal_event(
        "context_compacted",
        {
            "step": 2,
            "stage": "reactive_compact",
            "retry": 1,
            "transcript": ".transcripts/transcript_abc.jsonl",
            "before_tokens": 90000,
            "after_tokens": 6000,
            "content": "secret-summary-content",
        },
    )

    out = capsys.readouterr().out

    assert "reactive_compact" in out
    assert "1" in out
    assert ".transcripts/transcript_abc.jsonl" in out
    assert "90000" in out
    assert "6000" in out
    assert "secret-summary-content" not in out
    assert "tool_result_budget" not in out


def test_context_compacted_active_compact_stage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_terminal_event(
        "context_compacted",
        {
            "step": 2,
            "stage": "active_compact",
            "transcript": ".transcripts/transcript_abc.jsonl",
            "before_tokens": 50000,
            "after_tokens": 6000,
            "content": "secret-summary-content",
        },
    )

    out = capsys.readouterr().out

    assert "active_compact" in out
    assert ".transcripts/transcript_abc.jsonl" in out
    assert "50000" in out
    assert "6000" in out
    assert "secret-summary-content" not in out
    assert "tool_result_budget" not in out


def test_context_summary_event_shows_no_content(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_terminal_event(
        "context_summary",
        {"step": 2, "messages": "secret-history-content"},
    )

    out = capsys.readouterr().out

    assert "历史摘要" in out
    assert "secret-history-content" not in out


def test_context_usage_event_includes_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_terminal_event(
        "context_usage",
        {"step": 1, "used_tokens": 12345, "max_tokens": 100000},
    )

    out = capsys.readouterr().out

    assert "12345" in out
    assert "100000" in out
    assert "tokens" in out


def test_provider_usage_event_compares_local_and_server_tokens(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_terminal_event(
        "provider_usage",
        {
            "step": 1,
            "local_prompt_tokens": 120,
            "prompt_tokens": 123,
            "completion_tokens": 8,
            "total_tokens": 131,
        },
    )

    out = capsys.readouterr().out

    assert "prompt=123" in out
    assert "本地=120" in out


def test_context_usage_event_does_not_print_full_history(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_terminal_event(
        "context_usage",
        {
            "step": 1,
            "used_tokens": 100,
            "max_tokens": 100000,
            "messages": "some-long-history-that-should-not-appear",
        },
    )

    out = capsys.readouterr().out

    assert "some-long-history-that-should-not-appear" not in out


def test_model_call_event_prints_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_terminal_event("model_call", {"step": 3})

    out = capsys.readouterr().out

    assert out == ""


def test_tool_call_event_includes_step_and_name_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_terminal_event(
        "tool_call",
        {"step": 3, "tool_name": "write_file", "arguments": {"path": "x.txt"}},
    )

    out = capsys.readouterr().out

    assert out == "── 第 3 步：write_file ──\n"
    assert '"path": "x.txt"' not in out
    assert "x.txt" not in out


def test_tool_call_event_hides_json_string_arguments(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_terminal_event(
        "tool_call",
        {"step": 1, "tool_name": "write_file", "arguments": '{"path": "x.txt"}'},
    )

    out = capsys.readouterr().out

    assert "write_file" in out
    assert '"path": "x.txt"' not in out
    assert "x.txt" not in out


def test_tool_call_event_hides_sensitive_arguments(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_terminal_event(
        "tool_call",
        {
            "step": 1,
            "tool_name": "write_file",
            "arguments": {"content": "SECRET-API-KEY-12345"},
        },
    )

    out = capsys.readouterr().out

    assert "write_file" in out
    assert "SECRET-API-KEY-12345" not in out


def test_tool_result_event_hides_success(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_terminal_event(
        "tool_result",
        {"step": 1, "tool_name": "list_files", "result": "hello.txt"},
    )

    out = capsys.readouterr().out

    assert out == ""
    assert "list_files" not in out
    assert "hello.txt" not in out


def test_tool_result_event_hides_large_success(
    capsys: pytest.CaptureFixture[str],
) -> None:
    big = "y" * 5000

    print_terminal_event(
        "tool_result",
        {"step": 1, "tool_name": "search_text", "result": big},
    )

    out = capsys.readouterr().out

    assert "[truncated]" not in out
    assert big not in out


def test_tool_result_event_shows_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_terminal_event(
        "tool_result",
        {"step": 1, "tool_name": "write_file", "result": "Error: 路径越界"},
    )

    out = capsys.readouterr().out

    assert "write_file" in out
    assert "路径越界" in out


def test_tool_result_error_is_truncated(
    capsys: pytest.CaptureFixture[str],
) -> None:
    big = "Error: " + "z" * 5000

    print_terminal_event(
        "tool_result",
        {"step": 1, "tool_name": "search_text", "result": big},
    )

    out = capsys.readouterr().out

    assert "search_text" in out
    assert "[truncated]" in out
    assert big not in out


def test_approval_preview_still_shows_arguments(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    from vibe_harness.permissions.terminal import request_terminal_approval

    request_terminal_approval(
        "write_file",
        {"content": "SECRET-API-KEY-12345"},
        "修改文件前需要确认",
    )

    out = capsys.readouterr().out

    assert "write_file" in out
    assert "SECRET-API-KEY-12345" in out


def test_final_answer_not_truncated(
    capsys: pytest.CaptureFixture[str],
) -> None:
    answer = "unique-final-answer"

    print_terminal_event("final_answer", {"step": 2, "answer": answer})

    out = capsys.readouterr().out

    assert out.count(answer) == 1
