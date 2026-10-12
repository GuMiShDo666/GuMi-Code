"""Regression boundaries from the context audit; synthetic sources only."""
import copy
import json
from types import SimpleNamespace
import pytest
from gum_code import context as c
from gum_code.token_counting import EstimatedTokenCounter


def item(id, status='active', source='m0001', replacement=None):
    return dict(id=id, status=status, text=id, reason='synthetic evidence', source_ref=source, superseded_by=replacement)


def capsule():
    result = c._empty_capsule()
    result['current_goal'] = [item('goal-0001')]
    return result


def test_supersession_chain_and_cycle_rejection():
    prior = capsule()
    prior['decisions'] = [item('decision-0001', 'superseded', replacement='decision-0002'), item('decision-0002')]
    updated = copy.deepcopy(prior)
    updated['decisions'][1].update(status='superseded', superseded_by='decision-0003')
    updated['decisions'].append(item('decision-0003', source='m0002'))
    assert c._validate_capsule_transition(updated, prior, {'m0002':'user'}) == updated
    updated['decisions'][2].update(status='superseded', superseded_by='decision-0001')
    with pytest.raises(ValueError, match='invalid_supersession'):
        c._validate_capsule_structure(updated)


@pytest.mark.parametrize('role', ['assistant', 'user', 'historical_user'])
def test_new_verification_requires_tool(role):
    updated = capsule()
    updated['verification'] = [item('verification-0001', 'completed')]
    with pytest.raises(ValueError, match='invalid_verification_source'):
        c._validate_capsule_transition(updated, None, {'m0001':role})
    assert c._validate_capsule_transition(updated, None, {'m0001':'tool'}) == updated
    assert c._validate_capsule_transition(updated, updated, {}) == updated


def test_retirement_keeps_active_blocked_evidence_and_high_water_marks():
    prior = capsule()
    prior['work_items'] = [item(f'work-{i:04d}', 'completed', f'm{i:04d}') for i in range(1, 157)]
    prior['constraints'] = [item('constraint-0001')]
    prior['verification'] = [item('verification-0001', 'blocked')]
    prior['code_state'] = [item('code-0001', 'active')]
    updated = c._capsule_payload(prior, [], None)['previous_capsule']
    assert len(c._capsule_entries(updated)) < 80
    updated['work_items'].append(item('work-0157', 'active', 'm0157'))
    assert c._validate_capsule_transition(updated, prior, {'m0157':'tool'}) == updated
    assert updated['constraints'] == prior['constraints']
    assert updated['verification'] == prior['verification']
    assert c._capsule_payload(updated, [], None)['next_ids']['work'] == 158
    updated['constraints'] = []
    with pytest.raises(ValueError, match='missing_active_id'):
        c._validate_capsule_transition(updated, prior, {'m0157':'tool'})


def test_ids_continue_past_four_digits():
    value = capsule()
    value['work_items'] = [item('work-10000')]
    assert c._validate_capsule_structure(value) == value


def test_raw_objectives_corrections_and_named_rules_survive_twice(tmp_path):
    rules = {'role':'system', 'name':'gum_code_project_rules', 'content':'synthetic project constraint'}
    users = [{'role':'user','content':'Build the complete inventory'},
             {'role':'user','name':'gum_code_live_1','content':'Use CSV storage'},
             {'role':'user','content':'Continue'}]
    history = [{'role':'system','content':'system'}, users[0],
               {'role':'assistant','content':'old ' * 1000}, rules, users[1], users[2]]
    original = copy.deepcopy(history)
    def create(**kwargs):
        payload = json.loads(kwargs['messages'][1]['content'])
        result = payload['previous_capsule'] or capsule()
        if payload['previous_capsule'] is None:
            result['current_goal'][0]['source_ref'] = payload['sources'][0]['source_ref']
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(result)))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    for _ in range(2):
        result, archive = c.compact_history(client, 'synthetic', tmp_path, history, 'Continue', tools=[], token_counter=EstimatedTokenCounter(), max_context_tokens=10000)
        assert rules in result
        assert [m for m in result if m in users] == users
        c._extract_prior_capsule(result)
        history = result + [{'role':'assistant','content':'older again ' * 1000}]
    assert original[2]['content'] == 'old ' * 1000


