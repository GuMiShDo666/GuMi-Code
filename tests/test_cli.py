import io
import json
import sys
from pathlib import Path

import pytest

from vibe_harness import cli
from vibe_harness.agent import MAX_STEPS
from vibe_harness.config import Settings
from vibe_harness.context import DEFAULT_MAX_CONTEXT_TOKENS
from vibe_harness.session import IN_FLIGHT_MARKER_KEY, save_session


@pytest.fixture(autouse=True)
def _stub_token_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "load_token_counter", lambda model, path=None: object())


def _tool_call_dict(
    call_id: str,
    name: str = "write_file",
    arguments: str = '{"path": "x.txt"}',
) -> dict[str, object]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _assistant_with_tool_calls(call_ids: list[str]) -> dict[str, object]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [_tool_call_dict(call_id) for call_id in call_ids],
    }


def _tool_result(call_id: str, content: str = "真实结果") -> dict[str, object]:
    return {"role": "tool", "tool_call_id": call_id, "content": content}



def test_parse_args_valid_workspace(tmp_path: Path) -> None:
    args = cli.parse_args(["--workspace", str(tmp_path), "检查项目"])

    assert args.workspace == tmp_path
    assert args.task == "检查项目"


def test_parse_args_workspace_not_exist(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(SystemExit):
        cli.parse_args(["--workspace", str(missing), "检查项目"])


def test_parse_args_workspace_is_file(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("hi")

    with pytest.raises(SystemExit):
        cli.parse_args(["--workspace", str(f), "检查项目"])


def test_main_passes_args_and_returns_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_client = object()
    captured: dict[str, object] = {}

    def fake_openai(api_key: str, base_url: str) -> object:
        captured["api_key"] = api_key
        captured["base_url"] = base_url
        return fake_client

    def fake_run_agent(
        client: object,
        model: str,
        workspace: Path,
        user_query: str,
        approval_callback: object,
        event_callback: object,
        message_snapshot_callback: object = None,
        initial_messages: object = None,
        max_steps: int = MAX_STEPS,
        max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
        allow_run_command: bool = False,
        token_counter: object = None,
    ) -> str:
        captured["client"] = client
        captured["model"] = model
        captured["workspace"] = workspace
        captured["user_query"] = user_query
        captured["approval_callback"] = approval_callback
        captured["event_callback"] = event_callback
        captured["message_snapshot_callback"] = message_snapshot_callback
        return "最终回答内容"

    monkeypatch.setattr(cli, "OpenAI", fake_openai)
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: Settings(api_key="k", base_url="http://x", model="deepseek"),
    )
    monkeypatch.setattr(cli, "run_agent", fake_run_agent)

    code = cli.main(["--workspace", str(tmp_path), "检查项目"])

    out = capsys.readouterr().out

    assert code == 0
    assert captured["api_key"] == "k"
    assert captured["base_url"] == "http://x"
    assert captured["client"] is fake_client
    assert captured["model"] == "deepseek"
    assert captured["workspace"] == tmp_path
    assert captured["user_query"] == "检查项目"
    assert captured["approval_callback"] is cli.request_terminal_approval
    assert captured["event_callback"] is cli.print_terminal_event
    assert captured["message_snapshot_callback"] is None
    assert "最终回答内容" not in out


def test_main_returns_one_on_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "OpenAI", lambda api_key, base_url: object())
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: Settings(api_key="k", base_url="http://x", model="deepseek"),
    )

    def boom(**kwargs: object) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "run_agent", boom)

    code = cli.main(["--workspace", str(tmp_path), "检查项目"])

    captured = capsys.readouterr()

    assert code == 1
    assert "[错误]" in captured.err
    assert "boom" in captured.err


