"""Cross-platform SSH backend, used by Windows; passwords stay in memory."""
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

try:
    from .paths import default_identity_file
except ImportError:
    from paths import default_identity_file

_HOST_KEY_LOCK = threading.Lock()


def run_paramiko(server, remote_command, payload='', timeout=45, password=None):
    try:
        import paramiko
    except ImportError:
        raise ValueError('Windows SSH依赖未安装：请先运行 Install-Windows.bat，再运行 Start-Windows.bat。')
    auth = server.get('auth', 'key')
    if auth == 'password' and not password:
        raise ValueError('需要输入密码：请编辑此服务器的连接配置。')
    key_file = server.get('identity_file', default_identity_file()) or ''
    if key_file:
        path = Path(key_file).expanduser()
        if not path.is_file():
            if auth == 'key':
                raise ValueError('本机找不到私钥，请选择新电脑上的密钥路径，或切换为密码认证。')
            key_file = ''
        else:
            key_file = str(path)
    known_hosts = Path.home() / '.ssh' / 'known_hosts'

    class AcceptNew(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client, hostname, key):
            # Append under a lock so parallel connections never overwrite other new keys.
            with _HOST_KEY_LOCK:
                known = paramiko.HostKeys()
                if known_hosts.exists():
                    known.load(str(known_hosts))
                if known.lookup(hostname):
                    if not known.check(hostname, key):
                        raise ValueError('Host key verification failed: 主机密钥与本机记录不符。')
                else:
                    known_hosts.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    descriptor = os.open(str(known_hosts), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                    with os.fdopen(descriptor, 'a', encoding='utf-8') as stream:
                        stream.write('\n{} {} {}\n'.format(hostname, key.get_name(), key.get_base64()))
                client.get_host_keys().add(hostname, key.get_name(), key)

    deadline = time.monotonic() + timeout
    client = paramiko.SSHClient()
    timer = threading.Timer(timeout, client.close)
    timer.daemon = True
    try:
        if known_hosts.exists():
            client.load_host_keys(str(known_hosts))
        client.set_missing_host_key_policy(AcceptNew())
        timer.start()
        client.connect(hostname=server['host'], port=int(server['port']), username=server['user'],
                       password=password if auth != 'key' else None,
                       key_filename=key_file if auth != 'password' and key_file else None,
                       allow_agent=auth != 'password', look_for_keys=auth != 'password',
                       timeout=min(8, timeout), banner_timeout=min(15, timeout),
                       auth_timeout=timeout, channel_timeout=timeout)
        transport = client.get_transport()
        if transport is None:
            raise OSError('SSH连接已关闭。')
        transport.set_keepalive(30)
        remaining = max(.1, deadline - time.monotonic())
        channel = transport.open_session(timeout=remaining)
        channel.settimeout(remaining)
        channel.exec_command(remote_command)
        channel.sendall(payload.encode('utf-8'))
        channel.shutdown_write()
        stdout, stderr = bytearray(), bytearray()
        while True:
            received = False
            if channel.recv_ready():
                stdout.extend(channel.recv(65536))
                received = True
            if channel.recv_stderr_ready():
                stderr.extend(channel.recv_stderr(65536))
                received = True
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                break
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired('ssh', timeout)
            if not received:
                time.sleep(.02)
        return subprocess.CompletedProcess(['ssh', server['host']], channel.recv_exit_status(),
                                           stdout.decode('utf-8', 'replace'), stderr.decode('utf-8', 'replace'))
    except paramiko.BadHostKeyException:
        raise ValueError('Host key verification failed: 主机密钥与本机记录不符，请向管理员核实。')
    except paramiko.AuthenticationException:
        raise ValueError('Permission denied: 请检查用户名、密码或本机SSH密钥。')
    except (socket.timeout, TimeoutError):
        raise subprocess.TimeoutExpired('ssh', timeout)
    except (paramiko.SSHException, EOFError, OSError) as exc:
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired('ssh', timeout)
        raise OSError('SSH连接失败: ' + str(exc))
    finally:
        timer.cancel()
        client.close()