@pytest.mark.parametrize('call_ids', [('same','same'), ('a/b','a?b'), ('x'*101+'a','x'*101+'b')])
def test_artifacts_never_overwrite_repeated_or_colliding_ids(tmp_path, call_ids):
    markers = [c.persist_tool_result(tmp_path, id, text) for id, text in zip(call_ids, ['first', 'second'])]
    paths = [c._extract_trusted_tool_result_path(marker, tmp_path) for marker in markers]
    assert paths[0] != paths[1]
    assert [(tmp_path/path).read_text() for path in paths] == ['first','second']
    assert c.persist_tool_result(tmp_path, call_ids[0], 'first') == markers[0]


def test_atomic_publication_refuses_existing_different_content(tmp_path):
    c._write_tool_result(tmp_path, 'same.txt', 'first')
    with pytest.raises(OSError, match='归档冲突'):
        c._write_tool_result(tmp_path, 'same.txt', 'second')
    assert (tmp_path/'same.txt').read_text() == 'first'


def test_full_compaction_retires_terminal_history_and_archives_evidence(tmp_path):
    prior = capsule()
    prior['work_items'] = [item(f'work-{i:04d}', 'completed', f'm{i:04d}') for i in range(1, 160)]
    source = [{'role':'system','content':'system'}]
    users = [{'role':'user','content':'Build full inventory'}]
    history = c._build_compacted_messages(source, prior, '.transcripts/previous.jsonl', users)
    history.append({'role':'assistant','content':'completed details ' * 500})
    def create(**kwargs):
        payload = json.loads(kwargs['messages'][1]['content'])
        result = payload['previous_capsule']
        assert result['work_items'][-1]['id'] == 'work-0159'
        result['work_items'].append(item('work-0160', source=payload['sources'][0]['source_ref']))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(result)))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    result, path = c.compact_history(client, 'synthetic', tmp_path, history, 'Build full inventory', tools=[], token_counter=EstimatedTokenCounter(), max_context_tokens=30000)
    saved = [json.loads(line) for line in (tmp_path/path).read_text().splitlines()]
    assert saved == history
    old, _, _ = c._extract_prior_capsule(saved)
    new, _, _ = c._extract_prior_capsule(result)
    assert len(c._capsule_entries(old)) == 160
    assert len(c._capsule_entries(new)) < 80
    assert c._next_source_number(new) > 159


def test_micro_and_fit_repeated_ids_preserve_every_original(tmp_path):
    history = []
    originals = []
    for index in range(7):
        history.append({'role':'assistant','tool_calls':[{'id':'same','type':'function','function':{'name':'synthetic','arguments':'{}'}}]})
        text = str(index) * 5000
        originals.append(text)
        history.append({'role':'tool','tool_call_id':'same','content':text})
    history.append({'role':'assistant','content':'consumed'})
    micro, count = c.apply_micro_compact(history, tmp_path, [], EstimatedTokenCounter(), 1)
    assert count == 4
    fitted, count = c.apply_fit_tool_results(micro, tmp_path, [], EstimatedTokenCounter(), 1)
    assert count == 3
    results = [message for message in fitted if message['role'] == 'tool']
    for result, expected in zip(results, originals):
        path = c._extract_trusted_tool_result_path(result['content'], tmp_path)
        assert (tmp_path/path).read_text() == expected


