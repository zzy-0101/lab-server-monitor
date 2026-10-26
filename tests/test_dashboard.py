import concurrent.futures
import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lab_servers'))
from dashboard import Dashboard, make_handler, validate_server, ThreadingHTTPServer
from transport import PasswordPrompt, run_ssh


def sample(scope='mine', username='tester'):
    return dict(ok=True, scope=scope, sampled_at='2026-10-26T08:00:00+00:00', user=username,
                logical_cpus=112, cpu_idle_percent=50.0, idle_cpu_equivalents=56.0,
                memory_available_gib=100.0, memory_total_gib=188.0,
                load_1_5_15=[1, 1, 1], processes=[dict(pid=123, user=username)])


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'config.json'
        self.app = Dashboard(self.path, probe_fn=lambda *args: sample(args[2]))
        self.server = self.app.servers[0]
        self.server['enabled'] = True
        self.server['auth'] = 'key'
        self.sid = self.server['id']

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def complete(self, data, version=0, revision=0):
        future = concurrent.futures.Future()
        future.set_result(data)
        self.app.finished(future, self.sid, version, revision, data.get('scope', 'mine'))

    def test_password_only_in_memory_not_in_config_or_state(self):
        secret = 'disposable-test-secret-42'
        self.app.set_server(dict(self.server, auth='password', password=secret))
        self.assertEqual(self.app.passwords[self.sid], secret)
        self.assertNotIn(secret, self.path.read_text())
        self.assertNotIn(secret, json.dumps(self.app.public_state()))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        other = Dashboard(self.path)
        try:
            self.assertFalse(other.passwords)
        finally:
            other.close()

    def test_password_cleared_when_identity_changes(self):
        self.app.set_server(dict(self.server, password='test-value'))
        self.app.set_server(dict(self.server, host='192.0.2.1'))
        self.assertNotIn(self.sid, self.app.passwords)

    def test_scope_switch_hides_old_processes_and_discards_late_results(self):
        self.complete(sample())
        self.app.update_settings({'scope': 'all'})
        row = self.app.public_state()['servers'][0]
        self.assertTrue(row['scope_pending'])
        self.assertEqual(row['data']['processes'], [])
        self.complete(sample('mine'), revision=0)
        self.assertEqual(self.app.states[self.sid]['status'], 'waiting')
        self.complete(sample('all', 'root'), revision=1)
        self.assertEqual(self.app.public_state()['servers'][0]['data']['processes'][0]['user'], 'root')

    def test_error_preserves_last_success_and_blocks_auth_retries(self):
        self.complete(sample())
        self.complete(dict(ok=False, error='Permission denied'))
        row = self.app.public_state()['servers'][0]
        self.assertEqual(row['status'], 'error')
        self.assertIsNotNone(row['last_success'])
        self.assertEqual(row['data']['memory_available_gib'], 100)
        self.assertTrue(row['retry_blocked'])
        self.app.refresh(self.sid)
        self.assertFalse(self.app.states[self.sid]['retry_blocked'])

    def test_changed_connection_discards_inflight_result(self):
        self.app.set_server(dict(self.server, host='192.0.2.1'))
        self.complete(sample(), version=0)
        self.assertIsNone(self.app.states[self.sid]['data'])
        self.assertEqual(self.app.states[self.sid]['status'], 'waiting')

    def test_slow_host_does_not_block_fast_host_or_overlap(self):
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        calls = []
        def worker(server, timeout, scope, password):
            calls.append(server['id'])
            if server['id'] == self.sid:
                entered.set()
                release.wait(2)
            else:
                finished.set()
            return sample(scope)
        self.app.probe_fn = worker
        for server in self.app.servers:
            server['auth'] = 'key'
            server['enabled'] = True
        try:
            self.app.schedule_once()
            self.assertTrue(entered.wait(1))
            self.assertTrue(finished.wait(1))
            self.app.schedule_once()
            self.assertEqual(calls.count(self.sid), 1)
            self.assertTrue(self.app.states[self.sid]['busy'])
        finally:
            release.set()
            self.app.pool.shutdown(wait=True)

    def test_invalid_settings_leave_previous_settings_intact(self):
        previous = dict(self.app.settings)
        with self.assertRaises(ValueError):
            self.app.update_settings({'scope': 'all', 'interval': 0})
        self.assertEqual(self.app.settings, previous)
        self.assertEqual(self.app.revision, 0)

    def test_pause_and_disabled_server_do_not_submit_probes(self):
        self.app.probe_fn = Mock()
        self.app.update_settings({'paused': True})
        self.app.schedule_once()
        self.app.probe_fn.assert_not_called()
        self.app.update_settings({'paused': False})
        for server in list(self.app.servers):
            self.app.set_server(dict(server, enabled=False))
        self.app.schedule_once()
        self.app.probe_fn.assert_not_called()

    def test_remove_clears_password_and_accepts_late_completion(self):
        self.app.set_server(dict(self.server, password='test-value'))
        self.app.remove(self.sid)
        self.complete(sample())
        self.assertNotIn(self.sid, self.app.passwords)
        self.assertNotIn(self.sid, self.app.states)

    def test_validates_host_user_and_port_without_shell_execution(self):
        for bad in [dict(host='host;whoami'), dict(user='root -o ProxyCommand=x'), dict(port=0), dict(port=65536)]:
            with self.assertRaises(ValueError):
                validate_server(dict(self.server, **bad))


