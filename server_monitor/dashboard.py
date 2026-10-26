#!/usr/bin/env python3
"""Local browser GUI for multi-server monitoring."""
import argparse
import concurrent.futures
import copy
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

try:
    from .monitor import inventory, probe
except ImportError:
    from monitor import inventory, probe

ROOT = Path(__file__).resolve().parent
try:
    from .paths import default_identity_file
    from .credential_store import CredentialStore, CredentialError
except ImportError:
    from paths import default_identity_file
    from credential_store import CredentialStore, CredentialError
DEFAULT_KEY = default_identity_file()


def atomic_json(path, data):
    temporary = path.with_name(path.name + '.tmp-' + secrets.token_hex(4))
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def validate_server(raw, sid=None, allow_missing_user=False):
    def string(key, default='', limit=300):
        value = raw.get(key, default)
        if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
            raise ValueError('字段格式不正确: ' + key)
        return value.strip()
    host = string('host', limit=253)
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._:\-]*', host):
        raise ValueError('请输入正确的IP地址或主机名（不含协议和端口）。')
    user = string('user', limit=64)
    remember_user = bool(raw.get('remember_user', True))
    remember_password = bool(raw.get('remember_password', False))
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.\-]*\$?', user) and not (allow_missing_user and not user and not remember_user):
        raise ValueError('请输入正确的Linux用户名。')
    if remember_password and not remember_user:
        raise ValueError('记住密码时需要同时记住用户名。')
    try:
        port = int(raw.get('port', 22))
    except (ValueError, TypeError):
        raise ValueError('端口必须是1–65535之间的整数。')
    if not 1 <= port <= 65535:
        raise ValueError('端口必须是1–65535之间的整数。')
    auth = raw.get('auth', 'auto')
    if auth not in ('auto', 'key', 'password'):
        raise ValueError('请选择密钥、密码或自动认证。')
    python = string('python', 'python3') or 'python3'
    credential_id = string('credential_id', '', 32)
    if credential_id and not re.fullmatch(r'[0-9a-f]{32}', credential_id):
        raise ValueError('保存的凭据标识无效。')
    return {'id': sid or uuid.uuid4().hex[:12], 'name': string('name', host, 80) or host,
            'alias': string('alias', '', 80), 'host': host, 'port': port, 'user': user,
            'auth': auth, 'identity_file': string('identity_file', DEFAULT_KEY, 500),
            'python': python, 'enabled': bool(raw.get('enabled', True)),
            'remember_user': remember_user, 'remember_password': remember_password,
            'credential_id': credential_id}


