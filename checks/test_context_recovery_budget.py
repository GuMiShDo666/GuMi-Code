"""Provider rejection must shrink the budget without mutating rejected history."""
import copy
from types import SimpleNamespace as NS
import pytest
import gum_code.agent as agent
from gum_code.token_counting import TokenBudgetTracker


class Counter:
    def count_request(self, messages, tools):
        return sum(len(str(m.get('content', ''))) for m in messages) + 100


def test_calibration_survives_compaction_and_ignores_invalid_usage():
    tracker = TokenBudgetTracker(Counter())
    original = [{'role': 'user', 'content': 'x' * 900}]
    tracker.observe(NS(usage=NS(prompt_tokens=2000, completion_tokens=10, total_tokens=2010)), original, [])
    tracker.reset()
    smaller = [{'role': 'user', 'content': 'x' * 400}]
    assert tracker.count_request(smaller, []) == 1000
    tracker.observe(NS(usage=NS(prompt_tokens=True, completion_tokens=0, total_tokens=1)), smaller, [])
    assert tracker.count_request(smaller, []) == 1000
    assert tracker.local_count_request(smaller, []) == 500


@pytest.mark.parametrize('summary_rejections', [0, 1, 3])
def test_rejection_budget_shrinks_and_summary_retries_are_bounded(tmp_path, monkeypatch, summary_rejections):
    budgets, rejected, snapshots = [], [], []
    def compact(client, model, workspace, messages, query, **kwargs):
        budgets.append(kwargs['max_context_tokens'])
        rejected.append(copy.deepcopy(messages))
        if len(budgets) <= summary_rejections:
            raise RuntimeError('context_length_exceeded')
        return [m for m in messages if m.get('role') != 'assistant'], 'synthetic-archive'
    monkeypatch.setattr(agent, 'reactive_compact', compact)
    calls = []
    def create(**kwargs):
        calls.append(copy.deepcopy(kwargs['messages']))
        if len(calls) == 1:
            raise RuntimeError('context_length_exceeded')
        return NS(choices=[NS(finish_reason='stop', message=NS(content='done', tool_calls=None, reasoning_content=''))], usage=None)
    client = NS(chat=NS(completions=NS(create=create)))
    args = dict(client=client, model='synthetic', workspace=tmp_path, user_query='continue',
                initial_messages=[{'role':'system','content':'rules'}, {'role':'user','content':'build'},
                                  {'role':'assistant','content':'history '*4000}],
                max_context_tokens=800000, allow_subagent=False, token_counter=Counter(),
                message_snapshot_callback=lambda messages: snapshots.append(copy.deepcopy(messages)))
    if summary_rejections == 3:
        with pytest.raises(RuntimeError, match='context_length_exceeded'):
            agent.run_agent(**args)
        assert len(calls) == 1
        assert all(not any(m.get('content') == 'done' for m in s) for s in snapshots)
    else:
        assert agent.run_agent(**args) == 'done'
        assert len(calls) == 2
        assert not any(m.get('role') == 'assistant' for m in calls[1])
    assert len(budgets) == min(summary_rejections + 1, 3)
    assert budgets[0] < Counter().count_request(calls[0], []) < 800000
    assert all(a > b for a, b in zip(budgets, budgets[1:]))
    assert all(m == calls[0] for m in rejected)


def test_actual_compactor_recovers_rejected_agent_request(tmp_path):
    import json
    from gum_code.context import _CAPSULE_SECTIONS as CAPSULE_SECTIONS
    from gum_code.token_counting import EstimatedTokenCounter
    calls, events = [], []
    def create(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        if 'tools' in kwargs:
            if sum('tools' in c for c in calls) == 1:
                raise RuntimeError('context_length_exceeded')
            return NS(choices=[NS(finish_reason='stop', message=NS(content='done', tool_calls=None, reasoning_content=''))], usage=None)
        assert kwargs['extra_body'] == {'thinking': {'type': 'disabled'}}
        payload = json.loads(kwargs['messages'][1]['content'])
        capsule = payload['previous_capsule'] or {'version': 1, **{name: [] for name in CAPSULE_SECTIONS}}
        if not capsule['current_goal']:
            capsule['current_goal'] = [dict(id='goal-0001', status='active', text='Build inventory', reason='user request', source_ref=payload['sources'][0]['source_ref'], superseded_by=None)]
        return NS(choices=[NS(message=NS(content=json.dumps(capsule)))], usage=None)
    client = NS(chat=NS(completions=NS(create=create)))
    initial = [{'role':'system','content':'Synthetic system'}, {'role':'user','content':'Build inventory'}]
    initial += [{'role':'assistant','content':'historical detail '*1000} for _ in range(8)]
    assert agent.run_agent(client=client, model='synthetic', workspace=tmp_path, user_query='continue',
                           initial_messages=initial, max_context_tokens=800000, allow_subagent=False,
                           token_counter=EstimatedTokenCounter(), event_callback=lambda k,v: events.append((k,v))) == 'done'
    assert len([c for c in calls if 'tools' in c]) == 2
    assert any('对话状态压缩器' in str(c['messages'][0]) for c in calls)
    event = next(v for k,v in events if k == 'context_compacted' and v['stage'] == 'reactive_compact')
    assert event['after_tokens'] < event['max_tokens'] < event['before_tokens']
    assert any(m.get('content') == 'Build inventory' for m in calls[-1]['messages'])


def test_automatic_summary_rejection_retries_with_smaller_budget(tmp_path):
    import json
    from gum_code.context import _CAPSULE_SECTIONS
    from gum_code.token_counting import EstimatedTokenCounter
    calls = []
    def create(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        if len(calls) == 1:
            raise RuntimeError('context_length_exceeded')
        assert kwargs['extra_body'] == {'thinking': {'type': 'disabled'}}
        payload = json.loads(kwargs['messages'][1]['content'])
        capsule = payload['previous_capsule'] or {'version': 1, **{name: [] for name in _CAPSULE_SECTIONS}}
        if not capsule['current_goal']:
            capsule['current_goal'] = [dict(id='goal-0001', status='active', text='Build', reason='request', source_ref=payload['sources'][0]['source_ref'], superseded_by=None)]
        return NS(choices=[NS(message=NS(content=json.dumps(capsule)))], usage=None)
    initial = [{'role':'system','content':'Rules'}, {'role':'user','content':'Build'}]
    initial += [{'role':'assistant','content':'history '*1000} for _ in range(8)]
    original = copy.deepcopy(initial)
    tracker = TokenBudgetTracker(EstimatedTokenCounter())
    result, used = agent._prepare_request_context(
        client=NS(chat=NS(completions=NS(create=create))), model='synthetic', workspace=tmp_path,
        user_query='Build', messages=initial, agent_tool_schemas=[], token_budget=tracker,
        max_context_tokens=10000, step=1, event_callback=None, message_snapshot_callback=None,
    )
    assert len(calls) > 1
    assert tracker.recovered_limit == 5000
    assert used < tracker.recovered_limit
    assert initial == original
    assert any('[Compacted v1]' in str(m['content']) for m in result)