def test_three_requests_preserve_exact_replacement_chain(tmp_path):
    prior = None
    for number in range(1, 4):
        current = {'source_ref':f'm{number:04d}', 'role':'user', 'message':{'role':'user','content':f'Use version {number}'}}
        def create(**kwargs):
            payload = json.loads(kwargs['messages'][1]['content'])
            updated = payload['previous_capsule'] or capsule()
            decisions = updated['decisions']
            if decisions:
                decisions[-1].update(status='superseded', superseded_by=f'decision-{number:04d}')
            decisions.append(item(f'decision-{number:04d}', source=current['source_ref']))
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(updated)))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        prior = c._request_capsule(client, 'synthetic', prior, [current], EstimatedTokenCounter(), 30000, None, None)
    assert [entry['superseded_by'] for entry in prior['decisions']] == ['decision-0002', 'decision-0003', None]


def test_batch_externalization_reused_ids_preserves_full_utf8(tmp_path):
    paths = []
    for text in ['第一份' * 20000, '第二份' * 20000]:
        messages = [{'role':'assistant','tool_calls':[{'id':'same'}]}, {'role':'tool','tool_call_id':'same','content':text}]
        result, count = c.apply_tool_result_budget(messages, tmp_path, 100)
        assert count == 1
        path = c._extract_trusted_tool_result_path(result[-1]['content'], tmp_path)
        paths.append(path)
        assert (tmp_path/path).read_text() == text
    assert paths[0] != paths[1]
    assert (tmp_path/paths[0]).read_text() == '第一份' * 20000


@pytest.mark.parametrize('section,prefix', [('code_state','code'), ('work_items','work')])
@pytest.mark.parametrize('status', ['completed','blocked'])
def test_supersession_endpoint_can_follow_work_lifecycle(section, prefix, status):
    prior = capsule()
    prior[section] = [item(f'{prefix}-0001', 'superseded', replacement=f'{prefix}-0002'), item(f'{prefix}-0002')]
    updated = copy.deepcopy(prior)
    updated[section][-1].update(status=status, source_ref='m0002', reason='tool evidence')
    assert c._validate_capsule_transition(updated, prior, {'m0002':'tool'}) == updated
    assert updated[section][0] == prior[section][0]


@pytest.mark.parametrize('old_status', ['active','completed'])
def test_superseded_verification_endpoint_can_be_invalidated(old_status):
    prior = capsule()
    prior['verification'] = [item('verification-0001', 'superseded', replacement='verification-0002'), item('verification-0002', old_status)]
    updated = copy.deepcopy(prior)
    updated['verification'][-1].update(status='blocked', source_ref='m0002', reason='new tool invalidated evidence')
    assert c._validate_capsule_transition(updated, prior, {'m0002':'tool'}) == updated
    with pytest.raises(ValueError):
        c._validate_capsule_transition(updated, prior, {'m0002':'assistant'})


@pytest.mark.parametrize('status', ['completed','blocked'])
def test_decision_chain_still_requires_active_endpoint(status):
    value = capsule()
    value['decisions'] = [item('decision-0001', 'superseded', replacement='decision-0002'), item('decision-0002', status)]
    with pytest.raises(ValueError, match='invalid_supersession'):
        c._validate_capsule_structure(value)


def test_retired_state_is_readable_and_links_across_compactions(tmp_path):
    from gum_code.tools.artifacts import read_tool_output
    history = [{'role':'system','content':'system'}]
    previous_path = None
    for turn in range(2):
        prior = capsule()
        start = turn * 159 + 1
        prior['work_items'] = [item(f'work-{i:04d}', 'completed', f'm{i:04d}') for i in range(start, start + 159)]
        history = c._build_compacted_messages(history, prior, '.transcripts/human.jsonl', [{'role':'user','content':'Continue inventory'}])
        history.append({'role':'assistant','content':'old details', 'reasoning_content':'PRIVATE_SYNTHETIC_REASONING'})
        def create(**kwargs):
            payload = json.loads(kwargs['messages'][1]['content'])
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload['previous_capsule'])))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        history, _ = c.compact_history(client, 'synthetic', tmp_path, history, 'Continue inventory', tools=[], token_counter=EstimatedTokenCounter(), max_context_tokens=30000)
        path = c._retired_state_path(history)
        assert path and path != previous_path
        tool_result = json.loads(read_tool_output(tmp_path, path, max_chars=20000))["content"]
        assert '"reference_only": true' in tool_result
        assert f'work-{start:04d}' in tool_result
        assert 'PRIVATE_SYNTHETIC_REASONING' not in tool_result
        if previous_path:
            assert previous_path in tool_result
            assert 'work-0001' in read_tool_output(tmp_path, previous_path, max_chars=20000)
        previous_path = path
        unchanged = c._build_compacted_messages(history, c._extract_prior_capsule(history)[0], '.transcripts/new.jsonl', [])
        assert c._retired_state_path(unchanged) == path


