import io
import json
from pathlib import Path
import tempfile
import subprocess
import signal
import sys
import unittest
from uuid import uuid4
from unittest.mock import patch, MagicMock
from contextlib import ExitStack
from types import SimpleNamespace

from gum_code.desktop import DesktopHost, run, safe_text
from gum_code.session import SessionLease, save_session


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / 'session.jsonl'
        self.request = dict(v=1, action='turn', sessionId=str(uuid4()), turnId=str(uuid4()),
                            workspace=str(self.root), sessionPath=str(self.path), text='Synthetic task', trusted=True,
                            provider=dict(baseUrl='https://invalid.example',model='synthetic-model',apiKey='YOUR_TOKEN_HERE',contextWindow=8000))

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, request=None, runner=None):
        output = io.StringIO()
        kwargs = dict(turn_runner=runner) if runner else {}
        code = run(io.StringIO(json.dumps(request or self.request)+'\n'), output, **kwargs)
        return code, [json.loads(x) for x in output.getvalue().splitlines()]

    def test_probe(self):
        code, messages = self.invoke(dict(v=1, action='probe'))
        self.assertEqual(code, 0)
        self.assertEqual([m['type'] for m in messages], ['ready', 'finished'])

    def test_turn_terminal_and_print_isolation(self):
        def runner(args, host):
            print('private raw provider output')
            host.model_secret('synthetic-secret')
            host.event('task_outcome', {'status':'completed'})
            host.event('final_answer', {'answer':'Hello synthetic-secret'})
            return 0
        code, messages = self.invoke(runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(messages[-2]['text'], 'Hello <REDACTED>')
        self.assertEqual(messages[-1]['status'], 'completed')
        self.assertEqual([m['seq'] for m in messages], list(range(1,len(messages)+1)))

    def test_errors_are_safe_and_cleanup_overrides_success(self):
        def failure(args, host):
            raise RuntimeError('synthetic confidential payload')
        code, messages = self.invoke(runner=failure)
        self.assertEqual(code, 1)
        self.assertNotIn('confidential', str(messages))
        def cleanup(args, host):
            host.status = 'completed'
            host.cleanup_failed()
        code, messages = self.invoke(runner=cleanup)
        self.assertEqual(code, 1)
        self.assertEqual(messages[-1]['status'], 'failed')

    def test_interrupt_finishes_after_cleanup(self):
        state = []
        def runner(args, host):
            try:
                raise KeyboardInterrupt()
            finally:
                state.append('cleaned')
        code, messages = self.invoke(runner=runner)
        self.assertEqual(state, ['cleaned'])
        self.assertEqual(code, 130)
        self.assertEqual(messages[-1]['status'], 'interrupted')

    def test_exact_approval_and_eof_deny(self):
        for reply, expected in [(dict(v=1,type='approval_response',requestId='fixed',approved=True),True),
                                (dict(v=1,type='approval_response',requestId='stale',approved=True),False),
                                (dict(v=1,type='approval_response',requestId='fixed',approved='true'),False),
                                (None,False)]:
            host = DesktopHost(io.StringIO(json.dumps(reply)+'\n' if reply else ''),io.StringIO())
            host.model_secret('secret')
            with patch('gum_code.desktop.uuid4', return_value='fixed'):
                self.assertEqual(host.approve('run_command',{'argv':['python','secret']},'secret'), expected)
            self.assertNotIn('secret',host.output.getvalue())

    def test_local_approval_shows_operation_and_redacts_flag_values(self):
        host = DesktopHost(io.StringIO(), io.StringIO())
        with patch.object(host, 'ask', return_value=True) as ask:
            host.approve('run_command', {'argv':['python3', 'check.py', '--password', 'SYNTHETIC_PRIVATE'], 'cwd':'tests'}, '')
        detail = ask.call_args.args[1]
        self.assertIn('本机', detail)
        self.assertIn('不是安全沙箱', detail)
        self.assertIn('tests', detail)
        self.assertIn('check.py', detail)
        self.assertNotIn('SYNTHETIC_PRIVATE', detail)
        self.assertIn('<REDACTED>', detail)

    def test_all_approval_categories(self):
        host = DesktopHost(io.StringIO(),io.StringIO())
        with patch.object(host,'ask',return_value=True) as ask:
            self.assertTrue(host.environment({'ecosystem':'browser'}))
            self.assertTrue(host.network({}))
            class Vision:
                def add_credits(self,count):
                    self.count=count
                    return True
            vision=Vision()
            host.vision(vision)
            self.assertEqual(vision.count,1)
            self.assertEqual(ask.call_count,3)

    def test_history_recovery_lock_and_workspace(self):
        save_session(self.path,self.root,'synthetic-model',[
            {'role':'system','content':'Synthetic system'},
            {'role':'user','content':'hello'},{'role':'assistant','content':'world'}])
        request={**self.request,'action':'history'}
        code,messages=self.invoke(request)
        self.assertEqual(code,0)
        self.assertEqual(messages[0]['messages'],[{'role':'user','content':'hello'},{'role':'assistant','content':'world'}])
        with SessionLease(self.path):
            code,messages=self.invoke(request)
            self.assertEqual(code,1)
        code,_=self.invoke(request)
        self.assertEqual(code,0)
        other=self.root/'other'; other.mkdir()
        code,_=self.invoke({**request,'workspace':str(other)})
        self.assertEqual(code,1)

    def test_untrusted_and_symlink_rejected_before_runner(self):
        def runner(*args,**kwargs):
            self.fail('must not run')
        code,_=self.invoke({**self.request,'trusted':False},runner)
        self.assertEqual(code,1)
        target=self.root/'target';target.write_text('')
        self.path.symlink_to(target)
        code,_=self.invoke(runner=runner)
        self.assertEqual(code,1)

    def test_shared_orchestration_two_turns_and_pending_recovery(self):
        from gum_code.cli import _run_one_shot
        from gum_code.session import load_session
        seen_initial = []
        seen_models = []
        def agent(**kwargs):
            seen_initial.append(kwargs['initial_messages'])
            seen_models.append(kwargs['model'])
            messages = list(kwargs['initial_messages'] or [{'role':'system','content':'Synthetic system'}])
            messages.append({'role':'user','content':kwargs['user_query']})
            kwargs['message_snapshot_callback'](messages)
            messages.append({'role':'assistant','content':'Synthetic answer YOUR_TOKEN_HERE'})
            kwargs['message_snapshot_callback'](messages)
            kwargs['event_callback']('task_outcome',dict(status='completed',reason='no_code_changes',
                step=1,self_check_status='disabled',changed=False,summary=''))
            kwargs['event_callback']('final_answer',dict(answer='Synthetic answer YOUR_TOKEN_HERE'))
        project = MagicMock()
        project.close.return_value = ()
        browser = MagicMock()
        browser.close.return_value = ()
        vision = MagicMock()
        vision.cancel.return_value = None
        with ExitStack() as stack:
            patches = dict(load_settings=MagicMock(side_effect=AssertionError('CLI config forbidden')),OpenAI=lambda **kw:SimpleNamespace(),
                resolve_context_window=lambda *a:8000,resolve_context_budget=lambda *a:6000,
                load_token_counter=lambda *a:None,ProjectRuntime=lambda *a,**kw:project,
                ProjectBrowser=lambda *a,**kw:browser,VisionRuntime=lambda *a,**kw:vision,
                load_vision_settings=lambda:None,AUTOMATIC_SELF_CHECK_ENABLED=False,run_agent=agent)
            for name,value in patches.items():
                stack.enter_context(patch('gum_code.cli.'+name,value))
            for number in range(2):
                code,messages=self.invoke({**self.request,'text':f'Turn {number}', 'provider': {**self.request['provider'], 'model': f'synthetic-model-{number}'}},_run_one_shot)
                self.assertEqual(code,0,messages)
                self.assertNotIn('YOUR_TOKEN_HERE',str(messages))
            self.assertNotIn(b'YOUR_TOKEN_HERE',self.path.read_bytes())
            history_code,history=self.invoke({**self.request,'action':'history','provider':None})
            self.assertEqual(history_code,0)
            self.assertNotIn('YOUR_TOKEN_HERE',str(history))
            self.assertIsNone(seen_initial[0])
            self.assertEqual(len(seen_initial[1]),3)
            restored=load_session(self.path)
            self.assertEqual(len(restored.messages),5)
            self.assertEqual(seen_models, ['synthetic-model-0', 'synthetic-model-1'])
            self.assertEqual(restored.model, 'synthetic-model-1')
            original_bytes = self.path.read_bytes()
            def cli_policy(args, host):
                host.shared_api = False
                return _run_one_shot(args, host=host)
            rejected_code, _ = self.invoke(self.request, cli_policy)
            self.assertEqual(rejected_code, 1)
            self.assertEqual(self.path.read_bytes(), original_bytes)
            self.assertEqual(len(seen_models), 2)
            pending=restored.messages+[{'role':'user','content':'Interrupted original input'}]
            save_session(self.path,self.root,'synthetic-model',pending)
            code,messages=self.invoke({**self.request,'text':'Explicit retry'},_run_one_shot)
            self.assertEqual(code,0,messages)
            self.assertEqual(len(seen_initial[-1]),5)
            self.assertTrue(any('未自动重试' in m.get('text','') for m in messages))
            with SessionLease(self.path):
                pass
            self.assertEqual(project.close.call_count,3)
            self.assertEqual(vision.cancel.call_count,3)

    def test_subprocess_sigint_releases_session_before_finished(self):
        script = """
import sys, time
from pathlib import Path
from gum_code.desktop import run
from gum_code.session import SessionLease
def runner(args, host):
    with SessionLease(args.session_file):
        try:
            host.emit('status', text='synthetic-ready')
            time.sleep(30)
        finally:
            args.workspace.joinpath('cleaned').write_text('yes')
raise SystemExit(run(sys.stdin, sys.stdout, turn_runner=runner))
"""
        process = subprocess.Popen([sys.executable,'-c',script],stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            process.stdin.write(json.dumps(self.request)+'\n')
            process.stdin.flush()
            self.assertEqual(json.loads(process.stdout.readline())['type'],'status')
            self.assertEqual(json.loads(process.stdout.readline())['text'],'synthetic-ready')
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode,130)
            self.assertEqual(stderr,'')
            self.assertEqual(json.loads(stdout.strip())['status'],'interrupted')
            self.assertEqual((self.root/'cleaned').read_text(),'yes')
            with SessionLease(self.path):
                pass
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_provider_required_without_cli_fallback(self):
        with patch('gum_code.cli.load_settings',side_effect=AssertionError('must not read CLI config')):
            for provider in (None, {}, {**self.request['provider'],'apiKey':''},
                             {**self.request['provider'],'baseUrl':'https://user:secret@example.test'},
                             {**self.request['provider'],'contextWindow':True}):
                code,messages=self.invoke({**self.request,'provider':provider})
                self.assertEqual(code,1)
                self.assertEqual(messages[-2]['message'],
                    'GuMi-Code API 配置不可用。请在设置中配置有效的 API 地址、模型和密钥，再重试。')
                self.assertNotIn('YOUR_TOKEN_HERE',str(messages))
            def runner(args,host):
                self.assertEqual(host.settings().model,'synthetic-model')
                host.status='completed'
            code,_=self.invoke(runner=runner)
            self.assertEqual(code,0)

    def test_provider_context_optional_and_vision_no_cli_config(self):
        host=DesktopHost(io.StringIO(),io.StringIO())
        host.provider={key:value for key,value in self.request['provider'].items() if key!='contextWindow'}
        with patch('gum_code.cli.load_settings',side_effect=AssertionError()), \
             patch('gum_code.cli.load_vision_settings',side_effect=AssertionError()):
            self.assertIsNone(host.settings().context_window)
            self.assertIsNone(host.vision_settings())

    def test_history_omission_notice_is_projection_only(self):
        messages=[{'role':'system','content':'Synthetic system'}]
        for number in range(101):
            messages.extend([{'role':'user','content':f'Question {number}'},
                             {'role':'assistant','content':f'Answer {number}'}])
        save_session(self.path,self.root,'synthetic-model',messages)
        before=self.path.read_bytes()
        code,result=self.invoke({**self.request,'action':'history'})
        self.assertEqual(code,0)
        projected=result[0]['messages']
        self.assertEqual(len(projected),201)
        self.assertTrue(projected[0]['content'].startswith('【历史显示提示】'))
        self.assertEqual(projected[-1]['content'],'Answer 100')
        self.assertEqual(self.path.read_bytes(),before)
        from gum_code.session import SESSION_VERSION
        oversized=SimpleNamespace(version=SESSION_VERSION,workspace=self.root,
            messages=[{'role':'assistant','content':'x'*130000}])
        with patch('gum_code.desktop.load_session',return_value=oversized):
            code,result=self.invoke({**self.request,'action':'history'})
        self.assertEqual(code,0)
        self.assertTrue(result[0]['messages'][0]['content'].startswith('【历史显示提示】'))

    def test_project_approval_details_are_bounded_and_allowlisted(self):
        host=DesktopHost(io.StringIO(),io.StringIO())
        with patch.object(host,'ask',return_value=False) as ask:
            host.approve('project_service',dict(action='start',name='web',kind='app',
                ecosystem='node',port=3000,online=True,cwd='frontend?token=synthetic-private',
                argv=['node','synthetic-private']),'synthetic-private')
            detail=ask.call_args.args[1]
            self.assertIn('操作：启动',detail)
            self.assertIn('目标相对路径：frontend',detail)
            self.assertIn('端口：3000',detail)
            self.assertNotIn('synthetic-private',detail)
            host.approve('project_browser',dict(action='fill',service='web',
                path='/login?token=synthetic-private#synthetic-private',
                value='synthetic-private',selector='[value="synthetic-private"]'),'synthetic-private')
            detail=ask.call_args.args[1]
            self.assertIn('操作：填写控件',detail)
            self.assertIn('目标相对路径：/login',detail)
            self.assertNotIn('synthetic-private',detail)

    def test_subprocess_pipe_eof_cancels_and_cleans_up(self):
        script = """
import sys, time
from gum_code.desktop import run, ProtocolInput
from gum_code.session import SessionLease
def runner(args, host):
    with SessionLease(args.session_file):
        try:
            host.emit('status', text='synthetic-ready')
            time.sleep(30)
        finally:
            args.workspace.joinpath('eof-cleaned').write_text('yes')
raise SystemExit(run(ProtocolInput(sys.stdin), sys.stdout, turn_runner=runner))
"""
        process=subprocess.Popen([sys.executable,'-c',script],stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            process.stdin.write(json.dumps(self.request)+'\n')
            process.stdin.flush()
            self.assertEqual(json.loads(process.stdout.readline())['type'],'status')
            self.assertEqual(json.loads(process.stdout.readline())['text'],'synthetic-ready')
            process.stdin.close()
            process.stdin=None
            stdout,stderr=process.communicate(timeout=5)
            self.assertEqual(process.returncode,130)
            self.assertEqual(stderr,'')
            self.assertEqual(json.loads(stdout.strip())['status'],'interrupted')
            self.assertEqual((self.root/'eof-cleaned').read_text(),'yes')
            with SessionLease(self.path):
                pass
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_real_main_probe_eof_is_not_cancellation(self):
        process=subprocess.run([sys.executable,'-m','gum_code.desktop'],
            input=json.dumps(dict(v=1,action='probe'))+'\n',text=True,
            capture_output=True,timeout=5)
        self.assertEqual(process.returncode,0,process.stderr)
        self.assertEqual(process.stderr,'')
        self.assertEqual(json.loads(process.stdout.splitlines()[-1])['status'],'completed')

    def test_common_credentials_redacted(self):
        self.assertNotIn('fake-value', safe_text('api_key="fake-value"'))
        self.assertNotIn('fake-value', safe_text('postgres://user:fake-value@host/db'))

if __name__ == '__main__':
    unittest.main()

class DesktopProgressTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.host = DesktopHost(io.StringIO(), self.output)
        self.host.workspace = Path('/synthetic')
        self.host.model_secret('synthetic-private-key')

    def messages(self):
        return [json.loads(line) for line in self.output.getvalue().splitlines()]

    def test_actual_tool_events_share_id_and_only_emit_when_executed(self):
        payload = dict(step=1, tool_name='write_file', arguments=json.dumps({'path':'src/app.py','content':'PRIVATE BODY'}))
        self.host.event('tool_call', payload)
        self.assertEqual(self.messages(), [])
        self.host.event('tool_execution', {'step':1,'tool_name':'write_file'})
        self.host.event('tool_result', {'step':1,'tool_name':'write_file','result':'raw private result'})
        events = self.messages()
        self.assertEqual(events[0]['id'], events[1]['id'])
        self.assertEqual([e['status'] for e in events], ['running','completed'])
        self.assertEqual(events[0]['detail'], 'src/app.py')
        self.assertNotIn('PRIVATE', self.output.getvalue())
        self.assertNotIn('raw private', self.output.getvalue())

    def test_denied_command_and_nonzero_result_are_failures(self):
        self.host.event('tool_call', {'step':1,'tool_name':'run_command','arguments':json.dumps({'argv':['sh','private argument']})})
        self.host.event('tool_result', {'step':1,'tool_name':'run_command','result':'Error: private reason'})
        self.host.event('tool_execution', {'step':2,'tool_name':'run_command'})
        self.host.event('tool_result', {'step':2,'tool_name':'run_command','result':'<command-output>\n{"reason":"exited","exit_code":1}\nprivate output\n</command-output>'})
        self.assertEqual([e['status'] for e in self.messages()], ['failed','running','failed'])
        self.assertNotIn('private', self.output.getvalue())

    def test_bounded_redacted_paths_and_plan(self):
        for path, expected in [('/synthetic/src/a.py','src/a.py'),('/outside/secret',''),('../secret',''),('synthetic-private-key.py','<REDACTED>.py')]:
            self.assertEqual(self.host.tool_detail({'arguments':json.dumps({'path':path})}), expected)
        self.host.event('plan_state', {'current_plan_id':'a','plans':[{'id':'a','items':[{'content':'synthetic-private-key'+'x'*400,'status':'in_progress'}]*30}]})
        plan = self.messages()[0]['items']
        self.assertEqual(len(plan),20)
        self.assertTrue(all(len(i['text'])<=200 and i['status']=='running' for i in plan))
        self.assertNotIn('synthetic-private-key',self.output.getvalue())

    def test_context_status_ignores_private_payload(self):
        self.host.event('context_summary', {'summary':'private reasoning'})
        self.host.event('model_call', {'reasoning':'private reasoning'})
        self.assertEqual([e['text'] for e in self.messages()], ['正在整理上下文','正在等待模型响应'])
        self.assertNotIn('private', self.output.getvalue())

    def test_ids_and_parent_scopes_do_not_mix_calls(self):
        for parent in ('parent1','parent2'):
            self.host.event('tool_call', dict(step=1, tool_name='read_file', parent_tool_call_id=parent, arguments='{"path":"'+parent+'"}'))
            self.host.event('tool_execution', dict(step=1, tool_name='read_file', parent_tool_call_id=parent))
        self.host.event('tool_result',dict(step=1,tool_name='read_file',parent_tool_call_id='parent2',result='ok'))
        self.host.event('tool_result',dict(step=1,tool_name='read_file',parent_tool_call_id='parent1',result='ok'))
        events=self.messages()
        self.assertEqual(events[0]['id'],events[3]['id'])
        self.assertEqual(events[1]['id'],events[2]['id'])

    def test_safe_error_categories(self):
        from openai import APITimeoutError, APIConnectionError, AuthenticationError, BadRequestError
        request=SimpleNamespace()
        response=SimpleNamespace(status_code=401, request=request, headers={})
        errors=[(APITimeoutError(request=request),'超时'),
                (APIConnectionError(request=request),'无法连接'),
                (AuthenticationError('private detail',response=response,body=None),'鉴权'),
                (BadRequestError('private detail',response=response,body={'code':'context_length_exceeded'}),'上下文'),
                (RuntimeError('Session 已被另一个进程打开'),'另一个进程'),
                (RuntimeError('受保护的上下文批次超过当前预算'),'上下文预算不足'),
                (RuntimeError('上下文胶囊校验失败 (private detail)'),'一致性校验'),
                (RuntimeError('单个完整上下文批次无法在摘要预算内处理'),'无法安全压缩')]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            wire=dict(v=1,action='turn',sessionId=str(uuid4()),turnId=str(uuid4()),workspace=str(root),sessionPath=str(root/'s.jsonl'),text='hello',trusted=True,provider=dict(baseUrl='https://example.invalid',model='test',apiKey='YOUR_TOKEN_HERE',contextWindow=8000))
            for error, label in errors:
                output=io.StringIO()
                def fail(args,host):
                    raise error
                code=run(io.StringIO(json.dumps(wire)+'\n'),output,turn_runner=fail)
                self.assertEqual(code,1)
                self.assertIn(label,output.getvalue())
                self.assertNotIn('private detail',output.getvalue())
