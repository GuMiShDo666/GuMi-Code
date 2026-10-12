import copy
import json
from types import SimpleNamespace as NS
import pytest
from gum_code.agent import run_agent
from gum_code.session import IN_FLIGHT_MARKER_KEY, load_session, save_session
from gum_code.subagent import run_read_only_subagent

class Counter:
    def count_request(self, *args): return 100

def test_old_host_messages_migrate_without_altering_model_reasoning(tmp_path):
    messages = [
        {'role':'system','content':'base'}, {'role':'user','content':'old'},
        {'role':'assistant','content':'candidate','reasoning_content':'original reasoning'},
        {'role':'assistant','content':'[Harness completion recovery] continue'},
        {'role':'assistant','content':'[Harness review] 只读复核当前变更与验证范围。','tool_calls':[
            {'id':'host_review_test','type':'function','function':{'name':'task','arguments':'{}'}}]},
        {'role':'tool','tool_call_id':'host_review_test','content':'reviewed'},
    ]
    original = copy.deepcopy(messages)
    def create(**kwargs):
        sent = kwargs['messages']
        assert next(m for m in sent if m.get('content')=='candidate')['reasoning_content']=='original reasoning'
        assert next(m for m in sent if m.get('content')=='[Harness completion recovery] continue')['role']=='user'
        assert next(m for m in sent if m.get('tool_calls'))['reasoning_content']==''
        return NS(choices=[NS(finish_reason='stop', message=NS(content='done', tool_calls=None, reasoning_content='final reasoning'))],usage=None)
    assert run_agent(client=NS(chat=NS(completions=NS(create=create))), model='synthetic', workspace=tmp_path,
                     user_query='continue', initial_messages=messages, max_steps=2, max_context_tokens=800000,
                     allow_subagent=False, token_counter=Counter()) == 'done'
    assert messages == original


def tool_call(name, arguments, ident=None):
    return NS(id=ident or name, type='function', function=NS(
        name=name, arguments=arguments if isinstance(arguments, str) else json.dumps(arguments)))


def response(calls=None, finish='tool_calls', text=None):
    return NS(choices=[NS(finish_reason=finish, message=NS(
        content=text, reasoning_content='synthetic reasoning', tool_calls=calls))], usage=None)


def test_truncated_batch_has_no_effects_and_is_durable_before_retry(tmp_path, monkeypatch):
    import gum_code.agent as agent

    calls = [
        tool_call('write_file', {'path': 'blocked.txt', 'content': 'must not be written'}),
        tool_call('write_file', '{"path":', 'partial'),
        tool_call('task', {'description': 'must not delegate'}),
        tool_call('todo_write', {'items': []}),
        tool_call('compact', {}),
        tool_call('load_skill', {'name': 'blocked'}),
        tool_call('plan_control', {'action': 'list'}),
        tool_call('memory_write', {}),
        tool_call('run_command', {'argv': ['node', '--test']}),
    ]
    def unexpected(*args, **kwargs):
        pytest.fail('Truncated response reached a tool handler or approval')
    monkeypatch.setattr(agent, 'execute_tool', unexpected)
    monkeypatch.setattr(agent, '_execute_task_batch', unexpected)
    monkeypatch.setattr(agent, '_execute_todo_write', unexpected)
    monkeypatch.setattr(agent, 'compact_history', unexpected)
    session_path = tmp_path / 'session.jsonl'
    requests, snapshots, events = [], [], []
    def snapshot(messages):
        snapshots.append(copy.deepcopy(messages))
        save_session(session_path, tmp_path, 'synthetic', messages)
    def create(**kwargs):
        requests.append(copy.deepcopy(kwargs['messages']))
        if len(requests) == 1:
            return response(calls, 'length')
        restored = load_session(session_path, allow_pending_user_tail=True)
        results = [m for m in restored.messages if m['role'] == 'tool']
        assert [m['tool_call_id'] for m in results] == [call.id for call in calls]
        assert all('not executed' in m['content'] for m in results)
        return response(finish='stop', text='done')
    answer = run_agent(client=NS(chat=NS(completions=NS(create=create))), model='synthetic',
        workspace=tmp_path, user_query='synthetic', max_steps=3, token_counter=Counter(),
        approval_callback=unexpected, message_snapshot_callback=snapshot,
        event_callback=lambda kind, data: events.append((kind, data)))
    assert answer == 'done' and len(requests) == 2
    assert not (tmp_path / 'blocked.txt').exists()
    for saved in snapshots:
        if any(m.get('tool_calls') for m in saved):
            assert len([m for m in saved if m['role'] == 'tool']) == len(calls)
        assert not any(m.get(IN_FLIGHT_MARKER_KEY) for m in saved)
    assert next(m for m in requests[1] if m.get('tool_calls'))['reasoning_content'] == 'synthetic reasoning'
    assert not any(kind == 'tool_execution' for kind, _ in events)
    assert [data['tool_call_id'] for kind, data in events if kind == 'tool_result'] == [c.id for c in calls]


