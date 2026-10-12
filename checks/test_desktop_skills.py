import io
import json
from pathlib import Path

import pytest

from gum_code.desktop import DesktopHost, run
from gum_code.skills import SkillRuntime, discover_skills, MAX_RESOURCE_BYTES


def skill(root, name='demo'):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / 'SKILL.md').write_text('---\nname: Demo\ndescription: A synthetic skill\n---\nFollow the synthetic instruction.\n')
    (directory / 'guide.txt').write_text('abcdef')
    return directory


@pytest.mark.parametrize("prefix", ["/", "$"])
def test_discover_explicit_load_resources_and_sticky(tmp_path, prefix):
    skill(tmp_path)
    skill(tmp_path, 'bad name')
    skill(tmp_path, '.hidden')
    (tmp_path / 'linked').symlink_to(tmp_path / 'demo', target_is_directory=True)
    assert [s.name for s in discover_skills(tmp_path)] == ['demo']
    activated = []
    runtime = SkillRuntime(root=tmp_path, activation_callback=activated.append)
    assert json.loads(runtime.find('synthetic'))['skills'][0]['name'] == 'demo'
    assert runtime.read_resource('demo', 'guide.txt').startswith('Error:')
    runtime.start_task(prefix + 'demo use the instruction')
    assert len(activated) == 1
    assert 'Follow the synthetic instruction.' in runtime.apply_to_system('base')
    assert json.loads(runtime.read_resource('demo', 'guide.txt', offset=2, length=2)) == {'content': 'cd', 'offset': 2, 'has_more': True}
    resumed = SkillRuntime(root=tmp_path, sticky_names=runtime.sticky_names)
    resumed.start_task('continue without mentioning it')
    assert 'demo' in resumed.active
    assert json.loads(resumed.load('demo'))['script_execution'] == 'unsupported'
    assert resumed.read_resource('demo', '../outside').startswith('Error:')
    (tmp_path / 'demo' / 'outside').symlink_to(tmp_path / 'bad name' / 'SKILL.md')
    assert resumed.read_resource('demo', 'outside').startswith('Error:')
    (tmp_path / 'demo' / 'big.txt').write_bytes(b'x' * (MAX_RESOURCE_BYTES + 1))
    assert '上限' in resumed.read_resource('demo', 'big.txt')


@pytest.mark.parametrize("prefix", ["/", "$"])
def test_long_mention_does_not_activate_partial_name(tmp_path, prefix):
    skill(tmp_path, 'a' * 128)
    runtime = SkillRuntime(root=tmp_path)
    runtime.start_task(prefix + 'a' * 129)
    assert not runtime.active


def test_catalog_protocol_no_provider_session_or_model(tmp_path, monkeypatch):
    root = tmp_path / 'skills'
    skill(root)
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    output = io.StringIO()
    def forbidden(*args, **kwargs):
        raise AssertionError('Model must not be called')
    assert run(io.StringIO('{"v":1,"action":"skills"}\n'), output, turn_runner=forbidden) == 0
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert messages[0]['skills'] == [{'name': 'demo', 'displayName': 'Demo', 'description': 'A synthetic skill'}]
    assert messages[-1]['status'] == 'completed'
    assert messages[0]['sessionId'] is None
    assert set(root.iterdir()) == {root / 'demo'}


def test_activation_progress_contains_only_safe_name():
    output = io.StringIO()
    host = DesktopHost(io.StringIO(), output)
    host.event('skill_activated', {'name': 'demo', 'content': 'never expose instructions'})
    event = json.loads(output.getvalue())
    assert event['type'] == 'progress' and event['status'] == 'completed'
    assert event['detail'] == 'demo'
    assert 'instructions' not in output.getvalue()


def test_catalog_metadata_redacted_and_bounded(tmp_path, monkeypatch):
    root = tmp_path / 'skills'
    directory = skill(root)
    (directory / 'SKILL.md').write_text('---\nname: ' + 'D' * 200 + '\ndescription: token=YOUR_TOKEN_HERE ' + 'x' * 1000 + '\n---\nNever emit body marker\n')
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    output = io.StringIO()
    assert run(io.StringIO('{"v":1,"action":"skills"}\n'), output) == 0
    entry = json.loads(output.getvalue().splitlines()[0])['skills'][0]
    assert len(entry['displayName']) == 120
    assert len(entry['description']) == 500
    assert 'YOUR_TOKEN_HERE' not in output.getvalue()
    assert 'Never emit body marker' not in output.getvalue()


