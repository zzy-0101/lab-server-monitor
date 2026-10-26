import importlib.util
import io
import contextlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lab_servers'))
spec = importlib.util.spec_from_file_location('disk_probe', ROOT / 'lab_servers' / 'remote_probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
import monitor


def stats(total=1000, free=100, available=80, unit=4096):
    return SimpleNamespace(f_blocks=total, f_bfree=free, f_bavail=available, f_frsize=unit, f_bsize=unit)


class DiskTests(unittest.TestCase):
    def sample(self, table, stat=None):
        with patch.object(probe.Path, 'read_text', return_value=table), \
             patch.object(probe.os, 'statvfs', side_effect=stat if callable(stat) else None,
                          return_value=stat if stat is not None and not callable(stat) else stats()):
            return probe.disk_snapshot()

    def test_reserved_space_is_not_counted_as_available(self):
        disk = self.sample('24 1 8:1 / / rw - ext4 /dev/sda1 rw')['disks'][0]
        self.assertEqual(disk['total_bytes'], 1000 * 4096)
        self.assertEqual(disk['used_bytes'], 900 * 4096)
        self.assertEqual(disk['available_bytes'], 80 * 4096)
        self.assertEqual(disk['reserved_bytes'], 20 * 4096)
        self.assertAlmostEqual(disk['used_percent'], 900 / 980 * 100)
        self.assertEqual(disk['total_bytes'], disk['used_bytes'] + disk['available_bytes'] + disk['reserved_bytes'])

    def test_escaped_mount_paths_and_devices_are_preserved(self):
        table = r'24 1 8:1 / /data\040with\040space rw shared:7 - xfs /dev/disk\040volume rw'
        with patch.object(probe.Path, 'read_text', return_value=table), patch.object(probe.os, 'statvfs', return_value=stats()) as stat:
            disk = probe.disk_snapshot()['disks'][0]
        self.assertEqual(disk['mountpoint'], '/data with space')
        self.assertEqual(disk['device'], '/dev/disk volume')
        stat.assert_called_once_with('/data with space')
        self.assertEqual(probe.mount_text(r'/a\134b\011c'), '/a\\b\tc')

    def test_pseudo_mounts_are_excluded_but_data_network_and_root_remain(self):
        table = '\n'.join([
            '24 1 0:2 / / rw - overlay overlay rw',
            '25 24 0:3 / /proc rw - proc proc rw',
            '26 24 0:4 / /dev/shm rw - tmpfs tmpfs rw',
            '27 24 8:2 / /data1 rw - xfs /dev/sdb1 rw',
            '28 24 0:5 / /shared rw - nfs4 storage:/group rw',
            '29 24 7:0 / /snap/tool ro - squashfs /dev/loop0 ro',
            '30 24 0:6 / /run/user/1008/gvfs rw - fuse.gvfsd-fuse gvfsd-fuse rw',
        ])
        disks = self.sample(table)['disks']
        self.assertEqual([d['mountpoint'] for d in disks], ['/', '/data1', '/shared'])

    def test_unreadable_mount_does_not_hide_healthy_disks(self):
        table = '24 1 8:1 / / rw - ext4 /dev/sda1 rw\n25 1 8:2 / /restricted rw - xfs /dev/sdb1 rw'
        def stat(path):
            if path == '/restricted':
                raise PermissionError('permission denied')
            return stats()
        result = self.sample(table, stat)
        self.assertTrue(result['disks'][0]['ok'])
        self.assertFalse(result['disks'][1]['ok'])
        self.assertIn('permission denied', result['disks'][1]['error'])
        self.assertIsNone(result['disk_error'])

    def test_missing_mount_table_is_a_partial_error(self):
        with patch.object(probe.Path, 'read_text', side_effect=FileNotFoundError('mountinfo missing')):
            result = probe.disk_snapshot()
        self.assertEqual(result['disks'], [])
        self.assertIn('mountinfo missing', result['disk_error'])

    def test_bind_paths_are_not_summed_and_overmount_keeps_visible_device(self):
        table = '\n'.join([
            '24 1 8:1 / /data rw - ext4 /dev/sda1 rw',
            '25 1 8:1 /group /home/group rw - ext4 /dev/sda1 rw',
            '26 1 8:2 / /data ro - xfs /dev/sdb1 ro',
            'malformed line',
        ])
        disks = self.sample(table)['disks']
        self.assertEqual(len(disks), 2)
        by_path = {d['mountpoint']: d for d in disks}
        self.assertEqual(by_path['/data']['device'], '/dev/sdb1')
        self.assertTrue(by_path['/data']['read_only'])
        self.assertEqual(by_path['/home/group']['device'], '/dev/sda1')

    def test_read_only_superblock_is_reported_even_when_mount_option_is_rw(self):
        disk = self.sample('24 1 8:1 / /data rw - ext4 /dev/sda1 ro')['disks'][0]
        self.assertTrue(disk['read_only'])

    def test_empty_filesystem_and_zero_fragment_size_are_handled(self):
        zero = stats(total=0, free=0, available=0, unit=0)
        zero.f_bsize = 4096
        disk = self.sample('24 1 8:1 / / rw - ext4 /dev/sda1 rw', zero)['disks'][0]
        self.assertEqual(disk['used_percent'], 0)
        self.assertEqual(disk['total_bytes'], 0)

    def test_cli_reports_capacity_and_mount_path_without_processes(self):
        disk = self.sample('24 1 8:1 / /data rw - ext4 /dev/sda1 rw')['disks'][0]
        row = dict(ok=True, id=1, alias='test-server', user='tester', host='127.0.0.1', port=22,
                   cpu_idle_percent=50, idle_cpu_equivalents=4, logical_cpus=8,
                   memory_available_gib=8, memory_total_gib=16, load_1_5_15=[1, 1, 1],
                   processes=[], disks=[disk])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            monitor.render([row])
        text = output.getvalue()
        self.assertIn('/data', text)
        self.assertIn('/dev/sda1 / ext4', text)
        self.assertIn('总容量、已用、可用空间', text)


if __name__ == '__main__':
    unittest.main()
