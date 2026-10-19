# quark 网盘 CLI 使用手册

`quark` 是面向 Windows 和 Linux 的 Python 命令行工具，支持夸克网页版 Cookie 登录、文件浏览和 CRUD、分享查看与管理、转存、任务、传输及单向备份。要求 Python 3.10 或更高版本。

夸克网盘接口并非稳定公开 API；服务端调整可能影响功能。请先用 `--dry-run` 查看写操作计划，并在重要文件上谨慎使用覆盖、改名和删除。

## 1. 安装与升级

从项目的 `dist/` 目录取得 wheel、源码包和 `SHA256SUMS.txt`。先在下载目录验证校验值，再在 Python 虚拟环境安装 wheel：

Linux：

```sh
sha256sum -c SHA256SUMS.txt
python3 -m venv .venv
. .venv/bin/activate
python -m pip install ./quark_drive_cli-0.1.0-py3-none-any.whl
python -m pip check
quark --help
```

Windows PowerShell：

```powershell
Get-FileHash .\quark_drive_cli-0.1.0-py3-none-any.whl -Algorithm SHA256
Get-Content .\SHA256SUMS.txt
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install .\quark_drive_cli-0.1.0-py3-none-any.whl
python -m pip check
quark --help
```

如果虚拟环境中已有相同版本，使用 `python -m pip install --upgrade --force-reinstall ./quark_drive_cli-0.1.0-py3-none-any.whl` 安装这份 wheel。源码包可用 `python -m pip install ./quark_drive_cli-0.1.0.tar.gz` 安装；pip 会安装运行依赖。

## 2. 登录和本地配置

推荐用手机夸克 APP 扫描 CLI 生成的网页版二维码：

```sh
quark auth login
quark auth status
```

主要登录选项：

| 选项 | 作用 |
| --- | --- |
| `--timeout SEC` | 等待扫码确认的秒数，1–300，默认 300。 |
| `--qr-terminal` | 把二维码矩阵输出到 stderr，适合 SSH；终端需使用 UTF-8。 |
| `--qr-file PATH` | 把二维码 PNG 写到指定路径。目标文件必须不存在；登录结束后 CLI 会删除二维码。 |
| `--json` | 以 JSON 输出成功结果。 |

默认二维码 PNG 放在系统临时目录，并在登录结束后清理。二维码含短时登录令牌，不要分享图片或保存终端扫码输出。成功登录后 CLI 会检查 Cookie，再将其存入当前用户的私有配置目录。

```sh
quark auth status --json
quark auth logout
```

`auth status` 用只读网盘请求检查登录状态。`auth logout` 只删除 CLI 保存的 Cookie，不会撤销夸克服务端会话，也不会删除环境变量提供的 Cookie。

凭证选择顺序为：`QUARK_COOKIE`、`QUARK_COOKIE_FILE`、CLI 保存的 Cookie。避免把 Cookie 放进命令参数、脚本、截图或共享日志。建议让 CLI 的登录流程保存 Cookie；如使用 Cookie 文件，限制为当前系统用户可读。

| 系统 | 默认配置目录 |
| --- | --- |
| Windows | `%APPDATA%\quark-drive-cli` |
| Linux | `${XDG_CONFIG_HOME:-~/.config}/quark-drive-cli` |

目录中保存 Cookie、任务、备份清单和传输恢复状态，以及请求节奏/冷却状态（`request_guard.json`，只含本机时间与固定原因，不含 Cookie、身份或令牌）。`QUARK_CONFIG_DIR` 可指定非空绝对本地路径，并会同时改变凭证和任务状态的位置；切换目录不会搬运凭证。CLI 会检查私有文件所有者和权限，拒绝符号链接及不安全路径。Windows 普通 JSON 读取可恢复旧版管理员启动留下的文件归属：仅允许当前用户私有父目录下、保护权限只授权当前用户的 Administrators 所有文件；保留原数据和冷却记录。只读检查不修改权限，其他用户所有的文件仍拒绝读取。

## 3. 命令结构和通用选项

```text
quark [--json] [--scope REF] COMMAND ...
```

`--json` 和 `--scope` 可放在命令前；大部分具体命令也接受放在参数后。环境变量 `QUARK_SCOPE` 可设默认范围；显式 `--scope` 优先。

