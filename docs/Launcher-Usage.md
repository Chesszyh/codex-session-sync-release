# 一键打开会话档案

归档机负责采集、索引和保存各台机器的历史。客户端通过 SSH（安全远程连接）转发访问同一个查看器，本机地址为 `http://127.0.0.1:8765`。

## 日常打开

- Linux：在应用菜单搜索“会话档案”。
- Mac：双击桌面或 `~/Applications` 中的“会话档案.app”。
- 命令入口：`~/.local/bin/codex-session-viewer`。

入口等待查看器就绪后打开默认浏览器。Mac 的连接由登录服务维护，Linux 的连接由 systemd 用户服务维护，断线后自动重连，不需要保留 SSH 终端。首次使用浏览器时，等待左下角同步完成。

在线同步需要归档机可达。来源机器关机时，已经收录的历史仍可从归档机读取；来源重新开机后补采新增内容。离线时直接打开同一浏览器中的已有地址，可以阅读已同步副本；一键入口会先检查在线服务。

## 安装入口

先按[归档说明](Archive-Usage.md)和[查看器说明](Viewer-Usage.md)准备归档机的后台服务。Linux 归档机在项目目录执行 `python3 scripts/install_launcher.py`；Mac 归档机执行 `.venv/bin/python scripts/install_launcher.py --local`。

Linux 客户端运行：

```sh
python3 scripts/install_launcher.py --server ArchiveHost
```

作为客户端的 Mac 上将 `scripts/install_launcher.py` 与 `scripts/open_viewer.py` 放在同一目录，使用系统 Python 执行：

```sh
/usr/bin/python3 install_launcher.py --server ArchiveHost
```

`ArchiveHost` 是客户端已配置且能免交互登录的 SSH 别名。安装脚本会复制启动程序，创建应用、桌面快捷方式和后台连接服务。客户端无需复制完整归档或安装项目后端。切换前停止原来的本机查看服务或手动转发，释放端口 8765。

若归档机另有认证阅读监听器，可用 `--forward-port 8767` 额外转发其端口；现有本机代理继续使用原来的 loopback（本机回环）地址。更换归档机时，重新执行安装命令并指定新的 SSH 别名和所需附加端口。

## 运行状态

Linux 客户端：

```sh
systemctl --user status codex-session-connection
```

Mac 客户端：

```sh
launchctl list xyz.chesszyh.codex-session-viewer
curl --noproxy '*' http://127.0.0.1:8765/api/status
```

Mac 连接日志在 `~/Library/Logs/codex-session-sync/`，连接配置在 `~/.config/codex-session-sync/launcher.json`。归档机的服务状态按对应平台的后台服务工具检查。
