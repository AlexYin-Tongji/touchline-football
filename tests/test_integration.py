"""Cross-module checks using a local protocol fixture, never an external LLM."""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient


class ProviderHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
        self.server.requests.append(request)
        responses = self.path.endswith('/responses')
        names = {tool.get('function', tool)['name'] for tool in request.get('tools', [])}
        messages = request.get('messages', [])
        if responses:
            messages = [{'role': 'tool', 'content': item['output']} if item.get('type') == 'function_call_output'
                        else item for item in request.get('input', [])]
        tool_messages = [item for item in messages if item.get('role') == 'tool']
        if 'connection_probe' in names:
            if tool_messages:
                message = {'role': 'assistant', 'content': json.loads(tool_messages[-1]['content'])['result']}
            else:
                message = self.tool('connection_probe', {'nonce': 'touchline_connection_check'})
        elif not names:
            message = {'role': 'assistant', 'content': 'OK'}
        else:
            if not tool_messages:
                if self.server.slow:
                    time.sleep(2)
                message = self.tool('search_evidence', {'query': '训练', 'limit': 3})
            elif len(tool_messages) == 1:
                results = json.loads(tool_messages[-1]['content']).get('results', [])
                if results:
                    message = self.tool('read_evidence', {'evidence_id': results[0]['id']})
                else:
                    message = {'role': 'assistant', 'content': '资料不足，暂时无法回答。'}
            else:
                evidence = json.loads(tool_messages[-1]['content'])
                message = {'role': 'assistant', 'content': (
                    f"事实：导入的训练记录提到球队练习了后场接应。[{evidence['id']}]\n\n"
                    '分析：可以在下一场留意接应位置是否发生变化；这仍是需要比赛证据检验的观察点。\n\n'
                    '未知：目前没有正式首发资料，无法确认出场阵容。')}
        data = {'id': 'local-protocol-fixture', 'choices': [{'index': 0,
            'message': message, 'finish_reason': 'tool_calls' if message.get('tool_calls') else 'stop'}],
            'usage': {'prompt_tokens': 20, 'completion_tokens': 15}}
        if responses:
            output = [{'type': 'function_call', 'call_id': call['id'], 'name': call['function']['name'],
                       'arguments': call['function']['arguments']} for call in message.get('tool_calls', [])]
            if message.get('content'):
                output.append({'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': message['content']}]})
            data = {'id': 'resp_fixture', 'status': 'completed', 'output': output}
        result = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(result)))
        self.end_headers()
        try:
            self.wfile.write(result)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def tool(self, name, arguments):
        return {'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': f'call_{name}', 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}]}


class EndToEndProtocolTests(unittest.TestCase):
    def setUp(self):
        from server.main import create_app
        self.tmp = tempfile.TemporaryDirectory()
        self.provider = ThreadingHTTPServer(('127.0.0.1', 0), ProviderHandler)
        self.provider.requests = []
        self.provider.slow = False
        self.thread = threading.Thread(target=self.provider.serve_forever, daemon=True)
        self.thread.start()
        self.client = TestClient(create_app(Path(self.tmp.name) / 'integration.db'))
        self.client.__enter__()
        response = self.client.put('/api/settings', json={
            # This fixture checks wire protocol/cancellation, not semantic support.
            # Independent verification is covered by V3 quality tests.
            'mode': 'live', 'verification_enabled': False, 'llm': {'base_url': f'http://127.0.0.1:{self.provider.server_port}/v1',
            'model': 'local-protocol-fixture', 'api_key': 'fixture-only-not-a-real-key',
            'max_rounds': 6, 'timeout_seconds': 10}})
        self.assertEqual(response.status_code, 200, response.text)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.provider.shutdown()
        self.provider.server_close()
        self.thread.join(timeout=3)
        self.tmp.cleanup()

    def import_training(self):
        response = self.client.post('/api/import', json={
            'team_id': 'arsenal', 'title': '测试资料：赛前训练更新',
            'text': '这是一份仅用于软件验证的虚构训练记录。球队在本次训练中练习了后场接应与中场传球，记录没有提供正式首发名单，不能据此确认球员出场。',
            'source_name': '本地软件测试资料', 'kind': 'team'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def wait_run(self, run_id):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            response = self.client.get(f'/api/runs/{run_id}')
            self.assertEqual(response.status_code, 200, response.text)
            run = response.json()
            if run['status'] in ('completed', 'failed', 'cancelled'):
                return run
            time.sleep(.03)
        self.fail('Run did not reach a terminal state within the local test budget.')

    def test_import_tool_loop_citation_and_persistence_boundary(self):
        self.assertEqual(self.client.get('/api/state?mode=live').json()['evidence'], [])
        imported = self.import_training()
        duplicate = self.import_training()
        self.assertTrue(duplicate['duplicate'])
        test = self.client.post('/api/settings/test', json={})
        self.assertEqual(test.status_code, 200, test.text)
        self.assertTrue(test.json()['tool_support'])
        response = self.client.post('/api/runs', json={'mode': 'live', 'team_id': 'arsenal',
            'skill': 'ask', 'question': '训练发生了什么变化？下一场可以观察什么？'})
        self.assertEqual(response.status_code, 200, response.text)
        run = self.wait_run(response.json()['id'])
        self.assertEqual(run['status'], 'completed', run)
        self.assertIn(imported['evidence']['id'], run['evidence_ids'])
        self.assertIn(f"[{imported['evidence']['id']}]", run['answer'])
        self.assertGreaterEqual(len(run['steps']), 3)
        state = self.client.get('/api/state?mode=live').json()
        self.assertTrue(state['settings']['llm']['api_key_set'])
        self.assertNotIn('fixture-only-not-a-real-key', json.dumps(state))
        self.assertNotIn('fixture-only-not-a-real-key', json.dumps(run))
        self.assertEqual(len(state['evidence']), 1)
        self.assertTrue(all(item['mode'] == 'live' for item in state['evidence']))

    def test_cancel_interrupts_waiting_model(self):
        self.import_training()
        self.provider.slow = True
        response = self.client.post('/api/runs', json={'mode': 'live', 'team_id': 'arsenal',
            'skill': 'ask', 'question': '训练有什么变化？'})
        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()['id']
        deadline = time.monotonic() + 3
        while not self.provider.requests and time.monotonic() < deadline:
            time.sleep(.03)
        start = time.monotonic()
        self.client.post(f'/api/runs/{run_id}/cancel')
        run = self.wait_run(run_id)
        self.assertEqual(run['status'], 'cancelled')
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertEqual(run['answer'], '')

    def test_responses_protocol_full_tool_roundtrip(self):
        self.client.put('/api/settings', json={'llm': {'protocol': 'responses'}})
        imported = self.import_training()
        result = self.client.post('/api/settings/test', json={}).json()
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(result['checks']), 3)
        response = self.client.post('/api/runs', json={'mode': 'live', 'team_id': 'arsenal',
            'skill': 'ask', 'question': '训练有什么变化？', 'allow_network': False})
        self.assertEqual(response.status_code, 200, response.text)
        run = self.wait_run(response.json()['id'])
        self.assertEqual(run['status'], 'completed', run)
        self.assertEqual(run['protocol'], 'responses')
        self.assertIn(imported['evidence']['id'], run['evidence_ids'])
        self.assertTrue(any(any(i.get('type') == 'function_call_output' for i in r.get('input', [])) for r in self.provider.requests))


if __name__ == '__main__':
    unittest.main()