@pytest.mark.parametrize("text", [
    "https://example.com/demo", "./demo", "../demo", "~/demo",
    "/demo/guide.txt", "/demo/", "prefix/demo", "//demo", "/demo中文",
])
def test_slash_paths_and_name_fragments_do_not_activate(tmp_path, text):
    skill(tmp_path)
    runtime = SkillRuntime(root=tmp_path)
    runtime.start_task(text)
    assert not runtime.active


@pytest.mark.parametrize("text", ["/demo", "请用 /demo 帮我", "换行\n/demo，处理", "\t/demo!"])
def test_slash_whitespace_and_punctuation_boundaries(tmp_path, text):
    skill(tmp_path)
    runtime = SkillRuntime(root=tmp_path)
    runtime.start_task(text)
    assert tuple(runtime.active) == ('demo',)


def test_dotted_name_is_complete_and_mixed_mentions_deduplicate(tmp_path):
    skill(tmp_path)
    skill(tmp_path, 'demo.ext')
    runtime = SkillRuntime(root=tmp_path)
    runtime.start_task('/demo.ext $demo /demo')
    assert tuple(runtime.active) == ('demo.ext', 'demo')
    assert runtime.sticky_names == ('demo.ext', 'demo')


@pytest.mark.parametrize("pending_live", [False, True])
@pytest.mark.parametrize("query, expected", [("/fourth 请继续", ("fourth",)), ("这次不用技能", ())])
def test_desktop_resume_does_not_inherit_sticky_skills(tmp_path, monkeypatch, query, expected, pending_live):
    from gum_code import cli
    from gum_code.session import save_session
    from types import SimpleNamespace as NS
    from uuid import uuid4
    root = tmp_path / 'home' / 'skills'
    for name in ['first', 'second', 'third', 'fourth']:
        skill(root, name)
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'home'))
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    session = tmp_path / 'session.jsonl'
    messages = [{'role':'system','content':'base'}, {'role':'user','content':'old'}, {'role':'assistant','content':'done'}]
    if pending_live: messages.append({'role':'user','content':'keep this requirement','name':'input_'+str(uuid4()).replace('-','_')})
    save_session(session, workspace, 'synthetic', messages, sticky_skills=('first','second','third'))
    monkeypatch.setattr(cli, 'OpenAI', lambda **kwargs: NS())
    monkeypatch.setattr(cli, 'load_token_counter', lambda model: NS(count_request=lambda *args:100))
    captured = []
    def fake_agent(**kwargs):
        if pending_live: assert kwargs['initial_messages'][-1]['content'] == 'keep this requirement'
        runtime = kwargs['skill_runtime']
        assert runtime.sticky_names == ()
        runtime.start_task(kwargs['user_query'])
        captured.append(tuple(runtime.active))
        kwargs['event_callback']('task_outcome', {'status':'completed','reason':'answered','step':1,'self_check_status':'disabled','changed':False,'summary':''})
        return 'done'
    monkeypatch.setattr(cli, 'run_agent', fake_agent)
    request = dict(v=1, action='turn', sessionId=str(uuid4()), turnId=str(uuid4()), workspace=str(workspace),
                   sessionPath=str(session), text=query, trusted=True, live=True,
                   provider=dict(baseUrl='https://invalid.example',model='synthetic',apiKey='YOUR_TOKEN_HERE',contextWindow=1000000))
    output = io.StringIO()
    errors = []
    def runner(args, host):
        try: return cli._run_one_shot(args, host=host)
        except Exception as error:
            errors.append(repr(error))
            raise
    assert run(io.StringIO(json.dumps(request)+'\n'), output, turn_runner=runner) == 0, errors
    assert captured == [expected]


def test_skill_limit_has_actionable_desktop_error(tmp_path):
    from uuid import uuid4
    request = dict(v=1, action='turn', sessionId=str(uuid4()), turnId=str(uuid4()), workspace=str(tmp_path),
                   sessionPath=str(tmp_path/'s.jsonl'), text='/a /b /c /d', trusted=True,
                   provider=dict(baseUrl='https://invalid.example',model='synthetic',apiKey='YOUR_TOKEN_HERE',contextWindow=1000000))
    def runner(args, host):
        SkillRuntime(root=tmp_path).start_task(args.task)
    output = io.StringIO()
    assert run(io.StringIO(json.dumps(request)+'\n'), output, turn_runner=runner) == 1
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert next(m['message'] for m in messages if m['type']=='error') == '每条消息最多使用 3 个技能，请移除一个后重试。'
