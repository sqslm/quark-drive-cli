# AI agent 使用指南

CLI 每次运行一次即退出，适合由 agent、cron 或任务计划程序调用。调度交给外部系统，CLI 不在后台常驻。

## 配置与执行追更

先由用户扫码登录。使用同一系统用户与同一 `QUARK_CONFIG_DIR`，通过只读命令确认来源与目标。将来源尽量定位到剧名及版本目录，减少扫描；`--include`/`--exclude` 匹配选定目录的直接子项，选中目录会包含其完整子树。

```sh
quark --version
quark auth login
quark auth status --json
quark share inspect 'https://pan.quark.cn/s/EXAMPLE' --path '/剧集/4K' --json
quark job add series 'https://pan.quark.cn/s/EXAMPLE' --source '/剧集/4K' --include '*.mkv' --to '/影视/追更/剧集' --scope '/影视/追更' --json
quark job run series --dry-run --json
quark job run series --json
```

替换示例链接与路径；目标中间目录必须存在，任务可以创建目标的末级目录。不要在调度参数或共享日志里存放提取码。`job add` 固定来源 FID、目标和范围；执行时增加 `--scope` 只能进一步收紧范围。

## 解析结果

检查退出码并分别捕获 stdout 与 stderr。`job run` 的逐任务报告在 stdout，即使任务失败也应解析；命令级错误在 stderr 的 `error` 对象中。不要把 stderr 直接拼入 stdout 后当成单个 JSON 解析。全局 `--json` 或命令末尾 `--json` 均可。

| 字段/结果 | 处理方式 |
| --- | --- |
| `result=preview` | 预演，无写入；`new_items` 是计划新增项目数。 |
| `result=updated` | 已确认新增；用 `saved_entries` 中的 `source_fid`、`target_fid`、`file_name`、`file_type` 生成通知。0 是目录，1 是文件。 |
| `result=unchanged` | 无新增，不必通知用户；`saved_entries=[]`。 |
| `result=failed` | 查看 `error_type`、`message`、`next_action` 与 `pending`；一个任务失败不代表其他任务未执行。 |
| `next_action=authenticate` | 需要用户重新登录，不得索要或公开 Cookie。 |
| `next_action=wait` | 按 `retry_after_seconds` 等待后再决定执行；禁止忙循环。 |
| `next_action=review_access` | 检查官方客户端的访问限制；保留凭证，不要反复发起登录。 |
| `next_action=check_local_state` | 检查私有目录权限、空间和账本完整性，避免继续写入。 |
| `next_action=query_original_task` | 保留 `task_id`；冷却结束后可再次执行同一 `job run`，它核验原任务，不重新提交未知操作。 |
| `next_action=manual_review` | 停止自动重试，交由用户核验冲突或未知写入。 |

`saved_items` 统计已确认的文件和目录，`saved_entries` 也含目录。恢复原任务确认的项目包含在当次结果中。预演不会把只读核验发现的结果当作实际新增。

同一个任务在前一组成功、后一组失败时，失败结果仍列出当次已确认的 `saved_entries`。出现本地状态写入失败时，先检查账本，不能仅凭内存中的结果继续写入。

退出码：0 成功，2 参数/范围/身份冲突，3 登录问题，4 远端/网络/限流/访问限制，5 本地 I/O 或状态问题。对退出码 4 应读取错误类型，不能一概重试。批量任务返回非零时也可能已有部分成功，应逐项查看报告，并通过原任务账本继续核验。

## 未确认写入

`pending=true` 说明持久账本中保留未确认意图。没有 task ID 的请求、目录映射确认被中断或身份冲突，通常需要人工核验。不要删除账本、移除再创建任务、按同名文件重建映射，或换一个转存命令重发。`job remove` 只删本地配置并保留账本，不取消远端任务。

项目不会仅按文件名去重；已记录的来源或目标被移动、替换、修改时会停止。单次预检复用父目录核验，不会持久缓存目录列表；真正写入前仍重新读取目标并核验范围。
