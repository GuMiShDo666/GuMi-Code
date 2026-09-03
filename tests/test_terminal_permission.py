import pytest

from vibe_harness.permissions.terminal import request_terminal_approval


@pytest.mark.parametrize("answer", ["y", "Y", "yes", "YES", "Yes", "  y  ", "  yes "])
def test_approves_on_y_and_yes(
    monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)

    assert request_terminal_approval("write_file", {"path": "x.txt"}, "reason") is True


@pytest.mark.parametrize("answer", ["", "n", "no", "maybe", "yolo", " "])
def test_rejects_other_answers(
    monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)

    assert request_terminal_approval("write_file", {"path": "x.txt"}, "reason") is False


def test_rejects_on_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_eof(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)

    assert request_terminal_approval("write_file", {}, "reason") is False


def test_rejects_on_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_interrupt(prompt: str = "") -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", raise_interrupt)

    assert request_terminal_approval("write_file", {}, "reason") is False


def test_output_includes_tool_name_reason_and_arguments(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    request_terminal_approval(
        "delete_file",
        {"path": "secret.txt"},
        "删除文件前需要确认",
    )

    out = capsys.readouterr().out

    assert "delete_file" in out
    assert "删除文件前需要确认" in out
    assert '"path": "secret.txt"' in out


def test_truncates_long_arguments(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    big_content = "x" * 5000

    request_terminal_approval(
        "write_file",
        {"content": big_content},
        "修改文件前需要确认",
    )

    out = capsys.readouterr().out

    assert "[arguments truncated]" in out
    assert big_content not in out