def test_main_flushes_stdout_before_writing_stderr_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "OpenAI", lambda api_key, base_url: object())
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: Settings(api_key="k", base_url="http://x", model="deepseek"),
    )

    events: list[str] = []

    class RecordingStream:
        def __init__(self, name: str, target: object) -> None:
            self._name = name
            self._target = target

        def write(self, text: str) -> int:
            events.append(f"{self._name}.write")
            return self._target.write(text)  # type: ignore[attr-defined]

        def flush(self) -> None:
            events.append(f"{self._name}.flush")
            self._target.flush()  # type: ignore[attr-defined]

    fake_stdout = RecordingStream("stdout", io.StringIO())
    fake_stderr = RecordingStream("stderr", io.StringIO())

    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(sys, "stderr", fake_stderr)

    def progress_then_boom(**kwargs: object) -> str:
        print("进度：正在执行工具调用")
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "run_agent", progress_then_boom)

    code = cli.main(["--workspace", str(tmp_path), "检查项目"])

    assert code == 1
    assert events.index("stdout.flush") < events.index("stderr.write")


def test_main_returns_one_when_settings_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_to_load_settings() -> Settings:
        raise RuntimeError("配置缺失")

    monkeypatch.setattr(cli, "load_settings", fail_to_load_settings)

    code = cli.main(["--workspace", str(tmp_path), "检查项目"])

    captured = capsys.readouterr()

    assert code == 1
    assert "[错误]" in captured.err
    assert "配置缺失" in captured.err


def test_parse_args_without_session_file(tmp_path: Path) -> None:
    args = cli.parse_args(["--workspace", str(tmp_path), "任务"])

    assert args.session_file is None


def test_parse_args_with_session_file(tmp_path: Path) -> None:
    session = tmp_path / "session.json"

    args = cli.parse_args(
        ["--workspace", str(tmp_path), "--session-file", str(session), "任务"]
    )

    assert args.session_file == session.resolve()


def test_parse_args_session_file_expands_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    args = cli.parse_args(
        ["--workspace", str(tmp_path), "--session-file", "~/session.json", "任务"]
    )

    assert args.session_file == (home / "session.json").resolve()


def test_parse_args_session_file_missing_parent(tmp_path: Path) -> None:
    missing = tmp_path / "nested" / "session.json"

    with pytest.raises(SystemExit):
        cli.parse_args(
            ["--workspace", str(tmp_path), "--session-file", str(missing), "任务"]
        )


def test_parse_args_rejects_session_file_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text("{}")
    link = tmp_path / "session.json"
    link.symlink_to(real)

    with pytest.raises(SystemExit):
        cli.parse_args(
            ["--workspace", str(tmp_path), "--session-file", str(link), "任务"]
        )


def test_main_writes_session_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = tmp_path / "session.json"
    captured: dict[str, object] = {}

    def fake_run_agent(
        client: object,
        model: str,
        workspace: Path,
        user_query: str,
        approval_callback: object,
        event_callback: object,
        message_snapshot_callback: object = None,
        initial_messages: object = None,
        max_steps: int = MAX_STEPS,
        max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
        allow_run_command: bool = False,
        token_counter: object = None,
    ) -> str:
        captured["message_snapshot_callback"] = message_snapshot_callback
        if message_snapshot_callback is not None:
            message_snapshot_callback(
                [
                    {"role": "system", "content": "你是一个助手"},
                    {"role": "user", "content": "任务"},
                ]
            )
        return "ok"

    monkeypatch.setattr(cli, "OpenAI", lambda api_key, base_url: object())
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: Settings(api_key="k", base_url="http://x", model="deepseek"),
    )
    monkeypatch.setattr(cli, "run_agent", fake_run_agent)

    code = cli.main(
        ["--workspace", str(tmp_path), "--session-file", str(session), "任务"]
    )

    assert code == 0
    assert captured["message_snapshot_callback"] is not None
    assert session.exists()

    data = json.loads(session.read_text(encoding="utf-8"))
    assert data["model"] == "deepseek"
    assert data["workspace"] == str(tmp_path.resolve())
    assert data["messages"][0]["role"] == "system"


