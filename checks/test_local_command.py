import os
from pathlib import Path
import sys
import threading
import time

import pytest

from gum_code.local_command import LocalCommandConfig, passed_tests
from gum_code.tools.command import run_command, _validate_run_command
from gum_code.tools.filesystem import FileChange
from gum_code.tools.registry import execute_tool
from gum_code.events import _command_failure


def test_approval_denial_does_not_execute(tmp_path):
    config = LocalCommandConfig(tmp_path)
    (tmp_path / 'touch.py').write_text("from pathlib import Path\nPath('made').touch()")
    value = execute_tool(tmp_path, 'run_command', '{"argv":["python3","touch.py"]}',
                         command_sandbox=config, approval_callback=lambda *args: False)
    assert 'denied' in value.lower()
    assert not (tmp_path / 'made').exists()


def test_real_execution_clean_env_and_interpreter(tmp_path, monkeypatch):
    monkeypatch.setenv('SYNTHETIC_API_KEY', 'YOUR_TOKEN_HERE')
    (tmp_path / 'envcheck.py').write_text("import os,sys\nassert 'SYNTHETIC_API_KEY' not in os.environ\nassert 'PYTHONPATH' not in os.environ\nprint(sys.executable)\n")
    output = run_command(tmp_path, ['python3', 'envcheck.py'], sandbox=LocalCommandConfig(tmp_path))
    assert sys.executable in output and '"exit_code": 0' in output


def test_real_tests_then_source_change_invalidates(tmp_path):
    (tmp_path / 'test_a.py').write_text('import unittest\nclass TestA(unittest.TestCase):\n def test_a(self): self.assertEqual(1,1)\n')
    config = LocalCommandConfig(tmp_path)
    config.on_file_change(FileChange('test_a.py', None, b'import unittest'))
    assert config.needs_check()
    output = run_command(tmp_path, ['python3', '-m', 'unittest', 'discover', '-v'], sandbox=config)
    assert 'Ran 1 test' in output
    assert not config.needs_check()
    (tmp_path / 'new.go').write_text('package main')
    assert config.needs_check()


def test_failure_and_no_tests_do_not_verify(tmp_path):
    config = LocalCommandConfig(tmp_path)
    config.changed = True
    run_command(tmp_path, ['python3', '-m', 'unittest', 'discover'], sandbox=config)
    assert config.needs_check()
    (tmp_path / 'test_fail.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_fail(self): self.fail("synthetic")')
    output = run_command(tmp_path, ['python3', '-m', 'unittest', 'discover'], sandbox=config)
    assert _command_failure({'tool_name':'run_command','result':output})
    assert config.needs_check()


@pytest.mark.parametrize('fixture', ['seed.db', 'seed.sqlite', 'input.md', 'input.rst'])
def test_fixture_change_invalidates_passing_evidence(tmp_path, fixture):
    (tmp_path / fixture).write_text('original fixture')
    (tmp_path / 'test_ok.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n')
    config = LocalCommandConfig(tmp_path)
    config.changed = True
    run_command(tmp_path, ['python3', '-m', 'unittest', 'discover'], sandbox=config)
    assert not config.needs_check()
    (tmp_path / fixture).write_text('changed fixture')
    assert config.needs_check()


def test_docs_only_change_does_not_require_program_tests(tmp_path):
    config = LocalCommandConfig(tmp_path)
    (tmp_path / 'README.md').write_text('Documentation only')
    assert not config.needs_check()


def test_unknown_snapshot_command_mutation_requires_verification(tmp_path):
    os.mkfifo(tmp_path / 'unscannable')
    (tmp_path / 'mutate.py').write_text("from pathlib import Path\nPath('app.py').write_text('value = 2')\n")
    config = LocalCommandConfig(tmp_path)
    assert config.initial is None
    assert not config.needs_check()  # Reading or answering alone remains possible.
    output = run_command(tmp_path, ['python3', 'mutate.py'], sandbox=config)
    assert 'value = 2' == (tmp_path / 'app.py').read_text()
    assert 'exit_code' in output
    assert config.needs_check()


