#!/usr/bin/env python3
"""Show synthetic, account-free GUI data for documentation screenshots; no server connections."""
import json
import secrets
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from http.server import ThreadingHTTPServer

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'lab_servers'))
from dashboard import Dashboard, make_handler
from credential_store import CredentialStore


class DemoCredentials:
    """Keep documentation test logins separate from the real OS credential store."""
    name = CredentialStore().name

    def __init__(self):
        self.records = {}

    def get(self, key):
        return self.records.get(key)

    def set(self, key, value):
        self.records[key] = value

    def delete(self, key):
        self.records.pop(key, None)


def disk(path,device,total,used,available):
    scale=1024**3
    return dict(mountpoint=path,device=device,filesystem='ext4',read_only=False,ok=True,
                total_bytes=total*scale,used_bytes=used*scale,available_bytes=available*scale,
                free_bytes=(total-used)*scale,reserved_bytes=max(0,total-used-available)*scale,
                used_percent=100*used/(used+available) if used+available else 0)


def main():
    with tempfile.TemporaryDirectory(prefix='lab-guide-demo-') as directory:
        app=Dashboard(Path(directory)/'demo.json', credential_store=DemoCredentials())
        now=datetime.now(timezone.utc).isoformat()
        idle=[73,62,94,28,52,77,16]
        memory=[188,1258,188,754,188,188,250]
        for i,server in enumerate(app.servers):
            server.update(user='your_username',auth='password',identity_file='',enabled=True)
            app.passwords[server['id']] = 'demonstration-only'
            cores=48 if i==6 else 112
            processes=[]
            if i==1:
                processes=[dict(pid=4102,ppid=4001,user='your_username',uid=1000,state='R',threads=4,
                                cpu_percent=280.0,rss_gib=8.2,elapsed_seconds=19200,
                                command='python -u analysis.py --input data.csv',cwd='/data1/your_username/project'),
                           dict(pid=4103,ppid=4001,user='your_username',uid=1000,state='S',threads=1,
                                cpu_percent=0.0,rss_gib=.02,elapsed_seconds=19200,
                                command='bash run_analysis.sh',cwd='/data1/your_username/project')]
            disks=[disk('/','/dev/nvme0n1p4',100,66,29),
                   disk('/home','/dev/nvme0n1p3',350,150,182),
                   disk('/data1','/dev/sda1',1833.6,900,840),
                   disk('/data2','/dev/sdb1',1833.6,100,1640)]
            data=dict(ok=True,sampled_at=now,hostname='demo-server-'+str(i+1),user='your_username',
                      scope='mine',sample_seconds=1.0,logical_cpus=cores,cpu_idle_percent=idle[i],
                      idle_cpu_equivalents=cores*idle[i]/100,cpu_iowait_percent=0,
                      load_1_5_15=[round(cores*(1-idle[i]/100),1)]*3,
                      memory_total_gib=memory[i],memory_available_gib=round(memory[i]*.64,1),
                      processes=processes,disks=disks,disk_error=None)
            app.states[server['id']].update(data=data,last_success=now,status='online',busy=False)
        token=secrets.token_urlsafe(32)
        http=ThreadingHTTPServer(('127.0.0.1',0),make_handler(app,token))
        http.daemon_threads=True
        Path('/private/tmp/lab-guide-demo.json').write_text(json.dumps(dict(port=http.server_port,token=token)),encoding='utf-8')
        print('手册演示界面已启动；使用虚构账号与示意数值，不连接任何服务器。',flush=True)
        try:
            http.serve_forever(.3)
        finally:
            app.close();http.server_close()


if __name__=='__main__':
    main()