def test_main_does_not_create_session_file_when_not_specified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_agent(
        client: object,
        model: str,
        workspace: Path,
        user_query: str,
        approval_callback: object,
        event_callback: object,
        message_snapshot_callback: object = None,
        initial_messages: object = None,
        max_steps: int = MAX_STEPS,
        max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
        allow_run_command: bool = False,
        token_counter: object = None,
    ) -> str:
        captured["message_snapshot_callback"] = message_snapshot_callback
        return "ok"

    monkeypatch.setattr(cli, "OpenAI", lambda api_key, base_url: object())
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: Settings(api_key="k", base_url="http://x", model="deepseek"),
    )
    monkeypatch.setattr(cli, "run_agent", fake_run_agent)

    code = cli.main(["--workspace", str(tmp_path), "任务"])

    assert code == 0
    assert captured["message_snapshot_callback"] is None
    assert not (tmp_path / "session.json").exists()


def test_main_returns_one_when_session_save_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = tmp_path / "session.json"

    def fake_run_agent(
        client: object,
        model: str,
        workspace: Path,
        user_query: str,
        approval_callback: object,
        event_callback: object,
        message_snapshot_callback: object = None,
        initial_messages: object = None,
        max_steps: int = MAX_STEPS,
        max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
        allow_run_command: bool = False,
        token_counter: object = None,
    ) -> str:
        message_snapshot_callback([{"role": "user", "content": "x"}])
        return "ok"

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("磁盘写入失败")

    monkeypatch.setattr(cli, "OpenAI", lambda api_key, base_url: object())
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: Settings(api_key="k", base_url="http://x", model="deepseek"),
    )
    monkeypatch.setattr(cli, "run_agent", fake_run_agent)
    monkeypatch.setattr(cli, "save_session", boom)

    code = cli.main(
        ["--workspace", str(tmp_path), "--session-file", str(session), "任务"]
    )

    captured = capsys.readouterr()

    assert code == 1
    assert "[错误]" in captured.err
    assert "磁盘写入失败" in captured.err


def test_parse_args_session_args_mutually_exclusive(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(
            [
                "--workspace",
                str(tmp_path),
                "--session-file",
                str(tmp_path / "a.json"),
                "--resume-session",
                str(tmp_path / "b.json"),
                "任务",
            ]
        )


def test_parse_args_resume_session(tmp_path: Path) -> None:
    session = tmp_path / "session.json"
    session.write_text("{}")

    args = cli.parse_args(
        ["--workspace", str(tmp_path), "--resume-session", str(session), "任务"]
    )

    assert args.resume_session == session.resolve()


def test_parse_args_rejects_resume_session_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text("{}")
    link = tmp_path / "session.json"
    link.symlink_to(real)

    with pytest.raises(SystemExit):
        cli.parse_args(
            ["--workspace", str(tmp_path), "--resume-session", str(link), "任务"]
        )


def test_main_resumes_and_writes_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = tmp_path / "session.json"
    save_session(
        session,
        tmp_path,
        "deepseek",
        [
            {"role": "system", "content": "系统"},
            {"role": "user", "content": "旧任务"},
            {"role": "assistant", "content": "旧回答"},
        ],
    )
    captured: dict[str, object] = {}

    def fake_run_agent(
        client: object,
        model: str,
        workspace: Path,
        user_query: str,
        approval_callback: object,
        event_callback: object,
        message_snapshot_callback: object = None,
        initial_messages: object = None,
        max_steps: int = MAX_STEPS,
        max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
        allow_run_command: bool = False,
        token_counter: object = None,
    ) -> str:
        captured["initial_messages"] = initial_messages
        captured["message_snapshot_callback"] = message_snapshot_callback
        if message_snapshot_callback is not None:
            message_snapshot_callback(
                [
                    {"role": "system", "content": "系统"},
                    {"role": "user", "content": "旧任务"},
                    {"role": "assistant", "content": "旧回答"},
                    {"role": "user", "content": "继续检查"},
                    {"role": "assistant", "content": "新回答"},
                ]
            )
        return "新回答"

    monkeypatch.setattr(cli, "OpenAI", lambda api_key, base_url: object())
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: Settings(api_key="k", base_url="http://x", model="deepseek"),
    )
    monkeypatch.setattr(cli, "run_agent", fake_run_agent)

    code = cli.main(
        ["--workspace", str(tmp_path), "--resume-session", str(session), "继续检查"]
    )

    assert code == 0
    assert [m["role"] for m in captured["initial_messages"]] == [
        "system",
        "user",
        "assistant",
    ]

    data = json.loads(session.read_text(encoding="utf-8"))
    assert data["messages"][-1]["content"] == "新回答"