@pytest.mark.parametrize('output', ['Ran 0 tests in 0s\nOK\n', 'Ran 2 tests in 0s\nOK (skipped=2)\n', 'Ran 1 test in 0s\nFAILED\n'])
def test_empty_skipped_failed_reports(output):
    assert not passed_tests(['python3', '-m', 'unittest'], output)


def test_timeout_output_limit_and_parent_exit_clean_children(tmp_path):
    (tmp_path/'hang.py').write_text('import time\ntime.sleep(20)')
    output = run_command(tmp_path, ['python3','hang.py'], timeout_seconds=1, sandbox=LocalCommandConfig(tmp_path))
    assert '"reason": "timeout"' in output
    assert '本机' in _command_failure({'tool_name':'run_command','result':output})
    (tmp_path/'flood.py').write_text('while True: print("x" * 8192)')
    output = run_command(tmp_path, ['python3','flood.py'], sandbox=LocalCommandConfig(tmp_path))
    assert 'output_limit' in output and len(output) < 132000
    (tmp_path/'fork.py').write_text('import subprocess,sys\nfrom pathlib import Path\np=subprocess.Popen([sys.executable,"-c","import time; time.sleep(20)"])\nPath("childpid").write_text(str(p.pid))\n')
    start=time.monotonic()
    output=run_command(tmp_path,['python3','fork.py'],sandbox=LocalCommandConfig(tmp_path))
    assert time.monotonic()-start<3 and '"exit_code": 0' in output
    pid=int((tmp_path/'childpid').read_text())
    import subprocess
    state=subprocess.run(['ps','-o','stat=','-p',str(pid)],capture_output=True,text=True).stdout.strip()
    assert not state or state.startswith('Z')


def test_cancel_and_closed_stdout(tmp_path):
    (tmp_path/'hang.py').write_text('import os,time\nos.close(1)\nos.close(2)\ntime.sleep(.2)')
    output=run_command(tmp_path,['python3','hang.py'],sandbox=LocalCommandConfig(tmp_path))
    assert '"exit_code": 0' in output
    event=threading.Event();event.set()
    with pytest.raises(KeyboardInterrupt):
        run_command(tmp_path,['python3','hang.py'],sandbox=LocalCommandConfig(tmp_path),cancel_event=event)


def test_scan_fail_closed_fifo_and_symlink(tmp_path):
    os.mkfifo(tmp_path/'input')
    config=LocalCommandConfig(tmp_path);config.changed=True
    assert config.needs_check()
    (tmp_path/'input').unlink();(tmp_path/'input').symlink_to('/etc/hosts')
    assert config.needs_check()


@pytest.mark.parametrize('during_scan', [False, True])
def test_cancel_before_spawn_never_starts_process(tmp_path, monkeypatch, during_scan):
    import subprocess
    config = LocalCommandConfig(tmp_path)
    event = threading.Event()
    if during_scan:
        def snapshot():
            event.set()
            return {}
        monkeypatch.setattr(config, 'snapshot', snapshot)
    else:
        event.set()
    def unexpected_spawn(*args, **kwargs):
        pytest.fail('Stopped command must not launch a process')
    monkeypatch.setattr(subprocess, 'Popen', unexpected_spawn)
    results = []
    with pytest.raises(KeyboardInterrupt):
        run_command(tmp_path, ['python3', '--version'], sandbox=config,
                    cancel_event=event, result_callback=results.append)
    assert len(results) == 1 and results[0].started is False
    assert results[0].reason == 'interrupted'


def test_cli_still_requires_docker_and_long_local_args_rejected(tmp_path):
    with pytest.raises(ValueError,match='Docker'):
        _validate_run_command(tmp_path,['python3','--version'])
    with pytest.raises(ValueError):
        _validate_run_command(tmp_path,['python3','x'*4100],sandbox=LocalCommandConfig(tmp_path))


def test_node_real_runner_and_arbitrary_success_is_not_evidence(tmp_path):
    (tmp_path/'test.mjs').write_text("import test from 'node:test'; import assert from 'node:assert/strict'; test('works',()=>assert.equal(1,1));")
    config=LocalCommandConfig(tmp_path);config.changed=True
    run_command(tmp_path,['python3','--version'],sandbox=config)
    assert config.needs_check()
    output=run_command(tmp_path,['node','--test','test.mjs'],sandbox=config)
    assert '"exit_code": 0' in output
    assert not config.needs_check()


