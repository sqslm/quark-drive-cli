# 夸克网盘 PotPlayer 本地桥接接口

供 PotPlayer AngelScript 扩展调用的本地 HTTP 契约。服务只监听 `127.0.0.1`，随机端口，每用户单实例；除 `/v1/health`、`/v1/stream/*` 外，全部接口要求请求头 `Authorization: Bearer <token>`。

## 启动与生命周期

- `python -m quark_cli.potplayer --ensure-service`
  复用或隐藏启动后台服务，成功后在标准输出打印单行 JSON：

  ```json
  {"base_url":"http://127.0.0.1:52311","token":"<43 字符>"}
  ```

  进程随即退出（`0`）。扩展从 `HostExecuteProgram` 捕获该行并保存 `base_url` / `token`。
- `--serve`：在当前进程运行服务（供打包后的常驻组件使用）。
- `--stop`：以记录的 token 请求 `/v1/shutdown`；实例不存在时视为已停止（`0`）。
- `--login`：打开 Tkinter 扫码窗口，登录**插件自己的**凭证；不影响 `quark` CLI 的 Cookie。
- `--player-pid`：扩展读取当前播放器 PID 的本地动作。启动器从自身父进程链定位真正拥有 `PotPlayer`/`PotPlayer64` 窗口的进程，在标准输出打印单行 JSON `{"player_pid": 整数}`（严格 JSON 整数，非字符串）；无法验证时为 `{"player_pid":0}`。该动作由启动器就地完成，**不启动桥接、不读取 Cookie、不访问网络**；协议版本保持 `1`，动作语义不变。扩展每次解析都调用一次，不缓存（多实例可能共享 INI）。

单实例：插件目录下的文件锁；已有实例时 `--ensure-service` 直接复用而不启动第二个。运行信息（端口、token、pid）写入插件私有目录 `config_directory()/potplayer/runtime.json`，退出时仅当实例号匹配才清除。服务不注册 Windows 服务、不开机启动、不监听局域网。

## 公共接口

### `GET /v1/health`

无鉴权。返回：

```json
{"status":"ok","version":"1","instance":"<16 位十六进制>"}
```

### `GET /v1/auth`

返回插件凭证状态：

```json
{"authenticated":false,"login_state":"idle","login_message":null,"qr_png":null,"account_id":null}
```

- `authenticated`：当前账号是否有已保存凭证（本地判断，不联网校验）。`account_id` 为当前账号的本机不透明标识，未选择账号时为 `null`。
- `login_state`：`idle`（未开始或已取消）、`waiting`（窗口已打开，等待扫码）、`success`（凭证已保存）、`failed`（失败，见 `login_message`）。
- `login_message`：失败原因（中文，可直接展示）。
- `qr_png`：等待期间二维码图片的临时路径；窗口自己展示，扩展可忽略。

任何数据接口收到账号 API 的 `401`（`login_required`）后，服务立即丢弃插件自己的（过期）凭证——包括进程内注入的会话与磁盘上的插件凭证文件——`authenticated` 随即变 `false`，扩展据此重新扫码；`quark` CLI 的 Cookie 始终不受影响。源站签名 `403` 不会触发此失效：最多重新取链一次，仍失败则暂停 60 秒并返回 `403 access_restricted`，插件凭证与收藏/最近记录保持不变。

### `POST /v1/auth/login`

快速返回 `202`，立即在后台线程打开可见扫码窗口，`body` 与 `/v1/auth` 相同（`login_state` 为 `waiting`）。已在等待中重复调用只返回当前状态，不会叠加窗口。若共享请求冷却仍在生效，直接返回 `429 rate_limited` 或 `403 access_restricted`（带 `Retry-After`），不会创建任何窗口或线程。

- 二维码过期/超时：窗口自动刷新二维码重试（有上限，超过后按 `failed` 报告）。
- 关闭窗口：取消当前轮询，不保存凭证，状态回到 `idle`。
- 账号凭证在扫码确认后被校验；无效时窗口与 `login_message` 明确提示重新扫码，状态 `failed`。
- 成功后重新读取 `/v1/auth` 即得到 `authenticated: true`。

扩展建议流程：数据接口收到 `login_required`（或用户主动登录）→ `POST /v1/auth/login` → 轮询 `/v1/auth` 直到 `authenticated` 为真（成功）或 `login_state` 为 `failed`（提示 `login_message`）；`idle` 表示用户关闭了窗口，不要自动重开。

### `POST /v1/auth/logout`

清除当前插件账号凭证并使旧播放会话失效，保留该账号本机观看数据，返回 `{"authenticated": false}`。只删除插件自己的凭证，绝不读取或删除 `quark` CLI 的 Cookie。

### 请求体处理

`auth/login`、`auth/logout`、`shutdown` 不接受请求体；若客户端仍带 `Content-Length`，服务会读完这段有界请求体后再响应，避免污染同一 keep-alive 连接上的下一次请求（无请求体的 `POST` 亦正常）。请求体上限 `64 KiB`，超出则关闭连接。

## 账号管理与请求隔离

下列接口均需本地 Bearer；返回的账号列表含 `active`、`items`，读取接口另带 `login_state` 与 `login_message`。每行字段为 `id`、`nickname`、`logged_in`、`current`、`capacity`；容量是缓存的 `{used_bytes,total_bytes}` 或 `null`，剩余量取 `max(0,total_bytes-used_bytes)`，从不返回 Cookie 或原始 UID。

| 接口 | 行为 |
| --- | --- |
| `GET /v1/accounts` | 读取本地账号与扫码状态；可导入独立登录命令已保存的凭证 |
| `POST /v1/accounts/switch`，`{"id":"..."}` | 切换已登录账号，保留其他凭证；已退出账号返回 400 |
| `POST /v1/accounts/logout`，`{"id":"..."}` | 清除所选账号凭证；退出非当前账号不影响当前播放 |
| `POST /v1/accounts/refresh`，`{}` | 读取当前昵称与容量并更新缓存；只查询当前账号 |
| `POST /v1/auth/login` | 扫码添加或重新登录，成功后激活该账号；取消保持当前账号 |

