import ctypes
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lab_servers'))
from dashboard import Dashboard
from credential_store import CredentialError, WindowsCredentials, WindowsCredential


class MemoryCredentials:
    name = '测试凭据存储'

    def __init__(self):
        self.records = {}

    def get(self, key):
        return self.records.get(key)

    def set(self, key, value):
        self.records[key] = value

    def delete(self, key):
        self.records.pop(key, None)


class RememberLoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'gui.json'
        self.store = MemoryCredentials()
        self.app = Dashboard(self.path, credential_store=self.store)
        self.server = self.app.servers[0]
        self.server['enabled'] = True
        self.sid = self.server['id']

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def remember(self):
        self.app.set_server(dict(self.server, auth='password', password='test-only-密码',
                                 remember_user=True, remember_password=True))
        return self.app.servers[0]

    def test_restart_loads_remembered_password_and_passes_it_to_probe(self):
        self.remember()
        self.app.close()
        worker = Mock(return_value=dict(ok=True, processes=[]))
        other = Dashboard(self.path, probe_fn=worker, credential_store=self.store)
        try:
            self.assertEqual(other.passwords[self.sid], 'test-only-密码')
            for server in other.servers:
                server['enabled'] = server['id'] == self.sid
            other.schedule_once()
            other.pool.shutdown(wait=True)
            self.assertEqual(worker.call_args.args[-1], 'test-only-密码')
            self.assertNotIn('test-only-密码', self.path.read_text())
            state = json.dumps(other.public_state(), ensure_ascii=False)
            self.assertNotIn('test-only-密码', state)
            self.assertNotIn('credential_id', state)
        finally:
            other.close()

    def test_unchecking_password_forgets_durable_copy_but_keeps_current_session(self):
        server = self.remember()
        self.app.set_server(dict(server, remember_password=False))
        self.assertFalse(self.store.records)
        self.assertIn(self.sid, self.app.passwords)
        other = Dashboard(self.path, credential_store=self.store)
        try:
            self.assertNotIn(self.sid, other.passwords)
            self.assertEqual(other.servers[0]['user'], server['user'])
            self.assertEqual(other.public_state()['servers'][0]['status'], 'login')
        finally:
            other.close()

    def test_unchecking_username_starts_next_session_with_blank_login_and_no_probe(self):
        server = self.remember()
        self.app.set_server(dict(server, remember_user=False, remember_password=False))
        self.assertEqual(json.loads(self.path.read_text())['servers'][0]['user'], '')
        self.assertEqual(self.app.servers[0]['user'], server['user'])
        worker = Mock()
        other = Dashboard(self.path, credential_store=self.store, probe_fn=worker)
        try:
            for s in other.servers:
                s['enabled'] = s['id'] == self.sid
            other.schedule_once()
            worker.assert_not_called()
            self.assertEqual(other.servers[0]['user'], '')
            self.assertEqual(other.public_state()['servers'][0]['status'], 'login')
        finally:
            other.close()

    def test_editing_name_with_blank_password_keeps_saved_login(self):
        server = self.remember()
        self.app.set_server(dict(server, name='新名称', password=''))
        other = Dashboard(self.path, credential_store=self.store)
        try:
            self.assertEqual(other.servers[0]['name'], '新名称')
            self.assertEqual(other.passwords[self.sid], 'test-only-密码')
        finally:
            other.close()

    def test_new_host_requires_its_own_password_and_replaces_saved_identity(self):
        server = self.remember()
        with self.assertRaisesRegex(ValueError, '请输入'):
            self.app.set_server(dict(server, host='192.0.2.44'))
        self.assertEqual(self.app.servers[0]['host'], server['host'])
        self.app.set_server(dict(server, host='192.0.2.44', password='new-host-test'))
        other = Dashboard(self.path, credential_store=self.store)
        try:
            self.assertEqual(other.passwords[self.sid], 'new-host-test')
        finally:
            other.close()

    def test_saved_record_with_different_identity_is_not_used(self):
        server = self.remember()
        record = json.loads(self.store.get(server['credential_id']))
        record['identity'][0] = '192.0.2.55'
        self.store.set(server['credential_id'], json.dumps(record))
        other = Dashboard(self.path, credential_store=self.store)
        try:
            self.assertNotIn(self.sid, other.passwords)
        finally:
            other.close()

    def test_clear_password_and_remove_delete_the_durable_record(self):
        server = self.remember()
        self.app.set_server(dict(server, clear_password=True))
        self.assertFalse(self.store.records)
        self.assertNotIn(self.sid, self.app.passwords)
        self.assertFalse(self.app.servers[0]['remember_password'])
        self.server = self.app.servers[0]
        self.remember()
        self.app.remove(self.sid)
        self.assertFalse(self.store.records)

    def test_write_failure_does_not_change_active_connection(self):
        old = dict(self.app.servers[0])
        self.store.set = Mock(side_effect=CredentialError('存储不可用'))
        with self.assertRaisesRegex(CredentialError, '存储不可用'):
            self.remember()
        self.assertEqual(self.app.servers[0], old)
        self.assertFalse(self.app.passwords)
        self.assertFalse(self.path.exists())

    def test_unavailable_store_can_be_recovered_by_entering_password_again(self):
        server = self.remember()
        self.store.get = Mock(side_effect=CredentialError('锁定'))
        other = Dashboard(self.path, credential_store=self.store)
        try:
            self.assertEqual(other.public_state()['servers'][0]['status'], 'login')
            other.set_server(dict(server, password='reentered-test'))
            self.assertEqual(other.passwords[self.sid], 'reentered-test')
            self.assertNotIn('credential_error', other.states[self.sid])
        finally:
            other.close()


