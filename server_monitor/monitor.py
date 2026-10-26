#!/usr/bin/env python3
"""Parallel, read-only server monitoring using SSH and Python stdlib."""
import argparse
import concurrent.futures
import json
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
try:
    from .transport import run_ssh
except ImportError:
    from transport import run_ssh


def inventory(selection=None):
    path = ROOT / 'servers.json'
    if not path.exists():
        path = ROOT / 'servers.example.json'
    servers = json.loads(path.read_text(encoding='utf-8'))
    if selection:
        wanted = {int(n.strip()) for n in selection.split(',')}
        known = {s['id'] for s in servers}
        if not wanted <= known:
            raise ValueError('未知服务器编号；可用编号: ' + ','.join(map(str, sorted(known))))
        servers = [s for s in servers if s['id'] in wanted]
    return servers


def probe(server, timeout=45, scope='mine', password=None):
    base = {k: server[k] for k in ('id', 'alias', 'host', 'port', 'user') if k in server}
    remote = 'LC_ALL=C ' + shlex.quote(server.get('python', 'python3')) + ' - --scope ' + scope
    try:
        result = run_ssh(server, remote, (ROOT / 'remote_probe.py').read_text(encoding='utf-8'), timeout, password)
        if result.returncode:
            return dict(base, ok=False, error=result.stderr.strip() or 'SSH 返回 ' + str(result.returncode))
        for line in reversed(result.stdout.splitlines()):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict) and 'logical_cpus' in data:
                return dict(base, ok=True, **data)
        return dict(base, ok=False, error='未收到有效采样数据，请检查远端Python及/proc权限。')
    except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
        message = ('采样超时（{}秒），请检查网络或增大超时。'.format(timeout)
                   if isinstance(exc, subprocess.TimeoutExpired) else str(exc))
        return dict(base, ok=False, error=message)


def snapshot(servers, timeout, scope='mine'):
    if not servers:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(servers)) as pool:
        futures = [pool.submit(probe, s, timeout, scope) for s in servers]
        return [f.result() for f in futures]


def number(value, spec='.1f'):
    return format(value, spec) if value is not None else 'N/A'


def safe(value):
    return ''.join(c if c.isprintable() else ' ' for c in str(value))


def render(rows, top=12, full_command=False):
    print('\n组内服务器 — ' + datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z'))
    print('编号  账号       CPU空闲  空闲逻辑核/总数    可用/总内存 GiB       1分钟负载  账号进程')
    for row in rows:
        if not row['ok']:
            print('{}     {}  连接失败: {}'.format(row['id'], row['user'], safe(row['error'])))
            continue
        print('{:<5} {:<10} {:>6}% {:>7}/{:<5} {:>9}/{:<9} {:>8} {:>8}'.format(
            row['id'], row['user'], number(row['cpu_idle_percent']),
            number(row['idle_cpu_equivalents']), row['logical_cpus'],
            number(row['memory_available_gib']), number(row['memory_total_gib']),
            number(row['load_1_5_15'][0]), len(row['processes'])))
    for row in rows:
        if not row['ok']:
            continue
        processes = row['processes'] if top == 0 else row['processes'][:top]
        print('\n[{}] {}@{}:{} — 显示 {}/{} 个进程'.format(
            row['alias'], row['user'], row['host'], row['port'], len(processes), len(row['processes'])))
        print('PID      用户          CPU%     RSS GiB  线程  已运行        状态  命令')
        for p in processes:
            seconds = int(p['elapsed_seconds'])
            age = '{}d {:02}:{:02}:{:02}'.format(seconds // 86400, seconds // 3600 % 24, seconds // 60 % 60, seconds % 60)
            command = safe(p['command'])
            if not full_command and len(command) > 140:
                command = command[:137] + '...'
            print('{:<8} {:<12} {:>7} {:>9.2f} {:>5} {:>13} {:>4}  {}'.format(
                p['pid'], p.get('user', row['user']), number(p['cpu_percent']), p['rss_gib'], p['threads'], age, p['state'], command))
        if not processes:
            print('当前没有其他进程。')
        if row.get('disk_error'):
            print('磁盘采集失败: ' + safe(row['disk_error']))
        elif 'disks' in row:
            print('\n磁盘/文件系统：总容量、已用、可用空间（GiB）与挂载路径')
            print('总容量      已用       可用    已用%  挂载路径  [设备 / 文件系统]')
            for disk in row['disks']:
                if not disk.get('ok'):
                    print('{}  {}'.format(safe(disk['mountpoint']), safe(disk.get('error', '无法读取容量'))))
                    continue
                print('{:>8.1f} {:>9.1f} {:>9.1f} {:>7.1f}%  {}  [{} / {}]{}'.format(
                    disk['total_bytes'] / 1024**3, disk['used_bytes'] / 1024**3,
                    disk['available_bytes'] / 1024**3, disk['used_percent'],
                    safe(disk['mountpoint']), safe(disk['device']), safe(disk['filesystem']),
                    '（只读）' if disk.get('read_only') else ''))
    print('\nCPU为约1秒采样；空闲核是逻辑核等效值，不是资源预留。可用内存含可回收缓存。')
    print('进程CPU 100%=占满1个逻辑核；N/A=采样期间新出现。默认按CPU/RSS排序；--top 0 显示全部。')
    print('磁盘按挂载点列出，不跨挂载点求和；可用空间不含文件系统保留块。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope', choices=('mine', 'all'), default='mine', help='我的进程或全部可见进程')
    parser.add_argument('--servers', help='编号，逗号分隔，例如 2,4')
    parser.add_argument('--watch', type=float, metavar='SECONDS', help='持续刷新间隔，至少2秒；Ctrl+C退出')
    parser.add_argument('--json', action='store_true', help='输出完整JSON，含全部进程；watch时每轮一行JSON')
    parser.add_argument('--top', type=int, default=12, help='每台显示进程数，0=全部，默认12')
    parser.add_argument('--full-command', action='store_true', help='不截断进程命令')
    parser.add_argument('--timeout', type=float, default=45, help='每台SSH总超时秒数，默认45')
    args = parser.parse_args()
    if args.top < 0 or args.timeout <= 0 or (args.watch is not None and args.watch < 2):
        parser.error('top必须>=0，timeout必须>0，watch必须>=2')
    try:
        servers = inventory(args.servers)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        while True:
            started = time.monotonic()
            rows = snapshot(servers, args.timeout, args.scope)
            if args.json:
                print(json.dumps({'sampled_at': datetime.now(timezone.utc).isoformat(), 'servers': rows}, ensure_ascii=False), flush=True)
            else:
                if args.watch and sys.stdout.isatty():
                    print('\033[2J\033[H', end='')
                render(rows, args.top, args.full_command)
            if args.watch is None:
                return 1 if any(not r['ok'] for r in rows) else 0
            time.sleep(max(0, args.watch - (time.monotonic() - started)))
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    sys.exit(main())
