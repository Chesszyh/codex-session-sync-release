# 阅读与离线同步

查看器从归档读取历史，浏览器保存独立缓存。使用同一浏览器、同一地址，断网后仍可阅读已经缓存的内容。

## 同步方式

在会话列表底部选择“同步方式”，选择会保存在当前浏览器、当前网站中，并同步到同站点的其他标签页。首次使用时，触控设备默认“按需阅读”，电脑默认“完整离线副本”；改变窗口大小不会改变已选模式。

- **按需阅读**：先更新会话目录，打开会话后加载轮次摘要，打开轮次或翻页时才下载对应正文。已打开的页面保留离线缓存；未缓存内容需要联网。搜索覆盖目录标题和本地已有正文，不会为搜索下载全部历史。当前会话更新后会重新校验缓存版本，避免混用不同历史头。
- **完整离线副本**：持续同步全部会话与正文，首次完成后可离线全文搜索；后续按保存的游标补齐增量。

切换到按需阅读会停止全量同步，保留已有缓存与全量游标；切回完整离线副本会从原位置继续。切换模式本身不会释放已占用的存储空间。按需阅读跟随归档更新，完整模式还会接收原生实时通知。

已部署的电脑可使用[一键启动入口](Launcher-Usage.md)。

## 构建和启动

