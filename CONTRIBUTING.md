# Contributing

本项目的 Python CLI、桌面客户端和 PotPlayer 插件共享 `src/quark_cli` 核心。提交修改时请限定范围，避免改变无关产品的行为。

## 开发与验证

要求 Python 3.10+。在虚拟环境中从仓库根目录运行：

```sh
python -m pip install -e ".[test,dev]"
python -m unittest discover -s tests -v
quark --version
```

测试使用模拟 HTTP、本地临时文件和回环服务，不需要真实账号。Windows 专属测试在其他系统上会跳过；需要窗口的测试在无显示环境中会跳过。修改共享层时运行完整 Python 测试；只改客户端前端时按 `desktop/package.json` 运行类型检查和相关测试。

新增测试应放入 `tests/`。不要向仓库根目录复制 Python 模块或测试文件，以免遮蔽当前实现。凭证、分享提取码、真实请求日志和用户状态不能进入测试夹具。

## 发行验证

CLI 版本由 `pyproject.toml` 和 `src/quark_cli/__init__.py` 声明，两处需保持一致。桌面端与插件分别维护版本，不要求同步编号。

保持一个仓库，分别使用 `cli-vX.Y.Z`、`potplayer-vX.Y.Z`、`desktop-vX.Y.Z` 标签与 Release。源码进入 Git，二进制上传 Release assets；安装包不进入 Git 历史。每个 Release 说明产品、平台及构建来源，附 SHA-256 清单。本地交付目录为 `dist/<产品>/<版本>/`；清理历史产物时先校验并保留各产品最新交付。

```sh
python -m build
python -m twine check dist/*.whl dist/*.tar.gz
python -m pip_audit --strict --skip-editable
python -m pip install --force-reinstall dist/quark_drive_cli-VERSION-py3-none-any.whl
quark --version
quark --help
python -m pip check
```

用实际版本替换 `VERSION`。在新虚拟环境中验证安装，从仓库以外的目录运行命令，确认无需源码或 `PYTHONPATH`。检查源码包与 wheel 不含 Cookie、用户状态、缓存、构建暂存或历史发布包；保留 LICENSE 和第三方声明。为选定交付文件生成 SHA-256 清单，发布说明应标记版本和对应提交。构建和 CI 不自动发布到 PyPI 或 GitHub Releases。

## 提交与报告

在 PR 中说明问题、最终行为及实际验证结果。协议变更请提供脱敏的请求结构和离线回归；真实账号验证与离线测试分开报告。不要修改测试来掩盖错误，不要自动重发结果未知的写操作。安全问题按 [SECURITY.md](SECURITY.md) 私下报告。

项目代码使用 [MIT](LICENSE) 许可；贡献者需有权提交所贡献的代码与素材。第三方代码和资源需保留各自的许可证与来源说明。
