# 一键打开会话档案

Mac mini 负责采集、索引和保存各台机器的历史；Fedora 和 MacBook Air 通过 SSH 转发访问同一查看器。三台机器地址均为 `http://127.0.0.1:8765`。

## 日常打开

- Fedora：在应用菜单搜索“会话档案”。
- MacBook Air 和 Mac mini：双击桌面或 `~/Applications` 中的“会话档案.app”。
- 三台机器也可执行 `~/.local/bin/codex-session-viewer`。

归档机的服务在用户登录后由 macOS 后台运行。入口等待查看器就绪后打开默认浏览器。Air 的连接由 macOS 登录服务维护，Fedora 的连接由 systemd 用户服务维护，断线后自动重连，不需要保留 SSH 终端。首次使用浏览器时，等待左下角同步完成；来源筛选中选择 `MacbookAir` 即可检查 Air 的历史。

在线同步需要 Mac mini 可达。Fedora 关机时，已经收录的 Fedora 历史仍可从 Mac mini 读取；重新开机后补采新增内容。离线时直接打开同一浏览器中的已有地址，可以阅读已同步副本；一键入口会先检查在线服务。

## 安装入口

Mac mini 后端使用独立 Python 环境安装 `requirements.txt`，部署已构建的 `viewer/dist` 和对应平台的官方解析器，并准备来源配置后运行：

```sh
.venv/bin/python scripts/install_macos_services.py
.venv/bin/python scripts/install_launcher.py --local
```

Fedora 客户端运行 `python3 scripts/install_launcher.py --server Macmini`。切换前应停止原来的本机查看服务，释放端口 8765。

作为客户端的 Mac 上将 `scripts/install_launcher.py` 与 `scripts/open_viewer.py` 放在同一目录，使用系统 Python 执行：

```sh
/usr/bin/python3 install_launcher.py --server Macmini
```

`Macmini` 是 Mac 已配置且能免交互登录的 SSH 别名。安装脚本会复制启动程序，创建应用、桌面快捷方式和登录连接服务。客户端无需复制完整归档或安装项目后端。端口 8765 应留给此连接；从手动转发切换时先退出原来的项目 SSH 转发。

## 运行状态

Fedora 客户端：

```sh
systemctl --user status codex-session-connection
```

MacBook Air 客户端：

```sh
launchctl list xyz.chesszyh.codex-session-viewer
curl --noproxy '*' http://127.0.0.1:8765/api/status
```

Mac 连接日志在 `~/Library/Logs/codex-session-sync/`，连接配置在 `~/.config/codex-session-sync/launcher.json`。更换归档机时，重新执行安装命令并指定新的 SSH 别名。

Mac mini 中心服务的日志位于 `~/Library/Logs/codex-session-sync/`；用 `launchctl list xyz.chesszyh.codex-session-replica` 检查采集服务，其他服务后缀为 `indexer`、`viewer` 和 `observer`。