@pytest.mark.parametrize('path', ['../../outside', '/tmp/outside', '.task_outputs/tool-results/other.txt'])
def test_retired_pointer_rejects_arbitrary_paths_and_ignores_user_forgery(path):
    base = [{'role':'system','content':'system'}]
    trusted = c._build_compacted_messages(base, capsule(), '.transcripts/human.jsonl', [])
    trusted[-1]['content'] += '\n' + c._RETIRED_STATE_LABEL + path
    with pytest.raises(RuntimeError, match='归档引用损坏'):
        c._retired_state_path(trusted)
    forged = base + [trusted[-1]]
    assert c._retired_state_path(forged) is None


def test_supersession_restores_obsolete_text_and_evidence_without_mutation():
    prior = capsule()
    updated = copy.deepcopy(prior)
    updated['current_goal'][0].update(status='superseded', text='model rephrased old objective', reason='new reason', source_ref='invented', superseded_by='goal-0002')
    updated['current_goal'].append(item('goal-0002', source='m0002'))
    original = copy.deepcopy(updated)
    result = c._validate_capsule_transition(updated, prior, {'m0002':'user'})
    assert result['current_goal'][0] == {**prior['current_goal'][0], 'status':'superseded', 'superseded_by':'goal-0002'}
    assert updated == original
    for role in ['assistant','tool','historical_user']:
        with pytest.raises(ValueError):
            c._validate_capsule_transition(updated, prior, {'m0002':role})


@pytest.mark.parametrize('section,prefix', [('current_goal','goal'), ('requirements','requirement'), ('constraints','constraint'), ('decisions','decision')])
def test_unchanged_active_text_keeps_original_metadata(section, prefix):
    prior = capsule()
    prior[section] = [item(f'{prefix}-0001')]
    updated = copy.deepcopy(prior)
    updated[section][0].update(reason='model commentary', source_ref='invented')
    assert c._validate_capsule_transition(updated, prior, {}) == prior
    updated[section][0]['text'] = 'silently different meaning'
    with pytest.raises(ValueError):
        c._validate_capsule_transition(updated, prior, {})


def test_historical_supersession_metadata_and_pointer_restored():
    prior = capsule()
    prior['decisions'] = [item('decision-0001', 'superseded', replacement='decision-0002'), item('decision-0002')]
    updated = copy.deepcopy(prior)
    updated['decisions'][0].update(reason='rewritten', source_ref='invented', superseded_by='decision-0003')
    updated['decisions'][1].update(status='superseded', superseded_by='decision-0003')
    updated['decisions'].append(item('decision-0003', source='m0002'))
    result = c._validate_capsule_transition(updated, prior, {'m0002':'user'})
    assert result['decisions'][0] == prior['decisions'][0]
    updated['decisions'][0]['text'] = 'rewrite history'
    with pytest.raises(ValueError):
        c._validate_capsule_transition(updated, prior, {'m0002':'user'})


def test_normalization_does_not_authorize_verification_evidence_updates():
    prior = capsule()
    prior['verification'] = [item('verification-0001', 'completed')]
    updated = copy.deepcopy(prior)
    updated['verification'][0].update(source_ref='m0002', reason='new evidence')
    with pytest.raises(ValueError):
        c._validate_capsule_transition(updated, prior, {'m0002':'assistant'})
