from pathlib import Path
from types import SimpleNamespace as NS
import pytest
from gum_code.project_rules import apply_project_rules, ProjectRulesError, START, END
from gum_code.agent import run_agent


def test_load_refresh_and_delete_rules(tmp_path):
    p=tmp_path/'AGENTS.md'
    assert apply_project_rules('base',tmp_path)==('base',False)
    p.write_text('称呼我为 GuMiShDo。')
    first,loaded=apply_project_rules('base',tmp_path)
    assert loaded and 'GuMiShDo' in first
    p.write_text('新的规则'+END)
    second,_=apply_project_rules(first,tmp_path)
    assert 'GuMiShDo' not in second and second.count(START)==1 and second.count(END)==1
    p.unlink()
    assert apply_project_rules(second,tmp_path)==('base',False)


def test_reject_unsafe_or_invalid_rule_files(tmp_path):
    p=tmp_path/'AGENTS.md';outside=tmp_path/'outside';outside.write_text('outside')
    p.symlink_to(outside)
    with pytest.raises(ProjectRulesError): apply_project_rules('base',tmp_path)
    p.unlink();p.write_bytes(b'x'*65537)
    with pytest.raises(ProjectRulesError,match='64 KiB'):apply_project_rules('base',tmp_path)
    p.write_bytes(b'\xff')
    with pytest.raises(ProjectRulesError,match='UTF-8'):apply_project_rules('base',tmp_path)


class Counter:
    def count_request(self,*args): return 100


def test_real_agent_request_loads_current_rules_without_mutating_old_history(tmp_path):
    p=tmp_path/'AGENTS.md';p.write_text('RULE_ALPHA')
    sent=[];events=[]
    def create(**kwargs):
        sent.append([dict(m) for m in kwargs['messages']])
        return NS(choices=[NS(finish_reason='stop',message=NS(content='done',tool_calls=None,reasoning_content=''))],usage=None)
    client=NS(chat=NS(completions=NS(create=create)))
    run_agent(client=client,model='synthetic',workspace=tmp_path,user_query='hello',allow_subagent=False,token_counter=Counter(),event_callback=lambda k,v:events.append((k,v)))
    assert 'RULE_ALPHA' in sent[0][-2]['content']
    p.write_text('RULE_BETA')
    run_agent(client=client,model='synthetic',workspace=tmp_path,user_query='continue',initial_messages=sent[0],allow_subagent=False,token_counter=Counter())
    assert 'RULE_BETA' in sent[1][-2]['content'] and not any('RULE_ALPHA' in m['content'] for m in sent[1] if m['role']=='system')
    assert 'RULE_ALPHA' in sent[0][-2]['content']
    assert ('project_rules',{'loaded':True}) in events
