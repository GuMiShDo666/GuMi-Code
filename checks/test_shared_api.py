"""Real installed client serialization against a strict loopback synthetic API."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from openai import OpenAI

from gum_code.agent import run_agent
from gum_code.context import _empty_capsule, _request_capsule
from gum_code.desktop_memory import extract
from gum_code.model_protocol import thinking_options
from gum_code.subagent import run_read_only_subagent
from gum_code.token_counting import EstimatedTokenCounter


class SharedAPICompatibilityTests(unittest.TestCase):
    def test_main_child_compaction_and_memory_send_standard_fields(self):
        requests = []
        mode = 'main'
        capsule = _empty_capsule()
        capsule['current_goal'] = [dict(id='goal-0001', status='active', text='Synthetic goal',
            reason='Synthetic request', source_ref='m0001', superseded_by=None)]
        def handle(body):
            requests.append((mode, body))
            self.assertLessEqual(set(body), {'model', 'messages', 'tools', 'tool_choice', 'max_tokens'})
            message = {'role': 'assistant', 'content': 'Synthetic answer'}
            finish = 'stop'
            if mode == 'summary':
                message['content'] = json.dumps(capsule)
            elif mode == 'memory':
                message['content'] = '{"proposals":[]}'
            elif mode == 'child':
                message = {'role': 'assistant', 'content': None, 'tool_calls': [{
                    'id': 'synthetic-report', 'type': 'function', 'function': {
                        'name': 'submit_report', 'arguments': json.dumps({
                            'summary': 'Synthetic research', 'findings': [], 'evidence': [], 'risks': []})}}]}
                finish = 'tool_calls'
            return {'id': 'synthetic', 'object': 'chat.completion',
                'created': 0, 'model': 'custom-model', 'choices': [
                    {'index': 0, 'finish_reason': finish, 'message': message}]}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                try:
                    response = handle(body)
                    status = 200
                except AssertionError:
                    response = {'error': {'message': 'Non-standard request field'}}
                    status = 400
                raw = json.dumps(response).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as directory, OpenAI(
            api_key='YOUR_TOKEN_HERE', base_url=f'http://127.0.0.1:{server.server_port}/v1',
            max_retries=0,
        ) as client:
            client._gumcode_compatible_api = True
            counter = EstimatedTokenCounter()
            result = run_agent(client=client, model='custom-model', workspace=Path(directory),
                user_query='Reply briefly.', max_steps=1, max_context_tokens=100000,
                token_counter=counter, allow_subagent=False)
            self.assertIn('Synthetic answer', result)
            mode = 'child'
            child = run_read_only_subagent(client=client, model='custom-model', workspace=Path(directory),
                description='Inspect only.', parent_tool_call_id='parent', token_counter=counter,
                max_context_tokens=100000)
            self.assertEqual(json.loads(child)['status'], 'completed')
            mode = 'summary'
            self.assertEqual(_request_capsule(client, 'custom-model', None, [{'source_ref': 'm0001', 'role': 'user', 'content': 'Synthetic goal'}], counter, 100000, None, None), capsule)
            mode = 'memory'
            host = SimpleNamespace(settings=lambda: SimpleNamespace(api_key='YOUR_TOKEN_HERE',
                base_url='https://synthetic.invalid/v1', model='custom-model'))
            with patch('openai.OpenAI', return_value=client):
                self.assertEqual(extract(host, {'text': 'Synthetic memory request', 'existing': []}), [])
        self.assertEqual([name for name, _ in requests], ['main', 'child', 'summary', 'memory'])

    def test_cli_thinking_options_remain_unchanged(self):
        client = SimpleNamespace()
        self.assertEqual(thinking_options(client, True), {'extra_body': {'thinking': {'type': 'enabled'}}})
        self.assertEqual(thinking_options(client, False), {'extra_body': {'thinking': {'type': 'disabled'}}})


if __name__ == '__main__':
    unittest.main()
