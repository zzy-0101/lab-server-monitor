"""Linux read-only probe; sent over SSH stdin, requires Python 3.6+."""
import json
import os
import pwd
import re
import time
from datetime import datetime, timezone
from pathlib import Path

_USERNAMES = {}
_VIRTUAL_FILESYSTEMS = {
    'proc', 'sysfs', 'tmpfs', 'devtmpfs', 'devpts', 'cgroup', 'cgroup2',
    'pstore', 'securityfs', 'debugfs', 'tracefs', 'configfs', 'fusectl',
    'mqueue', 'hugetlbfs', 'rpc_pipefs', 'autofs', 'binfmt_misc', 'nsfs',
    'efivarfs', 'selinuxfs', 'bpf', 'squashfs', 'ramfs', 'fuse.gvfsd-fuse', 'fuse.portal',
}


def mount_text(value):
    """Decode the octal path escapes in Linux mountinfo, including spaces."""
    return re.sub(r'\\([0-7]{3})', lambda match: chr(int(match.group(1), 8)), value)


def disk_snapshot():
    """Mounted filesystem capacity; never sum rows, since bind mounts can repeat space."""
    try:
        lines = Path('/proc/self/mountinfo').read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError as exc:
        return {'disks': [], 'disk_error': '无法读取挂载表: ' + str(exc)}
    mounts = {}
    for line in lines:
        try:
            left, right = line.split(' - ', 1)
            fields, filesystem = left.split(), right.split()
            mountpoint = mount_text(fields[4])
            fs_type = filesystem[0]
            if fs_type in _VIRTUAL_FILESYSTEMS:
                continue
            # Keep the visible entry if a path is overmounted.
            mounts[mountpoint] = {
                'mountpoint': mountpoint, 'device': mount_text(filesystem[1]),
                'filesystem': fs_type, 'device_id': fields[2],
                'read_only': ('ro' in fields[5].split(',') or
                              (len(filesystem) > 2 and 'ro' in filesystem[2].split(','))),
            }
        except (ValueError, IndexError):
            continue
    rows = []
    for mountpoint, row in sorted(mounts.items(), key=lambda item: (item[0] != '/', item[0])):
        try:
            stats = os.statvfs(mountpoint)
            block_size = stats.f_frsize or stats.f_bsize
            total = max(0, stats.f_blocks * block_size)
            free = max(0, stats.f_bfree * block_size)
            available = max(0, stats.f_bavail * block_size)
            used = max(0, total - free)
            # Match df: reserved free blocks are not considered available to normal users.
            denominator = used + available
            row.update(ok=True, total_bytes=total, used_bytes=used, free_bytes=free,
                       available_bytes=available, reserved_bytes=max(0, free - available),
                       used_percent=100 * used / denominator if denominator else 0.0)
        except OSError as exc:
            row.update(ok=False, error='无法读取容量: ' + str(exc), total_bytes=None,
                       used_bytes=None, free_bytes=None, available_bytes=None,
                       reserved_bytes=None, used_percent=None)
        rows.append(row)
    return {'disks': rows, 'disk_error': None}


def cpu_snapshot():
    lines = Path('/proc/stat').read_text().splitlines()
    # guest times are already included in user/nice, so use only first 8 fields.
    fields = list(map(int, lines[0].split()[1:9]))
    return sum(fields), fields[3], fields[4], sum(line.startswith('cpu') and line[3:4].isdigit() for line in lines)