- `/v1/diagnostics` 只返回诊断、播放状态和用于操作校验的 `account_id`，不包含账号列表、扫码状态或精彩标记请求。账号页独立读取 `/v1/accounts`；精彩标记页独立读取 `/v1/bookmarks/state`。诊断导出白名单不变。
- 目录、分页、逻辑播放链接和字幕选项携带账号标识；请求用 `X-Quark-Account` 指明来源账号。与当前账号不同返回 `409 account_changed`，提示从根目录重新打开。存在多个账号时，缺少账号归属的数据请求同样拒绝；唯一随机播放会话可用于字幕查询，返回所属 `account_id` 后绑定字幕选项。
- 数据控制请求与切换互斥；账号资料查询在锁外发起，回写时核对账号和凭证，迟到成功或失效响应不会修改、退出新账号。本地诊断与精彩标记上下文读取不等待云端操作锁。旧流固定使用原客户端，迟到刷新不借用新账号凭证；切换撤销旧会话，旧观看写入被拒绝，旧客户端在最后一个流退出后关闭。
- 账号标识用扫码 Cookie 的 `__uid` 摘要生成；缺失 UID 时按整份凭证摘要单独建档，绝不按昵称合并。相同 UID 重新扫码仍使用原数据目录。
- 升级时，原 `library.json` / `bookmark.json` 仅归属导入旧凭证的那个账号，继续保留原路径。没有凭证时的旧文件不自动归给新账号。

## 目录与播放列表

### `GET /v1/files?parent=<fid>&page=<n>&size=<n>`

- `parent`：目录 FID，缺省或空串等价于根目录 `0`。
- `page`：从 `1` 开始，上限 `1000000`。
- `size`：每页条数，`1..100`，缺省 `50`。
- `include_series=1`：第一页从一次完整目录快照返回本页内容，并附 `series`（同 `/v1/series`）和 `favorite` 布尔值。扩展复用这些字段，避免为继续观看和收藏状态再读一次目录。后续页仍按原分页规则读取；未传该参数时仍按原分页返回 items/page/has_more，目录导航元数据独立附加。

返回服务端排序的原始一页，不递归、不重排：

```json
{"items":[{"fid":"...","name":"剧集","is_dir":true,"size":0}],"page":1,"has_more":false}
```

未知目录 → `502 remote_error`；参数非法 → `400 bad_request`。

### `GET /v1/playlist?parent=<fid>&start=<fid>`

返回同一目录内**自然排序**的视频项，供选集与自动下一集：

```json
{"parent":"<解析出的目录 fid>","items":[{"fid":"...","name":"第2集.mp4","is_dir":false,"size":123}]}
```

- 读取该目录全部页后按“第9集 < 第10集”的自然序排序（`library.playlist`）。
- 仅包含视频扩展名（见 `naming.VIDEO_EXTENSIONS`），目录与非视频文件跳过。
- `start`：从该视频起截取到末尾（含自身）。
- 只给 `start`（省略 `parent`）时，自动从该文件的详情推导其父目录再排序。
- `start` 不属于该目录视频 → `400`；目录或文件不存在 → `502`。

### `POST /v1/play`

请求体 `{"fid":"<视频 fid>"}`，可选 `"player_pid":1234`（**严格 JSON 整数**，非字符串），返回：

```json
{"url":"/v1/stream/<session>/<URL 编码文件名>","title":"第2集.mp4"}
```

`url` 是本地相对地址，只含不可猜测的会话标识；**不含** Cookie、账号信息或夸克签名地址。目录 FID → `502`；`fid` 缺失/空 → `400`。

带 `player_pid`（严格 JSON 整数，`1..4294967295`）时，服务把该播放器窗口进程与本次会话登记为绑定，用于按“PID + 完整逻辑 fid 地址”核对并恢复观看进度；扩展通过启动器本地动作 `--player-pid` 取得该整数，只有验证到拥有 PotPlayer 窗口的进程才发送，取不到时保持仅 `fid` 的旧请求体。省略或旧版调用仍然只在线播放、不绑定、不续播。响应结构不变；`player_pid` 不是该范围的整数（含字符串、布尔、浮点、`0`、负数）→ `400 bad_request`。

## 搜索、收藏、最近与继续观看

扩展开关在根页面选择类别：`drive`（我的网盘）、`search`（`type=search`，用 Query 输入关键词）、`favorites`、`recent`、`continue`（追剧中心）、`highlights`（精彩标记）。搜索只在根页面消费 `Query`；进入结果目录后按 `PathToken` 里的 FID 正常浏览，不再使用 `Query`。

### `GET /v1/search?q=<keyword>&page=<n>&size=<n>`

全网盘**文件名**搜索，支持中文（`q` 需 URL 编码）。`q` 必填且去空白后非空，`page` 从 `1` 开始（上限 `1000000`），`size` `1..100`（缺省 `50`）。返回：

```json
{"items":[{"fid":"...","name":"剧集","is_dir":true,"size":0}],"page":1,"has_more":false}
```

- 只返回**目录与视频**（过滤掉图片、文本等），`has_more` 依据远端原始总数而非过滤后的条数，避免隐藏海报/文本后整页丢失。
- 远端请求 `file/search`，参数 `q,_page,_size,_fetch_total=1,_sort=file_type:asc,file_name:asc`；不启用文件名 HTML 高亮。
- `q` 缺失/空白 → `400 bad_request`；未登录 → `401 login_required`。

### `GET /v1/favorites`

返回已收藏的目录，最近添加在前：

```json
{"items":[{"fid":"...","name":"剧集","is_dir":true,"size":0,
  "last_episode":{"fid":"...","name":"第2集.mp4","is_dir":false,"size":123,"parent":"...",
    "watched":false,"position_ms":0,"duration_ms":0,"display":"第2集.mp4"}}]}
```

`last_episode` 是该目录在最近观看里最新的一集（按记录的实际 `parent` 目录匹配，不会跨目录误配），本地没有记录时为 `null`。

### `POST /v1/favorites`

请求体 `{"fid":"<目录 fid>"}`。服务端向上游校验这是**真实目录**并取其名称，按 FID 去重（重复添加幂等，已存在的条目位置不变）。返回与 `GET /v1/favorites` 相同的 `items`。非目录或 `fid` 缺失/空 → `400 bad_request`；远端找不到 FID 或返回无效详情 → `502 remote_error`。

### `POST /v1/favorites/remove`

请求体 `{"fid":"<目录 fid>"}`。幂等删除：FID 不存在也返回成功，**不修改远端**，因此网盘里已删除/移动的目录仍能移除。返回 `items`。