def test_main_resume_workspace_mismatch_returns_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = tmp_path / "session.json"
    other_workspace = tmp_path / "other"
    save_session(
        session,
        other_workspace,
        "deepseek",
        [
            {"role": "system", "content": "系统"},
            {"role": "user", "content": "旧任务"},
            {"role": "assistant", "content": "旧回答"},
        ],
    )

    monkeypatch.setattr(cli, "OpenAI", lambda api_key, base_url: object())
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: Settings(api_key="k", base_url="http://x", model="deepseek"),
    )

    code = cli.main(
        ["--workspace", str(tmp_path), "--resume-session", str(session), "任务"]
    )

    captured = capsys.readouterr()

    assert code == 1
    assert "[错误]" in captured.err
    assert "workspace" in captured.err


def test_main_resume_model_mismatch_returns_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = tmp_path / "session.json"
    save_session(
        session,
        tmp_path,
        "other-model",
        [
            {"role": "system", "content": "系统"},
            {"role": "user", "content": "旧任务"},
            {"role": "assistant", "content": "旧回答"},
        ],
    )

    monkeypatch.setattr(cli, "OpenAI", lambda api_key, base_url: object())
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: Settings(api_key="k", base_url="http://x", model="deepseek"),
    )

    code = cli.main(
        ["--workspace", str(tmp_path), "--resume-session", str(session), "任务"]
    )

    captured = capsys.readouterr()

    assert code == 1
    assert "[错误]" in captured.err
    assert "model" in captured.err


def test_parse_args_max_steps_default(tmp_path: Path) -> None:
    args = cli.parse_args(["--workspace", str(tmp_path), "任务"])

    assert args.max_steps == MAX_STEPS


def test_parse_args_max_steps_custom(tmp_path: Path) -> None:
    args = cli.parse_args(
        ["--workspace", str(tmp_path), "--max-steps", "20", "任务"]
    )

    assert args.max_steps == 20


@pytest.mark.parametrize("value", ["0", "-3", "abc", "20.5"])
def test_parse_args_rejects_invalid_max_steps(
    tmp_path: Path,
    value: str,
) -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(
            ["--workspace", str(tmp_path), "--max-steps", value, "任务"]
        )


def test_max_steps_constant_is_sixteen() -> None:
    assert MAX_STEPS == 16


def test_parse_args_help_shows_default_max_steps_sixteen(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(["--help"])

    assert "默认 16" in capsys.readouterr().out


def test_main_passes_max_steps_three_to_run_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_agent(
        client: object,
        model: str,
        workspace: Path,
        user_query: str,
        max_steps: int,
        max_context_tokens: int,
        approval_callback: object,
        event_callback: object,
        message_snapshot_callback: object = None,
        initial_messages: object = None,
        allow_run_command: bool = False,
        token_counter: object = None,
    ) -> str:
        captured["max_steps"] = max_steps
        return "ok"

    monkeypatch.setattr(cli, "OpenAI", lambda api_key, base_url: object())
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: Settings(api_key="k", base_url="http://x", model="deepseek"),
    )
    monkeypatch.setattr(cli, "run_agent", fake_run_agent)

    code = cli.main(
        ["--workspace", str(tmp_path), "--max-steps", "3", "任务"]
    )

    assert code == 0
    assert captured["max_steps"] == 3


