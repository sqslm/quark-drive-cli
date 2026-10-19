"""Installation management page, independent of playback and cloud accounts."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys


class InstallationPanel:
    def __init__(self, client=None, endpoint=None, *, install_dir=None):
        self.directory = Path(install_dir) if install_dir else Path(sys.executable).resolve().parent.parent
        self.root = self.frame = None

    def mount(self, tk, root, frame):
        from tkinter import ttk
        self.root, self.frame = root, frame
        outer = ttk.Frame(frame, padding=16)
        outer.pack(fill='both', expand=True)
        try:
            manifest = json.loads((self.directory / 'install.json').read_text('utf-8'))
        except (OSError, ValueError):
            manifest = {}
        if not isinstance(manifest, dict) or manifest.get('schema') != 1:
            manifest = {}
        self.manifest = manifest
        for text in ('插件版本：' + str(manifest.get('version', '安装信息不可用 / 开发环境')),
                     '组件目录：' + str(self.directory),
                     'PotPlayer：' + str(manifest.get('potplayer_dir', '未记录')),
                     '安装包使用后可删除。日常账号、标记和卸载管理均使用已安装组件。'):
            ttk.Label(outer, text=text, wraplength=730).pack(anchor='w', pady=(0, 12))
        ttk.Button(outer, text='打开插件目录', command=self.open_directory).pack(anchor='w', pady=6)
        self.clear_data = tk.BooleanVar(value=False)
        ttk.Checkbutton(outer, text='卸载时同时清除账号与本地观看数据', variable=self.clear_data).pack(anchor='w', pady=12)
        self.uninstall_button = ttk.Button(outer, text='卸载插件', command=self.uninstall)
        self.uninstall_button.pack(anchor='w')
        if not manifest or not (self.directory / 'Uninstall.exe').is_file():
            self.uninstall_button.state(['disabled'])

    def open_directory(self):
        os.startfile(str(self.directory))

    def uninstall(self):
        from . import dialogs as messagebox
        if not messagebox.askyesno('卸载插件', '将关闭插件管理窗口并启动卸载。请先关闭 PotPlayer。继续？', parent=self.root):
            return
        command = [str(self.directory / 'Uninstall.exe'), '--uninstall', '--install-dir', str(self.directory), '--confirmed']
        if self.clear_data.get():
            command.append('--clear-credentials')
        try:
            subprocess.Popen(command, creationflags=0x08000000)
        except OSError as exc:
            messagebox.showerror('无法启动卸载', str(exc), parent=self.root)
            return
        self.root.quit()

    def _shutdown(self):
        self.root = self.frame = self.uninstall_button = self.clear_data = None