### `GET /v1/recent`

最近**成功播放**过的视频，最近在前，按 FID 去重，最多 `100` 条：

```json
{"items":[{"fid":"...","name":"第2集.mp4","is_dir":false,"size":123,"played_at":1699999999,
  "parent":"...","watched":false,"position_ms":0,"duration_ms":0,"display":"第2集.mp4"}]}
```

`played_at` 是本机 Unix 秒。`parent` 是该视频真实所在目录的 FID（从未知目录播放时为空串），`watched` 表示手动标记或已基本看完（见下），`position_ms`/`duration_ms` 是本机续播点，`display` 是把已知目录名与进度并入后的显示标题：已确认所属目录时为“目录名 / 原文件名 · 已看 x%”，未知目录则保留原文件名（原始 `name` 字段始终不变）。条目用于从该集开始同目录选集。

### `GET /v1/continue`

本机历史里**可续播且尚未看完**的视频，最近有进度者在前。过滤规则与播放器的续播判断一致：续播点至少 15 秒，且距结尾仍超过 `min(30 秒, 时长的 2%)`；手动标记为已看或已看完的视频不出现。返回与 `/v1/recent` 相同的行结构（不含 `played_at`）。纯本地读取，不向上游发请求。

该接口保持原有扁平结构不变（0.8.0 未改动）；追剧中心根页用的是下面的 `/v1/watch-center`。

### `GET /v1/watch-center`

本机观看记录的分组视图，供扩展的“追剧中心”根页使用。**不需要**夸克登录，纯本地读取，不向上游发请求。返回：

```json
{"items":[{"kind":"series","key":"...","parent":"...","name":"剧集","display":"剧集 · 上次：第9集.mp4 · 已看5%",
  "latest":{"fid":"...","name":"第9集.mp4","is_dir":false,"size":123,"parent":"...","watched":false,
    "position_ms":90000,"duration_ms":1800000,"display":"第9集.mp4 · 已看 5%"},
  "unfinished_count":2}]}
```

- 按最近观看记录中**真实的非根父目录 FID** 分组：同一目录的视频合成一条 `kind` 为 `series` 的条目；同名但 FID 不同的目录**分开**成多条，不按名字合并。分组按各自最近一集的时间从新到旧排列。
- 根目录（存储的 `parent` 为 `0`）或无法确认所属目录的视频各自单列一条 `kind` 为 `video` 的条目，用原文件名，不猜测剧名或类型。
- `key` 为分组标识（剧集分组为目录 FID，单集为视频 FID）；`parent` 为该分组视频真实所在目录的 FID（根/无法确认时为空串）。`name`：单集为该文件自己的名字；剧集分组为**已验证**的目录名（组件已经拉取过的目录清单，或记录里存的目录名），都确认不了时用固定占位“来源目录未命名”——它从不使用窗口标题、不猜剧名，也不为此新增任何云端请求。
- `display` 是给列表用的标题：剧集分组为“目录名 · 上次：<最近一集名>”再加那一集自己的进度（“ · 已看”或“ · 已看<百分比>”，此处进度紧接百分号，如“ · 已看5%”）；单集为文件名加同样的进度。它只反映**那一条最近记录**的进度，不代表目录总集数或整季是否看完。
- `latest` 为该分组最近观看过的一集视图（`fid`/`name`/`is_dir`/`size`/`parent`/`watched`/`position_ms`/`duration_ms`/`display`），按 `max(played_at, progress_at)` 取最新；`unfinished_count` 为该分组里仍可续播且未看完的视频数（判定与续播规则一致，并排除已标记已看）——已经全部看完的分组同样会列出，此时 `unfinished_count` 为 `0`。
- 该接口只读本地 `library.json`，不做后台扫描，也不为此发起任何云端请求。

### `GET /v1/series?parent=<目录 fid>`

某目录的继续观看目标与逐集状态。`parent` 必填，缺失/非法 → `400 bad_request`。只列出该目录本身一次（按自然顺序的视频），携带进度：

```json
{"parent":"...","total":3,"watched":1,"complete":false,
 "continue":{"fid":"...","name":"第10集.mp4","position_ms":90000,"duration_ms":1800000,"display":"第10集.mp4 · 已看 5%"},
 "latest":{"fid":"...","name":"第9集.mp4","position_ms":90000,"duration_ms":1800000,"display":"第9集.mp4 · 已看 5%"},
 "next":{"fid":"...","name":"第10集.mp4","position_ms":0,"duration_ms":0,"display":"第10集.mp4"},
 "episodes":[{"fid":"...","name":"第2集.mp4","size":123,"watched":false,
   "position_ms":0,"duration_ms":0,"display":"第2集.mp4"}]}
```

`watched` 为“手动标记已看 **或** 实际播放到结尾”的集数（用同一个 `min(30 秒, 2%)` 结尾窗口判定，位置为 0 或时长未知时不算已看）。`continue` 取进度最新的一集未看完视频；没有未看完的则取自然顺序里第一集未看的；整目录都看完时 `complete` 为 `true`、`continue` 为 `null`，扩展显示完成状态行而不会从头重播。目录中视频的名称、大小与归属只取服务端目录清单，存储里的旧名称/旧目录不会被采信。

`latest` 与 `next` 是 0.8.0 新增的两个**可空**单集视图，现有字段（`parent`/`total`/`watched`/`complete`/`continue`/`episodes`）语义不变：

- `latest`：该目录里最近观看过的一集（本机记录）。只有仍出现在本次目录清单中且**大小一致**的记录才算：被替换成不同大小的旧记录（`size` 不符）既不能作为 `latest`，其保存的续播点也被忽略——`episodes` 里该集按“无保存进度”处理（`position_ms`/`duration_ms` 为 `0`，除非被显式标记已看）。
- `next`：**本次目录清单**里自然顺序中 `latest` 的下一集，名称、大小等元数据全部取本次清单（与 `latest` 同由这一份清单推出，**不**读取该下一集的本机记录，也不要求它有同大小的记录）。`latest` 已是最后一集时为 `null`，**不会**回绕到第一集。`latest` 为 `null` 时（本机没有该目录可用的记录）改为给出清单里自然顺序第一集未看的（都看完则 `null`）。
- 两个视图都带 `episodes` 每项相同的字段，另含该集的 `is_dir`、`parent`、`unfinished`（是否仍可续播且未看完）与 `progress_at`（本机保存时间）；这些附加字段只用于分组页的排序与“下一集”判断，`episodes` 列表本身仍只有既有字段。
- 两者都只由本机记录与本次目录清单推出，不额外猜测剧集名或顺序。完成判定同理：仅当本次清单**完整、非空且每一集都已看**时 `complete` 才为 `true`（空清单或只有非视频文件时 `complete` 为 `false`）。