class AskpassTests(unittest.TestCase):
    def test_helper_receives_secret_only_over_socket_and_cleans_up(self):
        secret = 'not-a-real-password-42!'
        with PasswordPrompt(secret) as env:
            helper = Path(env['SSH_ASKPASS'])
            self.assertNotIn(secret, helper.read_text())
            self.assertNotIn(secret, json.dumps(env))
            result = subprocess.run([str(helper), "user@host's password:"], env=env, capture_output=True, text=True)
            self.assertEqual(result.stdout.rstrip('\n'), secret)
            rejected = subprocess.run([str(helper), 'Are you sure you want to continue?'], env=env, capture_output=True)
            self.assertNotEqual(rejected.returncode, 0)
        self.assertFalse(helper.exists())

    def test_transport_does_not_put_password_in_arguments_or_environment(self):
        server = dict(host='192.0.2.1', port=22, user='tester', auth='password')
        with patch('transport.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            run_ssh(server, 'python3 -', 'print(1)', password='test-secret-value')
            args, kwargs = run.call_args
            self.assertNotIn('test-secret-value', json.dumps(args))
            self.assertNotIn('test-secret-value', json.dumps(kwargs['env']))
            self.assertTrue(kwargs['start_new_session'])


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Dashboard(Path(self.temp.name) / 'config.json')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.app, 'test-token'))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, token='test-token', data=None, headers=None):
        request_headers = {'X-Lab-Token': token}
        request_headers.update(headers or {})
        raw = json.dumps(data).encode() if data is not None else None
        return self.opener.open(urllib.request.Request(self.url + path, raw, request_headers), timeout=2)

    def test_api_requires_token_and_correct_origin_and_host(self):
        for headers in ({'X-Lab-Token': 'bad'}, {'Origin': 'https://untrusted.example'}, {'Host': 'untrusted.example'}):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.request('/api/state', headers=headers)
            self.assertEqual(caught.exception.code, 403)
        with self.request('/api/state') as response:
            self.assertEqual(len(json.loads(response.read())['servers']), 7)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_config_roundtrip_and_rejects_unsupported_scope(self):
        raw = dict(name='测试服务器', host='192.0.2.1', port=2222, user='tester', auth='password',
                   password='dummy-test-only', enabled=False)
        with self.request('/api/server', data=raw) as response:
            sid = json.loads(response.read())['id']
        with self.request('/api/state') as response:
            payload = response.read().decode()
            row = next(s for s in json.loads(payload)['servers'] if s['id'] == sid)
            self.assertTrue(row['has_password'])
            self.assertNotIn('dummy-test-only', payload)
            self.assertEqual(row['status'], 'disabled')
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request('/api/settings', data={'scope': 'invalid'})
        self.assertEqual(caught.exception.code, 400)
        with self.request('/api/remove', data={'id': sid}) as response:
            self.assertTrue(json.loads(response.read())['ok'])


if __name__ == '__main__':
    unittest.main()
