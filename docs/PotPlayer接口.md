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
{"authenticated":false,"login_state":"idle","login_message":null,"qr_png":null}
```

- `authenticated`：插件凭证文件是否存在（本地判断，不联网校验）。
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

清除插件凭证（磁盘上的插件凭证文件与进程内注入的会话一并失效），返回 `{"authenticated": false}`。只删除插件自己的凭证，绝不读取或删除 `quark` CLI 的 Cookie。

### 请求体处理

`auth/login`、`auth/logout`、`shutdown` 不接受请求体；若客户端仍带 `Content-Length`，服务会读完这段有界请求体后再响应，避免污染同一 keep-alive 连接上的下一次请求（无请求体的 `POST` 亦正常）。请求体上限 `64 KiB`，超出则关闭连接。

## 目录与播放列表

### `GET /v1/files?parent=<fid>&page=<n>&size=<n>`

- `parent`：目录 FID，缺省或空串等价于根目录 `0`。
- `page`：从 `1` 开始，上限 `1000000`。
- `size`：每页条数，`1..100`，缺省 `50`。

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

请求体 `{"fid":"<视频 fid>"}`，返回：

```json
{"url":"/v1/stream/<session>/<URL 编码文件名>","title":"第2集.mp4"}
```

`url` 是本地相对地址，只含不可猜测的会话标识；**不含** Cookie、账号信息或夸克签名地址。目录 FID → `502`；`fid` 缺失/空 → `400`。

## 搜索、收藏与最近观看

扩展开关在根页面选择类别：`drive`（我的网盘）、`search`（`type=search`，用 Query 输入关键词）、`favorites`、`recent`。搜索只在根页面消费 `Query`；进入结果目录后按 `PathToken` 里的 FID 正常浏览，不再使用 `Query`。

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
{"items":[{"fid":"...","name":"剧集","is_dir":true,"size":0}]}
```

### `POST /v1/favorites`

请求体 `{"fid":"<目录 fid>"}`。服务端向上游校验这是**真实目录**并取其名称，按 FID 去重（重复添加幂等，已存在的条目位置不变）。返回与 `GET /v1/favorites` 相同的 `items`。非目录或 `fid` 缺失/空 → `400 bad_request`；远端找不到 FID 或返回无效详情 → `502 remote_error`。

### `POST /v1/favorites/remove`

请求体 `{"fid":"<目录 fid>"}`。幂等删除：FID 不存在也返回成功，**不修改远端**，因此网盘里已删除/移动的目录仍能移除。返回 `items`。

### `GET /v1/recent`

最近**成功播放**过的视频，最近在前，按 FID 去重，最多 `100` 条：

```json
{"items":[{"fid":"...","name":"第2集.mp4","is_dir":false,"size":123,"played_at":1699999999}]}
```

`played_at` 是本机 Unix 秒。条目只有位置信息，用于从该集开始同目录选集，不是秒级续播。

### `POST /v1/recent/clear`

清空本机最近记录，幂等；请求体按通用规则读完有界内容后忽略，返回 `{"items":[]}`（扩展固定发送一个非空 JSON 请求体）。

### 写入时机与数据边界

- 最近记录在**首个非空媒体块成功转发之后**写入，不等整集传输结束：取链、`HEAD`、未转发任何媒体字节的失败 `GET` 不记录。已经开始传输后再关闭、拖动或遇到上游中断，已写入的记录仍保留；每个 HTTP 请求只记一次，不按分段或数据块重复写盘。
- 收藏与最近观看存放在插件私有目录 `config_directory()/potplayer/library.json`（单进程内用一把普通线程锁保护读改写）；只含 FID、名称、大小与本机时间，**不含** Cookie、签名地址或视频数据。
- 显式插件退出登录（`POST /v1/auth/logout`）会一并清空这两份列表；本地清理失败时返回 `500 local_io_error`，凭证仍已失效，不误报清理成功。登录过期只要求重新扫码，不清空。跨进程重启与升级保留。

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
- 冷却期间的列表/数据/登录接口返回 `429 rate_limited` 或 `403 access_restricted` 并带 `Retry-After`；`/v1/health`、`/v1/favorites`、`/v1/recent`、`/v1/auth/logout` 等本地接口仍可用。
- 冷却错误**不会**清除插件凭证、不会打开扫码登录、不会清空收藏/最近记录；`401` 凭证失效的语义不变。
- 若冷却状态无法写入本机，服务会以 `500 local_io_error` 停止后续自动请求；另一个进程或重启无法得知未写入的冷却，需先修复本机存储并用官方夸克客户端确认账号。
- 此节奏不改变 4 MiB 串行分段、64 KiB 转发或内嵌字幕/音轨的原生处理，也不新增字节速率限制。以上数值是本工具的保守默认值，不代表夸克官方安全阈值，不能保证账号不被限制。`Retry-After` 定义见 [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110#field.retry-after)（`429` 源自 RFC 6585 §4）。

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
| `internal_error` | 500 | 未分类内部错误 |

## 凭证与数据边界

- 插件凭证：`config_directory()/potplayer/cookie`，由 `QRLogin` 单独保存；`quark` CLI 的 Cookie 从不读取、写入或删除。
- 运行配置：同目录 `runtime.json`；单实例锁 `service.lock`。
- 收藏与最近观看：同目录 `library.json`，只含 FID、名称、大小与本机时间；显式退出登录清空，过期不清、升级保留。
- 视频不落盘：无分片、无磁盘缓存、无转码；仅内存转发请求的字节范围。
- 内嵌字幕/音轨由播放器原生解码、显示与切换，本插件不替换、不自动选中外挂轨；同目录外挂字幕经原生“字幕搜索”手动选择，只从本桥接读取内存文本——桥接自身不写字幕或视频文件，播放器是否把所选字幕保存到磁盘由 PotPlayer 自身行为决定。
- 不含分享转存、海报库、转码、多账号；不含断点/秒级续播。