class Dashboard:
    def __init__(self, config_path, probe_fn=probe, credential_store=None):
        self.path = Path(config_path)
        self.probe_fn = probe_fn
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.passwords = {}
        self.credentials = credential_store if credential_store is not None else CredentialStore()
        self.states = {}
        self.revision = 0
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=12)
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding='utf-8'))
            self.servers = [validate_server(s, str(s['id']), allow_missing_user=True) for s in raw['servers']]
            self.settings = dict(interval=raw.get('interval', 10), scope=raw.get('scope', 'mine'),
                                 paused=False, timeout=raw.get('timeout', 45))
        else:
            source = inventory()
            self.servers = [validate_server(dict(s, name=str(s['id']) + ' 号服务器', auth=s.get('auth', 'key' if DEFAULT_KEY else 'password')),
                                           'server-' + str(s['id'])) for s in source]
            self.settings = dict(interval=10, scope='mine', paused=False, timeout=45)
        if self.settings['scope'] not in ('mine', 'all'):
            self.settings['scope'] = 'mine'
        self.settings['interval'] = max(3, min(300, float(self.settings['interval'])))
        self.settings['timeout'] = max(5, min(180, float(self.settings['timeout'])))
        if len({s['id'] for s in self.servers}) != len(self.servers):
            raise ValueError('服务器ID重复，请修复GUI配置。')
        for server in self.servers:
            self.states[server['id']] = self.new_state()
            self.restore_password(server)

    @staticmethod
    def identity(server):
        return [server['host'], server['port'], server['user']]

    def restore_password(self, server):
        if not server['remember_password'] or not server['credential_id']:
            return
        try:
            saved = self.credentials.get(server['credential_id'])
            record = json.loads(saved) if saved else None
            if (isinstance(record, dict) and record.get('identity') == self.identity(server)
                    and isinstance(record.get('password'), str) and record['password']):
                self.passwords[server['id']] = record['password']
        except (CredentialError, OSError, ValueError):
            self.states[server['id']]['credential_error'] = '读取保存的密码失败，请重新输入或检查系统凭据存储。'

    def needs_login(self, server):
        return not server['user'] or server['auth'] == 'password' and server['id'] not in self.passwords

    @staticmethod
    def new_state():
        return dict(data=None, error='', error_at=None, last_attempt=None, last_success=None,
                    status='waiting', busy=False, next_due=0, version=0, retry_blocked=False)

    def save(self):
        servers = [dict(s, user=s['user'] if s['remember_user'] else '') for s in self.servers]
        atomic_json(self.path, dict(servers=servers, interval=self.settings['interval'],
                                   scope=self.settings['scope'], timeout=self.settings['timeout']))

    def public_state(self):
        with self.lock:
            rows = []
            for server in self.servers:
                state = copy.deepcopy(self.states[server['id']])
                state.pop('next_due', None)
                state.pop('version', None)
                row = dict(server, **state, has_password=server['id'] in self.passwords)
                row.pop('credential_id', None)
                row['password_saved'] = bool(server['remember_password'] and server['credential_id'])
                if not server['enabled']:
                    row['status'] = 'disabled'
                elif self.needs_login(server):
                    row['status'] = 'login'
                    row['error'] = row.get('credential_error') or '请填写用户名和密码。'
                if row['data'] and row['data'].get('scope', 'mine') != self.settings['scope']:
                    row['data']['processes'] = []
                    row['scope_pending'] = True
                rows.append(row)
            return dict(servers=rows, settings=dict(self.settings), password_storage=self.credentials.name,
                        server_time=datetime.now(timezone.utc).isoformat())

    def set_server(self, raw):
        with self.lock:
            sid = raw.get('id')
            previous = next((s for s in self.servers if s['id'] == sid), None)
            if sid and previous is None:
                raise ValueError('服务器已删除，请刷新界面。')
            if previous is None and len(self.servers) >= 64:
                raise ValueError('最多配置64台服务器。')
            server = validate_server(raw, sid)
            server['credential_id'] = previous.get('credential_id', '') if previous else ''
            password = raw.get('password')
            if password is not None and (not isinstance(password, str) or len(password) > 1024 or '\n' in password or '\r' in password or '\x00' in password):
                raise ValueError('密码包含不支持的字符或过长。')
            identity_changed = previous is not None and self.identity(previous) != self.identity(server)
            current_password = None if identity_changed or previous is None else self.passwords.get(sid)
            if raw.get('clear_password'):
                current_password = None
                server['remember_password'] = False
            if password:
                current_password = password
            if server['remember_password']:
                if not current_password:
                    raise ValueError('请输入要记住的登录密码。')
                server['credential_id'] = server['credential_id'] or uuid.uuid4().hex
                self.credentials.set(server['credential_id'], json.dumps(
                    dict(identity=self.identity(server), password=current_password), ensure_ascii=False))
            elif server['credential_id']:
                self.credentials.delete(server['credential_id'])
                server['credential_id'] = ''
            if previous:
                state = self.states[sid]
                state['version'] += 1
                if identity_changed:
                    state['data'] = None
                    state['last_success'] = None
                self.servers[self.servers.index(previous)] = server
            else:
                sid = server['id']
                self.servers.append(server)
                self.states[sid] = self.new_state()
                state = self.states[sid]
            self.passwords.pop(sid, None)
            if current_password:
                self.passwords[sid] = current_password
            state.pop('credential_error', None)
            state.update(next_due=0, error='', error_at=None, retry_blocked=False, status='waiting' if server['enabled'] else 'disabled')
            self.save()
            self.wake.set()
            return sid

    def remove(self, sid):
        with self.lock:
            if not any(s['id'] == sid for s in self.servers):
                raise ValueError('未找到服务器。')
            server = next(s for s in self.servers if s['id'] == sid)
            if server['credential_id']:
                self.credentials.delete(server['credential_id'])
            self.servers = [s for s in self.servers if s['id'] != sid]
            self.states.pop(sid, None)
            self.passwords.pop(sid, None)
            self.save()
            self.wake.set()

    def update_settings(self, raw):
        with self.lock:
            updated = dict(self.settings)
            if 'scope' in raw:
                if raw['scope'] not in ('mine', 'all'):
                    raise ValueError('进程模式无效。')
                updated['scope'] = raw['scope']
            for key, low, high in [('interval', 3, 300), ('timeout', 5, 180)]:
                if key in raw:
                    try:
                        value = float(raw[key])
                    except (ValueError, TypeError):
                        raise ValueError('刷新间隔和超时必须是数字。')
                    if not low <= value <= high:
                        raise ValueError('{}必须在{}到{}秒之间。'.format(key, low, high))
                    updated[key] = value
            if 'paused' in raw:
                updated['paused'] = bool(raw['paused'])
            if updated['scope'] != self.settings['scope']:
                self.revision += 1
                for state in self.states.values():
                    state['next_due'] = 0
            self.settings = updated
            self.save()
            self.wake.set()

    def refresh(self, sid=None):
        with self.lock:
            for server in self.servers:
                if sid is None or sid == server['id']:
                    self.states[server['id']]['next_due'] = 0
                    self.states[server['id']]['retry_blocked'] = False
            self.wake.set()

    def finished(self, future, sid, version, revision, scope):
        try:
            data = future.result()
        except Exception as exc:
            data = dict(ok=False, error='采样失败: ' + str(exc))
        with self.lock:
            state = self.states.get(sid)
            if state is None:
                return
            state['busy'] = False
            if state['version'] != version or self.revision != revision:
                state['next_due'] = 0
                state['status'] = 'waiting'
            elif data.get('ok'):
                data['scope'] = scope
                state.update(data=data, last_success=datetime.now(timezone.utc).isoformat(), error='', error_at=None, status='online', retry_blocked=False)
            else:
                message = data.get('error') or '未知错误'
                blocked = any(text in message for text in ('Permission denied', '需要输入密码', 'Host key verification failed', 'REMOTE HOST IDENTIFICATION HAS CHANGED'))
                state.update(error=message, retry_blocked=blocked, next_due=time.monotonic() + max(30, self.settings['interval']),
                             error_at=datetime.now(timezone.utc).isoformat(), status='error')
            self.wake.set()

    def schedule_once(self):
        with self.lock:
            if self.settings['paused'] or self.stop.is_set():
                return
            now = time.monotonic()
            for server in self.servers:
                state = self.states[server['id']]
                if not server['enabled'] or self.needs_login(server) or state['busy'] or state['retry_blocked'] or state['next_due'] > now:
                    continue
                scope = self.settings['scope']
                version, revision = state['version'], self.revision
                state.update(busy=True, last_attempt=datetime.now(timezone.utc).isoformat(),
                             next_due=now + self.settings['interval'])
                future = self.pool.submit(self.probe_fn, copy.deepcopy(server), self.settings['timeout'],
                                          scope, self.passwords.get(server['id']))
                future.add_done_callback(lambda f, sid=server['id'], v=version, r=revision, sc=scope:
                                         self.finished(f, sid, v, r, sc))

    def loop(self):
        while not self.stop.is_set():
            self.schedule_once()
            self.wake.wait(.3)
            self.wake.clear()

    def close(self):
        self.stop.set()
        self.wake.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
        with self.lock:
            self.passwords.clear()


