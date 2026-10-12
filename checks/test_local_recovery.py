"""Real local child cancellation followed by Session reload and another Agent turn."""
import io
import json
import os
from pathlib import Path
import signal
import threading
import time
from types import SimpleNamespace as NS

import pytest

from gum_code.agent import run_agent
from gum_code.desktop import DesktopHost
from gum_code.local_command import LocalCommandConfig
from gum_code.session import (IN_FLIGHT_MARKER_KEY, save_session, load_session,
                              build_tool_batch_recovery_messages)


class Counter:
    def count_request(self, *args):
        return 100


def test_real_cancel_saved_session_resumes_without_replaying(tmp_path):
    workspace=tmp_path/'workspace';workspace.mkdir()
    marker=workspace/'started'
    (workspace/'hold.py').write_text("from pathlib import Path\nimport time\np=Path('started')\np.write_text(p.read_text()+'again' if p.exists() else 'once')\ntime.sleep(30)\n")
    path=tmp_path/'session.jsonl'
    tool=NS(id='local_hold',type='function',function=NS(name='run_command',arguments=json.dumps({'argv':['python3','hold.py'],'timeout_seconds':60})))
    response=NS(choices=[NS(finish_reason='tool_calls',message=NS(content='',tool_calls=[tool]))],usage=None)
    def interrupt_when_running():
        deadline=time.monotonic()+5
        while not marker.exists() and time.monotonic()<deadline:
            time.sleep(.01)
        if marker.exists():
            os.kill(os.getpid(),signal.SIGINT)
    interrupter=threading.Thread(target=interrupt_when_running)
    interrupter.start()
    with pytest.raises(KeyboardInterrupt):
        run_agent(client=NS(chat=NS(completions=NS(create=lambda **kwargs:response))),model='synthetic',workspace=workspace,
                  user_query='Run synthetic hold',max_steps=4,max_context_tokens=800000,
                  allow_run_command=True,command_sandbox=LocalCommandConfig(workspace),allow_subagent=False,
                  approval_callback=lambda *args:True,token_counter=Counter(),
                  message_snapshot_callback=lambda messages:save_session(path,workspace,'synthetic',messages))
    interrupter.join(timeout=6)
    assert marker.read_text()=='once'
    restored=load_session(path,allow_incomplete_tool_batch=True)
    assert any(m.get(IN_FLIGHT_MARKER_KEY) for m in restored.messages)
    with pytest.raises(ValueError):
        build_tool_batch_recovery_messages(restored.messages)
    host=DesktopHost(io.StringIO(),io.StringIO())
    recovered=host.recover_messages(restored.messages)
    assert not any(m.get(IN_FLIGHT_MARKER_KEY) for m in recovered)
    tool_result=next(m['content'] for m in recovered if m.get('tool_call_id')=='local_hold')
    assert '未重放' in tool_result and '未确认' in tool_result and '只读' not in tool_result
    save_session(path,workspace,'synthetic',recovered)
    resumed=load_session(path,allow_incomplete_tool_batch=True)
    events=[]
    answer=NS(choices=[NS(finish_reason='stop',message=NS(content='继续成功；上次命令未经验证。',tool_calls=None))],usage=None)
    run_agent(client=NS(chat=NS(completions=NS(create=lambda **kwargs:answer))),model='synthetic',workspace=workspace,
              user_query='Continue without running the hold again',initial_messages=resumed.messages,
              max_steps=4,max_context_tokens=800000,allow_run_command=True,
              command_sandbox=LocalCommandConfig(workspace),allow_subagent=False,
              token_counter=Counter(),approval_callback=lambda *args:False,
              event_callback=lambda kind,value:events.append((kind,value)),
              message_snapshot_callback=lambda messages:save_session(path,workspace,'synthetic',messages))
    assert marker.read_text()=='once'
    assert [value['status'] for kind,value in events if kind=='task_outcome']==['completed']
    assert load_session(path).messages[-1]['content'].startswith('继续成功')


def inflight(name='run_command'):
    return [{'role':'system','content':'Synthetic'},{'role':'user','content':'Do'},
            {'role':'assistant','content':None,'tool_calls':[{'id':'call','type':'function','function':{'name':name,'arguments':'{}'}}]},
            {'role':'tool','tool_call_id':'call','content':'',IN_FLIGHT_MARKER_KEY:True}]


@pytest.mark.parametrize('flag',[None,1,'true',[],{}])
def test_recovery_flag_requires_boolean(flag):
    with pytest.raises(ValueError):
        build_tool_batch_recovery_messages(inflight(),allow_interrupted_commands=flag)


def test_desktop_exception_does_not_recover_unconfirmed_file_write():
    with pytest.raises(ValueError):
        build_tool_batch_recovery_messages(inflight('write_file'),allow_interrupted_commands=True)