### `POST /v1/mark`

请求体 `{"fid":"<视频 fid>","parent":"<目录 fid>","watched":<bool>}`。手动标记某集已看/未看；服务端用 `parent` 目录清单校验 `fid` 确是其中的视频并取其真实名称与大小，**不信任**客户端提供的名称或大小。非该目录中的视频、目录 fid、缺失/非布尔 `watched` → `400 bad_request`。成功返回 `{"fid":"<视频 fid>","watched":<bool>}`。同一目标状态重复执行幂等。

- `watched=true`：该集不再自动续播，也不出现在 `/v1/continue`。
- `watched=false`：清除该集的手动标记与旧的续播点；此后播放器的真实进度探测可再次写入新的续播点（不会向正在播放的窗口发送跳转）。

### `POST /v1/recent/clear`

清空本机最近记录，幂等；请求体按通用规则读完有界内容后忽略，返回 `{"items":[]}`（扩展固定发送一个非空 JSON 请求体）。

### `POST /v1/recent/remove`

请求体 `{"fid":"<视频 fid>"}`。只从本机最近记录里删除**该视频这一条**（连同它的续播点），返回 `{"status":"removed"}`；FID 不存在或已经删除也返回同样结果（**幂等**）。`fid` 缺失/空 → `400 bad_request`。

**不访问夸克**，也不改动收藏、独立的“已看”标记（`marks`）或精彩标记；**不会**对播放器跳转或停止，播放本身继续，但该 `fid` 的实时监控会被抑制：正在播放的会话不再把这条观看历史与进度写回，删除**不会**被在途写入或关闭时的保存复活——只有再次显式播放（新的 `/v1/play`）才恢复该视频的记录与进度写入。

### 写入时机与数据边界

- 最近记录在**首个非空媒体块成功转发之后**写入，不等整集传输结束：取链、`HEAD`、未转发任何媒体字节的失败 `GET` 不记录。已经开始传输后再关闭、拖动或遇到上游中断，已写入的记录仍保留；每个 HTTP 请求只记一次，不按分段或数据块重复写盘。
- 收藏与最近观看存放在账号私有目录 `config_directory()/potplayer/accounts/<account_id>/library.json`（升级导入账号沿用原 `potplayer/library.json`）（单进程内用一把普通线程锁保护读改写）；只含 FID、名称、大小与本机时间，**不含** Cookie、签名地址或视频数据。文件版本 2 起，最近观看条目另含观看进度字段 `position_ms`、`duration_ms`、`progress_at`（本机时间）；版本 3 起另含真实 `parent` 目录 FID，并新增独立的 `marks` 列表保存显式“已看”标记（不受 100 条最近上限影响）；`marks` 按 FID **和大小** 记录，FID 被替换成不同大小的文件时不会继承旧的已看标记。版本 4 起，最近观看条目另存真实目录名 `parent_name`：它只取自桥接**已经拉取过**的目录清单或“收藏目录”，从不使用窗口标题、从不猜测剧名、也不为此新增任何云端请求；`/v1/recent` 与 `/v1/continue` 的 `display` 因此显示“目录名 / 原文件名”，无法确认所属目录（含根目录，`parent` 为 `0`）时保留原文件名。版本 1/2/3 记录可正常读取，读取时不改写文件，仅在下次正常写入时才就地升级为版本 4，既有收藏/最近观看不受影响。
- 切换、退出与登录过期均保留各账号的本机数据。退出仅移除所选账号凭证；切回或重新登录同一账号后读取其独立记录。
- `/v1/watch-center`、`/v1/recent/remove`、本机 `/v1/bookmarks` 查看与编辑接口 与 `/v1/diagnostics*` 都只要求本地 Bearer，**不需要**夸克登录：`watch-center` 只读本地 `library.json`（不扫盘、不联网）；`recent/remove` 只删本地那一条观看记录，不动云端文件、收藏、`marks` 或精彩标记；`bookmarks/open` 只打开本机窗口。自动播放接口 `/v1/bookmarks/play` 需要登录；目录筛选使用本机标记，缺失的祖先信息可在登录时补全；失败不会隐藏本地标记。它们都不改变视频落盘策略：无分片、无磁盘缓存、无转码、无后台扫描，也不存在向云端回传记录或报告的步骤。

## 播放流

### `GET|HEAD /v1/stream/<session>/<name>`

无需 Bearer（会话标识本身即凭据，不可猜测）。每个会话绑定一个视频：

- 首次请求时按需向上游取链并转发；支持单范围 `Range`（含 `bytes=N-`、`bytes=-N` 后缀）。
- **大请求分段**：播放器请求的字节长度超过 4 MiB 时（含无 `Range` 的整文件、`bytes=N-` 开放范围、较大的有限范围与 `-N` 后缀范围），桥接按 4 MiB 切成**串行**上游有限 `Range`，每段校验通过后立即以 64 KiB 顺序转发，取满再取下一段，以避免源站对大请求限速；不预取、不并发、不落盘、不缓存整集。每段都校验精确的 `Content-Range`（起点/终点/总大小）与 `identity` 编码；源站返回 `Content-Length` 时校验其与范围一致，强 `ETag` 按下文规则比对。不符即中止。播放器仍收到原始状态码、`Content-Length`、`Content-Range`（整文件是 `200`，范围是 `206`）。≤4 MiB 的请求保持原样单次转发。
- `200` 整文件、`206` 分段（带 `Content-Range`、`Accept-Ranges: bytes`）、越界或多段/非法范围 `416`（带 `Content-Range: bytes */<size>`）。
- 编码固定 `Accept-Encoding: identity`，不落盘、不整文件预取、不做磁盘缓存；只保留有限内存缓冲。
- 上游签名地址失效（`401/403`）时，最多重新取链一次再试；仍失败 → `403 access_restricted`（暂停 60 秒新播放请求，插件凭证保留），绝不与账号 API 的 `401` 混为一类。
- **版本校验**：上游返回强 `ETag`（非 `W/` 前缀）时，会话在首个 Range 响应上原子固定该 ETag（并发首 Range 亦只有一个值胜出）；后续 Range 若收到不同的强 `ETag` 即中止播放。响应头尚未发出时返回 `409 media_changed`；同一播放请求已开始传输时关闭连接，停止后续分段。上游只给弱 `ETag` 或不给 `ETag` 时无法比对版本。
- 文件大小变化 → `409 media_changed`；`206` 的 `Content-Range` 边界非法或 `Content-Length` 与范围长度不符 → `502 remote_error`；上游返回非 `200/206`、非 `identity` 编码、或忽略分段请求而回 `200` 整文件 → `502`，不伪装成 `200`、不整文件下载。
- 未知/已失效会话 → `404`；`/v1/stream/` 缺会话 → `400`。
- 客户端断开则关闭当前上游连接，并停止请求后续分段。
- 每路播放使用独立传输会话，互不共享 Cookie jar；只向上游发送必要的 `__puus`，账号 Cookie 永不发往源站。