def processes(uid):
    result = {}
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            # effective UID, matching the logged-in account.
            status = (path / 'status').read_text()
            effective = int(next(x for x in status.splitlines() if x.startswith('Uid:')).split()[2])
            if uid is not None and effective != uid:
                continue
            raw = (path / 'stat').read_text()
            measured_at = time.monotonic()
            end = raw.rfind(')')
            fields = raw[end + 2:].split()
            pid = int(path.name)
            try:
                command = (path / 'cmdline').read_bytes().replace(b'\0', b' ').decode('utf-8', 'replace').strip()
            except OSError:
                command = ''
            if effective not in _USERNAMES:
                try:
                    _USERNAMES[effective] = pwd.getpwuid(effective).pw_name
                except KeyError:
                    _USERNAMES[effective] = str(effective)
            username = _USERNAMES[effective]
            try:
                cwd = os.readlink(str(path / 'cwd'))
            except OSError:
                cwd = None
            result[pid] = {'pid': pid, 'user': username, 'uid': effective, 'ppid': int(fields[1]), 'state': fields[0],
                           'ticks': int(fields[11]) + int(fields[12]), 'measured_at': measured_at,
                           'start_ticks': int(fields[19]), 'threads': int(fields[17]),
                           'rss_gib': max(0, int(fields[21])) * os.sysconf('SC_PAGE_SIZE') / 1024**3,
                           'command': command or raw[raw.find('(') + 1:end], 'cwd': cwd}
        except (OSError, ValueError, StopIteration, IndexError):
            continue  # A process may exit while /proc is being read.
    return result


def collect(sample_seconds=1.0, scope='mine'):
    if scope not in ('mine', 'all'):
        raise ValueError('scope must be mine or all')
    uid = os.geteuid() if scope == 'mine' else None
    start_cpu = cpu_snapshot()
    before = processes(uid)
    start = time.monotonic()
    time.sleep(sample_seconds)
    end_cpu = cpu_snapshot()
    after = processes(uid)
    elapsed = time.monotonic() - start
    mem = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        mem[key] = int(value.split()[0])
    # Exclude only this probe and its ancestor chain, not the user's other sessions/tasks.
    excluded = {os.getpid()}
    current = os.getpid()
    while current in after:
        current = after[current]['ppid']
        if current in excluded:
            break
        excluded.add(current)
    uptime = float(Path('/proc/uptime').read_text().split()[0])
    hz = os.sysconf('SC_CLK_TCK')
    rows = []
    for pid, row in after.items():
        if pid in excluded:
            continue
        previous = before.get(pid)
        interval = (row.get('measured_at', elapsed) - previous.get('measured_at', 0)) if previous else elapsed
        row['cpu_percent'] = (max(0, row['ticks'] - previous['ticks']) / hz / max(interval, .001) * 100
                              if previous and previous['start_ticks'] == row['start_ticks'] else None)
        row['elapsed_seconds'] = max(0, uptime - row['start_ticks'] / hz)
        row.pop('measured_at', None)
        del row['ticks']
        del row['start_ticks']
        rows.append(row)
    rows.sort(key=lambda p: (p['cpu_percent'] or 0, p['rss_gib']), reverse=True)
    total = end_cpu[0] - start_cpu[0]
    idle = 100 * (end_cpu[1] - start_cpu[1]) / total if total > 0 else None
    iowait = 100 * (end_cpu[2] - start_cpu[2]) / total if total > 0 else None
    cores = end_cpu[3]
    # MemAvailable is the kernel estimate including reclaimable cache.
    available = mem.get('MemAvailable')
    return {'sampled_at': datetime.now(timezone.utc).isoformat(), 'hostname': os.uname().nodename,
            'user': pwd.getpwuid(os.geteuid()).pw_name, 'scope': scope,
            'visibility_note': '仅包含当前账号有权读取的进程；系统可能限制其他用户的详情。', 'sample_seconds': round(elapsed, 3),
            'logical_cpus': cores, 'cpu_idle_percent': idle, 'cpu_iowait_percent': iowait,
            'idle_cpu_equivalents': cores * idle / 100 if idle is not None else None,
            'load_1_5_15': list(os.getloadavg()), 'memory_total_gib': mem['MemTotal'] / 1024**2,
            'memory_available_gib': available / 1024**2 if available is not None else None,
            'swap_free_gib': mem.get('SwapFree', 0) / 1024**2, 'processes': rows,
            **disk_snapshot()}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--scope', choices=('mine', 'all'), default='mine')
    args = parser.parse_args()
    print(json.dumps(collect(scope=args.scope), ensure_ascii=True))