先按[归档使用说明](Archive-Usage.md)建立归档，并构建 README 中的[官方读取适配器](../README.md#读取分页历史)。前端需要 Node.js 和 npm：

```sh
npm --prefix viewer ci
npm --prefix viewer run build
.venv/bin/python -m replica --store ~/.local/share/codex-session-sync/replica normalize
.venv/bin/python -m replica --store ~/.local/share/codex-session-sync/replica serve
```

打开 `http://127.0.0.1:8765`。首次索引需要遍历已有归档，后续 `normalize` 只解码新增的完整字节。持续更新使用 `normalize --interval 15`。两个进程都需要运行；原始数据仍由独立采集器保存。

Linux 后台服务提供 [indexer 模板](../deploy/codex-session-indexer.service.in)和 [viewer 模板](../deploy/codex-session-viewer.service.in)。按[归档服务安装方法](Archive-Usage.md#后台采集)替换模板中的解释器和项目路径，分别保存为同名 `.service`，再执行：

```sh
systemctl --user daemon-reload
systemctl --user enable --now codex-session-indexer.service codex-session-viewer.service
```

服务状态以 `systemctl --user status` 为准。查看器只监听本机回环地址，远程访问可使用已有 SSH 连接将远程本地端口转发到归档机。

HTTP 连接设有读写空闲超时和并发上限，超限连接会关闭；默认值见 `replica/gateway.py` 的 `GatewayServer`。反向代理仍应限制客户端慢请求及请求速率。

## 通过域名访问

手机或没有 SSH 的设备可以通过 Cloudflare Tunnel 访问查看器，并由 Cloudflare Access 验证登录身份。公网查看器使用独立的回环端口，强制校验每个请求的 Access 令牌签名、签发者、应用 Audience 和有效期；缺少配置、令牌或无法验证时拒绝访问。本机及 SSH 查看器继续使用原来的端口。

```sh
.venv/bin/python -m replica --store ~/.local/share/codex-session-sync/replica \
  serve --port 8767 --public-origin https://archive.example.com \
  --access-team YOUR_TEAM --access-audience YOUR_ACCESS_APPLICATION_AUD
```

macOS 安装后台服务时，将 `--public-origin`、`--access-team`、`--access-audience` 同时传给 `scripts/install_macos_services.py`；它会另建 `public-viewer` 服务，默认使用端口 8767，可通过 `--public-port` 修改。本机 `viewer` 服务保持端口 8765。升级旧的单入口部署时，将隧道改指向新的公网端口，并从本机服务移除 `--public-origin`。

先为域名创建 Access 自托管应用，将允许策略限定到自己的邮箱。再配置隧道的入口：

```yaml
ingress:
  - hostname: archive.example.com
    path: '^(/|/app[.]js|/style[.]css|/sw[.]js|/api/(status|snapshot|changes|read/(index|page)|live/(snapshot|changes|ack)))$'
    service: http://127.0.0.1:8767
    originRequest:
      httpHostHeader: archive.example.com
      connectTimeout: 5s
      disableChunkedEncoding: true
      access:
        required: true
        teamName: YOUR_TEAM
        audTag:
          - YOUR_ACCESS_APPLICATION_AUD
  - service: http_status:404
```

此入口只转发浏览器查看器使用的页面、资源和接口，其余路径返回 404。`/api/threads`、`/api/items` 仍可通过本机或 SSH 入口使用。

`teamName` 和 `audTag` 分别取自 Access 团队域名前缀和应用的 Audience 标识。隧道连接器和公网查看器分别验证 Access 签发的令牌，三个启动参数必须同时配置。后端只接受配置域名作为 Host，不能通过改用本机 Host 绕过校验。本机查看器拒绝带代理转发标记的请求，隧道只能指向公网端口。确认入口校验已配置后，将域名路由到此隧道。未登录时，首页、页面资源和 `/api/status` 都应跳转登录或拒绝访问；登录后再核对会话读取和 `/api/live/ack` 确认请求。官方配置说明见 [Access 与 Tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/origin-parameters/#access)。

登录过期时，已缓存页面显示“登录已过期 · 阅读本地副本”，点击“重新登录”恢复在线同步。Access 控制网络访问，不清除浏览器已有副本；共享设备使用完毕后应清除此站点数据。域名与原来的本机地址各有独立副本，首次通过域名登录需要重新同步。Mac mini 离线时，隧道不能提供新数据。

## 阅读

会话标题优先使用原生 `session_index.jsonl` 中最新的非空名称，没有名称时回退到数据库标题。改名会随下一轮采集同步。

左侧按来源 home 和归档状态筛选，搜索会话标题和已保存的正文。会话按日期分组，已知的子代理显示在父会话下。点击会话打开轮次列表，点击 **Detail** 阅读该轮的消息和工具记录；**Back** 返回轮次列表。搜索正文后选择会话会跳到命中位置。

轮次列表使用虚拟滚动，详情通过“上一页 / 下一页”按页读取。工具记录默认折叠，点击摘要展开，右侧弹窗按钮打开完整工具内容。“来源与原始条目”保留内容代次、原始字节位置和条目原文。子代理记录中的“打开子代理”跳到同一来源下已归档的会话。

展示组件复用 codex-trace，固定来源、许可和本地适配范围见 [source.json](../viewer/src/vendor/codex-trace/source.json)。内联的 PNG、JPEG、WebP、GIF 图片可以显示；尚未保存的本地文件或外部媒体引用会明确提示，不会自动访问任意源路径或下载外部图片。

“部分历史”表示当前读取适配器存在无法投影的记录、依赖缺口或原始文件缺失；不影响阅读已保存的部分。没有原生界面事件的旧格式 response-only 历史显示为“旧格式记录”，保留与官方标准化条目的区别。

## 离线与重新连接

完整模式的左下角区分“保存初始副本”和“正在补齐增量”，数字是本次保存的条目数量，不是会话总数。“副本已同步”表示已保存到本轮服务端序号。按需模式的“目录已更新”仅指目录，不表示正文已完整缓存。断开网络或关闭服务后仍可读取缓存；未缓存的正文会提示联网。完整模式首次同步大归档时，目录可能晚于正文到达，此时同步进度仍会持续更新。

浏览器通过 IndexedDB 保存记录与同步进度，并使用 Service Worker 保存页面资源。同一浏览器配置和地址重开会从已保存的游标补增量；`localhost:8765` 与 `127.0.0.1:8765` 属于不同站点，各有独立副本，日常固定使用其中一个。首次启动会申请持久存储；能否获得由浏览器决定。不要在清理网站数据后期待仍有离线副本。浏览器空间不足时同步进度不会前进，界面会显示同步暂停。

如果服务端更换或重建索引导致序号回退，当前版本会提示“归档序号已变化，需要重新连接副本”，不会自动重建缓存。确认服务端档案可用后，在浏览器清除此站点的数据并重新同步；离线时不要清理唯一可用副本。普通断线补齐不需要这个步骤。

原始加密档案的备份方式见[归档使用说明](Archive-Usage.md#备份与验证)。`view.sqlite` 是归档目录内可重新生成的可读索引，包含明文历史；浏览器副本也属于本地私人数据。

## 状态含义

“归档来源 N 个 · 最近采集成功 M 个”来自归档器最近一次扫描结果；悬停可查看各来源最后完成采集的时间。它不是即时在线探测，离线浏览时显示的是缓存状态。

“通知入口已连接 N 个”表示原生通知观察连接的数量，不是电脑在线数量。没有通知 socket 的来源仍可正常归档；归档成功也不代表拥有完整逐字直播。

## 验证

```sh
.venv/bin/python -m unittest discover -s tests -v
npm --prefix viewer test
npm --prefix viewer run check
```

浏览器测试启动自己的生成数据服务，验证搜索、工具展开、浏览器重开后的离线读取、重连替换及 HTML 内容隔离。codex-trace 接入测试还覆盖旧缓存升级、长历史分页、虚拟列表和历史头替换。手机测试使用 Chromium 与 WebKit，覆盖触摸导航、工具弹窗、登录过期后重新连接及服务离线后的阅读；它不替代真机验证。生成样本截图保存在 `.m2/`，手机测试截图保存在 `.m6/mobile-access/`。