## 播放进度与诊断

### 播放进度（本机）

- 播放期间组件按绑定核对当前播放器进度，并把位置写入 `library.json` 中该最近观看条目的进度字段（`position_ms`、`duration_ms`、`progress_at`）。显式标记为已看的视频既不续播也不再写入真实进度，直到标记被清除。`/v1/recent`、`/v1/continue`、`/v1/series` 会把这些字段转为只读视图；其中 `progress_at` 只在 `/v1/series` 的 `latest`/`next` 单集视图里出现（用于分组页排序与判断），`/v1/recent`、`/v1/continue` 与 `/v1/watch-center` 都不暴露它。
- 读取频率约每 2 秒一次，落盘约每 10 秒一次，并在暂停、切换集数与关闭时各写一次。保存位置不足 15 秒时不续播；剩余时长不足 `min(30 秒, 时长 2%)` 时不回跳（从 0 开始）也不计为已看完；保存时的文件大小或时长与当前明显不符时不套用旧进度。
- 首次读到播放器位置已超过约 5 秒（例如用户手动拖过）时，尊重播放器当前位置，组件**不**把它强行拉回保存点。
- 绑定标识为播放器窗口进程 PID 与完整逻辑 fid 地址（`https://quark.invalid/{video,play}/<fid>`）；带 `?account=<account_id>` 时必须与创建该播放会话的账号完全一致，精彩标记地址同样校验账号。不使用文件名、窗口标题或下载字节比例推断归属。同一 fid 被多个实例同时打开时暂停该 fid 的进度写入，实例减少后恢复。
- 续播和缓存消费量估算依赖 Windows 原生进程读取能力；缺少该能力时不记录/恢复进度，先按网络原速提供最多 20% 数据，之后等待可信播放进度，避免无上限预下载。此限制不影响其它平台上的 CLI。

### `GET /v1/diagnostics`

需 Bearer（本地令牌）；**不需要**夸克登录，未登录/登录过期/冷却中均可访问。返回本机快照 `snapshot`：

```json
{
  "account": {"credential_present": true, "validation": "saved_unverified", "last_success_at": null},
  "transfer": {"totals": {"plays": 0, "active_plays": 0, "bytes": 0, "chunks": 0,
                          "speed_bytes_per_second": 0, "origin_requests": 0,
                          "status": {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0, "other": 0}},
               "plays": [{"name": "第2集.mp4", "bytes": 0, "chunks": 0, "seconds": 0,
                          "speed_bytes_per_second": 0, "origin_requests": 0,
                          "status": {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0, "other": 0},
                          "last_status": null, "last_elapsed_ms": null}]},
  "errors": {"count": 0, "events": [{"at": null, "phase": "播放", "kind": "unknown", "message": "未知错误", "status": null}]},
  "versions": {"release": "0.12.4", "protocol": "1", "schema_version": 1, "python": "3.x", "os": "Windows"},
  "protection": {"available": true, "active": false, "reason": null, "remaining_seconds": 0.0},
  "playback": [{"player_pid": 1234, "fid": "…", "name": "第2集.mp4", "position_ms": 0,
                "duration_ms": 0, "state": "waiting", "message": "等待播放开始"}]
}
```

- `account.validation` 取 `no_login` / `saved_unverified` / `recently_valid` / `expired`：前两者只表示“无凭证/凭证已保存但尚未验证”，`recently_valid` 只表示最近一次普通调用成功，不能证明账号当前未被限制；组件不会为此额外联网校验。
- `transfer.totals`/`plays` 统计的是**实际转发的字节**，重复拖动同一区间会重复计入；`speed_bytes_per_second` 为最近 5 秒平均转发速率，`last_elapsed_ms` 为最近一次上游响应头耗时。均为被动统计，不发起探针、缓冲或码率估算。
- `errors.events` 为固定（白名单）字段与固定中文 `message`，只保留最近 20 条，不含上游原文。`playback` 是本机绑定列表（含本机 `name`，仅本接口返回）；`protection` 反映与 CLI 共用的请求冷却，本地冷却状态不可读时 `available` 为 `false` 并给出固定 `instruction`。
- 精彩标记请求通过独立的 `GET /v1/bookmarks/state` 返回，诊断快照不承载该字段。

### `GET /v1/diagnostics/report`

需 Bearer；返回一份严格白名单的 JSON 报告，**不写盘、不上传**（导出动作由状态窗口发起，见下）。顶层字段为：`schema_version`（`1`）、`generated_at`（真实墙钟 UTC ISO 时刻）、`release`、`protocol`、`python`、`os`、`account`、聚合后的 `transfer`、`cooldown`、`errors`。

报告**不包含** Cookie、令牌、任何 URL/签名地址、FID、文件名、观看进度/历史或绝对主机路径。状态窗口的“保存排查信息”把它按 UTF-8 JSON 另存到用户选择的路径，由用户决定是否对外提供。

### `POST /v1/management/open`

需 Bearer；异步打开“夸克网盘插件管理”窗口，返回 `202`；重复调用复用窗口，关闭不停止播放。账号、诊断与标记三个独立页面分别持有客户端、工作线程和队列，壳窗口不持有账号列表或精彩标记数据；新增“安装管理”页只读取本机安装清单并调用独立卸载入口。`POST /v1/diagnostics/open` 保留为同一窗口入口。

