# 现有进程观察与事件补齐

观察器只连接已经运行的 Codex owner（原生会话所属进程），保存该连接实际收到的通知。原始历史归档和阅读索引继续独立运行。当前已验证的运行时提供状态通知；旁观连接尚未取得逐字输出订阅，界面因此继续标为持久历史跟随。

## 连接

先更新 Python 依赖，再将 [owners.example.json](../config/owners.example.json)中的主机、实际 home、已有 socket 和二进制路径填入私有配置。通过 SSH 连接时复用已有登录，不复制登录凭据。

```sh
rtk proxy .venv/bin/python -m pip install -r requirements.txt
rtk proxy .venv/bin/python -m replica --store ~/.local/share/codex-session-sync/replica observe \
  --config ~/.config/codex-session-sync/owner.json --seconds 15
```

省略 `--seconds` 可保持连接。后台部署使用 [observer 服务模板](../deploy/codex-session-observer.service.in)，安装方法与[归档服务](Archive-Usage.md#后台采集)相同。没有已有 socket 的 stdio owner 不在此入口的覆盖范围内，仍由归档器读取持久历史。

观察器的请求白名单在 `replica/live.py` 的 `READ_METHODS` 中。它不调用 resume、启动会话或执行 turn，不回复审批或工具执行请求。SSH 使用原生 `app-server proxy`，在字节流之上建立 WebSocket；不是向 socket 直接发送 JSON 行。

## 保存和断线含义

每次连接分配一个 epoch（连接代次），通知先进入加密对象，再与本地事件序号、当前可见状态一起提交到 `live.sqlite`。读取 owner 元数据快照前已开启通知保存。上游没有提供快照与通知共享的序号，连接前未采到的过程明确记为未知区间。

浏览器先读取 `/api/live/snapshot` 的本地序号，再从 `/api/live/changes` 补读。实体与浏览器游标在同一 IndexedDB 事务保存后，才向 `/api/live/ack` 确认。该 POST 只更新本项目的接收游标，没有原生执行权限。

delta（增量文本）进入临时阅读状态，completed item（完成条目）替换增量拼接结果；原始持久投影到达后以它为准。重复完成通知不会追加第二份答复。断线时未完成条目会标记内容可能不连续，重连不能补造未捕获的瞬时输出。

事件投递索引不自动清理。需要归档已经由所有已登记接收者确认的投递记录时，可以执行：

```sh
rtk proxy .venv/bin/python -m replica --store ~/.local/share/codex-session-sync/replica live-prune --through SEQ
```

此操作保留加密原始事件及其批次清单。落后于投递保留范围的游标返回 `reset_required`，浏览器重新取得事件快照，同时保留已有的持久历史副本。

## 覆盖范围

通知能到达、增量合并逻辑可用和原生完整直播是三项不同能力。观察器保留所有收到的通知；阅读覆盖层处理用户/助手条目、助手文本增量和命令输出增量，其余通知保留在原始事件档案中。可使用 `tests/test_live.py` 和浏览器用例复跑生成样本验证。
