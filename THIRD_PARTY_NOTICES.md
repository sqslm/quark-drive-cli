# Third-party notices

## Quark share API references

The request shapes for share tokens, paginated share details, save submission, and task polling were consulted in [Pearsoon/quark](https://github.com/Pearsoon/quark/blob/master/quark.py) and [QuarkPanTool](https://github.com/ihmily/QuarkPanTool/blob/main/quark.py). No source code from these repositories is included in this project.

## QuarkPan reference

The project [QuarkPan](https://github.com/lich0821/QuarkPan) is licensed under the MIT License and was consulted as a reference for Quark Drive web API request shapes and the upload sequence. For the 2026-10-01 extensions, its [file service](https://github.com/lich0821/QuarkPan/blob/master/quark_client/services/file_service.py), [QR helper](https://github.com/lich0821/QuarkPan/blob/master/quark_client/utils/qr_code.py), and web-login flow were reviewed. This repository does not copy QuarkPan source code. The terminal renderer uses the existing qrcode dependency with a standard four-module border.

The [public API reference](https://github.com/zhaocongqi/clouddrive-auto-save/blob/main/docs/quark_apis.md) was cross-checked for endpoint shapes. Search and tree are implemented by walking directory listings; they do not reuse a third-party search or tree implementation. The 2026-10-01 plan also references quark-auto-save (AGPL-3.0) for later task/filter workflows; no AGPL source has been copied.

MIT License

Copyright (c) 2025 QuarkPan

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Backup workflow references

The MIT-licensed [rclone](https://github.com/rclone/rclone) documentation and copy/backup implementation were reviewed for non-deleting directory copies, changed-file conflicts, and retention of old versions. No rclone source is copied and no rclone runtime is required. Quark operations use this project's existing HTTP client, transfer implementation, FID checks, and private state. The detailed references and adaptation boundaries are recorded in [extension API research](docs/research/2026-10-01-extension-apis.md).