### `GET /v1/bookmarks/state`

需 Bearer；只读本地 `{account_id, playback, bookmark_request}`，不访问夸克、不加载账号列表。`bookmark_request` 无请求时为 `null`；单视频请求带 `id/fid/name`；全部管理请求另带 `scope=all`、空 `fid`。请求 ID 用于一次性消费。该数据不进入诊断快照或导出报告。

账号文件异常时，诊断和此状态接口仍正常响应；账号接口返回明确的本地存储错误并禁止写入。支持旧 `{current, accounts: [...]}` 文件迁移：先写私有备份，再保存带版本号的结构，保留全部凭证、当前选择和原账号观看记录路径。

> 该接口的具体响应字段尚未最终固定，扩展只应依赖“HTTP `202` / 请求被接受”的能力语义，不应解析未约定的响应字段。

### `POST /v1/playback/restart`

需 Bearer；请求体 `{"player_pid":1234,"fid":"<视频 fid>"}`（`player_pid` 为**严格 JSON 整数** `1..4294967295`，`fid` 为字符串）。服务核对该 PID 当前确实绑定到同一 fid 的会话后，令播放器跳回 0 并把该 fid 的本机保存进度重置一次，**确认成功**才返回 `200 {"status":"restarted"}`（未确认的排队、超时或仅入队一律不算成功）。

失败一律返回 `409 media_changed`（固定错误，绝不对任意 PID/文件生效）：

- 该 PID 没有任何绑定，或绑定会话的 fid 与请求不符；
- 会话当前未在传输、播放器不可达，或该 fid 的进度记录已暂停（同一 fid 被多实例打开）；
- 本机进度写盘失败。

使播放器跳回 0 但进度重置未完成同样按失败处理（`409`）。参数缺失或非法（`player_pid` 非上述范围的整数、`fid` 缺失/空）→ `400 bad_request`；方法不是 `POST` → `405 method_not_allowed`。失败时状态窗口显示固定中文提示（如“从头开始失败，请稍后重试”）。

### 精彩标记

沿用既有 `bookmark.json` 和 `/v1/bookmarks*` 路径，名称变化不迁移旧数据。根分类新增 `highlights`；标记点数据仅在本机保存；目录名称缺失时可读取并缓存祖先详情。

- `GET /v1/bookmarks` 省略 `fid` 时返回所有视频的标记，最近添加在前，并增加 `groups: [{parent, name, count, category}]`，按真实剧集 FID 分组（跳过分辨率等版本层）。同名目录不合并；无法确认目录的组为 `parent: ""`、`name: "未分类"`。`?unclassified=1` 只列未分类标记。明确传空 `fid` 仍为 `400`；`?fid=<fid>` 仍返回 `{"items":[…]}`。
- `?parent=<目录 fid>` 按已保存目录筛选，离线可用。优先使用本机目录信息；祖先缺失时读取并缓存父目录详情，不扫描兄弟目录或整份网盘。补全失败仍返回本地标记及固定 `metadata_notice` 提示。返回项可含 `parent`、`parent_name`，已有最近记录与已浏览目录可用于就地补全。
- `POST /v1/bookmarks/open` 可传 `{"scope":"all"}`、`{"fid":"…"}` 或 `{"parent":"目录 fid"}`。独立启动/复用 `--highlights-window`，不打开插件管理或诊断窗口。目录请求在状态中包含 `scope: "parent"` 与 `parent`；可附加 `action: "add"`，列表加载后针对该目录唯一的播放绑定弹出备注框。
- 全局打开请求在 `/v1/bookmarks/state` 的 `bookmark_request` 中表示为 `{"id":"…","scope":"all","fid":"","name":"全部视频"}`；单视频请求仍是非空 `id`/`fid`。导出报告不包含此字段。
- `POST /v1/bookmarks/play` 请求体 `{"id":"…"}`：核对本机标记存在并要求已登录，然后通过安装清单定位 PotPlayer，用 `https://quark.invalid/mark/<id>` 打开。返回 `202 {"status":"opening"}` 只表示已请求打开；未知标记 `404`，安装信息不可用为本地错误。
- PlayParse 拦截标记链接，调用 `POST /v1/play`，请求体 `{"bookmark_id":"…","player_pid":1234}`。服务从本机标记确定真实 FID，拒绝传入不一致的 `fid`、缺少有效 PID 或已变化的文件大小。播放器实际开始读取且时长就绪后，监控线程按原生身份与标记时长核验并跳转一次；不应用普通续播的 15 秒/片尾限制，也不再回跳到旧的继续观看位置。响应仍是原来的 `url`/`title`。

以下原有接口继续可用：

需 Bearer；查看、修改备注、删除和打开管理窗口不要求夸克登录，记录存于对应账号的 `bookmark.json`。添加时间点仍取本机播放器的实时采样；自动打开未播放视频需要有效夸克登录。

- `GET /v1/bookmarks?fid=<视频 fid>` → `{"items":[{"id":"…","fid":"…","name":"…","size":123,"position_ms":0,"duration_ms":0,"note":""}]}`。`fid` 可省略以列出全部，最近添加在前；无精彩标记时 `items` 为空数组。
- `POST /v1/bookmarks/add`，请求体 `{"player_pid":1234,"fid":"…","note":"…"}`（`player_pid` 为**严格 JSON 整数**，`note` 为字符串、最长 200 个字符、**可以为空**）→ `200 {"bookmark":{…}}`。时间点与时长取自播放器的实时采样，**不信任**客户端提供的时间、名称或大小。
- `POST /v1/bookmarks/rename`，请求体 `{"id":"…","note":"…"}`（同样可空、最长 200 字符）→ `200 {"status":"renamed"}`；`id` 不存在 → `404 not_found`。
- `POST /v1/bookmarks/remove`，请求体 `{"id":"…"}` → `200 {"status":"removed"}`；`id` 不存在 → `404 not_found`。
- `POST /v1/bookmarks/jump`，请求体 `{"player_pid":1234,"fid":"…","id":"…"}` → `200 {"status":"jumped"}`。
- `POST /v1/bookmarks/open` 返回 `202 {"status":"opened"}` 或 `already_open`，每次使用新请求 id 重新定位目标列表；打开本身不要求正在播放、不访问云端。管理窗口中的精彩标记页不消费此请求，避免同时弹出两个窗口。查看、编辑备注、删除离线可用；跳转可通过 `/v1/bookmarks/play` 自动打开视频。