class WindowsCredentialApiTests(unittest.TestCase):
    def setUp(self):
        self.backend = WindowsCredentials.__new__(WindowsCredentials)
        self.backend.api = Mock()
        self.key = 'test-record'

    def test_unicode_secret_uses_generic_local_machine_persistence(self):
        written = {}
        def write(pointer, flags):
            entry = ctypes.cast(pointer, ctypes.POINTER(WindowsCredential)).contents
            written.update(value=ctypes.string_at(entry.CredentialBlob, entry.CredentialBlobSize),
                           persist=entry.Persist, type=entry.Type, target=entry.TargetName)
            return 1
        self.backend.api.CredWriteW.side_effect = write
        self.backend.set(self.key, '中文-password-test')
        self.assertEqual(written['value'].decode(), '中文-password-test')
        self.assertEqual((written['persist'], written['type']), (2, 1))
        self.assertEqual(written['target'], 'LabMonitor/server-test-record')

    def test_read_copies_value_then_frees_native_allocation(self):
        value = ctypes.create_string_buffer('中文-test'.encode())
        entry = WindowsCredential(CredentialBlob=ctypes.addressof(value), CredentialBlobSize=len(value.raw)-1)
        def read(target, kind, flags, out):
            ctypes.cast(out, ctypes.POINTER(ctypes.POINTER(WindowsCredential)))[0] = ctypes.pointer(entry)
            return 1
        self.backend.api.CredReadW.side_effect = read
        self.assertEqual(self.backend.get(self.key), '中文-test')
        self.backend.api.CredFree.assert_called_once()

    def test_missing_record_is_normal_and_other_errors_are_reported(self):
        self.backend.api.CredReadW.return_value = 0
        self.backend.api.CredDeleteW.return_value = 0
        with patch('ctypes.get_last_error', return_value=1168, create=True):
            self.assertIsNone(self.backend.get(self.key))
            self.backend.delete(self.key)
        with patch('ctypes.get_last_error', return_value=5, create=True):
            with self.assertRaises(CredentialError):
                self.backend.get(self.key)


if __name__ == '__main__':
    unittest.main()
