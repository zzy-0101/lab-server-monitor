#!/bin/zsh
cd -- "${0:A:h:h}" || exit 1
/usr/bin/python3 server_monitor/monitor.py --watch 10
