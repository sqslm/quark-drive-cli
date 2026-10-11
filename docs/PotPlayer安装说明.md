# 夸克网盘 PotPlayer 插件安装说明

适用于 Windows 10/11 x64 + PotPlayer 64 位。安装程序完成全部部署：不需要预装 Python、不需要执行命令、不需要手工复制 Cookie。

## 交付物

| 文件 | 说明 |
| --- | --- |
| `QuarkPotPlayer-Setup.exe` | 安装/升级/卸载程序（Tkinter，单文件） |
| `SHA256SUMS.txt` | 安装程序 SHA-256 |
| `build-info.json` | 构建时间、组件版本、随包扩展脚本清单 |

当前版本为 **0.7.0**；发行包由 `packaging/potplayer/build.py` 生成到 `dist/potplayer/0.7.0/`（含 `QuarkPotPlayer-Setup.exe`、`SHA256SUMS.txt`、`build-info.json`）。

构建方式见文末“开发者构建”。

## 安装前

1. 关闭 PotPlayer。运行中的播放器会占用扩展脚本文件，导致覆盖失败。
2. 确认 PotPlayer 安装在本地磁盘（含 `PotPlayerMini64.exe` 与 `Extension` 目录）。

## 安装

1. 双击 `QuarkPotPlayer-Setup.exe`。
2. 安装程序自动查找 PotPlayer（注册表 `HKCU/HKLM\Software\DAUM\PotPlayer*` 的 `ProgramPath`、卸载项，以及 `Program Files\DAUM\PotPlayer` 等常见路径）；未找到时点“浏览…”手动选择包含 `PotPlayerMini64.exe` 的目录。
3. 可取消“创建开始菜单快捷方式”“安装完成后打开 PotPlayer”。开始菜单快捷方式直接打开 PotPlayer（插件入口在播放器内，见“首次使用”）。
4. 点“安装 / 升级”，下方日志显示每一步写入的位置。

安装位置（当前用户，无需管理员权限）：

```
%LOCALAPPDATA%\QuarkPotPlayer\
    app\QuarkPotPlayer.exe          本地桥接组件（内嵌 Python 运行时）
    app\licenses\                   随组件打包的 Python/Tcl-Tk 与依赖许可证
    QuarkPotPlayer-Setup.exe        安装程序副本，用于卸载
    install.json                    本次安装创建的全部路径清单
<PotPlayer 目录>\Extension\quark-potplayer.exe    启动器，扩展用 HostExecuteProgram 调用它
<PotPlayer 目录>\Extension\quark-potplayer.path   记录上面的组件路径
<PotPlayer 目录>\Extension\Media\UrlList\MediaUrlList - *.as
<PotPlayer 目录>\Extension\Media\PlayParse\MediaPlayParse - *.as
<PotPlayer 目录>\Extension\Subtitle\Search\SubtitleSearch - *.as
%APPDATA%\Microsoft\Windows\Start Menu\Programs\夸克网盘 (PotPlayer).lnk   指向 PotPlayer 本身的快捷方式
```

安装程序只写入以上路径，不修改 PotPlayer 的播放设置、不触碰其它扩展、不读写 quark CLI 的登录 Cookie。

组件、安装程序副本与 `install.json` 写在 `%LOCALAPPDATA%`（当前用户目录），**不需要管理员权限**。但扩展脚本 `*.as`、启动器 `quark-potplayer.exe` 与 `quark-potplayer.path` 必须写入 PotPlayer 的 `Extension` 目录：当 PotPlayer 装在 `Program Files` 等受保护位置时，安装程序需要对该目录的**写权限**，否则覆盖会失败——请对 `Extension` 目录授予当前用户写权限，或以管理员身份运行安装程序。

组件安装目录只接受**空目录**或**本插件已有的安装目录**（含 `install.json` 或 `app\QuarkPotPlayer.exe`）。选择非空的无关目录时安装程序拒绝写入，并在界面上给出可读的中文提示，避免把文件写进用户的目录。

目录框留空（默认）或点“自动检测”时，按注册表与常见路径自动查找 PotPlayer/安装位置；**手动填入**的目录会被严格校验：无效时立即报错并停止，**不会**回退到自动检测的结果，以免把文件装到你没指定的位置。

## 首次使用

1. 打开 PotPlayer，按 `Ctrl+U` 打开媒体地址列表，选择“夸克网盘”，再进入“我的网盘”目录。
2. 未登录时插件会打开扫码窗口；用手机夸克 App 扫码。
3. 扫码成功后刷新目录即可看到文件；直接打开视频在线播放，支持拖动进度、同目录自然排序选集，并会在下次打开同一视频时接着上次位置继续播放（见[使用指南](PotPlayer使用指南.md)）。媒体地址列表里还有“搜索网盘”“收藏目录”“最近观看”“继续观看”与“播放状态与诊断”入口；视频目录首屏另有“继续观看”和“剧集观看状态”两行，可按集标记已看/未看。是否自动播下一集取决于 PotPlayer 的“播放结束后”设置，插件不会修改用户的全局设置。