def test_complete_reissued_write_executes_once_after_truncation(tmp_path):
    calls = [tool_call('write_file', {'path': 'output.txt', 'content': 'complete'})]
    replies = iter([response(calls, 'length'), response(calls), response(finish='stop', text='done')])
    events = []
    def create(**kwargs):
        reply = next(replies)
        if reply.choices[0].finish_reason == 'tool_calls':
            assert not (tmp_path / 'output.txt').exists()
        return reply
    assert run_agent(client=NS(chat=NS(completions=NS(create=create))), model='synthetic',
        workspace=tmp_path, user_query='synthetic', max_steps=3, token_counter=Counter(),
        allow_subagent=False, event_callback=lambda kind, data: events.append((kind, data))) == 'done'
    assert (tmp_path / 'output.txt').read_text() == 'complete'
    assert len([1 for kind, _ in events if kind == 'tool_execution']) == 1


@pytest.mark.parametrize('max_steps,expected_calls,reason', [(1, 1, 'step_budget'), (10, 3, 'completion_stalled')])
def test_repeated_truncated_calls_stop_without_writes(tmp_path, max_steps, expected_calls, reason):
    requests, outcomes = [], []
    def create(**kwargs):
        requests.append(1)
        return response([tool_call('write_file', {'path': 'blocked.txt', 'content': 'blocked'},
                                   f'call-{len(requests)}')], 'length')
    run_agent(client=NS(chat=NS(completions=NS(create=create))), model='synthetic',
        workspace=tmp_path, user_query='synthetic', max_steps=max_steps, token_counter=Counter(),
        allow_subagent=False, event_callback=lambda kind, data: outcomes.append(data) if kind == 'task_outcome' else None)
    assert len(requests) == expected_calls
    assert not (tmp_path / 'blocked.txt').exists()
    assert outcomes[-1]['status'] == 'incomplete' and outcomes[-1]['reason'] == reason


def test_plain_text_truncation_still_continues(tmp_path):
    replies = iter([response(finish='length', text='First part.'), response(finish='stop', text='Second part.')])
    answer = run_agent(client=NS(chat=NS(completions=NS(create=lambda **kwargs: next(replies)))),
        model='synthetic', workspace=tmp_path, user_query='synthetic', max_steps=3,
        token_counter=Counter(), allow_subagent=False)
    assert 'First part.' in answer and 'Second part.' in answer


@pytest.mark.parametrize('name', ['read_file', 'compact', 'submit_report'])
def test_child_rejects_truncated_tools_before_complete_report(tmp_path, monkeypatch, name):
    import gum_code.subagent as child

    report = {'summary': 'complete report', 'findings': [], 'evidence': [], 'risks': []}
    arguments = {'read_file': {'path': 'missing.txt'}, 'compact': {}, 'submit_report': report}[name]
    requests, executions = [], []
    def unexpected(*args, **kwargs):
        pytest.fail('Truncated child response executed a tool')
    monkeypatch.setattr(child, 'execute_tool', unexpected)
    monkeypatch.setattr(child, 'compact_history', unexpected)
    def create(**kwargs):
        requests.append(copy.deepcopy(kwargs['messages']))
        if len(requests) == 1:
            return response([tool_call(name, arguments)], 'length')
        assert requests[-1][-1]['role'] == 'tool'
        assert 'not executed' in requests[-1][-1]['content']
        return response([tool_call('submit_report', report, 'complete-report')])
    result = run_read_only_subagent(client=NS(chat=NS(completions=NS(create=create))),
        model='synthetic', workspace=tmp_path, description='synthetic', parent_tool_call_id='parent',
        token_counter=Counter(), max_context_tokens=10000,
        event_callback=lambda kind, data: executions.append(data['tool_name']) if kind == 'tool_execution' else None)
    assert json.loads(result)['status'] == 'completed' and len(requests) == 2
    assert executions == ['submit_report']


def test_child_repeated_truncation_uses_existing_request_budget(tmp_path):
    from gum_code.subagent import MAX_SUBAGENT_PROVIDER_CALLS

    requests, executions = [], []
    def create(**kwargs):
        requests.append(1)
        return response([tool_call('compact', {}, f'call-{len(requests)}')], 'length')
    result = run_read_only_subagent(client=NS(chat=NS(completions=NS(create=create))),
        model='synthetic', workspace=tmp_path, description='synthetic', parent_tool_call_id='parent',
        token_counter=Counter(), max_context_tokens=10000,
        event_callback=lambda kind, data: executions.append(kind) if kind == 'tool_execution' else None)
    assert json.loads(result)['code'] == 'child_step_limit'
    assert len(requests) == MAX_SUBAGENT_PROVIDER_CALLS and not executions