def test_main_passes_max_steps_to_run_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_agent(
        client: object,
        model: str,
        workspace: Path,
        user_query: str,
        max_steps: int,
        max_context_tokens: int,
        approval_callback: object,
        event_callback: object,
        message_snapshot_callback: object = None,
        initial_messages: object = None,
        allow_run_command: bool = False,
        token_counter: object = None,
    ) -> str:
        captured["max_steps"] = max_steps
        return "ok"

    monkeypatch.setattr(cli, "OpenAI", lambda api_key, base_url: object())
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: Settings(api_key="k", base_url="http://x", model="deepseek"),
    )
    monkeypatch.setattr(cli, "run_agent", fake_run_agent)

    code = cli.main(
        ["--workspace", str(tmp_path), "--max-steps", "20", "任务"]
    )

    assert code == 0
    assert captured["max_steps"] == 20


def test_main_step_budget_finalization_returns_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "OpenAI", lambda api_key, base_url: object())
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: Settings(api_key="k", base_url="http://x", model="deepseek"),
    )
    monkeypatch.setattr(cli, "run_agent", lambda **kwargs: "阶段性总结")

    code = cli.main(["--workspace", str(tmp_path), "任务"])

    assert code == 0


def test_parse_args_max_context_tokens_default(tmp_path: Path) -> None:
    args = cli.parse_args(["--workspace", str(tmp_path), "任务"])

    assert args.max_context_tokens == DEFAULT_MAX_CONTEXT_TOKENS


def test_parse_args_max_context_tokens_custom(tmp_path: Path) -> None:
    args = cli.parse_args(
        ["--workspace", str(tmp_path), "--max-context-tokens", "12000", "任务"]
    )

    assert args.max_context_tokens == 12000


def test_parse_args_rejects_removed_max_context_chars(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(
            ["--workspace", str(tmp_path), "--max-context-chars", "12000", "任务"]
        )


def test_parse_args_tokenizer_file(tmp_path: Path) -> None:
    tokenizer_file = tmp_path / "tokenizer.json"

    args = cli.parse_args(
        ["--workspace", str(tmp_path), "--tokenizer-file", str(tokenizer_file), "任务"]
    )

    assert args.tokenizer_file == tokenizer_file


def test_main_loads_and_passes_token_counter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tokenizer_file = tmp_path / "tokenizer.json"
    counter = object()
    captured: dict[str, object] = {}

    def fake_load_token_counter(model: str, path: Path | None) -> object:
        captured["model"] = model
        captured["path"] = path
        return counter

    def fake_run_agent(**kwargs: object) -> str:
        captured["counter"] = kwargs["token_counter"]
        return "ok"

    monkeypatch.setattr(cli, "OpenAI", lambda api_key, base_url: object())
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: Settings(
            api_key="k",
            base_url="http://x",
            model="deepseek-v4-flash",
        ),
    )
    monkeypatch.setattr(cli, "load_token_counter", fake_load_token_counter)
    monkeypatch.setattr(cli, "run_agent", fake_run_agent)

    code = cli.main(
        ["--workspace", str(tmp_path), "--tokenizer-file", str(tokenizer_file), "任务"]
    )

    assert code == 0
    assert captured == {
        "model": "deepseek-v4-flash",
        "path": tokenizer_file,
        "counter": counter,
    }


def test_main_tokenizer_failure_happens_before_client_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client_created = False

    def fake_openai(api_key: str, base_url: str) -> object:
        nonlocal client_created
        client_created = True
        return object()

    def fail_tokenizer(model: str, path: Path | None) -> object:
        raise RuntimeError("无法加载 tokenizer")

    monkeypatch.setattr(cli, "OpenAI", fake_openai)
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: Settings(
            api_key="k",
            base_url="http://x",
            model="deepseek-v4-flash",
        ),
    )
    monkeypatch.setattr(cli, "load_token_counter", fail_tokenizer)

    code = cli.main(["--workspace", str(tmp_path), "任务"])

    assert code == 1
    assert client_created is False
    assert "无法加载 tokenizer" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["0", "-3", "abc", "20.5"])
def test_parse_args_rejects_invalid_max_context_tokens(
    tmp_path: Path,
    value: str,
) -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(
            ["--workspace", str(tmp_path), "--max-context-tokens", value, "任务"]
        )


