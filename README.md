# 服务器监控

在自己的电脑上同时查看多台 Linux 服务器的 CPU、内存、磁盘和进程。程序通过 SSH 采集数据，在浏览器中显示并定时更新。

- CPU 空闲率、等效空闲逻辑核、可用内存和系统负载。
- 磁盘总容量、可用空间、设备名称和挂载路径。
- 查看当前账号或所有用户的进程，按状态、服务器和关键字筛选。
- 在界面中添加或修改连接，并选择记住用户名和密码。

## 开始使用

从[下载页面](https://github.com/zzy-0101/lab-server-monitor/releases/latest)获取 `server_monitor.zip`，完整解压后打开其中的 `LabMonitor` 文件夹。电脑需要 Python 3.9 或更新版本，并能访问服务器所在内网或相应 VPN。

| 系统 | 首次使用 | 后续启动 |
|---|---|---|
| macOS | 确认已安装 Python 3 | 双击 `Start-Mac.command` |
| Windows | 双击 `Install-Windows.bat` 安装依赖 | 双击 `Start-Windows.bat` |

从 GitHub 下载源码时，Mac 启动文件名为 `服务器监控.command`。也可以在项目目录运行：

```bash
python3 tools/launch_monitor.py
```

首次打开会列出 7 台预设服务器，用户名为 `your_username`，监控默认停用。点击服务器旁的设置按钮，填写自己的用户名和密码，勾选“启用此服务器的监控”，保存并连接。IP 和端口可以修改，也可以自行添加服务器。

勾选“记住用户名”和“记住密码”后，下次启动可以自动连接。密码保存在这台电脑的 Mac 钥匙串或 Windows 凭据管理器中，没有配置免密连接的电脑可以直接使用密码登录。

## 查看资源与进程

顶部显示在线服务器的资源合计，“运行中的进程”统计状态为 R 或采样期间有 CPU 占用的进程。

进程列表默认显示这些活跃进程，可选择全部状态、运行中、休眠、I/O 等待、已停止或僵尸进程。搜索和排序会保留当前状态筛选；选择“运行中 (R)”可只查看 R 状态。

“磁盘与挂载”显示各挂载路径的总容量、已用和可用空间。点击进程行可以查看完整命令与工作目录。

连接设置保存在 `lab_servers/gui_config.json`，密码保存在本机系统凭据存储中。命令行用户可复制 `lab_servers/servers.example.json` 为 `lab_servers/servers.json`，填写自己的账号并配置 SSH 密钥后运行：

```bash
python3 lab_servers/monitor.py --watch 10
```

## 两份说明

- [服务器监控入门指南](docs/guide/服务器监控入门指南.pdf)：程序启动、连接设置、资源查看和进程筛选。
- [服务器使用指南](docs/references/服务器使用指南.pdf)：服务器登录、文件传输与运行任务等基本操作。

远端服务器需要 Linux 的 `/proc` 和 Python 3.6 或更新版本。macOS 使用系统 OpenSSH；Windows 使用 Paramiko。Windows 后端已通过测试，尚未在 Windows 实机完成验证。
