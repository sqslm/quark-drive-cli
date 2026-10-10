"""PyInstaller entry point for the standalone downloader EXE.

PyInstaller runs this as ``__main__``; the real command line lives in
:func:`quark_cli.downloader.__main__.main` so the frozen program and
``python -m quark_cli.downloader`` behave identically.
"""

from __future__ import annotations

import sys

from quark_cli.downloader.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
