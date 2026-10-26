#!/usr/bin/env python3
"""Build a portable distribution from an explicit allowlist, excluding user state and secrets."""
import json
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    'docs/portable/README.md': 'README.md',
    'docs/guide/服务器监控入门指南.pdf': '使用说明.pdf',
    'tools/launch_monitor.py': 'tools/launch_monitor.py',
    '服务器监控.command': 'Start-Mac.command',
    'Start-Windows.bat': 'Start-Windows.bat',
    'Install-Windows.bat': 'Install-Windows.bat',
    'lab_servers/requirements-windows.txt': 'lab_servers/requirements-windows.txt',
}
for name in ('dashboard.py', 'monitor.py', 'remote_probe.py', 'transport.py', 'transport_paramiko.py', 'credential_store.py', 'paths.py', 'servers.json'):
    FILES['lab_servers/' + name] = 'lab_servers/' + name
for name in ('index.html', 'style.css', 'app.js'):
    FILES['lab_servers/web/' + name] = 'lab_servers/web/' + name


def build(target=None):
    target = Path(target) if target is not None else ROOT / 'dist' / 'LabMonitor-Mac-Windows.zip'
    target.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(target, 'w', ZIP_DEFLATED) as archive:
        for source, name in FILES.items():
            if source == 'lab_servers/servers.json':
                path = ROOT / source
                if not path.exists():
                    path = ROOT / 'lab_servers' / 'servers.example.json'
                servers = json.loads(path.read_text(encoding='utf-8'))
                for server in servers:
                    server.update(user='your_username', enabled=False, auth='password',
                                  remember_user=True, remember_password=False, credential_id='')
                archive.writestr('LabMonitor/' + name, json.dumps(servers, ensure_ascii=False, indent=2) + '\n')
            else:
                archive.write(ROOT / source, 'LabMonitor/' + name)
    return target


if __name__ == '__main__':
    print(build())
