#!/bin/zsh
cd -- "${0:A:h:h}" || exit 1
/usr/bin/python3 lab_servers/monitor.py --watch 10