- `--json`：将成功结果以 JSON 写到 stdout；错误和进度写到 stderr。JSON 使用经过筛选的字段，不包含 Cookie、分享令牌、签名下载 URL 或原始 API 响应。
- `--scope REF`：将账户网盘文件读写限制在指定目录及后代。`REF` 可以是 FID 或绝对路径。每个对象都要通过 FID、父目录成员关系和祖先链检查；不能证明范围时，CLI 会在写请求前停止。
- `--dry-run`：仅适用于下文明确列出的写命令。它执行本地检查和必要的远端读取，不提交网盘变更。预览是当前状态的快照；真正执行时会重新校验。

启用范围后不能直接改动范围目录本身，也不能写到账户根目录。外部分享目录属于独立视图：查看分享内容不等于读取账户目录；`save` 的目标目录和自动创建的末级目录仍受账户范围约束。未指定范围时，CLI 可访问当前账户其他目录，使用前应确认 FID/路径正确。

### 路径和引用

`REF` 接受网盘 FID 或绝对网盘路径。`/` 和 FID `0` 表示网盘根目录。路径按名称逐级精确匹配，支持中文和空格；拒绝相对路径、`.`、`..`、反斜杠、重复或结尾分隔符、控制字符、歧义重名，以及作为中间路径的文件。

本地路径（`LOCAL`、`LOCAL_DIR`、`PATH`）是运行 CLI 的机器上的路径，与网盘路径不是同一种路径。传有空格或通配符的参数时，将其用引号括起来。

### 退出码

| 退出码 | 含义 |
| ---: | --- |
| `0` | 成功；或用户取消了交互删除；空转存选择也算成功跳过。 |
| `2` | 参数错误、不安全路径或操作预检失败。 |
| `3` | 缺少或过期的认证信息。 |
| `4` | 网络、夸克 API、服务端失败或关键远端状态未能确认。 |
| `5` | 本地文件、配置、报告或其他 I/O 错误。 |

### 请求节奏与冷却

CLI 与 PotPlayer 桥接共用当前配置目录下的同一份请求节奏状态 `request_guard.json`（配稳定锁文件），因此跨进程和重启都生效；其中只记录本机时间与固定原因，不保存 Cookie、账户身份或令牌。

