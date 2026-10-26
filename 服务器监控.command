#!/bin/zsh
cd -- "${0:A:h}" || exit 1
for monitor_python in "$(command -v python3)" /opt/homebrew/bin/python3 /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /usr/bin/python3; do
  if [[ -n "$monitor_python" && -x "$monitor_python" ]] && "$monitor_python" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
    exec "$monitor_python" tools/launch_monitor.py
  fi
done
print '需要 Python 3.9 或更新版本。安装后重新双击此文件。'
read -r '?按回车关闭…'