组件只做内存转发，不创建视频文件、分片或磁盘缓存；登录凭证保存在 `%APPDATA%\quark-drive-cli\potplayer\`（与 CLI 的 `cookie` 分开）。

## 升级

先**退出 PotPlayer**，再直接运行新版本 `QuarkPotPlayer-Setup.exe`：安装程序会识别既有安装（日志显示“按升级处理”），替换组件与扩展脚本并保留插件凭证，无需重新扫码；“收藏目录”“最近观看”、观看进度与“已看”标记也一并保留。安装程序只替换磁盘上的文件，**不会**自动升级正在运行的播放器；先关闭再安装，重新打开后才生效。

## 卸载

三种方式等价：

* 设置 → 应用 → 已安装的应用 → “夸克网盘 PotPlayer 插件” → 卸载
* 开始菜单或 `%LOCALAPPDATA%\QuarkPotPlayer\QuarkPotPlayer-Setup.exe --uninstall`
* 安装窗口中的“卸载”按钮

卸载默认**保留**插件凭证（下次安装无需重新扫码）。若同时要清除凭证，在安装窗口中勾选“卸载时同时清除插件凭证”，或使用 `--uninstall --silent --clear-credentials`。清除范围仅限 `%APPDATA%\quark-drive-cli\potplayer\`，`quark` CLI 的登录状态（`%APPDATA%\quark-drive-cli\cookie`）不受影响。

卸载删除本插件创建的文件：组件树 `app\`、安装程序副本 `QuarkPotPlayer-Setup.exe`、`install.json`，以及清单记录的扩展脚本、启动器与开始菜单快捷方式；安装目录中此后加入的其它文件会保留，目录清空后才删除。清单缺失、位置与记录不一致时安装程序拒绝删除并保留文件；扩展脚本位于 PotPlayer 目录之外时同样保留。

## 静默安装

```
QuarkPotPlayer-Setup.exe --silent --potplayer-dir "D:\PotPlayer"
QuarkPotPlayer-Setup.exe --uninstall --silent [--clear-credentials]
```

其他参数：`--install-dir <目录>`（显式指定时严格校验，无效目录直接失败、不回退到自动检测；省略则自动查找）、`--no-shortcut`、`--no-registry`、`--no-open`。静默模式把日志写入 `%TEMP%\QuarkPotPlayer-Setup.log`，退出码 `0` 成功、`4` 失败。

## 故障排查

| 现象 | 处理 |
| --- | --- |
| 提示未找到 PotPlayer | 手动选择含 `PotPlayerMini64.exe` 的目录（应有 `Extension` 子目录） |
| 目录框提示无效 / 自动检测报错 | 手动填入的目录会被严格校验，无效时直接报错且不会回退；清空该栏或点“自动检测”改用自动查找 |
| 扩展脚本覆盖失败 | 关闭 PotPlayer 后重试安装 |
| 目录/播放异常 | 运行 `%LOCALAPPDATA%\QuarkPotPlayer\app\QuarkPotPlayer.exe --stop` 结束后台服务，再重新打开播放器 |
| 需要重新登录 | 在插件入口重新扫码；如需彻底重置，卸载时勾选清除插件凭证后重装 |
| 卸载后有残留 | 文件被占用时先退出 PotPlayer，再删除 `%LOCALAPPDATA%\QuarkPotPlayer` |

## 首版不包含

分享转存、影视海报库、转码、多账号；不实现视频落盘缓存。内嵌字幕/音轨保留原生解码与切换；同目录外挂字幕在原生“字幕搜索”里手动选择，插件自身不写字幕或视频文件。

## 开发者构建

```
python packaging/potplayer/build.py                        # 读取 potplayer/*.as 生成发行包（dist/potplayer/）
python packaging/potplayer/build.py --as-dir DIR --out-dir DIR   # 指定扩展脚本目录与输出目录
python packaging/potplayer/build.py --component-only       # 只构建并打包组件
python packaging/potplayer/build.py --skip-component       # 复用既有组件产物
```

脚本先用 PyInstaller 构建窗口化 onedir 组件 `QuarkPotPlayer.exe` 并压缩为 `component.zip`，再把 `component.zip`、`potplayer/` 下的 AngelScript 文件与 `build-info.json` 嵌入 PyInstaller onefile 窗口化安装程序。需要带有 PyInstaller 6 的解释器（`--python` 指定，默认当前解释器）。构建时从该解释器环境读取依赖包的 `dist-info` 许可证元数据与 Python、Tcl/Tk 许可证文本，写入组件的 `licenses\` 目录随包分发（只读本地文件，不联网）。