`add`/`jump` 只对**当前正在播放同一逻辑文件**的绑定生效，并核对实时身份：该 PID 没有绑定、播放器中的文件不符、同一 fid 被多实例打开、或本机缺少原生读取能力时返回 `409 media_changed`；`add` 在位置无效（为 0 或已在结尾）时同样失败，但它始终记录当前文件的真实时间点，因此文件换了大小/时长后仍可对新文件添加精彩标记；`jump` 额外在精彩标记保存的大小/时长与当前文件明显不符时失败（指向旧文件的旧精彩标记因此不可跳转）。参数、`fid`、`id` 或备注非法（含 `note` 非字符串或超过 200 字符）→ `400 bad_request`；写入路由只接受 `POST`，任何其它方法（含 `HEAD`）都只会得到 `405 method_not_allowed`，不会执行写入。`rename`/`remove` 只读写本地文件；`GET` 的目录元数据补全可访问夸克，标记本身始终来自本机；`add`/`jump` 也不产生额外的云请求。

精彩标记存放在账号目录的独立小文件 `bookmark.json`（升级导入账号沿用原路径）（与 `library.json` 分开，见下），**不**进入诊断报告；切换、退出及登录过期均保留。文件版本为 3，兼容读取版本 1/2；已知目录的条目包含 `parent`、`parent_name`，版本 3 还保存 `series_fid`、`series_name`、`version`、`category`、`video_prefix`。目录信息随标记持久保存，不依赖最近观看条数；旧文件缺少目录信息时保留原条目。

> 通用约定：本地令牌无效 → `401 unauthorized`；参数非法 → `400 bad_request`；方法不被支持 → `405 method_not_allowed`（与既有路由一致）。这些接口只要求本地 Bearer，不要求夸克登录，因此在未登录、登录过期或冷却中仍可用于查看状态与导出报告。

## 外挂字幕

原生“字幕搜索”列表用的两个只读接口：按当前视频找出同目录匹配的 SRT/ASS，并返回其文本。两者同样要求 Bearer 与已登录，只读当前夸克视频/字幕文件，绝不接受客户端提供的任意源 URL，也不写盘。

### `GET /v1/subtitles?fid=<视频 fid>` 或 `?session=<现有播放会话>`

- 两者只能指定一个：都给或都不给 → `400 bad_request`。
- `session` 经现有播放会话表查找，失效 → `404 not_found`。
- 核验是实际文件、视频扩展名，读取其 `pdir_fid` 并完整翻页扫描同目录，只匹配 SRT/ASS（大小写不敏感）。
- 返回：

```json
{"video_fid":"...","items":[{"fid":"...","name":"剧.S01E01.zh.ass","format":"ass","lang":"zh","match":"name"}]}
```

- `format` 为 `srt`/`ass`；`lang` 为 `zh`/`en`/空字符串（未知）；`match` 为 `name`（同名）或 `episode`（季集）。同名优先，允许常用语言后缀，季集支持 `SxxEyy` 与“第N集”，拒绝另一季/集/剧；中文候选优先，稳定排序，同目录其它文件不进入候选。
- 不暴露 Cookie 或签名源地址；未登录 → `401 login_required`。

### `GET /v1/subtitle?fid=<字幕 fid>`

- 核验是实际非目录的 SRT/ASS 才读取；视频或任意其它文件被拒绝。
- 仅内存读取，最多 4 MiB（元数据大小与实际读取长度同时约束）；复用现有可信域名与 `__puus` 隔离，签名失效刷新一次；刷新后仍被拒则暂停 60 秒并返回 `403 access_restricted`，凭证保留。
- 返回 `text/plain; charset=utf-8`，支持 UTF-8 BOM/UTF-16 BOM/GB18030，保留时间轴与格式，不替换乱码。
- 失败走统一 JSON 错误路径（扩展必须把错误 JSON 与字幕正文区分开，不能把错误当字幕）；不更新最近观看，不写盘。

## 请求节奏与冷却

桥接与 `quark` CLI 共用当前配置目录下的同一份请求节奏状态 `request_guard.json`（配稳定锁 `request_guard.lock`），因此跨进程与重启生效；其中只记录本机墙钟时间与固定原因，**不含** Cookie、账号身份、令牌或签名地址。

