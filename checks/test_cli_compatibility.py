"""Desktop extraction retains CLI flags, Docker ownership and paused self-checks."""
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

from gum_code import cli
from gum_code.config import Settings
from gum_code.self_check import AUTOMATIC_SELF_CHECK_ENABLED


def test_cli_flags_and_docker_backend_are_preserved(tmp_path, monkeypatch):
    args = cli.parse_args([
        '--workspace', str(tmp_path), '--allow-run-command', '--sandbox-image', 'synthetic-image',
        '--no-subagent', '--max-steps', '7', '--max-context-tokens', '6000', 'Synthetic task',
    ], interactive=False)
    assert not AUTOMATIC_SELF_CHECK_ENABLED
    sandbox = object()
    project = MagicMock()
    project.close.return_value = ()
    browser = MagicMock()
    browser.close.return_value = ()
    vision = MagicMock()
    vision.cancel.return_value = None
    renderer = MagicMock()
    renderer.paused.side_effect = lambda callback: callback
    project_factory = MagicMock(return_value=project)
    docker_factory = MagicMock(return_value=sandbox)
    captured = []

    def agent(**kwargs):
        captured.append(kwargs)
        kwargs['event_callback']('task_outcome', dict(
            status='completed', reason='answered', step=1, self_check_status='disabled', changed=False, summary='',
        ))
        return 'Synthetic answer'

    def forbidden(*_args, **_kwargs):
        raise AssertionError('Automatic verification remains paused')

    patches = dict(
        load_settings=lambda: Settings('YOUR_TOKEN_HERE', 'https://invalid.example', 'synthetic', 8000),
        load_vision_settings=lambda: None,
        OpenAI=lambda **_kwargs: NS(),
        resolve_context_window=lambda *_args: 8000,
        load_token_counter=lambda *_args: None,
        load_docker_sandbox=docker_factory,
        ProjectRuntime=project_factory,
        ProjectBrowser=lambda *_args, **_kwargs: browser,
        VisionRuntime=lambda *_args, **_kwargs: vision,
        RichEventRenderer=lambda *_args, **_kwargs: renderer,
        SelfCheckRuntime=forbidden,
        run_agent=agent,
    )
    for name, value in patches.items():
        monkeypatch.setattr(cli, name, value)
    assert cli._run_one_shot(args) == 0
    docker_factory.assert_called_once_with('synthetic-image')
    assert project_factory.call_args.kwargs['command_sandbox'] is sandbox
    assert captured[0]['command_sandbox'] is sandbox
    assert captured[0]['max_steps'] == 7
    assert captured[0]['max_context_tokens'] == 6000
    assert captured[0]['allow_subagent'] is False
    assert captured[0]['self_check_runtime'] is None
    assert 'desktop_host' not in captured[0]
    assert not getattr(captured[0]['client'], '_gumcode_compatible_api', False)
    browser.close.assert_called_once()
    project.close.assert_called_once()
    vision.cancel.assert_called_once()


def test_desktop_reads_segmented_cli_session_without_rewriting(tmp_path, monkeypatch):
    import hashlib
    import io
    import json
    from uuid import uuid4
    from gum_code import session
    from gum_code.desktop import run

    monkeypatch.setattr(session, 'MAX_SESSION_FILE_BYTES', 2048)
    path = tmp_path / 'history.jsonl'
    messages = [{'role': 'system', 'content': 'Synthetic system'}]
    session.save_session(path, tmp_path, 'synthetic', messages)
    for index in range(9):
        messages.extend([
            {'role': 'user', 'content': f'Synthetic request {index} ' + 'u' * 400},
            {'role': 'assistant', 'content': f'Synthetic answer {index} ' + 'a' * 400},
        ])
        session.save_session(path, tmp_path, 'synthetic', messages)
    assert session.session_history_usage(path)['segments'] > 1
    paths = [path, *session.session_internal_paths(path)]
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}
    request = dict(v=1, action='history', sessionId=str(uuid4()), turnId=str(uuid4()),
                   workspace=str(tmp_path), sessionPath=str(path))
    output = io.StringIO()
    assert run(io.StringIO(json.dumps(request) + '\n'), output) == 0
    result = [json.loads(line) for line in output.getvalue().splitlines()]
    assert result[0]['messages'] == messages[1:]
    assert session.load_session(path).messages == messages
    assert {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()} == before
