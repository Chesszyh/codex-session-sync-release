# 从归档恢复到独立 Codex 目录

`restore` 恢复一个来源明确的会话，以及该会话实际引用的父历史前缀。它通过官方库建立新的原生数据库，再启动隔离的原生进程读取全部分页。成功结果表示历史和已支持元数据验证通过；命令不会恢复执行会话。

## 执行恢复

先按 [README](../README.md#读取分页历史) 构建官方适配器。从归档清单取得准确的 `host:home` 来源和会话 ID：

```sh
python3 -m replica --store /path/to/archive inventory
python3 -m replica --store /path/to/archive restore \
  --origin 'Host:/path/to/source-codex-home' \
  --thread-id 'native-thread-uuid' \
  --binary /path/to/tested/codex \
  --output /path/to/new-restore-directory
```

输出目录必须尚不存在。已有目录即使包含相同历史或较短前缀，也会被拒绝；此命令不向已有原生 home 合并会话。

支持的目标运行时由 [source.json](../adapters/official/source.json) 中的 `restore_tested_runtimes` 管理。源数据必须符合 [restore-schema.json](../adapters/official/restore-schema.json) 中的元数据列集合，并使用官方解码器支持的 `paginated` 记录。旧版本留下的日志若已由原生程序迁移为这一格式，可以恢复。未知格式、缺失依赖、无法证明的历史头或不匹配的运行时会停止导入；归档和查看器内容保留。

## 结果文件

| 路径 | 内容 |
| --- | --- |
| `home/` | 原始历史的完整记录前缀；压缩输入恢复为逐字节相同的逻辑 JSONL |
| `sqlite/` | 官方库建立的独立原生数据库 |
| `plan.json` | 来源、所选历史头、父历史边界、原始元数据和目标路径 |
| `import.json` | 官方导入生成的分组、项目 ID 对应关系 |
| `expected.sqlite` | 官方原始记录投影，供完整分页核对 |
| `report.json` | 验证结果、覆盖范围和环境状态 |
| `import.log`、`native.log` | 导入和官方回读的本地日志 |

输出目录权限为 `700`，文件为 `600`。日志、原始历史和元数据都可能包含会话中的私人内容。

`report.json` 的 `read_verified: true` 表示：官方接口返回的条目内容与顺序、轮次状态、所选历史头和支持的会话元数据一致；`raw_unchanged: true` 表示导入和回读未改变复制的原始字节。父历史只复制被引用的前缀，未完成的末尾记录不进入恢复结果。源归档仍保留完整采集内容。

恢复保留名称、归档状态、工作目录、模型配置、权限策略、时间、Git 信息及显式清空值。分组和项目通过官方接口创建，原生 ID 会重新分配；项目根目录顺序和附件绑定内容会核对。旧的 `has_user_event`、`is_pinned` 列，以及项目原始排列位置、创建/更新时间和附件创建时间只保存在元数据文件中；固定版本官方模型使用 `thread_section_id` 表示分组和置顶关系。

工作区文件、外部资源和凭据不属于本次恢复产物。`workspace_exists` 仅报告源工作目录在目标机器是否存在，不代表其文件内容相同。之后使用恢复会话时，需要明确指定产物中的 `CODEX_HOME` 与 `CODEX_SQLITE_HOME`，并提供目标机器自己的工作区和凭据。不要移动已经导入的目录，原生数据库中的历史路径是绝对路径。

## 回归验证

测试运行时必须显式选择，不使用自动更新的桌面二进制作为默认值。可将受支持版本安装到独立目录，例如：

```sh
npm install --prefix .m6/restore-runtime --no-save @openai/codex@0.155.0-alpha.16.4
REPLICA_TEST_BINARY="$PWD/.m6/restore-runtime/node_modules/.bin/codex" python scripts/check.py --full
```

此版本对应当前适配器允许列表；更换适配器时以 `source.json` 为准。没有设置 `REPLICA_TEST_BINARY` 时普通测试会明确跳过恢复；显式提供错误版本或缺少 importer 则失败。

```sh
env REPLICA_TEST_BINARY=/path/to/tested/codex \
  python3 -m unittest discover -s tests -p 'test_restore.py' -v
```

测试在临时目录生成源数据和目标目录，覆盖完整分页、元数据、分叉依赖、当前历史头、压缩归档、较大前缀和拒绝导入的情况。运行结果以指定运行时下的本次测试输出为准。