def test_main_passes_max_context_tokens_to_run_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_agent(
        client: object,
        model: str,
        workspace: Path,
        user_query: str,
        max_steps: int,
        max_context_tokens: int,
        approval_callback: object,
        event_callback: object,
        message_snapshot_callback: object = None,
        initial_messages: object = None,
        allow_run_command: bool = False,
        token_counter: object = None,
    ) -> str:
        captured["max_context_tokens"] = max_context_tokens
        return "ok"

    monkeypatch.setattr(cli, "OpenAI", lambda api_key, base_url: object())
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: Settings(api_key="k", base_url="http://x", model="deepseek"),
    )
    monkeypatch.setattr(cli, "run_agent", fake_run_agent)

    code = cli.main(
        ["--workspace", str(tmp_path), "--max-context-tokens", "12000", "任务"]
    )

    assert code == 0
    assert captured["max_context_tokens"] == 12000


def test_parse_args_allow_run_command_default(tmp_path: Path) -> None:
    args = cli.parse_args(["--workspace", str(tmp_path), "任务"])

    assert args.allow_run_command is False


def test_parse_args_allow_run_command_enabled(tmp_path: Path) -> None:
    args = cli.parse_args(
        ["--workspace", str(tmp_path), "--allow-run-command", "任务"]
    )

    assert args.allow_run_command is True


def test_main_passes_allow_run_command_to_run_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_agent(
        client: object,
        model: str,
        workspace: Path,
        user_query: str,
        approval_callback: object,
        event_callback: object,
        message_snapshot_callback: object = None,
        initial_messages: object = None,
        max_steps: int = MAX_STEPS,
        max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
        allow_run_command: bool = False,
        token_counter: object = None,
    ) -> str:
        captured["allow_run_command"] = allow_run_command
        return "ok"

    monkeypatch.setattr(cli, "OpenAI", lambda api_key, base_url: object())
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: Settings(api_key="k", base_url="http://x", model="deepseek"),
    )
    monkeypatch.setattr(cli, "run_agent", fake_run_agent)

    code = cli.main(
        ["--workspace", str(tmp_path), "--allow-run-command", "任务"]
    )

    assert code == 0
    assert captured["allow_run_command"] is True


def test_main_warns_when_allow_run_command_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "OpenAI", lambda api_key, base_url: object())
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: Settings(api_key="k", base_url="http://x", model="deepseek"),
    )
    monkeypatch.setattr(cli, "run_agent", lambda **kwargs: "ok")

    code = cli.main(
        ["--workspace", str(tmp_path), "--allow-run-command", "任务"]
    )

    captured = capsys.readouterr()

    assert code == 0
    assert "run_command 已启用" in captured.out


def test_help_contains_allow_run_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.parse_args(["--help"])

    captured = capsys.readouterr()

    assert exc_info.value.code == 0
    assert "--allow-run-command" in captured.out


def test_main_resume_partial_batch_pads_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = tmp_path / "session.json"
    save_session(
        session,
        tmp_path,
        "deepseek",
        [
            {"role": "system", "content": "系统"},
            {"role": "user", "content": "写文件"},
            _assistant_with_tool_calls(["call_1", "call_2", "call_3"]),
            _tool_result("call_1", "真实结果 1"),
        ],
    )
    captured: dict[str, object] = {}

    def fake_run_agent(**kwargs: object) -> str:
        captured["initial_messages"] = kwargs["initial_messages"]
        return "ok"

    monkeypatch.setattr(cli, "OpenAI", lambda api_key, base_url: object())
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: Settings(api_key="k", base_url="http://x", model="deepseek"),
    )
    monkeypatch.setattr(cli, "run_agent", fake_run_agent)

    code = cli.main(
        ["--workspace", str(tmp_path), "--resume-session", str(session), "继续"]
    )

    assert code == 0
    messages = captured["initial_messages"]
    appended = messages[-2:]
    assert [m["tool_call_id"] for m in appended] == ["call_2", "call_3"]
    assert all("尚未执行" in m["content"] for m in appended)


