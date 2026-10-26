"""OpenSSH transport. Passwords reach askpass through a private local socket only."""
import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WINDOWS = os.name == 'nt'
try:
    from .paths import default_identity_file
except ImportError:
    from paths import default_identity_file


class PasswordPrompt:
    def __init__(self, password):
        self.password = password
        self.stop = threading.Event()

    def __enter__(self):
        self.directory = tempfile.TemporaryDirectory(prefix='monitor-pass-', dir='/tmp')
        root = Path(self.directory.name)
        self.socket_path = str(root / 's')
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(self.socket_path)
        os.chmod(self.socket_path, 0o600)
        self.socket.listen(2)
        self.socket.settimeout(.2)
        helper = root / 'askpass'
        helper.write_text('#!' + sys.executable + '\n' + '''import os,socket,sys
prompt = ' '.join(sys.argv[1:]).lower()
if 'password' not in prompt:
    sys.exit(1)
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(3)
s.connect(os.environ['SERVER_MONITOR_ASKPASS_SOCKET'])
parts = []
while True:
    b = s.recv(4096)
    if not b: break
    parts.append(b)
s.close()
sys.stdout.buffer.write(b''.join(parts) + b'\\n')
''')
        helper.chmod(0o700)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()
        self.env = dict(os.environ, SSH_ASKPASS=str(helper), SSH_ASKPASS_REQUIRE='force',
                        DISPLAY=os.environ.get('DISPLAY') or ':0', SERVER_MONITOR_ASKPASS_SOCKET=self.socket_path)
        return self.env

    def serve(self):
        while not self.stop.is_set():
            try:
                client, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with client:
                client.settimeout(2)
                try:
                    client.sendall(self.password.encode('utf-8'))
                except OSError:
                    pass

    def __exit__(self, *_):
        self.stop.set()
        self.socket.close()
        self.thread.join(timeout=1)
        self.directory.cleanup()
        self.password = None


def run_ssh(server, remote_command, payload='', timeout=45, password=None):
    if WINDOWS or server.get('transport') == 'paramiko':
        try:
            from .transport_paramiko import run_paramiko
        except ImportError:
            from transport_paramiko import run_paramiko
        return run_paramiko(server, remote_command, payload, timeout, password)
    auth = server.get('auth', 'key')
    args = ['ssh', '-T', '-o', 'ConnectTimeout=8', '-o', 'ConnectionAttempts=1',
            '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=2',
            '-o', 'StrictHostKeyChecking=accept-new', '-o', 'ForwardAgent=no',
            '-o', 'LogLevel=ERROR', '-o', 'NumberOfPasswordPrompts=1']
    if auth == 'password':
        if not password:
            raise ValueError('需要输入密码：请编辑此服务器的连接配置。')
        args += ['-o', 'PubkeyAuthentication=no', '-o', 'PreferredAuthentications=password,keyboard-interactive']
    elif password and auth == 'auto':
        args += ['-o', 'PreferredAuthentications=publickey,password,keyboard-interactive']
    else:
        args += ['-o', 'BatchMode=yes']
    identity = server.get('identity_file', default_identity_file())
    if identity and auth != 'password':
        args += ['-o', 'IdentitiesOnly=yes', '-i', str(Path(identity).expanduser())]
    args += ['-p', str(server['port']), '-l', server['user'], '--', server['host'], remote_command]
    kwargs = dict(input=payload, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                  timeout=timeout, start_new_session=True)
    if password and auth in ('password', 'auto'):
        with PasswordPrompt(password) as env:
            return subprocess.run(args, env=env, **kwargs)
    return subprocess.run(args, **kwargs)
