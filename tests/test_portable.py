import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'lab_servers'))
from build_portable import build
from dashboard import Dashboard
from paths import default_identity_file
from transport import run_ssh
from transport_paramiko import run_paramiko


class PortablePackageTests(unittest.TestCase):
    def test_package_excludes_machine_credentials_and_preserves_launchers(self):
        with tempfile.TemporaryDirectory() as directory:
            target = build(Path(directory) / 'portable.zip')
            with ZipFile(target) as archive:
                names = archive.namelist()
                self.assertIn('LabMonitor/Start-Mac.command', names)
                self.assertIn('LabMonitor/Start-Windows.bat', names)
                self.assertIn('LabMonitor/使用说明.pdf', names)
                self.assertIn('LabMonitor/tools/launch_monitor.py', names)
                self.assertIn('LabMonitor/lab_servers/requirements-windows.txt', names)
                servers = json.loads(archive.read('LabMonitor/lab_servers/servers.json'))
                self.assertTrue(all(s['user'] == 'your_username' and not s['enabled'] and s['auth'] == 'password' for s in servers))
                self.assertTrue(all(s['remember_user'] and not s['remember_password'] and not s['credential_id'] for s in servers))
                self.assertIn('LabMonitor/lab_servers/transport_paramiko.py', names)
                self.assertIn('LabMonitor/lab_servers/credential_store.py', names)
                for name in names:
                    self.assertNotIn('gui_config', name)
                    self.assertNotIn('.runtime', name)
                    self.assertNotIn('id_ed25519', name)
                    self.assertNotIn('latest_status', name)
                    if not name.endswith('.pdf'):
                        self.assertNotIn(str(Path.home()) + '/', archive.read(name).decode('utf-8'))
                self.assertTrue(archive.getinfo('LabMonitor/Start-Mac.command').external_attr >> 16 & 0o100)
                self.assertIn(b'\r\n', archive.read('LabMonitor/Start-Windows.bat'))

    def test_new_computer_without_key_defaults_to_password(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(default_identity_file(directory), '')
            with patch('dashboard.DEFAULT_KEY', ''):
                app = Dashboard(Path(directory) / 'gui.json')
            try:
                self.assertTrue(all(server['auth'] == 'password' and not server['identity_file'] for server in app.servers))
            finally:
                app.close()

    def test_windows_dispatch_does_not_use_unix_askpass(self):
        response = subprocess.CompletedProcess([], 0, 'ok', '')
        with patch('transport.WINDOWS', True), patch('transport_paramiko.run_paramiko', return_value=response) as backend:
            self.assertIs(run_ssh({'host': '192.0.2.1'}, 'python3 -', password='test-only'), response)
            self.assertEqual(backend.call_args.args[-1], 'test-only')


@unittest.skipUnless(importlib.util.find_spec('paramiko'), 'Windows SSH backend dependency not installed')
class ParamikoBackendTests(unittest.TestCase):
    def setUp(self):
        import paramiko
        self.paramiko = paramiko
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.client = Mock()
        self.client.get_host_keys.return_value = paramiko.HostKeys()
        self.channel = Mock()
        self.output = [b'{"hostname":"server"}\n']
        self.errors = [b'warning\n']
        self.channel.recv_ready.side_effect = lambda: bool(self.output)
        self.channel.recv_stderr_ready.side_effect = lambda: bool(self.errors)
        self.channel.recv.side_effect = lambda count: self.output.pop(0)
        self.channel.recv_stderr.side_effect = lambda count: self.errors.pop(0)
        self.channel.exit_status_ready.return_value = True
        self.channel.recv_exit_status.return_value = 0
        self.client.get_transport.return_value.open_session.return_value = self.channel
        self.server = dict(host='192.0.2.1', port=2222, user='tester', auth='password')
        self.patchers = [patch('pathlib.Path.home', return_value=self.home),
                         patch('paramiko.SSHClient', return_value=self.client)]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    def test_password_auth_and_probe_payload_eof(self):
        result = run_paramiko(self.server, 'python3 - --scope all', 'print("test")', password='not-real')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, 'warning\n')
        self.assertEqual(json.loads(result.stdout)['hostname'], 'server')
        kwargs = self.client.connect.call_args.kwargs
        self.assertEqual(kwargs['password'], 'not-real')
        self.assertFalse(kwargs['allow_agent'])
        self.assertFalse(kwargs['look_for_keys'])
        self.assertIsNone(kwargs['key_filename'])
        self.channel.sendall.assert_called_once_with(b'print("test")')
        self.channel.shutdown_write.assert_called_once()
        self.client.close.assert_called_once()

    def test_first_host_key_is_saved_and_changed_key_rejected(self):
        run_paramiko(self.server, 'true', password='not-real')
        policy = self.client.set_missing_host_key_policy.call_args.args[0]
        first = self.paramiko.RSAKey.generate(1024)
        changed = self.paramiko.RSAKey.generate(1024)
        policy.missing_host_key(self.client, '[192.0.2.1]:2222', first)
        policy.missing_host_key(self.client, '[192.0.2.1]:2222', first)
        known = self.paramiko.HostKeys(str(self.home / '.ssh' / 'known_hosts'))
        self.assertTrue(known.check('[192.0.2.1]:2222', first))
        with self.assertRaisesRegex(ValueError, 'Host key verification failed'):
            policy.missing_host_key(self.client, '[192.0.2.1]:2222', changed)

    def test_auth_error_does_not_disclose_password(self):
        self.client.connect.side_effect = self.paramiko.AuthenticationException('bad auth')
        with self.assertRaisesRegex(ValueError, 'Permission denied') as caught:
            run_paramiko(self.server, 'true', password='not-real')
        self.assertNotIn('not-real', str(caught.exception))
        self.client.close.assert_called_once()

    def test_socket_timeout_is_bounded_and_connection_is_closed(self):
        self.client.connect.side_effect = socket.timeout()
        with self.assertRaises(subprocess.TimeoutExpired):
            run_paramiko(self.server, 'true', timeout=1, password='not-real')
        self.client.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