def test_main_resume_completed_result_not_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = tmp_path / "session.json"
    save_session(
        session,
        tmp_path,
        "deepseek",
        [
            {"role": "system", "content": "系统"},
            {"role": "user", "content": "写文件"},
            _assistant_with_tool_calls(["call_1", "call_2"]),
            _tool_result("call_1", "真实结果 1"),
        ],
    )
    captured: dict[str, object] = {}

    def fake_run_agent(**kwargs: object) -> str:
        captured["initial_messages"] = kwargs["initial_messages"]
        return "ok"

    monkeypatch.setattr(cli, "OpenAI", lambda api_key, base_url: object())
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: Settings(api_key="k", base_url="http://x", model="deepseek"),
    )
    monkeypatch.setattr(cli, "run_agent", fake_run_agent)

    code = cli.main(
        ["--workspace", str(tmp_path), "--resume-session", str(session), "继续"]
    )

    assert code == 0
    messages = captured["initial_messages"]
    completed = messages[3]
    assert completed["tool_call_id"] == "call_1"
    assert completed["content"] == "真实结果 1"
    assert "尚未执行" not in completed["content"]


def test_main_resume_in_flight_returns_one_and_no_run_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = tmp_path / "session.json"
    save_session(
        session,
        tmp_path,
        "deepseek",
        [
            {"role": "system", "content": "系统"},
            {"role": "user", "content": "写文件"},
            _assistant_with_tool_calls(["call_1", "call_2"]),
            _tool_result("call_1", "真实结果 1"),
            {
                "role": "tool",
                "tool_call_id": "call_2",
                "content": "",
                IN_FLIGHT_MARKER_KEY: True,
            },
        ],
    )
    called: list[object] = []

    def fake_run_agent(**kwargs: object) -> str:
        called.append(kwargs)
        return "ok"

    monkeypatch.setattr(cli, "OpenAI", lambda api_key, base_url: object())
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: Settings(api_key="k", base_url="http://x", model="deepseek"),
    )
    monkeypatch.setattr(cli, "run_agent", fake_run_agent)

    code = cli.main(
        ["--workspace", str(tmp_path), "--resume-session", str(session), "继续"]
    )

    captured = capsys.readouterr()

    assert code == 1
    assert called == []
    assert "[错误]" in captured.err


def test_main_resume_in_flight_does_not_overwrite_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = tmp_path / "session.json"
    save_session(
        session,
        tmp_path,
        "deepseek",
        [
            {"role": "system", "content": "系统"},
            {"role": "user", "content": "写文件"},
            _assistant_with_tool_calls(["call_1", "call_2"]),
            _tool_result("call_1", "真实结果 1"),
            {
                "role": "tool",
                "tool_call_id": "call_2",
                "content": "",
                IN_FLIGHT_MARKER_KEY: True,
            },
        ],
    )
    original_bytes = session.read_bytes()

    monkeypatch.setattr(cli, "OpenAI", lambda api_key, base_url: object())
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: Settings(api_key="k", base_url="http://x", model="deepseek"),
    )
    monkeypatch.setattr(cli, "run_agent", lambda **kwargs: "ok")

    code = cli.main(
        ["--workspace", str(tmp_path), "--resume-session", str(session), "继续"]
    )

    assert code == 1
    assert session.read_bytes() == original_bytes


def test_main_resume_read_only_in_flight_enters_run_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = tmp_path / "session.json"
    save_session(
        session,
        tmp_path,
        "deepseek",
        [
            {"role": "system", "content": "系统"},
            {"role": "user", "content": "读文件"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    _tool_call_dict(
                        "call_1",
                        name="read_file",
                        arguments='{"path": "a.txt"}',
                    ),
                    _tool_call_dict(
                        "call_2",
                        name="list_files",
                        arguments='{"path": "."}',
                    ),
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "",
                IN_FLIGHT_MARKER_KEY: True,
            },
        ],
    )
    captured: dict[str, object] = {}

    def fake_run_agent(**kwargs: object) -> str:
        captured["initial_messages"] = kwargs["initial_messages"]
        return "ok"

    monkeypatch.setattr(cli, "OpenAI", lambda api_key, base_url: object())
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: Settings(api_key="k", base_url="http://x", model="deepseek"),
    )
    monkeypatch.setattr(cli, "run_agent", fake_run_agent)

    code = cli.main(
        ["--workspace", str(tmp_path), "--resume-session", str(session), "继续"]
    )

    assert code == 0
    messages = captured["initial_messages"]
    assert all(IN_FLIGHT_MARKER_KEY not in m for m in messages)
    result_ids = [
        m["tool_call_id"] for m in messages if m.get("role") == "tool"
    ]
    assert result_ids == ["call_1", "call_2"]
    assert "被中断" in messages[-2]["content"]
    assert "尚未执行" in messages[-1]["content"]


