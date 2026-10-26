#!/usr/bin/env python3
"""Install SSH aliases and authorize the existing public key. Never saves a password."""
import argparse
import getpass
import json
import os
import pty
import select
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SSH_DIR = Path.home() / '.ssh'
KEY = SSH_DIR / 'id_ed25519_cnot'


def config_text(servers):
    chunks = ['# Managed by lab_servers/setup.py; configured lab servers.\n']
    for server in servers:
        aliases = server['alias'] + (' zzy-server' if server['id'] == 2 else '')
        chunks.append('Host {}\n  HostName {}\n  Port {}\n  User {}\n  IdentityFile {}\n  IdentitiesOnly yes\n  ConnectTimeout 7\n  ServerAliveInterval 30\n  ServerAliveCountMax 3\n  StrictHostKeyChecking yes\n  ForwardAgent no\n\n'.format(
            aliases, server['host'], server['port'], server['user'], KEY))
    return '\n'.join(chunks)


def install_config(servers):
    SSH_DIR.mkdir(mode=0o700, exist_ok=True)
    target = SSH_DIR / 'lab_servers.conf'
    config = SSH_DIR / 'config'
    include = 'Include "{}"'.format(target)
    original = config.read_text() if config.exists() else ''
    # Exact managed include, prepended so alias settings take precedence over Host *.
    remaining = '\n'.join(line for line in original.splitlines() if line.strip() != include).lstrip('\n')
    updated = include + '\n\n' + remaining + ('\n' if remaining else '')
    if original != updated:
        if config.exists():
            backup = SSH_DIR / ('config.before-lab-' + time.strftime('%Y%m%d-%H%M%S'))
            shutil.copy2(config, backup)
            print('原配置备份:', backup)
        config.write_text(updated)
        config.chmod(0o600)
    target.write_text(config_text(servers))
    target.chmod(0o600)
    print('已安装:', target)


def ssh_args(server, batch=True):
    args = ['ssh', '-T', '-o', 'ConnectTimeout=7', '-o', 'ConnectionAttempts=1',
            '-o', 'StrictHostKeyChecking=accept-new', '-o', 'IdentitiesOnly=yes',
            '-o', 'LogLevel=ERROR', '-i', str(KEY), '-p', str(server['port'])]
    if batch:
        args += ['-o', 'BatchMode=yes']
    else:
        args += ['-o', 'NumberOfPasswordPrompts=1']
    return args + [server['user'] + '@' + server['host']]


def password_ssh(args, password, timeout=35):
    pid, master = pty.fork()
    if pid == 0:
        os.execvp(args[0], args)
    deadline = time.monotonic() + timeout
    output = bytearray()
    pending = bytearray()
    sent = False
    status = None
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master], [], [], 0.2)
            if readable:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    chunk = b''
                if not chunk:
                    break
                output.extend(chunk)
                pending.extend(chunk)
                if not sent and b'password:' in pending.lower():
                    os.write(master, password.encode() + b'\n')
                    sent = True
                    pending.clear()
            ended, status_value = os.waitpid(pid, os.WNOHANG)
            if ended:
                status = status_value
                break
        if status is None:
            ended, status_value = os.waitpid(pid, os.WNOHANG)
            if ended:
                status = status_value
            else:
                os.kill(pid, 15)
                _, status = os.waitpid(pid, 0)
    finally:
        os.close(master)
    # Do not print transcripts: remote banners can contain arbitrary data.
    return os.waitstatus_to_exitcode(status) == 0 and b'LAB_KEY_READY' in output


def check(server):
    try:
        result = subprocess.run(ssh_args(server) + ['LC_ALL=C ' + shlex.quote(server.get('python', 'python3')) + ' -c ' + shlex.quote(
            'import os,pwd;print("LAB_READY",pwd.getpwuid(os.geteuid()).pw_name,os.uname().nodename)')],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=45)
        return result.returncode == 0 and 'LAB_READY ' in result.stdout, result
    except subprocess.TimeoutExpired:
        return False, None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-config', action='store_true')
    parser.add_argument('--authorize', action='store_true', help='使用一次密码将现有公钥加入各账号 authorized_keys')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--servers', help='只配置这些编号，例如 3,7')
    parser.add_argument('--per-server-password', action='store_true', help='每台首次配置时单独输入密码')
    args = parser.parse_args()
    if not (args.install_config or args.authorize or args.check):
        parser.error('请选择 --install-config / --authorize / --check')
    servers = json.loads((ROOT / 'servers.json').read_text())
    if not KEY.is_file() or not Path(str(KEY) + '.pub').is_file():
        parser.error('缺少现有密钥 ' + str(KEY))
    if args.install_config:
        install_config(servers)
    if args.servers:
        wanted = {int(x) for x in args.servers.split(',')}
        if not wanted <= {s['id'] for s in servers}:
            parser.error('未知服务器编号')
        servers = [s for s in servers if s['id'] in wanted]
    password = None
    failures = []
    for server in servers if (args.authorize or args.check) else []:
        ok, result = check(server)
        if not ok and args.authorize:
            if password is None or args.per_server_password:
                password = getpass.getpass(server['alias'] + ' 首次配置密码（不保存）: ')
            public = Path(str(KEY) + '.pub').read_text().strip()
            script = ('umask 077; mkdir -p "$HOME/.ssh" && '
                      'chmod 700 "$HOME/.ssh" && touch "$HOME/.ssh/authorized_keys" && '
                      'chmod 600 "$HOME/.ssh/authorized_keys" && '
                      '(grep -qxF -- {key} "$HOME/.ssh/authorized_keys" || '
                      'printf "\\n%s\\n" {key} >> "$HOME/.ssh/authorized_keys") && '
                      'printf "LAB_KEY_READY\\n"').format(key=shlex.quote(public))
            installed = password_ssh(ssh_args(server, False) + [script], password)
            ok, result = check(server)
            if not installed and not ok:
                print(server['alias'], '公钥配置失败')
        if ok:
            print(server['alias'], '免密码连接成功:', result.stdout.strip())
        else:
            failures.append(server['id'])
            print(server['alias'], '连接失败:', result.stderr.strip() if result else '连接超时')
    password = None
    return int(bool(failures))


if __name__ == '__main__':
    sys.exit(main())
