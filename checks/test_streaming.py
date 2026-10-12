import io
import json
from types import SimpleNamespace as NS
from uuid import uuid4
import pytest
from openai.types.chat import ChatCompletionChunk
from gum_code.streaming import create_streamed_completion
from gum_code.desktop import DesktopHost, ProtocolInput
from gum_code.agent import run_agent

class Stream:
    closed = False
    def __init__(self, chunks): self.chunks = chunks
    def __iter__(self): return iter(self.chunks)
    def close(self): self.closed = True

def chunk(delta, finish=None):
    return ChatCompletionChunk(id='test', created=0, model='synthetic', object='chat.completion.chunk', choices=[dict(index=0,delta=delta,finish_reason=finish)])

def test_sdk_chunks_preserve_reasoning_and_interleaved_tool_arguments():
    stream = Stream([chunk({'reasoning_content':'private thought'}),chunk({'content':'Hello '}),chunk({'content':'world','tool_calls':[{'index':1,'id':'b','type':'function','function':{'name':'read_file','arguments':'{"path":'}},{'index':0,'id':'a','type':'function','function':{'name':'list_files','arguments':'{}'}}]}),chunk({'tool_calls':[{'index':1,'function':{'arguments':'"x"}'}}]},'tool_calls')])
    events=[]
    client=NS(chat=NS(completions=NS(create=lambda **kwargs: stream)))
    result=create_streamed_completion(client,lambda text,done:events.append((text,done)),model='synthetic')
    msg=result.choices[0].message
    assert msg.content=='Hello world' and msg.reasoning_content=='private thought'
    assert [t.id for t in msg.tool_calls]==['a','b']
    assert json.loads(msg.tool_calls[1].function.arguments)=={'path':'x'}
    assert events[-1]==('Hello world',True) and stream.closed
    assert all('private' not in text for text,_ in events)

def test_unterminated_stream_fails_and_closes():
    stream=Stream([chunk({'content':'partial'})]); events=[]
    with pytest.raises(ValueError,match='before completion'):
        create_streamed_completion(NS(chat=NS(completions=NS(create=lambda **kwargs:stream))),lambda *e:events.append(e))
    assert stream.closed and not any(e[1] for e in events)

def test_host_redacts_split_known_secret_and_preserves_safe_text(monkeypatch):
    output=io.StringIO();host=DesktopHost(io.StringIO(),output); secret='SYNTHETIC_TEST_SECRET_123456789'
    host.secrets=[secret]
    raw='This is a safe introduction. '+secret+'\n'+'More public words. '*8
    stream=Stream([*[chunk({'content':raw[i:i+5]}) for i in range(0,len(raw),5)],chunk({},'stop')])
    ticks=iter(range(10000));monkeypatch.setattr('gum_code.desktop.time.monotonic',lambda:next(ticks))
    host.complete(NS(chat=NS(completions=NS(create=lambda **kwargs:stream))))
    events=[json.loads(l) for l in output.getvalue().splitlines()]
    assert len(events)>2
    visible=''
    for event in events:
        visible=event['text'] if event['replace'] else visible+event['text']
        assert secret not in visible and 'SYNTHETIC_TEST_SECRET' not in visible
    assert '<REDACTED>' in visible and events[-1]['done']

def test_input_during_approval_is_retained_and_cannot_approve():
    ident=str(uuid4());sid=str(uuid4());tid=str(uuid4());out=io.StringIO()
    host=DesktopHost(io.StringIO(json.dumps(dict(v=1,type='input',id=ident,text='change',sessionId=sid,turnId=tid))+'\n'+json.dumps(dict(v=1,type='approval_response',requestId='wrong',approved=True))+'\n'),out)
    host.session_id,host.turn_id=sid,tid
    assert host.ask('test','test') is False
    assert host.take_inputs()==[dict(id=ident,text='change')]

class Counter:
    def count_request(self,*args): return 100

def test_input_after_model_answer_is_saved_before_receipt_and_next_request(tmp_path):
    host=DesktopHost(io.StringIO(),io.StringIO());sent=[];snapshots=[];ident=str(uuid4())
    def complete(**kwargs):
        sent.append([dict(m) for m in kwargs['messages']])
        if len(sent)==1: host.pending_inputs.append(dict(id=ident,text='改成蓝色'))
        return NS(choices=[NS(finish_reason='stop',message=NS(content='done',reasoning_content='original',tool_calls=None))],usage=None)
    host.complete=complete
    answer=run_agent(client=NS(),model='synthetic',workspace=tmp_path,user_query='create',desktop_host=host,allow_subagent=False,token_counter=Counter(),message_snapshot_callback=lambda m:snapshots.append([dict(x) for x in m]))
    assert answer=='done' and len(sent)==2
    assert sent[1][-1]['role']=='user' and sent[1][-1]['content']=='改成蓝色'
    assert any(m.get('name')=='input_'+ident.replace('-','_') for m in snapshots[-1])
    assert json.loads(host.output.getvalue().splitlines()[0])['type']=='input_received'


def test_tool_only_stream_does_not_create_empty_chat_bubble():
    host=DesktopHost(io.StringIO(),io.StringIO())
    stream=Stream([chunk({'tool_calls':[{'index':0,'id':'tool','type':'function','function':{'name':'list_files','arguments':'{}'}}]},'tool_calls')])
    response=host.complete(NS(chat=NS(completions=NS(create=lambda **kwargs:stream))))
    assert response.choices[0].message.tool_calls[0].id=='tool'
    assert host.output.getvalue()==''


def test_truncated_stream_never_executes_valid_or_partial_sibling_calls(tmp_path):
    host = DesktopHost(io.StringIO(), io.StringIO())
    streams = [Stream([chunk({'tool_calls': [
        {'index': 0, 'id': 'valid', 'type': 'function', 'function': {
            'name': 'write_file', 'arguments': json.dumps({'path': 'blocked.txt', 'content': 'blocked'})}},
        {'index': 1, 'id': 'partial', 'type': 'function', 'function': {
            'name': 'write_file', 'arguments': '{"path":'}},
    ]}, 'length')]), Stream([chunk({'content': 'done'}, 'stop')])]
    requests = []
    def create(**kwargs):
        requests.append(json.loads(json.dumps(kwargs['messages'])))
        assert kwargs['stream'] is True
        assert not (tmp_path / 'blocked.txt').exists()
        return streams[len(requests) - 1]
    answer = run_agent(client=NS(chat=NS(completions=NS(create=create))), model='synthetic',
        workspace=tmp_path, user_query='synthetic', desktop_host=host, max_steps=2,
        allow_subagent=False, token_counter=Counter(), event_callback=host.event)
    assert answer == 'done' and len(requests) == 2
    assert all(stream.closed for stream in streams)
    results = [m for m in requests[1] if m['role'] == 'tool']
    assert [m['tool_call_id'] for m in results] == ['valid', 'partial']
    assert all('not executed' in m['content'] for m in results)
    progress = [json.loads(line) for line in host.output.getvalue().splitlines()
                if json.loads(line)['type'] == 'progress']
    assert len(progress) == 2 and all(item['status'] == 'failed' for item in progress)
