import contextlib
import copy
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lab_servers'))


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'lab_servers' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = load('remote_probe')
monitor = load('monitor')
setup = load('setup')


class MonitoringTests(unittest.TestCase):
    def test_cpu_memory_new_process_and_probe_exclusion(self):
        def process(pid, ppid, ticks, start=100):
            return dict(pid=pid, ppid=ppid, ticks=ticks, start_ticks=start,
                        rss_gib=1, command='python job.py', state='R', threads=2)
        before = {10: process(10, 9, 10), 9: process(9, 1, 20), 20: process(20, 1, 100)}
        after = copy.deepcopy(before)
        # Scanning /proc before sleep can be slow: each PID needs its own time interval.
        before[20]['measured_at'] = -1.0
        after[20]['measured_at'] = 1.0
        after[20]['ticks'] = 300  # 200 ticks over 2 seconds = 100%, not 200%
        after[21] = process(21, 1, 10)
        # Same PID reused: do not charge old task's ticks to the new process.
        before[22] = process(22, 1, 900, 50)
        after[22] = process(22, 1, 10, 150)
        def read_text(path):
            if str(path) == '/proc/meminfo':
                return 'MemTotal: 2097152 kB\nMemAvailable: 1048576 kB\nSwapFree: 0 kB\n'
            if str(path) == '/proc/uptime':
                return '200.0 0.0'
            raise AssertionError(path)
        with patch.object(probe, 'cpu_snapshot', side_effect=[(1000, 500, 10, 4), (1400, 700, 30, 4)]), \
             patch.object(probe, 'processes', side_effect=[before, after]), \
             patch.object(probe.time, 'monotonic', side_effect=[0.0, 1.0]), \
             patch.object(probe.time, 'sleep'), patch.object(probe.os, 'getpid', return_value=10), \
             patch.object(probe.os, 'sysconf', return_value=100), \
             patch.object(probe, 'disk_snapshot', return_value={'disks': [], 'disk_error': None}), \
             patch.object(probe.Path, 'read_text', read_text):
            data = probe.collect()
        self.assertEqual(data['cpu_idle_percent'], 50)
        self.assertEqual(data['cpu_iowait_percent'], 5)
        self.assertEqual(data['idle_cpu_equivalents'], 2)
        self.assertEqual(data['memory_available_gib'], 1)
        rows = {p['pid']: p for p in data['processes']}
        self.assertEqual(set(rows), {20, 21, 22})
        self.assertEqual(rows[20]['cpu_percent'], 100)
        self.assertNotIn('measured_at', rows[20])
        self.assertIsNone(rows[21]['cpu_percent'])
        self.assertIsNone(rows[22]['cpu_percent'])

    def test_failure_does_not_hide_other_servers(self):
        completed = subprocess.CompletedProcess([], 255, '', 'Permission denied')
        with patch.object(monitor.subprocess, 'run', return_value=completed):
            row = monitor.probe(dict(monitor.inventory('3')[0], auth='key'))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            monitor.render([row])
        self.assertFalse(row['ok'])
        self.assertIn('Permission denied', output.getvalue())

    def test_probe_timeout_is_reported(self):
        with patch.object(monitor.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ssh', 1)):
            row = monitor.probe(dict(monitor.inventory('2')[0], auth='key'), 1)
        self.assertFalse(row['ok'])
        self.assertIn('超时', row['error'])

    def test_banner_before_json(self):
        result = subprocess.CompletedProcess([], 0, 'Welcome\n{"logical_cpus": 112, "user": "tester"}\n', '')
        with patch.object(monitor.subprocess, 'run', return_value=result):
            row = monitor.probe(dict(monitor.inventory('2')[0], auth='key'))
        self.assertTrue(row['ok'])
        self.assertEqual(row['logical_cpus'], 112)

    def test_inventory_includes_seven_and_port_mapping(self):
        with self.assertRaises(ValueError):
            monitor.inventory('8')
        servers = monitor.inventory()
        self.assertEqual(servers[6]['port'], 3)
        self.assertEqual(servers[6]['python'], '/usr/local/anaconda3/bin/python3')
        self.assertNotEqual(servers[3]['port'], servers[5]['port'])

    def test_clean_checkout_uses_example_and_local_config_takes_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            example = json.loads((ROOT / 'lab_servers' / 'servers.example.json').read_text())
            (root / 'servers.example.json').write_text(json.dumps(example))
            with patch.object(monitor, 'ROOT', root):
                servers = monitor.inventory()
                self.assertEqual(len(servers), 7)
                self.assertTrue(all(s['user'] == 'your_username' and not s['enabled']
                                    and s['auth'] == 'password' for s in servers))
                personal = [dict(servers[0], user='my_account')]
                (root / 'servers.json').write_text(json.dumps(personal))
                self.assertEqual(monitor.inventory(), personal)

    def test_install_preserves_old_config_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            ssh = Path(tmp)
            original = 'Host old-server\n  HostName 192.0.2.1\n'
            (ssh / 'config').write_text(original)
            with patch.object(setup, 'SSH_DIR', ssh), contextlib.redirect_stdout(io.StringIO()):
                setup.install_config(monitor.inventory())
                first = (ssh / 'config').read_text()
                setup.install_config(monitor.inventory())
                self.assertEqual(first, (ssh / 'config').read_text())
            self.assertIn(original, first)
            self.assertEqual(first.count('Include '), 1)
            self.assertEqual(len(list(ssh.glob('config.before-lab-*'))), 1)


if __name__ == '__main__':
    unittest.main()
