#!/usr/bin/env python3
"""Portable GUI entry point. Use Python 3.9+ on macOS or Windows."""
import importlib.util
import os
import shutil
import sys
from pathlib import Path


def main():
    if sys.version_info < (3, 9):
        print('Python 3.9 or newer is required.')
        return 1
    if os.name == 'nt':
        if importlib.util.find_spec('paramiko') is None:
            print('Please run Install-Windows.bat once, then run Start-Windows.bat.')
            return 1
    elif shutil.which('ssh') is None:
        print('OpenSSH client (ssh) was not found. Please install it and retry.')
        return 1
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / 'lab_servers'))
    sys.argv = [str(root / 'lab_servers' / 'dashboard.py'), '--open'] + sys.argv[1:]
    from dashboard import main as start
    start()
    return 0


if __name__ == '__main__':
    sys.exit(main())