- 元数据/取链请求的发起间隔至少 0.5 秒；冷却期内新请求在本地直接拒绝，不触达上游。
- 上游 `429` 至少暂停 60 秒；账号或扫码 API 的 `403`，或失败业务响应中明确的“访问受限/需要验证”至少暂停 600 秒；更长的 `Retry-After`（秒数或 HTTP 日期）按其执行；`503` 仅在带有效 `Retry-After` 时暂停。
- 播放/字幕签名地址在一次重新取链后仍连续 `403`，暂停 60 秒新播放/字幕请求。
- 冷却期间的列表/数据/登录接口返回 `429 rate_limited` 或 `403 access_restricted` 并带 `Retry-After`；`/v1/health`、`/v1/favorites`、`/v1/recent`、`/v1/continue`、`/v1/auth/logout` 等本地接口仍可用。
- 冷却错误**不会**清除插件凭证、不会打开扫码登录、不会清空收藏/最近记录；`401` 凭证失效的语义不变。
- 若冷却状态无法写入本机，服务会以 `500 local_io_error` 停止后续自动请求；另一个进程或重启无法得知未写入的冷却，需先修复本机存储并用官方夸克客户端确认账号。
- 此节奏不改变 4 MiB 串行分段、64 KiB 转发或内嵌字幕/音轨的原生处理，预缓冲按总大小 20% 暂停、剩余 10% 恢复；媒体字节不设下载速率上限，未知时钟也不按时间补充预算。以上数值是本工具的保守默认值，不代表夸克官方安全阈值，不能保证账号不被限制。`Retry-After` 定义见 [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110#field.retry-after)（`429` 源自 RFC 6585 §4）。

## 错误格式

所有错误统一为：

```json
{"error":{"type":"<kind>","message":"<中文说明>"}}
```

| kind | HTTP | 含义 |
| --- | --- | --- |
| `unauthorized` | 401 | 本地 Bearer token 无效 |
| `login_required` | 401 | 插件未登录或登录已失效，需重新扫码 |
| `bad_request` | 400 | 参数或请求体非法 |
| `method_not_allowed` | 405 | 方法不被该路由支持 |
| `not_found` | 404 | 未知路由或播放会话 |
| `rate_limited` | 429 | 上游限流或本机冷却中；响应带 `Retry-After` |
| `access_restricted` | 403 | 上游拒绝访问或限制冷却中；响应带 `Retry-After` |
| `media_changed` | 409 | 文件大小/版本变化，播放中止 |
| `range_not_satisfiable` | 416 | 请求范围超出文件 |
| `remote_error` | 502 | 上游（夸克）返回错误 |
| `network_error` | 504 | 上游网络失败/超时 |
| `local_io_error` | 500 | 本地读写失败 |
| `account_changed` | 409 | 账号已切换或旧链接缺少账号归属 |
| `internal_error` | 500 | 未分类内部错误 |

## 凭证与数据边界

- 插件账号与凭证：`config_directory()/potplayer/accounts.json`，通过现有私有状态读写及当前用户 ACL 保存。扫码先写 `login-cookie`，完成建档后删除临时凭证。旧版及独立 `--login` 的 `cookie` 在启动或下一次 `/v1/auth` 时导入；`quark` CLI 的 Cookie 从不读取、写入或删除。
- 运行配置：同目录 `runtime.json`；单实例锁 `service.lock`。
- 收藏、最近观看与已看标记：`accounts/<account_id>/library.json`，含 FID、名称、大小与本机时间，版本 2 起另含观看进度（`position_ms`/`duration_ms`/`progress_at`），版本 3 起另含真实 `parent` 目录与 `marks` 已看列表，版本 4 起另存目录名 `parent_name`（只来自已拉取的目录清单或收藏目录，零额外云请求；未知目录保留原文件名）；切换、退出、过期与升级均保留。
- 精彩标记：`accounts/<account_id>/bookmark.json`（含 fid、名称、大小、时间点、时长与备注，备注最长 200 字符、可为空；不含 Cookie/令牌/签名地址）；切换、退出、过期与升级均保留；不进入诊断报告。
- 视频不落盘：无分片、无磁盘缓存、无转码；仅内存转发请求的字节范围。
- 内嵌字幕/音轨由播放器原生解码、显示与切换，本插件不替换、不自动选中外挂轨；同目录外挂字幕经原生“字幕搜索”手动选择，只从本桥接读取内存文本——桥接自身不写字幕或视频文件，播放器是否把所选字幕保存到磁盘由 PotPlayer 自身行为决定。
- 本地凭证与诊断数据保持私有：`/v1/diagnostics/report` 只在用户显式导出时才写文件，且经白名单裁剪，不含 Cookie/令牌/URL/FID/文件名/观看进度/绝对路径。
- 不含分享转存、海报库、转码。观看进度依赖 Windows 原生读取能力；其它平台的 CLI 不受影响。

## 0.12 目录与播放策略

- `GET /v1/files` 增加 `up`（真实父 FID）、`path`（祖先到当前目录的 fid/name/parent 数组）。缺失元数据通过 file/info 补齐并写入当前账号的 `directories.json`；无法补齐时保留目录列表，增加 `navigation_error`，原生入口显示重试提示。`parent=0` 不读取父链。
- 标记 `parent` 筛选同时匹配实际视频父目录或 `series_fid`，因此可管理一部剧的多个清晰度版本。全局分组使用 series_fid，分类独立于剧名。
- 原生有效播放采样超过 90% 后写入既有 `marks`，FID/size 隔离、存储代次屏障和手动操作校验不变；下次采样触发，不等待原先 10 秒进度周期。
- Content-Length、Range、Content-Range 不变，仍按最大 4 MiB 范围顺序读取；媒体字节不设下载速率上限。前方预缓存按视频总大小计算，达到 20% 后暂停，剩余降到 10% 或以下恢复，补到 20% 再暂停；依据可信播放时钟估算已消费字节。未知时钟先按网络原速提供最多 20%，等待有效播放进度后再补充，不设按秒增长预算。连续、并发 Range 和索引读取共用上限，重叠范围不重复计数；拖动后按新位置核算，未使用的预留额度在断连时释放。no-store 与无视频磁盘缓存策略不变。
- 安装默认 `<PotPlayer>/Plugin/QuarkPotPlayer`；独立 `Uninstall.exe` 不含组件压缩包。根管理窗口增加“安装管理”，日常管理不依赖下载的安装包。


## 按剧集跳过片头片尾

以下接口要求 Bearer token 和当前账号 `X-Quark-Account` 归属校验。规则单独存于当前账号的 `series-settings.json`（version 1，series 字典以真实剧集 FID 为键），不属于诊断或精彩标记数据。

- `POST /v1/series-settings/open`，JSON `{"parent":"目录FID"}`：按已有祖先解析规则定位剧集，必要时只取缺失祖先。返回 202 `{"status":"opened"}`；同账号同剧集窗口已打开时返回 `already_open` 并置前。根目录和无法识别的剧集不支持设置。
- `GET /v1/series-settings?series=剧集或版本目录FID`：本地读取，返回 `{"series":"真实剧集FID","name":"蒸蒸日上","intro_seconds":90,"outro_seconds":120}`，未设置时两项为 0。
- `POST /v1/series-settings`，JSON `{"series":"剧集FID","intro_seconds":90,"outro_seconds":120}`：两项须为 0–3600 整数秒，0 关闭；返回 200 和实际剧集 FID、规则。损坏的配置拒绝覆盖，旧账号窗口请求返回 409。
- `--series-settings-window --series-fid <FID> --account-id <账号标识>`：独立设置窗口，仅连接已运行本地服务；复用现有 Tk 工作线程和窗口图标/置前逻辑。

监控只在原生播放器 PID、完整文件身份、实际时间和时长匹配后自动 seek。有效续播、显式精彩标记优先；同文件多实例冲突、暂停、无时长或片头与片尾覆盖整片时不自动跳过。片尾 seek 到结束前 1000ms，后续结束行为由 PotPlayer 控制，不更改全局播放器设置。