def test_main_resume_mismatch_priority_over_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = tmp_path / "session.json"
    save_session(
        session,
        tmp_path,
        "other-model",
        [
            {"role": "system", "content": "系统"},
            {"role": "user", "content": "写文件"},
            _assistant_with_tool_calls(["call_1", "call_2"]),
            _tool_result("call_1", "真实结果 1"),
        ],
    )

    monkeypatch.setattr(cli, "OpenAI", lambda api_key, base_url: object())
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: Settings(api_key="k", base_url="http://x", model="deepseek"),
    )
    monkeypatch.setattr(cli, "run_agent", lambda **kwargs: "ok")

    code = cli.main(
        ["--workspace", str(tmp_path), "--resume-session", str(session), "继续"]
    )

    captured = capsys.readouterr()

    assert code == 1
    assert "model" in captured.err


def test_main_resume_event_does_not_leak_tool_args(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = tmp_path / "session.json"
    secret_arg = "/very/secret/path.txt"
    save_session(
        session,
        tmp_path,
        "deepseek",
        [
            {"role": "system", "content": "系统"},
            {"role": "user", "content": "写文件"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    _tool_call_dict(
                        "call_1",
                        arguments=json.dumps(
                            {"path": secret_arg, "content": "secret-content"}
                        ),
                    ),
                    _tool_call_dict("call_2"),
                ],
            },
            _tool_result("call_1", "真实结果 1"),
        ],
    )

    monkeypatch.setattr(cli, "OpenAI", lambda api_key, base_url: object())
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: Settings(api_key="k", base_url="http://x", model="deepseek"),
    )
    monkeypatch.setattr(cli, "run_agent", lambda **kwargs: "ok")

    code = cli.main(
        ["--workspace", str(tmp_path), "--resume-session", str(session), "继续"]
    )

    out = capsys.readouterr().out

    assert code == 0
    assert "恢复中断工具批次" in out
    assert "1 个已完成结果" in out
    assert "1 个未执行调用" in out
    assert secret_arg not in out
    assert "secret-content" not in out


def test_main_resume_new_task_after_complete_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = tmp_path / "session.json"
    save_session(
        session,
        tmp_path,
        "deepseek",
        [
            {"role": "system", "content": "系统"},
            {"role": "user", "content": "写文件"},
            _assistant_with_tool_calls(["call_1", "call_2"]),
            _tool_result("call_1", "真实结果 1"),
        ],
    )
    captured: dict[str, object] = {}

    def fake_run_agent(**kwargs: object) -> str:
        captured["initial_messages"] = kwargs["initial_messages"]
        captured["user_query"] = kwargs["user_query"]
        return "ok"

    monkeypatch.setattr(cli, "OpenAI", lambda api_key, base_url: object())
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: Settings(api_key="k", base_url="http://x", model="deepseek"),
    )
    monkeypatch.setattr(cli, "run_agent", fake_run_agent)

    code = cli.main(
        ["--workspace", str(tmp_path), "--resume-session", str(session), "继续检查"]
    )

    assert code == 0
    messages = captured["initial_messages"]
    assert messages[-1]["role"] == "tool"
    assert messages[-1]["tool_call_id"] == "call_2"
    assert captured["user_query"] == "继续检查"

    combined = messages + [
        {"role": "user", "content": captured["user_query"]}
    ]
    assert combined[-1]["role"] == "user"
    assert combined[-1]["content"] == "继续检查"
    assert combined[-2]["role"] == "tool"
