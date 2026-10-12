"""Large completed reasoning/tool batches must be summarized as a whole."""
import copy
import json
from gum_code.context import _select_recent_batches, _split_protocol_batches, _source_records
from gum_code.token_counting import EstimatedTokenCounter


def history(complete=True):
    messages = [
        {'role': 'user', 'content': 'Build inventory'},
        {'role': 'assistant', 'content': None, 'reasoning_content': 'synthetic-private' * 7000,
         'tool_calls': [{'id': 'plan', 'type': 'function', 'function': {'name': 'todo_write', 'arguments': '{}'}}]},
    ]
    if complete:
        messages.append({'role': 'tool', 'tool_call_id': 'plan', 'content': 'Plan saved'})
    return messages


def test_completed_oversized_batch_moves_whole_to_summary():
    messages = history()
    original = copy.deepcopy(messages)
    batches = _split_protocol_batches(messages)
    selected = _select_recent_batches({'role': 'system', 'content': 'system'}, batches, [], EstimatedTokenCounter(), 12800)
    assert selected == {0}
    older = [batch for i, (batch, _) in enumerate(batches) if i not in selected]
    assert len(older) == 1 and [m['role'] for m in older[0]] == ['assistant', 'tool']
    records, _ = _source_records([(batch, True) for batch in older], 0)
    assert 'synthetic-private' not in json.dumps(records)
    assert messages == original


def test_unfinished_large_batch_remains_protected():
    batches = _split_protocol_batches(history(False))
    selected = _select_recent_batches({'role': 'system', 'content': 'system'}, batches, [], EstimatedTokenCounter(), 12800)
    assert selected == {0, 1}


def test_small_recent_batch_keeps_exact_reasoning_and_results():
    messages = history()
    messages[1]['reasoning_content'] = 'synthetic-small'
    batches = _split_protocol_batches(messages)
    selected = _select_recent_batches({'role': 'system', 'content': 'system'}, batches, [], EstimatedTokenCounter(), 12800)
    assert selected == {0, 1}
    assert batches[1][0] == messages[1:]


def test_desktop_budget_uses_advertised_input_limit_or_user_fallback():
    import io
    from types import SimpleNamespace
    from gum_code.desktop import DesktopHost
    from gum_code.cli import OneShotHost
    from gum_code.config import Settings, resolve_context_budget
    settings = Settings(api_key='YOUR_TOKEN_HERE', base_url='https://example.invalid/v1', model='synthetic')
    host = DesktopHost(io.StringIO(), io.StringIO())
    client = SimpleNamespace(models=SimpleNamespace(retrieve=lambda model: SimpleNamespace(max_input_tokens=200000, context_window=300000)))
    assert resolve_context_budget(host.context_window(settings, client), None) == 160000
    unknown = SimpleNamespace(models=SimpleNamespace(retrieve=lambda model: SimpleNamespace()))
    assert resolve_context_budget(host.context_window(settings, unknown), None) == 800000
    import pytest
    with pytest.raises(RuntimeError, match='无法确定'):
        OneShotHost().context_window(settings, unknown)


def test_compaction_fits_original_failure_shape_and_archives_exact_history(tmp_path):
    from types import SimpleNamespace
    from gum_code.context import compact_history
    messages = [{'role':'system','content':'Synthetic system'}] + history()
    original = copy.deepcopy(messages)
    requests = []
    def create(**kwargs):
        payload = json.loads(kwargs['messages'][1]['content'])
        requests.append(payload)
        capsule = {'version':1, 'current_goal':[{'id':'goal-0001','status':'active','text':'Build inventory','reason':'latest request','source_ref':payload['sources'][-1]['source_ref'],'superseded_by':None}]}
        for section in ['requirements','constraints','decisions','code_state','verification','work_items']:
            capsule[section] = []
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(capsule)))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    result, archive = compact_history(client, 'synthetic', tmp_path, messages, 'Build inventory', tools=[], token_counter=EstimatedTokenCounter(), max_context_tokens=12800)
    assert EstimatedTokenCounter().count_request(result, []) < 12800
    assert messages == original
    assert [json.loads(line) for line in (tmp_path/archive).read_text().splitlines()] == original
    assert 'synthetic-private' not in json.dumps(requests)
    assert not any(m.get('tool_calls') or m.get('role') == 'tool' for m in result)
