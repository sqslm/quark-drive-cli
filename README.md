# Quark Drive Tools

一个仓库，三个入口：AI 自动化、PotPlayer 播放、桌面文件管理。三者共享 Python 核心，分别维护版本和发行包。

这是非官方项目，与夸克网盘、PotPlayer 官方无关联。夸克网页版接口可能变化；请先预览写操作，妥善保管凭证。

## 选择适合你的产品

| 产品 | 使用场景 | 平台 | 当前版本与下载 | 使用文档 |
| --- | --- | --- | --- | --- |
| **CLI** | AI agent 管理网盘、转存追更、脚本与定时任务 | Windows / Linux，Python 3.10+ | [CLI 0.2.0](https://github.com/sqslm/quark-drive-cli/releases/tag/cli-v0.2.0) | [完整手册](docs/CLI使用手册.md) · [Agent 指南](docs/AGENT_GUIDE.md) |
| **PotPlayer 插件** | 在 PotPlayer 浏览、播放网盘视频，续播、跳过片头片尾、管理追剧状态 | Windows 10/11 x64，PotPlayer 64 位 | [插件 0.12.10](https://github.com/sqslm/quark-drive-cli/releases/tag/potplayer-v0.12.10) | [安装说明](docs/PotPlayer安装说明.md) · [使用指南](docs/PotPlayer使用指南.md) |
| **桌面客户端** | 图形化浏览、搜索、上传下载、分享与文件管理 | Windows 10/11 x64 | [客户端 0.5.29](https://github.com/sqslm/quark-drive-cli/releases/tag/desktop-v0.5.29) | [客户端指南](docs/客户端使用指南.md) |

安装对应产品即可。插件和桌面客户端自带运行环境，无需另装 CLI 或 Python。插件使用独立账号配置；桌面端沿用 CLI 核心的登录配置，详细边界见各产品文档。

## CLI 快速开始

从 CLI Release 下载 wheel 与校验清单，在 Python 虚拟环境安装：

~~~sh
python -m pip install ./quark_drive_cli-0.2.0-py3-none-any.whl
quark --version
quark auth login
quark auth status --json
quark ls / --json
~~~

AI agent 转存追更示例，替换分享链接与路径；目标中间目录需预先存在：

~~~sh
quark job add series 'https://pan.quark.cn/s/EXAMPLE' --source '/剧集/4K' --include '*.mkv' --to '/影视/追更/剧集' --scope '/影视/追更' --json
quark job run series --dry-run --json
quark job run series --json
~~~

CLI 每次运行一次即退出，不常驻后台。`--json` 供脚本解析；普通命令错误在 stderr，`job run` 的逐任务结果在 stdout，失败时也需要读取。`saved_entries` 列出已确认新增，`retry_after_seconds` 提供冷却时间，`next_action` 指示等待、认证或人工核验。

任务按精确 FID、身份及父关系去重。不要删除账本来重试未知写入，也不要仅凭同名文件认定成功。完整命令、范围限制和退出码见 [CLI 手册](docs/CLI使用手册.md)。

## 源码与本地交付

~~~text
src/quark_cli/            共享核心与 CLI
  potplayer/             PotPlayer 本地服务及窗口
  desktop/               桌面客户端 Python 后端
  downloader/            共享下载模型与早期 Tk 窗口
potplayer/               AngelScript 扩展、图标与资源许可
desktop/                Electron / React 客户端、测试和安装器
packaging/               Windows 构建脚本
tests/                  Python 离线回归
docs/                   各产品现行文档
video/                  原创项目介绍视频及可编辑源码
dist/cli/0.2.0/          当前 CLI wheel、源码包和校验信息
dist/potplayer/0.12.10/  当前插件安装包和校验信息
dist/desktop/0.5.29/     当前客户端安装包和校验信息
~~~

`dist/` 是本地交付目录，不进入 Git。安装包通过 GitHub Releases 分发，源码只推送一个仓库。下载后核对各 Release 的 `SHA256SUMS.txt`。本地默认只保留三个产品各自最新交付；构建目录、依赖目录和缓存可由源码重新生成。

## 开发与构建

~~~sh
python -m pip install -e ".[test,dev]"
python -m unittest discover -s tests -v
python -m build --outdir dist/cli/0.2.0
~~~

桌面端：先在 `desktop/` 运行 `npm ci`，再运行 `npm run typecheck`。Windows 构建使用 `python packaging/desktop/build.py --backend-only` 生成后端，在 `desktop/` 运行 `npm run package` 与 `npm run make:installer`；安装器编译需要 Inno Setup 7。完整步骤见 [客户端指南](docs/客户端使用指南.md)。

插件使用 `python packaging/potplayer/build.py` 构建；需要 Windows、PyInstaller 和相应运行依赖，见 [插件构建说明](docs/PotPlayer安装说明.md#开发者构建)。

版本标签分别使用 `cli-vX.Y.Z`、`potplayer-vX.Y.Z`、`desktop-vX.Y.Z`。独立 Release 说明适用场景、平台、构建来源和校验值。GitHub 自动生成的 Source code 是该标签的仓库源码，不是独立安装包。

贡献、测试和发行要求见 [CONTRIBUTING.md](CONTRIBUTING.md)。CI 覆盖 Windows/Linux Python 测试、wheel 安装及发行检查；CI 结果与真实账号验证分别报告。

## System scheduling examples

The CLI runs once and exits. Scheduling belongs to the operating system; these examples do not deploy a timer. Configure authentication privately for the same OS user, then use an installed virtual environment's absolute executable. Protect saved extraction codes and logs and use a consistent `QUARK_CONFIG_DIR` if overriding the default.

Cron example (`crontab -e`), with paths adjusted for your environment:

```cron
0 * * * * /home/user/.venvs/quark/bin/quark job run notes --json >> /home/user/.local/state/quark-jobs.log 2>&1
```

A user systemd service `~/.config/systemd/user/quark-jobs.service`:

```ini
[Unit]
Description=Run Quark incremental share jobs
[Service]
Type=oneshot
ExecStart=/home/user/.venvs/quark/bin/quark job run --json
```

Its matching `quark-jobs.timer`:

```ini
[Unit]
Description=Schedule Quark incremental share jobs
[Timer]
OnCalendar=hourly
Persistent=true
[Install]
WantedBy=timers.target
```

Windows Task Scheduler: create a task for your current user, set an hourly trigger, choose “Do not start a new instance”, use `C:\Users\YOUR_USER\.venvs\quark\Scripts\quark.exe` as Program, and `job run --json` as Arguments. Keep credentials in the same user's protected config directory. Do not put a Cookie or extraction code in scheduler arguments.

## Implementation notes

网盘操作使用固定 Quark API 主机。认证请求与传输请求隔离；上传不携带账号 Cookie，下载仅向核验后的 Quark 存储域按需发送受限凭证。写入前核验范围，持久化未知意图，并按精确 FID 读回结果；不自动重发无法确认的写操作。

接口参考与资源来源见 [第三方声明](THIRD_PARTY_NOTICES.md)，插件本地协议见 [接口文档](docs/PotPlayer接口.md)。项目采用 [MIT](LICENSE)，第三方组件与资源保留各自许可。安全问题按 [SECURITY.md](SECURITY.md) 私下报告。

[CLI 更新记录](CHANGELOG.md) · [33 秒项目介绍](video/quark-drive-intro.mp4) · [视频源码](video/README.md)