def test_agent_completion_recovery_requires_current_real_test(tmp_path):
    import json
    from types import SimpleNamespace as NS
    from gum_code.agent import run_agent
    calls=[]
    def response(name=None,args=None):
        tool = NS(id=f'call_{len(calls)}',type='function',function=NS(name=name,arguments=json.dumps(args))) if name else None
        calls.append(tool)
        return NS(choices=[NS(finish_reason='tool_calls' if tool else 'stop',message=NS(content='' if tool else 'done',tool_calls=[tool] if tool else None))],usage=None)
    replies=iter([
        response('write_file',{'path':'test_example.py','content':'import unittest\nclass T(unittest.TestCase):\n def test_a(self): self.assertEqual(1,1)\n'}),
        response(),  # Host must reject premature completion and request real tests.
        response('run_command',{'argv':['python3','-m','unittest','discover']}),
        response(),
    ])
    requests=[]
    def create(**kwargs):
        requests.append(str(kwargs['messages']))
        for message in kwargs['messages']:
            if str(message.get('content', '')).startswith('[Harness completion recovery] '):
                assert message['role'] == 'user', 'Thinking/tools rejects synthetic assistant reminders'
        return next(replies)
    class Counter:
        def count_request(self,*args):return 100
    events=[]
    run_agent(client=NS(chat=NS(completions=NS(create=create))),model='synthetic',workspace=tmp_path,
              user_query='Write and test',max_steps=8,max_context_tokens=800000,
              allow_run_command=True,command_sandbox=LocalCommandConfig(tmp_path),allow_subagent=False,
              approval_callback=lambda *args:True,token_counter=Counter(),event_callback=lambda kind,value:events.append((kind,value)))
    assert len(requests)==4 and '当前版本缺少通过的测试执行证据' in requests[2]
    assert [payload['status'] for kind,payload in events if kind=='task_outcome']==['completed']


def test_agent_denied_verification_ends_incomplete(tmp_path):
    from types import SimpleNamespace as NS
    from gum_code.agent import run_agent
    import json
    responses=iter([NS(choices=[NS(finish_reason='tool_calls',message=NS(content='',tool_calls=[NS(id='test',type='function',function=NS(name='run_command',arguments=json.dumps({'argv':['python3','-m','unittest','discover']})))]))],usage=None)] +
        [NS(choices=[NS(finish_reason='stop',message=NS(content='done',tool_calls=None))],usage=None)]*4)
    class Counter:
        def count_request(self,*args):return 100
    events=[]
    run_agent(client=NS(chat=NS(completions=NS(create=lambda **kwargs:next(responses)))),model='synthetic',workspace=tmp_path,
              user_query='Test',max_steps=8,max_context_tokens=800000,
              allow_run_command=True,command_sandbox=LocalCommandConfig(tmp_path),allow_subagent=False,
              approval_callback=lambda *args:False,token_counter=Counter(),event_callback=lambda kind,value:events.append((kind,value)))
    assert [payload['status'] for kind,payload in events if kind=='task_outcome']==['incomplete']


def test_finder_minimal_path_uses_installed_nvm_node(tmp_path, monkeypatch):
    import shutil
    from gum_code.local_command import _host_path
    actual_node=shutil.which('node')
    assert actual_node
    home=tmp_path/'home'; workspace=tmp_path/'workspace';workspace.mkdir()
    for version in ['v9.1.0','v24.19.0']:
        binary=home/'.nvm/versions/node'/version/'bin';binary.mkdir(parents=True)
        (binary/'node').symlink_to(actual_node)
    monkeypatch.setenv('HOME',str(home));monkeypatch.setenv('PATH','/usr/bin:/bin')
    assert _host_path(workspace).endswith('v24.19.0/bin')
    (workspace/'test.mjs').write_text("import test from 'node:test';test('ok',()=>{});")
    config=LocalCommandConfig(workspace);config.changed=True
    output=run_command(workspace,['node','--test','test.mjs'],sandbox=config)
    assert '"exit_code": 0' in output and not config.needs_check()
