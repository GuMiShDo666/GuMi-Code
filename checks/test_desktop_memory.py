"""Synthetic memory boundary checks; no provider or real user memory is used."""
import io
import json
from queue import Queue
from types import SimpleNamespace
from unittest.mock import patch

from gum_code.desktop import DesktopHost, ProtocolInput
from gum_code.desktop_memory import DesktopMemory
from gum_code.token_counting import EstimatedTokenCounter


def runtime():
    source=ProtocolInput(io.StringIO(''))
    source.started=True
    host=DesktopHost(source,io.StringIO())
    host.session_id='aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa'
    host.turn_id='bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb'
    memory=DesktopMemory(host);host.memory=memory
    return host,memory


def test_memory_rpc_routes_live_input_without_stealing_approval():
    host,memory=runtime()
    def emit(kind,**payload):
        assert kind=='memory_request'
        host.source.lines.put(json.dumps({'v':1,'type':'input','sessionId':host.session_id,'turnId':host.turn_id,'id':'cccccccc-cccc-4ccc-cccc-cccccccccccc','text':'new addition'}))
        host.source.lines.put(json.dumps({'v':1,'type':'memory_response','requestId':payload['requestId'],'result':{'enabled':True,'items':[]}}))
    host.emit=emit
    assert memory.rpc({'action':'refresh','ids':[]})['enabled'] is True
    assert host.take_inputs()==[{'id':'cccccccc-cccc-4ccc-cccc-cccccccccccc','text':'new addition'}]
    assert host.source.lines.empty()


def test_expired_memory_response_never_approves_or_becomes_user_input():
    host,memory=runtime()
    assert host.queue_input({'v':1,'type':'memory_response','requestId':'expired','result':{'approved':True}})
    assert host.pending_inputs==[]
    assert host.queue_input({'v':1,'type':'approval_response','approved':True}) is False


def test_receipts_survive_prepare_and_bodies_are_ephemeral_deleted_on_refresh():
    host,memory=runtime()
    item={'id':'one','kind':'fact','body':'PRIVATE_SYNTHETIC_MEMORY','revision':1}
    receipts=[{'role':'assistant','tool_calls':[{'id':'call','function':{'name':'memory_search'}}]}, {'role':'tool','tool_call_id':'call','content':'{"ids":["one"],"cursor":"next-page","budgetRemaining":300}'}]
    alive=True
    def rpc(r):
        return {'enabled':True,'items':[item] if alive else [],'cursor':'next-page'}
    memory.rpc=rpc
    with patch('gum_code.skills.discover_skills',return_value=[]):
        memory.prepare(receipts,'synthetic',EstimatedTokenCounter(),40000)
        assert 'next-page' in receipts[-1]['content']
        assert 'PRIVATE_SYNTHETIC_MEMORY' not in json.dumps(receipts)
        assert 'PRIVATE_SYNTHETIC_MEMORY' in json.dumps(memory.messages(receipts))
        alive=False
        memory.prepare(receipts,'synthetic',EstimatedTokenCounter(),40000)
        assert 'PRIVATE_SYNTHETIC_MEMORY' not in json.dumps(memory.messages(receipts))


def test_cumulative_page_budget_does_not_reset_or_charge_duplicate_ids():
    host,memory=runtime();memory.counter=EstimatedTokenCounter();memory.limit=200
    first={'id':'a','body':'x'*400}
    memory.accept({'items':[first]})
    used=memory.used
    memory.accept({'items':[first]})
    assert memory.used==used
    memory.accept({'items':[{'id':'b','body':'x'*400}],'cursor':'page3'})
    assert memory.ids==['a']
    assert memory.used<=200


def test_write_tool_preserves_success_and_failure_receipt():
    host,memory=runtime()
    memory.rpc=lambda r:{'saved':[{'id':'a','status':'enabled'}]}
    assert json.loads(memory.execute('memory_write','{"body":"hello"}'))['saved'][0]['status']=='enabled'
    memory.rpc=lambda r:{'error':'disabled'}
    assert json.loads(memory.execute('memory_write','{"body":"hello"}'))['error']=='disabled'


def test_missing_skill_invalidates_reference_before_model_request():
    host,memory=runtime();calls=[]
    def rpc(r):
        calls.append(r)
        return {'enabled':True,'items':[{'id':'skill-a','kind':'skill','body':'obsolete skill','skillRef':'missing','skillDigest':'a'*64}]}
    memory.rpc=rpc
    with patch('gum_code.skills.discover_skills',return_value=[]):
        memory.prepare([],'skill',EstimatedTokenCounter(),40000)
    assert any(c.get('action')=='invalidate_skills' for c in calls)
    assert memory.reference is None


def test_source_evidence_filters_repeated_tool_ids_by_digest(tmp_path):
    import hashlib
    from gum_code.desktop import run
    session_file=tmp_path/'source.jsonl'
    session_file.touch()
    user='new synthetic source'
    transcript=[{'role':'user','content':user},
        {'role':'assistant','tool_calls':[{'id':'reused','function':{'name':'run_command'}}]},
        {'role':'tool','tool_call_id':'reused','content':'old unrelated output'},
        {'role':'assistant','tool_calls':[{'id':'reused','function':{'name':'run_command'}}]},
        {'role':'tool','tool_call_id':'reused','content':'Ran 2 tests\nOK'}]
    source={'v':1,'action':'memory_source','sessionId':'aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa','turnId':'bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb','workspace':str(tmp_path.resolve()),'sessionPath':str(session_file.resolve()),'digest':hashlib.sha256(user.encode()).hexdigest(),'toolIds':['reused'],'evidenceRefs':[{'id':'reused','tool':'run_command','digest':hashlib.sha256(b'Ran 2 tests\nOK').hexdigest()}]}
    output=io.StringIO()
    with patch('gum_code.desktop.load_session',return_value=SimpleNamespace(workspace=tmp_path,messages=transcript)):
        assert run(io.StringIO(json.dumps(source)+'\n'),output)==0
    result=json.loads(output.getvalue().splitlines()[0])
    assert [e['content'] for e in result['evidence']]==['Ran 2 tests\nOK']


def test_experience_arbitrary_success_is_not_verification():
    host,memory=runtime()
    memory.enabled=True
    memory.local_verification=SimpleNamespace(verified=None,needs_check=lambda:False)
    host.event('tool_result',{'tool_name':'run_command','tool_call_id':'actual-call','result':'command exited 0'})
    host.event('task_outcome',{'status':'completed'})
    events=[json.loads(line) for line in host.output.getvalue().splitlines()]
    proof=next(x for x in events if x['type']=='memory_evidence')
    assert proof['verified'] is False
    assert proof['toolIds']==['actual-call']


def test_evidence_excerpt_preserves_test_footer_and_marks_omission():
    from gum_code.desktop import memory_excerpt
    result='test detail\n'*1000+'\nRan 46 tests in 0.015s\n\nOK'
    excerpt=memory_excerpt(result)
    assert len(excerpt)<1800
    assert '[中间内容省略]' in excerpt
    assert excerpt.endswith('Ran 46 tests in 0.015s\n\nOK')