- 元数据/取链请求的发起间隔至少 0.5 秒；冷却期内新请求在本地直接拒绝，不触达夸克。
- 收到 HTTP 429 时至少暂停 60 秒；账号或扫码 API 返回 HTTP 403，或失败业务响应里明确提示“访问受限/需要验证”时至少暂停 600 秒；更长的 `Retry-After`（秒数或 HTTP 日期）按其执行；HTTP 503 仅在带有有效 `Retry-After` 时暂停。
- 播放/字幕签名地址在一次重新取链后仍连续返回 403，会暂停 60 秒的新播放/字幕请求。4 MiB 串行分片与内嵌字幕/音轨的原生处理不受影响；本次限速只约束请求发起节奏，不新增字节速率限制。
- CLI 的 JSON 错误类型为 `rate_limited`/`access_restricted`，消息提示剩余冷却时间；插件的 HTTP 响应通过 `Retry-After` 返回剩余等待时间。冷却错误不会清除已保存 Cookie、不会打开扫码登录、不会清空收藏/最近记录；401 凭证过期的处理保持不变。
- 恢复方式是等待冷却结束，并用官方夸克客户端确认账号状态；不要把清空状态或轮换凭证当成恢复手段。若冷却状态无法写入本地，当前进程会以本地存储错误停止后续自动请求；另一个进程或重启无法得知未写入的冷却，需先修复本地存储并确认账号。
- 冷却期间本地健康检查、收藏/最近记录和退出登录仍可用。
- 以上数值是本工具的保守默认值，不代表夸克官方安全阈值，不能保证账号不被限制。`Retry-After` 的定义见 [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110#field.retry-after)（`429 Too Many Requests` 源自 RFC 6585 §4）。

## 4. 全部命令速查

```text
auth status | login | logout
ls [REF] [--all] [--page N] [--size N]
info REF
tree [REF] [--depth N]
search QUERY [--in REF]
du REF
duplicates REF
rename-batch REF --pattern REGEX --replacement TEXT [--apply] [--dry-run]
backup LOCAL_DIR --to REF [--apply] [--update] [--dry-run] [--progress] [--limit-rate BPS]
share inspect URL [--passcode CODE] [--path SHARE_PATH]
share create REF [--passcode CODE] [--expire-days {0,1,7,30}] [--dry-run]
share list [--all]
share revoke SHARE_ID [--yes] [--dry-run]
save URL --to REMOTE_PATH [--passcode CODE] [--source SHARE_PATH] [--include GLOB] [--exclude GLOB] [--dry-run]
save-batch FILE [--to REMOTE_PATH] [--source SHARE_PATH] [--include GLOB] [--exclude GLOB] [--report PATH] [--dry-run]
job add NAME URL --to REMOTE_PATH [--passcode CODE] [--source SHARE_PATH] [--include GLOB] [--exclude GLOB]
job list
job run [NAME] [--dry-run]
job remove NAME
mkdir NAME|REMOTE_PATH [--parent REF] [--dry-run]
upload LOCAL [--parent REF] [--recursive] [--resume] [--progress] [--limit-rate BPS] [--dry-run]
download REF --output PATH [--recursive] [--resume] [--overwrite] [--progress] [--limit-rate BPS] [--connections N]
rename REF NAME [--dry-run]
mv REF --to REF [--dry-run]
rm REF [--yes] [--dry-run]
```

## 5. 认证和文件浏览

| 命令 | 说明和选项 |
| --- | --- |
| `quark auth status` | 只读验证当前 Cookie 是否可用；支持 `--json`。 |
| `quark auth login` | 生成网页版登录二维码并等待手机确认；支持 `--timeout SEC`、`--qr-terminal`、`--qr-file PATH`、`--json`。 |
| `quark auth logout` | 删除本机保存的 Cookie；不撤销服务端会话，也不删除环境变量 Cookie；支持 `--json`。 |
| `quark ls [REF]` | 列出目录。REF 默认 `0`；默认第 1 页、每页 50 项。`--page N` 从 1 开始，`--size N` 范围 1–100，`--all` 读取所有分页。 |
| `quark info REF` | 精确读取文件或目录详情，并核对返回 FID。 |
| `quark tree [REF]` | 递归显示目录树和绝对网盘路径。REF 默认 `/`；`--depth N` 范围 0–1024、默认 3。 |
| `quark search QUERY [--in REF]` | 在指定子树按文件名不区分大小写地搜索子串；REF 默认 `/`，不包括起始目录本身。 |

`ls --all`、`tree` 和 `search` 会检查分页、FID 和父子关系；遍历最多 1024 层、100,000 个对象。服务端索引不一致或循环对象会令命令失败，而不是输出不完整结果。

示例：

```sh
quark ls --all --size 100 '/工作资料/发票'
quark info '/工作资料/发票/2026.pdf'
quark tree '/工作资料' --depth 2
quark search invoice --in '/工作资料'
```

## 6. 创建目录、上传、下载、改名、移动和删除

| 命令 | 参数与行为 |
| --- | --- |
| `quark mkdir NAME [--parent REF]` | 在父目录创建一个目录；父目录默认网盘根 `0`。支持 `--dry-run`、`--json`、`--scope`。 |
| `quark mkdir /完整/网盘/路径` | 完整路径形式只创建缺失的末级目录；父目录必须存在。不能与 `--parent` 同用。 |
| `quark upload LOCAL [--parent REF]` | 上传本地普通文件；目标父目录默认 `0`。支持 `--resume`、`--progress`、`--limit-rate BPS`、`--dry-run`、`--recursive`。空文件不支持。 |
| `quark download REF --output PATH` | 下载文件到本地路径。默认不覆盖已有文件；`--overwrite` 允许替换。支持 `--resume`、`--progress`、`--limit-rate BPS`、`--connections N`、`--recursive`。下载命令没有 `--dry-run`。 |
| `quark rename REF NAME` | 重命名文件或目录；不能重命名根目录。支持 `--dry-run`。 |
| `quark mv REF --to REF` | 移动文件或目录至已有目标目录。支持 `--dry-run`；拒绝把目录移动到自身或后代。 |
| `quark rm REF` | 将对象移到夸克回收站，不是永久删除。交互终端会询问确认；脚本/非交互环境必须传 `--yes`。支持 `--dry-run`。 |

递归传输选项：

| 选项 | 作用 |
| --- | --- |
| `--recursive` | 上传完整本地目录树，远端路径为 `父目录/本地目录名/...`；下载网盘目录内容到 `--output`，保留空目录。文件仍逐个处理；符合条件的单个下载文件可以使用 Range 并发。 |
| `--resume` | 依据私有状态核验并恢复原传输。未知写请求不会自动重发；本地源、远端身份或已确认内容变化会停止恢复。 |
| `--progress` | 将字节进度和速度写到 stderr，不混入 JSON stdout。 |
| `--limit-rate BPS` | 正整数，单位字节/秒。递归操作中的所有文件共享总预算；哈希计算不受限速。 |
| `--connections N` | 下载连接数 1–16，默认 4；新下载文件至少 8 MiB 且服务端支持强 ETag 校验的 Range 请求时启用。`--resume` 保持串行检查点流程。 |

下载链接通过固定的夸克 API 获取，并使用桌面客户端 User-Agent，避免浏览器式接口对大文件返回 23018。对象存储请求仍执行目标主机校验；只有经过验证的传输域会按需收到最小化 Cookie。目录传输会预检本地链接、远端路径和跨平台文件名。传输状态保存在用户私有配置目录；不要删除状态文件来“重试”一个结果未知的传输。网盘上传不支持零字节文件。已安装 CLI 不会将完整 Cookie 发送给上传 OSS。

### 大文件下载与并发测速经验

使用当前登录会话下载 `三猴拆炸弹.rar`（312,697,554 字节，约 298.2 MiB）的实测结果如下：

| 并发连接数 | 耗时 | 平均速度 |
| ---: | ---: | ---: |
| 1 | 36.37 秒 | 8.60 MB/s |
| 8 | 9.97 秒 | 31.35 MB/s |
| **16** | **8.73 秒** | **35.80 MB/s** |
| 32（实验） | 9.58 秒 | 32.63 MB/s |

这次 16 路最快，约为单路的 4.16 倍；32 路只用于对比，正式参数范围仍是 1–16。为避免请求过于密集，当前默认并发为 4，需要复现实测最高速时可显式传 `--connections 16`。实际速度受夸克节点、网络线路和本机磁盘影响，50 MB/s 是上限而非保证值。大文件若遇到网页下载接口的 23018 大小限制，CLI 会使用桌面客户端 UA 获取下载链接；不需要重新登录。

PowerShell 下载示例：

```powershell
quark download '/游戏/瞎子聋子哑巴拆炸弹/三猴拆炸弹.rar' `
  --output 'E:\TempDownload\三猴拆炸弹.rar' --connections 16 --progress
```

```sh
quark mkdir '/工作资料/新目录' --dry-run
quark upload './报告.pdf' --parent '/工作资料' --dry-run
quark upload './照片' --parent '/工作资料' --recursive --resume --progress --limit-rate 1048576
quark download '/工作资料/照片' --output ./下载结果 --recursive --resume --progress
quark rename '/工作资料/旧名.pdf' '新名.pdf' --dry-run
quark mv '/工作资料/新名.pdf' --to '/归档'
quark rm '/归档/旧文件.pdf' --yes
```

## 7. 容量、重复项、批量改名和备份

| 命令 | 说明与选项 |
| --- | --- |
| `quark du REF` | 完整遍历目录或统计单文件的文件数、目录数、已知字节和未知大小文件。只有完整遍历成功才返回报告。 |
| `quark duplicates REF` | 仅用可靠全文件 MD5/SHA-1 与大小报告确定重复；只有名称或大小一致的情况标为候选。命令不会删除对象。 |
| `quark rename-batch REF --pattern REGEX --replacement TEXT` | 对目录直接子项使用 Python `re.sub` 改名。默认只预览；传 `--apply` 才提交。`--apply --dry-run` 仍然只预览。 |
| `quark backup LOCAL_DIR --to REF` | 将本地目录的内容单向备份到一个已存在的远端目录。默认预览；`--apply` 执行上传。 |

`rename-batch` 会在写入前整体检查目标名称、冲突和范围，并按 FID 逐项提交。它不支持需要临时改名的名称交换或改名链。出现部分失败时应先检查 stderr 中的操作证据，不要直接重复提交。

`backup` 不删除远端多余文件。已管理文件发生变化时默认报冲突；只有指定 `--update` 才执行更新流程，并保留旧内容为归档。命令维护本机私有清单，将本地对象映射到精确远端 FID。清单损坏、对象身份改变或写请求结果未知时会停止，不能通过删除清单重置备份。

`backup` 支持 `--progress`、`--limit-rate BPS`、`--dry-run` 和 `--json`。默认预览与 `--apply --dry-run` 都不提交远端变更。`--limit-rate` 为正整数，单位字节/秒。

```sh
quark --scope '/工作资料' du '/工作资料/项目' --json
quark duplicates '/工作资料/项目' --json
quark rename-batch '/工作资料/项目' --pattern '^旧-' --replacement '新-' --dry-run
quark rename-batch '/工作资料/项目' --pattern '^旧-' --replacement '新-' --apply
quark backup './本地项目' --to '/工作资料/备份' --dry-run
quark backup './本地项目' --to '/工作资料/备份' --apply --progress
quark backup './本地项目' --to '/工作资料/备份' --apply --update
```

## 8. 查看分享和管理自己的分享

| 命令 | 说明与选项 |
| --- | --- |
| `quark share inspect URL` | 查看别人分享的内容。`--passcode CODE` 提供提取码；URL 中有 `pwd` 时，显式选项优先。`--path SHARE_PATH` 选择分享内目录，默认 `/`。只读，不会转存。 |
| `quark share create REF` | 分享本人网盘中的一个文件或目录；不允许分享账户根目录。可用 `--passcode CODE` 设置恰好 4 位 ASCII 字母数字码，省略则为公开分享。`--expire-days` 允许 `0`（永久）、`1`、`7`（默认）或 `30`。支持 `--dry-run`。 |
| `quark share list` | 列出本人的分享。默认最多返回 50 项；`--all` 读完整目录。指定 `--scope` 时只显示已完整证明所有成员都在范围内的分享。 |
| `quark share revoke SHARE_ID` | 取消本人分享，要求账户内部 `share_id`。必须显式传 `--yes`；`--dry-run` 可先预览。公开链接中的 ID 不是这里需要的 ID。 |

`inspect` 的 `URL` 应是 `https://pan.quark.cn/s/<分享ID>`。分享内路径从分享视图的根开始；可先查看 `/`，再以返回目录名称指定子路径。分享来源目录的内容会整体包含在分享里。

创建分享会返回链接及内部 ID。请自行妥善保存私密分享提取码；分享创建/取消是单次提交操作，响应或回读不能确认时 CLI 不会自动重试，也不会替你自动取消部分成功分享。分享标题采用源文件/目录名称。

```sh
quark share inspect 'https://pan.quark.cn/s/example' --passcode Ab12 --path '/共享目录/视频'
quark --scope '/CLI-CRUD-TEST' share create '/CLI-CRUD-TEST/demo.txt' --passcode Ab12 --expire-days 1 --dry-run
quark share list --all --json
quark share revoke INTERNAL_SHARE_ID --dry-run
quark share revoke INTERNAL_SHARE_ID --yes
```

## 9. 转存分享

```sh
quark save URL --to REMOTE_PATH [--passcode CODE] [--source SHARE_PATH]
              [--include GLOB] [--exclude GLOB] [--dry-run]
```

`URL` 是分享链接；`--to` 是本人网盘中的绝对目标路径。`--source` 指定分享中的来源目录，默认 `/`。CLI 可以在已存在的目标父目录下创建缺失的末级目标目录，不会递归创建多个缺失目录。已存在同名目标对象会导致冲突，不会覆盖。

不加筛选时转存来源目录的所有直接项目。可重复指定 `--include`，命中项取并集；`--exclude` 从中剔除，且排除优先。匹配区分大小写，只匹配直接子项名称；选中目录后会转存其整个子树。Shell 通配符要加引号，例如 `'*.mkv'`。没有任何选中项目时命令成功返回跳过结果，不创建目标目录。自身分享无法转存到本人账户。

`--dry-run` 会读取分享、筛选项目并验证账户目标，但不提交转存或创建目标。示例：

```sh
quark save 'https://pan.quark.cn/s/example' --passcode Ab12 --source '/电视剧/测试' --include '*.mkv' --to '/影视/待整理' --dry-run
quark --scope '/CLI-CRUD-TEST' save 'https://pan.quark.cn/s/example' --to '/CLI-CRUD-TEST/转存结果' --json
```

### 批量转存

`save-batch FILE` 串行处理 UTF-8 输入文件。全批输入格式和目标范围会先检查，再逐行执行；失败行、已写入结果和阻止执行的后续行会单独报告。未知写操作不会自动重新提交。

- `.txt`：每行一个链接；空行及去除空白后以 `#` 开头的行忽略。必须使用 `--to` 指定所有行的目标。
- `.csv`：必须有 `url` 列；可包含 `to`、`passcode`、`source`。CSV 行的非空 `to` 和 `source` 优先于命令行默认值。提取码应仅保存在私密 CSV 中。
- 重复的 `--include`/`--exclude` 对每一行生效；`--source` 默认 `/`。
- `--report PATH` 将安全 JSON 结果原子写到本地文件；父目录必须已存在。报告不含链接、提取码或凭证。
- `--dry-run` 只预览，不提交网盘变更。

```sh
quark save-batch links.txt --to '/工作资料/批量转存' --include '*.pdf' --dry-run
quark save-batch links.csv --scope '/工作资料' --report ./batch-result.json --json
```

## 10. 增量转存任务

| 命令 | 说明 |
| --- | --- |
| `quark job add NAME URL --to REMOTE_PATH` | 新建本地增量任务。可选 `--passcode CODE`、`--source SHARE_PATH`、重复的 `--include GLOB`、`--exclude GLOB` 和 `--scope REF`。只保存配置，不立即转存。 |
| `quark job list` | 查看本地任务的安全字段，不需要 Cookie。不会显示提取码。 |
| `quark job run [NAME]` | 执行指定任务；省略 NAME 时依次执行所有任务。支持 `--dry-run` 预览。 |
| `quark job remove NAME` | 只移除任务配置；保留增量账本及未知写入意图，不会取消已提交到服务端的操作。 |

任务首次运行会按来源身份、版本及目录关系核验并转存新项目，后续运行只处理新出现的项目。源文件发生变化、目标身份不符或服务端结果无法确认时会报告冲突并停止相关写入；不会按相同文件名擅自覆盖。`job add` 设置的 scope 会作为任务固定边界，后续的 `--scope` 只能再缩小范围，不能放宽原范围。配置和账本存放在用户私有配置目录。

```sh
quark job add shows 'https://pan.quark.cn/s/example' --source '/电视剧' --include '*.mkv' --to '/影视/电视剧'
quark job list --json
quark job run shows --dry-run
quark job run shows
quark job run
quark job remove shows
```

CLI 每次执行后退出。定时运行由操作系统调度器负责；使用当前用户的 `quark` 安装路径和私有配置目录，不要把 Cookie 或提取码放入调度参数。Windows 任务计划程序、Linux cron 和 systemd timer 示例见 [README 增量任务说明](../README.md#system-scheduling-examples)。

## 11. 常见工作流

先安装、登录并检查认证，再用 `ls`/`tree` 确认路径：

```sh
quark auth login
quark auth status
quark tree '/工作资料' --depth 2
```

限定在一个目录内做写入时，先建立范围，预览再执行：

```sh
quark --scope '/工作资料/待处理' ls --all
quark --scope '/工作资料/待处理' mkdir '/工作资料/待处理/已归档' --dry-run
quark --scope '/工作资料/待处理' mkdir '/工作资料/待处理/已归档'
quark --scope '/工作资料/待处理' mv '/工作资料/待处理/报告.pdf' --to '/工作资料/待处理/已归档' --dry-run
```

分享转存前查看项目，先预览目标和过滤结果，再实际提交：

```sh
quark share inspect 'https://pan.quark.cn/s/example'
quark save 'https://pan.quark.cn/s/example' --source '/分享目录/剧集' --include '*.mkv' --to '/工作资料/剧集' --dry-run
quark save 'https://pan.quark.cn/s/example' --source '/分享目录/剧集' --include '*.mkv' --to '/工作资料/剧集'
```

## 12. 故障排查

| 现象 | 检查方法 |
| --- | --- |
| 找不到 `quark` 命令 | 确认虚拟环境已激活；运行 `python -m pip show quark-drive-cli`，或用对应环境中的绝对路径运行 `quark`。 |
| 未认证或认证过期 | 运行 `quark auth status`；失效后用 `quark auth login` 重新扫码。不要复制浏览器 Cookie 到共享日志。 |
| 目标路径歧义或父路径不存在 | 用 `quark ls REF` 逐层确认名称；路径要求精确匹配，`mkdir` 只自动创建末级目录。 |
| 操作因 scope 被拒绝 | 检查 `--scope` 与 `QUARK_SCOPE`；显式 `--scope` 优先。确认源、目标及其父链都属于该范围。 |
| 写操作报告结果未知 | 保留 stderr 的安全 `error.operation` 证据，不要直接重新运行相同写命令。CLI 会保留可恢复状态，但不会盲目重发。 |
| 分享转存失败或筛选结果为空 | 用 `share inspect URL --path ...` 确认分享路径和提取码；筛选只匹配来源目录的直接名称。 |
| Windows/Linux 文件名冲突 | 目录传输使用两平台通用名称规则；不支持的设备名、大小写或 Unicode 规范化冲突会被拒绝。 |

更多协议边界及实现验收记录见 [README](../README.md) 和 [扩展验证记录](validation/2026-10-01-cli-extensions.md)。
