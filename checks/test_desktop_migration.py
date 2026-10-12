"""Import-only legacy conversion validates private staging and leaves originals intact."""
import io
import json
from uuid import uuid4

import pytest

from gum_code.desktop import run
from gum_code.session import SESSION_VERSION, load_session, save_session


def invoke(request):
    output = io.StringIO()
    code = run(io.StringIO(json.dumps(request) + '\n'), output)
    return code, [json.loads(line) for line in output.getvalue().splitlines()]


@pytest.mark.parametrize('version', [1, 2])
def test_legacy_staged_conversion_opens_in_desktop_and_preserves_ids_and_source(tmp_path, version):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    source = tmp_path / 'original.jsonl'
    ident, input_id = str(uuid4()), str(uuid4())
    messages = [
        {'role': 'system', 'content': 'Synthetic system'},
        {'role': 'user', 'content': 'First requirement'},
        {'role': 'assistant', 'content': 'Previous answer'},
        {'role': 'user', 'name': 'input_' + input_id.replace('-', '_'), 'content': 'Pending correction'},
    ]
    snapshot = dict(version=version, workspace=str(workspace), model='synthetic', messages=messages)
    if version == 2:
        snapshot['title'] = 'Legacy title'
    source.write_text(json.dumps(snapshot))
    before = source.read_bytes()
    staging = tmp_path / '.workbench-import-synthetic'
    staging.mkdir(mode=0o700)
    staged = staging / (ident + '.jsonl')
    staged.write_bytes(before)
    request = dict(v=1, action='migrate_history', sessionId=ident, turnId=str(uuid4()),
                   workspace=str(workspace), sessionPath=str(staged))
    code, events = invoke(request)
    assert code == 0
    assert [e['type'] for e in events] == ['history_migrated', 'finished']
    assert events[0]['converted'] is True
    assert events[0]['sessionId'] == ident
    assert 'Pending correction' not in str(events)
    assert source.read_bytes() == before
    assert staged.read_bytes() == before
    converted = staging / (ident + '.converted.jsonl')
    data = load_session(converted, allow_pending_user_tail=True)
    assert data.version == SESSION_VERSION
    assert data.messages == messages
    converted.replace(staged)
    code, history = invoke({**request, 'action': 'history', 'turnId': str(uuid4())})
    assert code == 0
    assert history[0]['sessionId'] == ident
    assert history[0]['messages'][-1] == dict(role='user', content='Pending correction', inputId=input_id)
    assert source.read_bytes() == before


def test_staged_v3_is_validated_without_conversion_or_rewrite(tmp_path):
    stage = tmp_path / '.workbench-import-current'
    stage.mkdir(mode=0o700)
    ident = str(uuid4())
    path = stage / (ident + '.jsonl')
    messages = [{'role': 'system', 'content': 'Synthetic system'}, {'role': 'assistant', 'content': 'Previous answer'}]
    save_session(path, tmp_path, 'synthetic', messages)
    before = path.read_bytes()
    request = dict(v=1, action='migrate_history', sessionId=ident, turnId=str(uuid4()),
                   workspace=str(tmp_path), sessionPath=str(path))
    code, events = invoke(request)
    assert code == 0
    assert events[0]['converted'] is False
    assert path.read_bytes() == before
    assert not path.with_name(ident + '.converted.jsonl').exists()
    # Valid event envelopes must still pass the actual session replay contract.
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[-1]['data']['message']['role'] = 'invalid_role'
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    code, events = invoke(request)
    assert code == 1
    assert events[-1]['status'] == 'failed'
    assert 'invalid_role' not in str(events)


def test_conversion_refuses_regular_profile_and_existing_destination(tmp_path):
    ident = str(uuid4())
    source = tmp_path / (ident + '.jsonl')
    source.write_text(json.dumps(dict(version=1, workspace=str(tmp_path), model='synthetic',
                                    messages=[{'role': 'system', 'content': 'Synthetic'}])))
    before = source.read_bytes()
    request = dict(v=1, action='migrate_history', sessionId=ident, turnId=str(uuid4()),
                   workspace=str(tmp_path), sessionPath=str(source))
    code, _ = invoke(request)
    assert code == 1
    assert source.read_bytes() == before
    stage = tmp_path / '.workbench-import-collision'
    stage.mkdir(mode=0o700)
    staged = stage / source.name
    staged.write_bytes(before)
    target = stage / (ident + '.converted.jsonl')
    target.write_text('Preserve this file')
    code, _ = invoke({**request, 'sessionPath': str(staged)})
    assert code == 1
    assert target.read_text() == 'Preserve this file'
    assert staged.read_bytes() == before