def make_handler(app, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, status, data, mime='application/json; charset=utf-8'):
            content = json.dumps(data, ensure_ascii=False).encode() if isinstance(data, (dict, list)) else data
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def allowed(self, api=False):
            expected = '127.0.0.1:' + str(self.server.server_port)
            if self.headers.get('Host') != expected:
                self.respond(403, {'error': '仅允许通过本机地址访问。'})
                return False
            if api and not hmac.compare_digest(self.headers.get('X-Monitor-Token', ''), token):
                self.respond(403, {'error': '会话已失效，请重新双击启动程序。'})
                return False
            origin = self.headers.get('Origin')
            if origin and origin != 'http://' + expected:
                self.respond(403, {'error': '请求来源无效。'})
                return False
            return True

        def do_GET(self):
            path = urlsplit(self.path).path
            if not self.allowed(path.startswith('/api/')):
                return
            if path == '/api/state':
                self.respond(200, app.public_state())
            elif path == '/api/ping':
                self.respond(200, dict(ok=True, pid=os.getpid()))
            elif path in ('/', '/app.js', '/style.css'):
                name, mime = {'/': ('index.html', 'text/html; charset=utf-8'),
                              '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                              '/style.css': ('style.css', 'text/css; charset=utf-8')}[path]
                self.respond(200, (ROOT / 'web' / name).read_bytes(), mime)
            elif path == '/favicon.ico':
                self.respond(204, b'', 'image/x-icon')
            else:
                self.respond(404, {'error': '未找到页面。'})

        def do_POST(self):
            if not self.allowed(True):
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16000:
                    raise ValueError('请求大小无效。')
                raw = json.loads(self.rfile.read(length))
                if not isinstance(raw, dict):
                    raise ValueError('请求内容无效。')
                path = urlsplit(self.path).path
                if path == '/api/server':
                    result = dict(id=app.set_server(raw))
                elif path == '/api/remove':
                    app.remove(raw.get('id'))
                    result = dict(ok=True)
                elif path == '/api/settings':
                    app.update_settings(raw)
                    result = dict(ok=True)
                elif path == '/api/refresh':
                    app.refresh(raw.get('id'))
                    result = dict(ok=True)
                elif path == '/api/shutdown':
                    app.close()
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    result = dict(ok=True)
                else:
                    self.respond(404, {'error': '未找到操作。'})
                    return
                self.respond(200, result)
            except (ValueError, KeyError, TypeError) as exc:
                self.respond(400, {'error': str(exc)})
            except OSError:
                self.respond(500, {'error': '配置保存失败，请检查本地文件写入权限。'})
    return Handler


def existing(runtime):
    try:
        info = json.loads(runtime.read_text(encoding='utf-8'))
        if info.get('pid') == os.getpid():
            return None
        request = urllib.request.Request('http://127.0.0.1:{}/api/ping'.format(int(info['port'])),
                                         headers={'X-Monitor-Token': info['token']})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        response = json.loads(opener.open(request, timeout=1).read())
        if response.get('pid') == info['pid']:
            return info
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        pass
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--open', action='store_true', help='自动打开浏览器')
    parser.add_argument('--port', type=int, default=0, help='默认选择本机空闲端口')
    parser.add_argument('--config', type=Path, default=ROOT / 'gui_config.json')
    args = parser.parse_args()
    runtime = args.config.with_suffix('.runtime.json')
    info = existing(runtime)
    if info:
        if args.open:
            webbrowser.open('http://127.0.0.1:{}/#{}'.format(info['port'], info['token']))
        print('服务器监控已在运行。')
        return
    app = Dashboard(args.config)
    token = secrets.token_urlsafe(32)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(app, token))
    server.daemon_threads = True
    atomic_json(runtime, dict(pid=os.getpid(), port=server.server_port, token=token))
    worker = threading.Thread(target=app.loop, daemon=True)
    worker.start()
    url = 'http://127.0.0.1:{}/#{}'.format(server.server_port, token)
    print('服务器监控已启动。点击界面左下角“退出程序”关闭。', flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=.3)
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
        server.server_close()
        try:
            if json.loads(runtime.read_text(encoding='utf-8')).get('pid') == os.getpid():
                runtime.unlink()
        except (OSError, ValueError):
            pass


if __name__ == '__main__':
    main()
