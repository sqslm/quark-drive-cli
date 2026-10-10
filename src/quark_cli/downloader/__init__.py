"""The standalone Tkinter downloader window (``python -m quark_cli.downloader``).

The window is a thin front-end over the existing library: :class:`~quark_cli.auth.QRLogin`
for scanning, :class:`~quark_cli.files.Files` for browsing, and
:class:`~quark_cli.transfer.Transfer` / :class:`~quark_cli.recursive.RecursiveTransfer`
for one file or one directory. It adds no new transport, no new credential
handling and no new download path: the request guard, the download protection
(atomic publish, existing-file refusal) and the credential rules are the ones the
CLI already enforces.

There is no separate credential here. The QR panel is the shared
:class:`~quark_cli.potplayer.login_gui.QrLoginWindow` and it stores the CLI's own
Cookie (``auth.credential_path()``), so a login from this window is a login for
``quark``.

Layout:

``core``
    headless rules: listing rows, navigation, target path, progress and error text
``worker``
    one background thread that browses and downloads
``app``
    the Tk window itself
"""

from __future__ import annotations

__all__ = ["app", "core", "worker"]
