"""CLI presentation preserves narrow terminals, safe text and REPL dispatch."""
from io import StringIO
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from prompt_toolkit.formatted_text import to_formatted_text
from rich.console import Console
from rich.cells import cell_len

from gum_code import interactive
from gum_code.events import RichEventRenderer


@pytest.mark.parametrize('width', [40, 80, 120])
def test_startup_and_status_preserve_information_at_terminal_width(width):
    stream = StringIO()
    runtime = NS(
        console=Console(file=stream, width=width, no_color=True),
        workspace=Path.home() / 'Desktop' / '练习项目' / ('long-project-' * 9),
        settings=NS(model='demo-model'), title='', session_path=None,
        args=NS(allow_run_command=False, allow_subagent=True),
        self_check_runtime=None, last_task_outcome=None, messages=[],
    )
    interactive.InteractiveRuntime.show_status(runtime, show_title=True)
    startup = stream.getvalue()
    assert 'GuMi-Code' in startup and 'demo-model' in startup
    bird = interactive._SMALL_STARTUP_BIRD if width < 60 else interactive._STARTUP_BIRD
    assert bird in startup
    assert '/help' in startup and '/status' in startup
    assert '上下文' in startup and '0%' in startup
    assert '命令关闭' in startup
    assert '\x1b' not in startup
    assert all(cell_len(line) <= width for line in startup.splitlines())
    stream.seek(0); stream.truncate()
    interactive.InteractiveRuntime.show_status(runtime)
    assert 'Session' in stream.getvalue() and 'Sandbox' in stream.getvalue()
    assert bird not in stream.getvalue()


def test_repl_prompt_brand_and_command_dispatch(monkeypatch):
    prompts, options = [], {}
    class FakePrompt:
        def __init__(self, **kwargs): options.update(kwargs)
        def prompt(self, message):
            prompts.append(''.join(text for _, text in to_formatted_text(message)))
            return '/exit'
    monkeypatch.setattr(interactive, 'PromptSession', FakePrompt)
    runtime = NS(show_status=Mock(), close=Mock(), console=Console(file=StringIO(), no_color=True))
    assert interactive.run_repl(runtime) == 0
    assert prompts == ['GuMi> ']
    assert options['multiline'] is True
    runtime.close.assert_called_once()


def test_answer_has_brand_and_strips_terminal_controls():
    out = StringIO()
    RichEventRenderer(Console(file=out, width=40, no_color=True)).render_answer('Hello\x1b[2J **world**')
    text = out.getvalue()
    assert 'GuMi' in text and 'world' in text
    assert '\x1b' not in text
