# 归档使用说明

`replica` 将指定 Codex home 的原始历史和会话元数据保存到自己的归档目录。查看状态、导出已保存历史时不需要连接源主机。

## 安装与配置

采集机需要 Python 3、[Python 依赖](../requirements.txt) 和 SSH 客户端。源主机需要 Python 3；读取 `.jsonl.zst` 还需要源主机上的 `zstd`。远程读取复用已有 SSH 配置，不在远程安装后台进程。

原始文件读取仅允许 `sessions/`、`archived_sessions/` 内的普通文件及 `session_index.jsonl`。这些路径中的符号链接、父目录跳转和特殊文件会被拒绝，并使本轮扫描标记为不完整；已有归档继续保留。

## 一条命令接入新机器

在本仓库运行，将 `NewMachine` 换成**采集机能够连接的** SSH 别名或 `user@address`：

```sh
python3 scripts/add_source.py --server Macmini --ssh NewMachine --host NewMachine
```

此命令先从中心采集机连接来源，检查默认 `~/.codex/sessions` 与 `state_5.sqlite`，然后备份并更新中心的来源配置。如果中心正在运行本项目的 macOS LaunchAgent 或 Linux 用户采集服务，会请求重启采集服务，使配置生效。无需在新机器安装本项目；SSH 登录应已配置。重复加入相同来源不改配置，也不重启服务。

中心就是当前机器时省略 `--server`；加入中心本机历史时省略 `--ssh`。自定义原生目录通过 `--home /absolute/codex-home --sqlite-home /absolute/database-directory` 指定；`--config` 指定的是中心上的配置文件。`--check` 仅检查连通性与目录，不改配置。

注册成功不等于历史已采集完成。下一轮采集后，在页面来源列表中选择新机器，并用 `status` 核对 `last_scan`。手动运行的 `watch` 需要自行重启；命令会明确提示没有发现运行中的托管服务。

## 手工配置

在项目目录运行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
install -d -m 700 ~/.config/codex-session-sync
install -m 600 config/sources.example.json ~/.config/codex-session-sync/sources.json
```

编辑 `sources.json`，为每个来源填写稳定的 `host` 名称、原生进程实际使用的 `home` 和 `sqlite_home`；远程来源增加 `ssh` 主机别名。[配置示例](../config/sources.example.json) 展示本地和远程来源。不同 host/home 的同 ID 会话分别保存。

## 首次采集与查看

单次本地采集：

```sh
.venv/bin/python -m replica \
  --store ~/.local/share/codex-session-sync/replica collect \
  --host local --home /path/to/codex-home --sqlite-home /path/to/sqlite-home
```

远程采集在相同命令上增加 `--ssh Macmini`，并将两个 home 换成远程绝对路径。采集失败返回非零退出码；输出仅含计数、状态和进度，不显示会话正文。

查看已提交的归档状态与目录：

```sh
.venv/bin/python -m replica --store ~/.local/share/codex-session-sync/replica status
.venv/bin/python -m replica --store ~/.local/share/codex-session-sync/replica inventory --host Macmini
```

`status` 中 `last_scan.complete` 表示这一轮目录扫描和读取完成；`dependency_issues` 单独表示缺失历史头或父历史等缺口。两个值的含义不同，详见[状态定义](Archive-Format.md#状态与一致性)。`inventory` 返回会话选中的历史文件、对应 generation（文件内容代次）、仅有原始文件的会话，以及同一读取事务中的变更序号。

## 后台采集

前台启动可持续采集多个来源：

```sh
.venv/bin/python -m replica \
  --store ~/.local/share/codex-session-sync/replica watch \
  --config ~/.config/codex-session-sync/sources.json --interval 60
```

每轮依次采集所有来源，然后等待指定秒数。首次大批量归档耗时较长；后续轮次跳过未变化的日志内容。关闭查看器不影响采集。

Linux 用户服务使用 [systemd 模板](../deploy/codex-session-replica.service.in)。在项目目录执行下面的命令，将模板中的项目与 Python 路径替换为本机绝对路径：

```sh
python3 - <<'PY'
from pathlib import Path
repo = Path.cwd()
unit = (repo / 'deploy/codex-session-replica.service.in').read_text()
unit = unit.replace('@REPOSITORY@', str(repo)).replace('@PYTHON@', str(repo / '.venv/bin/python'))
target = Path.home() / '.config/systemd/user/codex-session-replica.service'
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(unit)
PY
systemctl --user daemon-reload
systemctl --user enable --now codex-session-replica.service
journalctl --user -u codex-session-replica.service -n 10 --no-pager
```

日志逐来源显示这一轮是否完成、读取字节数和缺口数量。停止采集使用 `systemctl --user stop codex-session-replica.service`，重新启动使用 `systemctl --user start codex-session-replica.service`。停止服务会保留归档；恢复后补采源目录中仍存在的数据。

## 导出已保存的原始历史

从 `inventory` 选择 generation，导出到一个尚不存在的文件：

```sh
.venv/bin/python -m replica --store ~/.local/share/codex-session-sync/replica export \
  --generation GENERATION_ID --output /path/to/new-rollout.jsonl
```

导出按原始字节重组已完整换行的部分，半行留在归档里等待后续补齐。文件权限为 `600`。导出原始历史不执行会话；如需把支持的样本转成可读历史，使用 README 中的[官方历史读取器](../README.md#读取分页历史)，其输入范围单独受限。

增量消费者可从 `inventory.seq` 开始，用 `changes --after SEQ --limit 100` 读取已提交的变更序号。原始对象与 manifest（分段清单）在归档目录内，格式见[归档格式](Archive-Format.md)。

## 备份与验证

备份归档前停止采集，再复制整个归档目录，包含 `archive.key`、数据库及其 WAL 文件、`objects/`。恢复时使用完整目录；密钥用于解密已保存的对象。

回归测试和大文件测试均只操作生成数据：

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m tests.stress_archive --root .m1/stress-new --mib 1024
```

大文件测试目录必须尚不存在，测试会写入约三份指定规模的数据，输出内存峰值、增量传输量和原始字节回读结果。
