# quark-drive-cli

Watch the [33-second project introduction video](video/quark-drive-intro.mp4) or edit its [Remotion source](video/README.md).

`quark` is a Python 3.10+ command-line client for managing files in Quark Drive on Windows and Linux. It supports QR login and manual Cookie credentials; it does not read browser storage.

The Quark web API is undocumented and may change. The commands below are implemented against the request shapes summarized in [Implementation notes](#implementation-notes). Offline tests cover the HTTP flows.

See the [中文 CLI 使用手册](docs/CLI使用手册.md) for installation, every command and option, examples, scope behavior, exit codes and troubleshooting.

On Windows 10/11 x64 you can also browse and play your own Quark Drive inside PotPlayer with the companion plugin (`QuarkPotPlayer-Setup.exe`). See the [使用指南](docs/PotPlayer使用指南.md) and the [安装说明](docs/PotPlayer安装说明.md). The plugin keeps its own login and never touches the CLI Cookie.

## Install

The build produces a platform-independent wheel, a source archive and `SHA256SUMS.txt` in `dist/`. Copy these files to the Windows or Linux host. Compare the artifact SHA-256 with the checksum file before installing:

```powershell
Get-FileHash .\quark_drive_cli-0.1.0-py3-none-any.whl -Algorithm SHA256
Get-Content .\SHA256SUMS.txt
```

```sh
sha256sum -c SHA256SUMS.txt
```

In an activated Python 3.10+ virtual environment, install the wheel and check the installed dependencies and command entry point:

```sh
python -m pip install ./quark_drive_cli-0.1.0-py3-none-any.whl
python -m pip check
quark --help
```

To replace an existing `0.1.0` installation, use `python -m pip install --upgrade --force-reinstall ./quark_drive_cli-0.1.0-py3-none-any.whl` so pip installs this build even when the version number matches.

The wheel works on both Windows and Linux. Installing it does not require copying the source tree or setting `PYTHONPATH`. For a source build, use `python -m pip install ./quark_drive_cli-0.1.0.tar.gz`; the archive includes the documentation, third-party notices and offline tests. Runtime dependencies are installed by pip.

Create and activate a virtual environment, then install from this directory:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install .
quark --help
```

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
quark --help
```

You can also run the source tree without installing it:

```sh
PYTHONPATH=src python -m quark_cli --help
```

## Login and Cookie setup

Run `quark auth login` to create a Quark web-login QR code (`ssb=weblogin`) and scan it with the Quark mobile app. The CLI does not launch or depend on the standalone Quark PC client. After confirmation, it verifies the resulting Cookie through the Quark HTTP API (`drive-pc.quark.cn` is the API host) and saves it in the current user's config directory. Login takes up to 300 seconds by default; `--timeout` accepts 1–300 seconds.

```sh
quark auth login
quark auth login --qr-terminal
quark auth status
```

The CLI prints the temporary QR PNG path to stderr while it waits. By default the image is created in the system temporary directory. You can choose a path with `--qr-file PATH`; the CLI refuses to overwrite an existing file and deletes the QR image after login succeeds, fails, or is cancelled. The image contains a short-lived login token, so do not share it. If needed to complete first-time Cookie fields, login reads `pan.quark.cn/list`, the drive-pc root listing, and the drive member endpoint; it does not modify files. Login fails without saving when the required Cookie fields are still missing or the final read-only root-list check fails.

`--qr-terminal` also renders the QR matrix to stderr, using black modules on a white background and a four-module quiet zone. Use a UTF-8 terminal with ANSI color support, including SSH sessions. The terminal matrix is opt-in, including in noninteractive runs; JSON stdout contains only the login result. `--qr-file` can be used together with it. The matrix contains the same short-lived web-login token as the PNG, so avoid recording or sharing terminal output during login. Windows sessions with a legacy output encoding can set `$env:PYTHONUTF8 = '1'` before running Python.

The saved Cookie is stored in `%APPDATA%/quark-drive-cli/cookie` on Windows and `${XDG_CONFIG_HOME:-~/.config}/quark-drive-cli/cookie` on Linux. `QUARK_CONFIG_DIR` can select a different config directory and must be a nonempty absolute local path. It changes the location of the saved Cookie and job state together; unset it to use the existing default credential. Explicit `QUARK_COOKIE` and `QUARK_COOKIE_FILE` still take precedence. The CLI does not copy or replace credentials when the directory changes.

The config directory is restricted to the current user. Linux requires ownership by the current user and applies 0700 to directories and 0600 to private files. Windows replaces the entire protected DACL, including existing explicit grants, with one allow rule for the current user SID. Symbolic links and reparse points in private state paths are rejected. If these protections cannot be applied, the operation fails. The Cookie file contains only the Cookie, not the QR token, service ticket, or account information.

Credentials are selected in this order: `QUARK_COOKIE`, `QUARK_COOKIE_FILE`, then the Cookie saved by `auth login`. `quark auth logout` removes only the saved Cookie; it does not modify environment variables or revoke a server-side session.

```sh
quark auth logout
```

You can also provide a Cookie from your own logged-in Quark web session. Treat it like a password and do not paste it into a command argument, issue, screenshot, or shared log. API requests use the Cookie with Quark hosts. Upload and download transfers use a separate client: uploads have no Cookie, while downloads send only the `__puus` Cookie to validated HTTPS `*.pds.quark.cn` or `*.drive.quark.cn` storage subdomains when required. Other hosts never receive a Cookie.

In PowerShell, read the Cookie without echoing it, use it for the session, then remove it from the environment:

```powershell
$secureCookie = Read-Host 'Quark web Cookie' -AsSecureString
$cookiePtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureCookie)
try { $env:QUARK_COOKIE = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($cookiePtr) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($cookiePtr) }
quark auth status
Remove-Item Env:QUARK_COOKIE
```

On Linux, `read -rs` avoids terminal echo:

```sh
read -rs -p 'Quark web Cookie: ' QUARK_COOKIE; echo
export QUARK_COOKIE
quark auth status
unset QUARK_COOKIE
```

Alternatively set `QUARK_COOKIE_FILE` to a local text file containing the Cookie on one line. Restrict that file to your account (for example, `chmod 600 cookie.txt` on Linux). `QUARK_COOKIE` takes precedence when both are set. Environment-provided credentials are not changed by `auth logout`.

## Commands

Remote references (`REF`) accept an existing FID or an absolute Quark Drive path. `/` and FID `0` mean the root directory. Names, including Chinese and spaces, are matched exactly across all directory pages. Relative paths, `.`, `..`, backslashes, repeated separators, trailing separators, control characters, ambiguous same-name entries, and file components used as intermediate directories are rejected. `auth status` checks the Cookie with a read-only request for one root-directory list entry.

```text
quark auth status
quark auth login [--qr-terminal] [--qr-file PATH] [--timeout 300]
quark auth logout
quark ls [REF] [--all] [--page N] [--size N]
quark info REF
quark tree [REF] [--depth N]
quark search QUERY [--in REF]
quark du REF
quark duplicates REF
quark rename-batch REF --pattern REGEX --replacement TEXT [--apply] [--dry-run]
quark backup LOCAL_DIR --to REF [--apply] [--update] [--dry-run]
             [--progress] [--limit-rate BYTES_PER_SECOND]
quark share inspect URL [--passcode CODE] [--path /分享内路径]
quark share create REF [--passcode CODE]
                   [--expire-days {0,1,7,30}] [--dry-run]
quark share list [--all]
quark share revoke SHARE_ID [--yes] [--dry-run]
quark save URL --to /绝对路径 [--passcode CODE] [--source /分享内路径]
           [--include GLOB ...] [--exclude GLOB ...] [--dry-run]
quark save-batch FILE [--to /绝对路径] [--source /分享内路径]
                 [--include GLOB ...] [--exclude GLOB ...] [--report PATH] [--dry-run]
quark job add NAME URL --to /绝对路径 [--passcode CODE] [--source /分享内路径]
              [--include GLOB ...] [--exclude GLOB ...]
quark job list
quark job run [NAME] [--dry-run]
quark job remove NAME
quark mkdir NAME [--parent REF] [--dry-run]
quark mkdir /父目录/新目录 [--dry-run]
quark upload LOCAL [--parent REF] [--recursive] [--resume]
             [--progress] [--limit-rate BYTES_PER_SECOND] [--dry-run]
quark download REF --output PATH [--recursive] [--resume] [--overwrite]
               [--progress] [--limit-rate BYTES_PER_SECOND] [--connections N]
quark rename REF NAME [--dry-run]
quark mv REF --to REF [--dry-run]
quark rm REF [--yes] [--dry-run]
```

`info` uses the exact-FID `GET /file/info?fid=...&_fetch_full_path=0` endpoint and rejects a response whose `data.fid` does not match the requested FID. This avoids treating an unrelated root listing as a successful lookup.

`mkdir /父目录/新目录` creates one missing final directory under an existing parent. It rejects an existing destination and cannot be combined with `--parent`. Missing intermediate directories are never created.

`tree` recursively lists the selected directory and its children, with absolute path, FID, type, and depth; depth 0 lists only the selected directory, and the default depth is 3. `search` finds case-insensitive filename substrings anywhere under `--in` (root by default), excluding the selected root itself. Both use the account's paginated directory listings, without a third-party index. Invalid pagination, duplicate/cyclic FIDs, or paths that cannot be represented safely stop browsing; traversal is bounded to 1024 levels and 100,000 items.

Quark's directory index can lag briefly after an upload or move. If a complete listing disagrees with its declared total or totals change between pages, the CLI discards that attempt and rereads the requested pages from the start, up to three attempts with 250 ms and 750 ms waits. It returns only a complete, consistent attempt. Persistent inconsistency, duplicate pages/FIDs, invalid response shapes, network errors, and authorization failures stop the command; writes are never resubmitted by this recovery.

`share inspect` accepts a direct `https://pan.quark.cn/s/<share-id>` link and lists all pages of the directory selected by `--path` (default `/`). An optional `pwd` query parameter supplies the extraction code; `--passcode` takes precedence. Share paths start at the share's virtual root, which may contain the shared directory itself. Inspect `/` first to find that directory's name. Each path component must match one directory exactly; ambiguous components, mismatched nested parent FIDs, and cyclic FIDs stop the command. Root entries retain their original account `pdir_fid`, which can differ from the virtual root FID `0`. JSON includes `share_id`, `source_path`, `source_fid`, and safe file metadata in `items`; credentials and signed URLs are omitted. Duplicate names can be displayed by inspection.

`save` selects direct children of `--source` (default `/`) after reading all source pages. Without filters it selects all of them. Repeat `--include GLOB` to include the union of matching names, and `--exclude GLOB` to remove matches; exclusions take precedence. Matching is case-sensitive on Windows and Linux. Quote globs to prevent shell expansion. Patterns match direct names and cannot be empty or contain directory separators or control characters. A selected directory is transferred as a whole. Selected names must be valid and unique; invalid or duplicate names stop before any target creation or save submission.

`--to` must be an absolute Quark Drive path. Every existing path segment must identify one directory; a missing final segment is created under its verified parent. Missing intermediate folders and existing same-name target contents stop the operation. An empty selection returns `saved: false`, `skipped: true`, and `reason: "no_selected_items"` with exit code 0; no target is read or created. Its destination syntax is still checked. The CLI submits each nonempty save once, with the resolved source FID and that page's item tokens. Success requires task status `2` and matching destination names, types, sizes when available, and membership in the task's result FIDs when provided. An explicitly empty result FID list for a nonempty selection is contradictory and fails confirmation. Missing results are read again up to six times at 500 ms intervals while the directory index catches up. Conflicting or malformed results stop immediately; uncertain task states and readback timeouts are never resubmitted automatically.

Quark forbids saving files from a share owned by the same account. Such a share can be inspected, but positive save verification requires an external share.

Examples:

```sh
quark --json ls 0 --all --size 100
quark ls '/工作资料/发票' --all
quark tree '/工作资料' --depth 2
quark search invoice --in '/工作资料'
quark mkdir invoices --parent 123456
quark share inspect 'https://pan.quark.cn/s/example' --path '/共享目录/中文 子目录'
quark save 'https://pan.quark.cn/s/example' --source '/共享目录/中文 子目录' --include '*.txt' --exclude 'skip*' --to '/工作资料/转存'
quark upload ./invoice.pdf --parent 123456
quark download 987654 --output ./invoice.pdf
quark rename 987654 invoice-2026.pdf
quark mv 987654 --to 123456
quark rm 987654
```

Without `--all`, `ls` prints one page (page 1 by default, 50 items). The page size must be 1–100. Downloads stream into a temporary file in the destination directory, verify the received length when the server provides it, then install the completed file. Existing files are preserved unless `--overwrite` is passed. Delete asks for confirmation in a terminal; scripts must pass `--yes`. `rm` moves an item to Quark's recycle bin; it does not request permanent deletion.

## Scope and previews

`--scope REF` restricts file reads and writes to an existing remote directory and its descendants. It can appear before or after the command. The explicit argument takes precedence over `QUARK_SCOPE`; an empty or invalid scope is an error. Authentication commands do not use this boundary. Scope checks follow each object's exact-FID metadata to the root and independently confirm every parent relationship in the parent's directory listing. A path prefix or a claimed `pdir_fid` alone does not establish membership. Missing parents, contradictory records, cycles, and unreadable or unverifiable ancestry stop the operation before a write.

Account sources and destination directories must be within the scope. External share contents belong to the share's separate directory view: `share inspect` and the source of `save` are read through that view, while `save` checks its account destination and any implicit mkdir against the scope. New contents may be written inside the scope directory, but the scope directory itself cannot be renamed, moved, or deleted. Writing directly to the remote root is refused whenever scope is enabled, including `--scope /`. Moves to the source directory or its descendants are refused. When scope is set, commands whose default reference is root (`ls`, `tree`, `search`, `mkdir`, `upload`) require an explicit reference or parent inside that scope.

`mkdir`, `upload`, `rename`, `mv`, `rm`, and `save` accept `--dry-run`. A preview performs local checks and remote reads, then prints `dry_run: true`, the planned action, and its objects; it never submits a remote mutation. `save` may POST to the read-only share-token endpoint before reading share pages, but does not create a directory or submit a save task. Its preview includes a missing final directory as a planned creation, and stops on an existing target name conflict. `rm --dry-run` does not require `--yes`. A preview is a snapshot of the current remote state; execution repeats validation.

```sh
quark --scope '/工作资料' mkdir '/工作资料/新目录' --dry-run
quark rename '/工作资料/报告.pdf' '报告-2026.pdf' --scope '/工作资料' --dry-run
quark --scope '/工作资料' rm '/工作资料/旧报告.pdf' --dry-run --json
quark --scope '/工作资料' save 'https://pan.quark.cn/s/example' --to '/工作资料/转存' --dry-run
```

Use `--json` before the command or after its options to emit machine-readable results. Data is written to stdout; errors are written to stderr. JSON output uses command-specific safe fields and never forwards raw API result objects, signed URLs, authorization values, or nested remote fields. Exit codes are:

| Code | Meaning |
| ---: | --- |
| 0 | Success or deletion cancelled |
| 2 | Invalid arguments or unsafe invocation |
| 3 | Missing or expired authentication |
| 4 | Network, API, or remote service failure |
| 5 | Local file or I/O failure |

The default root `save` success JSON retains `saved`, `task_id`, `destination_fid`, `destination_path`, `item_count`, `created_directory`, and `status`. Supplying a source path or filters adds `source_path` and `source_fid`; previews also include the safe selected `items`. Empty selections use the explicit skip result described above.

## Own shares

`share create REF` shares one existing account file or directory. It accepts a FID or absolute path, including the scope anchor itself, and rejects the account root. A directory is shared as one root with its complete subtree. The share title is the source object's name; custom titles are not supported. Omit `--passcode` for a public link, or supply exactly four ASCII letters/digits for a private link. `--expire-days` accepts 0 (permanent), 1, 7, or 30, with 7 as the default. Account membership and service restrictions can still reject a supported option.

```sh
quark --scope '/CLI-CRUD-TEST' share create '/CLI-CRUD-TEST/公开测试.txt' --expire-days 1 --dry-run
quark --scope '/CLI-CRUD-TEST' share create '/CLI-CRUD-TEST/公开测试.txt' --passcode Ab12 --expire-days 1 --json
quark --scope '/CLI-CRUD-TEST' share list --all --json
quark --scope '/CLI-CRUD-TEST' share revoke INTERNAL_SHARE_ID --dry-run --json
quark --scope '/CLI-CRUD-TEST' share revoke INTERNAL_SHARE_ID --yes --json
```

The internal `share_id` identifies a share in your account catalog. The public `pwd_id` appears in its link and is a different identifier. `share revoke` requires the internal ID and `--yes`; a URL, title, or public ID is never guessed into an internal ID. Its preview performs the same ownership and range checks without cancelling anything. Creating and cancelling previews do not change the saved Cookie, job configuration, or ledgers.

Without scope, `share list` reads page 1 (up to 50 entries) and sets `catalog_complete: false`; `--all` reads the complete catalog. With scope, it first reads the entire own-share catalog and the bounded account subtree, then verifies every candidate's complete virtual-root and descendant-directory pages with explicit numeric owner `1` on every page. Only shares whose **every shared member** matches an account object within the range are displayed. Each FID is checked against the scope set before account info, ancestry, or a child share-page request; an outside member filters the entire share. Traversal rejects duplicate/cyclic FIDs and stops at 1024 levels or 100,000 members. An existing share may contain a valid subset of its account subtree; it need not contain every current account descendant. The default shows the first 50 verified results; `--all` shows all verified results. Unreadable, expired, malformed, or unprovable scoped candidates are counted and omitted.

Scoped JSON includes `scope_applied`, `catalog_complete`, `catalog_total`, `verified_total`, `outside_scope_count`, `unverifiable_count`, `returned_count`, `has_more`, and safe catalog `items`. `has_more` refers to further verified results. An incomplete catalog, incomplete scope traversal, or authentication failure stops the command; a candidate verification failure only increases `unverifiable_count`. Catalog and share-member proof requests are ownership interfaces and do not themselves enforce file scope on the server. Lists never output extraction codes, tokens, `first_file`, or raw nested fields.

Creation is submitted once. Success requires the original task to finish, the exact internal ID to appear in the complete own catalog, matching link/public ID/type/expiry/source-name title and requested code, and complete owner-verified membership. The password endpoint need not return an internal ID; its result is associated through the original task and exact catalog entry. An extra contradictory internal ID is rejected. An associated actual link, title, and code are retained for cleanup even if the returned creation options differ; a mismatch still returns nonzero. A directory source is captured before submission and all descendants are checked after creation; a root directory alone cannot prove a complete share. Partial-failure snapshots and malformed counts cannot be reported as success.

Partial or unconfirmed creation returns exit code 4 and keeps available operation evidence in JSON stderr at `error.operation`, including the original task/internal ID and a verified link/code when obtained. Human errors print the same safe evidence. `result` distinguishes `partial`, `unknown`, and `failed`; successful operations use `created` or `revoked`. A verified extraction code is an intentional creation result. Neither a partial share nor an unknown request is automatically retried or cancelled. There is no cross-process automatic recovery for share creation in this version.

Cancellation submits one `share/delete` request, then rereads the complete own catalog up to six times at 500 ms intervals to prove that exact ID disappeared. A lost response can be resolved only by that complete readback. A still-present ID or incomplete readback remains unconfirmed and returns nonzero; an HTTP success or empty response alone is insufficient. With scope, the exact account identities and parents of all proved shared members are checked again before submitting. Historical shared descendants that moved outside the account scope prevent cancellation. Without scope, an expired share already proven to be in your own catalog can be cancelled without reading its unavailable contents.

Writes are single-submit operations and do not store share tokens or a new task ledger.

## Batch saves

`save-batch` accepts UTF-8 files, including a UTF-8 BOM. A `.txt` file contains one share URL per line; empty lines and lines starting with `#` after surrounding whitespace are ignored. TXT requires `--to` for its common destination. A `.csv` file requires a `url` header and permits `to`, `passcode`, and `source` columns, in any order. Duplicate or unknown headers, malformed quoting, wrong column counts, and invalid URLs or paths are errors. Empty CSV rows are ignored. Keep inputs containing extraction codes private.

Each CSV row's nonempty `to` takes precedence over `--to`; each nonempty `source` takes precedence over `--source` (default `/`). TXT uses the command's source for all rows. Repeated `--include` and `--exclude` apply the same direct-name filtering rules as `save` to every row. Repeated entries for the same share ID, source, and destination are rejected before execution.

Example CSV:

```csv
url,to,passcode,source
https://pan.quark.cn/s/example,/工作资料/转存A,,/共享目录
https://pan.quark.cn/s/another,"/工作资料/目标, 二",,/共享目录/子目录
```

```sh
quark save-batch links.txt --to '/工作资料/批量转存' --source '/共享目录' --include '*.txt' --exclude 'skip*' --dry-run --report ./batch-preview.json
quark --scope '/工作资料' save-batch saves.csv --include '*.txt' --report ./batch-result.json --json
```

The CLI parses every row and verifies every destination and its scope before the first remote write, including empty selections. Any invalid input or unverifiable destination aborts the whole batch. It then reads each source and previews its selection. Source failures, existing target conflicts, and name collisions with earlier batch rows become independent failed results; other prepared rows execute serially. Execution repeats source and destination validation. An execution failure blocks later rows for that same verified destination, because accepted writes may lack a reply. Other destinations can continue. No failed write is automatically resubmitted.

Results contain `total`, `succeeded`, `skipped`, `failed`, and `rows`. Each row includes its starting physical line number, share ID, destination, source, and `result` (`saved`, `preview`, `skipped`, `failed`, or `blocked`). Available counts, destination FID and task ID are included; failures include an error category and exit code. URLs, extraction codes, credentials, and raw remote error messages are omitted. Any failed or blocked row produces a nonzero exit code; mixed errors use the highest row exit code.

`--report` writes this safe JSON result through a temporary file in the report directory, flushes and syncs it, then atomically replaces the report. Its parent directory must exist; it cannot overwrite the input or use a symbolic link. Report preparation failure stops before any remote write. A publication failure preserves an existing report, prints the row results, and exits with code 5; it does not repeat completed operations. `--dry-run` submits no remote mutations and can write the explicitly requested local report. An empty input is a successful zero-row result.

## Incremental share jobs

`job add` reads the share selection, verifies the account destination and scope, and saves a local task. The destination must be an existing directory or one missing final directory under an existing parent. Add does not create remote contents. Job names must be valid single names and unique. `job list` and `job remove` use only local state and work without a Cookie; add and run require authentication.

```sh
quark --scope '/工作资料' job add notes 'https://pan.quark.cn/s/example' --source '/共享目录' --include '*.txt' --to '/工作资料/定时转存'
quark job list --json
quark job run notes --dry-run --json
quark job run notes --json
quark job run --json
quark job remove notes
```

Source paths and repeated include/exclude filters use the same rules as `save` and are persisted in the task. Filters apply only to the selected source's direct children. A selected directory includes its whole subtree; subsequent runs recursively discover new descendants without applying the top-level globs again. Directory aggregate size and update time can change when children are added and do not cause a file-content conflict.

The first run copies new selected items and records verified source-to-target FID mappings. A second run with the same identities submits no writes. Files require a nonnegative size and a usable positive, finite `updated_at` version; missing or invalid identity fields stop before writing. A changed source FID, name, type, size, version or parent relationship, or a missing/changed target, is a conflict. Existing same-name targets are never adopted or overwritten. A bound target directory keeps its original FID: deleting it and creating a new directory with the same name does not rebind the task. The share source directory also keeps its original FID.

When add uses a scope, its verified anchor FID is saved with the task. Every run enforces that boundary. A current `--scope` or `QUARK_SCOPE` adds a further boundary and cannot widen or replace the saved one. Scope checks retain exact metadata and parent-list membership verification. A newly created directory is confirmed through bounded readback before it receives writes.

The config directory contains `jobs.json`, `jobs-ledger.json` and a stable `jobs.lock` file, protected like credentials. Config stores the normalized share URL/ID and, if supplied, a separate private extraction code; it does not store arbitrary URL queries or account Cookies. The ledger stores identities, mappings, and pending intents, without share tokens or signed URLs. Updates flush and sync a temporary file before atomic replacement; Linux also syncs the containing directory. A kernel file lock covers the full configuration change or run, prevents overlapping timers, and is released automatically when a process exits. The lock path remains in place. `job run --dry-run` performs reads and checks without changing configuration or ledger bytes.

Before each remote mkdir or save, an unknown intent is durably recorded. A returned task ID or created directory FID is persisted immediately. A later run can query the original task or confirm that exact created directory; it never resubmits an unknown operation. Without an ID, automatic recovery stops for manual verification. New target mappings require exact parents, unique new FIDs absent from the pre-submit snapshot, and matching names/types/sizes; task result order is not used as correspondence evidence.

For a directory transfer, `confirmation_started` is persisted before validating the copied subtree. That initial attempt makes bounded read-only retries for briefly missing directory-index entries. If mapping then fails or the process exits, later runs require manual verification and do not fill the mapping from later same-name files. File-only transfers can recover through their original task and exact new-FID confirmation. Mapping updates are committed together; an incomplete subtree does not create partial mappings. Corrupt/missing required state stops without resetting the ledger, and a state write failure stops further remote writes in that run.

`job run` without a name runs tasks serially and reports each result. Independent task failures allow other tasks to proceed unless local state cannot be safely persisted; failed tasks produce a nonzero exit code. JSON uses safe task fields, counts, target/task IDs and error categories, without extraction codes, tokens or raw remote errors. `job remove` deletes only the local task configuration, preserves its ledger and unknown intents, and does not cancel an accepted server task. Preserved unknown intents prevent a replacement local task from writing into their unconfirmed destination. Do not delete ledger files to retry uncertain operations.

### System scheduling examples

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

## Upload and download details

Uploads calculate MD5, SHA-1 and SHA-256 in streaming passes, request a Quark pre-upload task, update its hashes, obtain per-part OSS authorization, stream parts, submit verified ETags for completion, and ask Quark to finish. A Quark instant-save response with an explicit completed FID skips multipart/finish writes and verifies that exact object. Ordinary success requires an explicit successful terminal state and exact-FID info plus parent-list readback with matching parent, name, type and size. Missing completion evidence, contradictory/failed task states and HTTP 200 error XML cannot report success. OSS status 203 preserves a callback warning; success still requires Quark completion and target verification. The server-provided part size is used when present; otherwise the implementation uses 4 MiB. Alibaba Cloud documents parts from 100 KB to 5 GB, with a smaller final part permitted. Quark's web upload flow rejects zero-byte files; the CLI refuses them before upload requests. A recursive tree containing one stops before mkdir, while empty directories remain supported.

Downloads use the signed URL returned for the exact requested FID. The full Cookie is never sent to a transfer URL; only the `__puus` field is sent to validated HTTPS `*.pds.quark.cn` and `*.drive.quark.cn` storage subdomains. It is omitted for all other hosts, including redirect targets outside those domains. If `__puus` is missing for a recognized storage URL, download stops with a clear authentication error. Redirects are followed manually only when each destination uses HTTPS on an allowed Quark or Alibaba OSS domain. Partial files are removed on transfer failure.

### Private transfer state and resume

Upload state is durably recorded even without `--resume`, including an intent before each remote mutation. Its destination lock is shared by competing local sources targeting the same remote parent/name; the stored canonical source, full hashes and file identity must match. If an upload is incomplete, `--resume` works only on its original task. Running without it cannot discard unknown acceptance and start another task.

```sh
quark upload ./large.bin --parent '/工作资料' --resume --progress --limit-rate 1048576
quark download '/工作资料/large.bin' --output ./large.bin --resume --progress
```

Metadata is stored under the private config directory's `transfers/` folder, using the same current-user permissions, atomic fsync writes and stable kernel locks as jobs. Upload state includes original task/upload IDs, local identity and hashes, confirmed parts, and the minimum auth_info/callback needed to recover the original operation. Those fields are credentials and remain private; account Cookies, auth_key and signed URLs are never stored. Completion removes upload credentials. Damaged state, changed local contents (even with the same size/mtime), changed remote identity, missing original tasks or expired authorization stop with the state preserved.

For multipart recovery the CLI obtains fresh authorization for the original upload, queries all ListParts pages, verifies the object/markers/part numbers, and compares each returned length and MD5 ETag with the local part. It skips only verified parts. Extra, missing or changed confirmed parts stop recovery. Second and later parts use the official SHA-1 hash context/canonical header order. An uncertain pre/hash/merge/finish acceptance is preserved and not automatically repeated. A durably received finish task ID permits read-only polling of that task, followed by exact FID verification; ListParts absence or a same-name file alone cannot resolve acceptance.

Downloads without `--resume` use random temporary files and remove incomplete data on a handled failure. Resume uses a stable tool-owned `.quark-<key>.part` beside the output and a private metadata file. Data is flushed and fsynced before each offset/prefix-hash checkpoint. Resume rehashes the confirmed prefix, checks output/remote identity, and obtains a fresh signed URL. A nonzero offset requires the original strong ETag, `Range` and `If-Range`; weak/missing validators refuse append. `206` must have matching ETag, start, end and total in Content-Range, the exact body length and no content encoding change. A `200` at a nonzero offset leaves the existing part intact and fails. `416` cannot prove completion by itself; it needs a prior durable complete-payload hash and consistent identity/size/validator. State and the confirmed part remain after interruption or refusal.

Completed files are installed atomically. Default publishing cannot replace a file that appeared concurrently; `--overwrite` explicitly permits atomic replacement after validation. A crash after publishing can be recovered only after the complete output SHA-256 is confirmed. Ordinary and recursive transfers never send an undocumented multipart abort or delete partial server tasks automatically.

### Recursive directories

```sh
quark upload './中文 目录' --parent '/工作资料' --recursive --dry-run --json
quark upload './中文 目录' --parent '/工作资料' --recursive --resume --progress
quark download '/工作资料/中文 目录' --output ./downloaded --recursive --resume --progress
```

Upload maps a directory to `parent/LOCAL.name/...`; download maps the selected remote directory's **contents** directly into `--output`, preserving empty directories. Both require explicit `--recursive` and process files serially; eligible individual files can use concurrent byte ranges during a fresh download. The whole known tree, scope and path/name conflicts are checked before the first remote upload/mkdir or local destination creation. Existing unique directories can be reused during the initial upload preflight; existing same-name files are conflicts. Download defaults to preserving existing files, with explicit `--overwrite` for replacement.

Transfer names use a portable Windows/Linux policy: reject separators, control characters, `<>:"|?*`, ADS/drive colons, trailing dots/spaces, and Windows device names (including COM/LPT superscript 1/2/3). Sibling names cannot collide under NFC normalization and casefolding. Root/child symlinks, junctions, reparse points, device objects, unsafe local paths, repeated/cyclic remote FIDs and false parent records stop the operation. These rules also protect Linux downloads of names that would be dangerous on Windows.

A private directory manifest binds the fixed tree, source/output and exact remote directory/file FIDs. Each mkdir is recorded as unknown before submission, and its response FID is persisted before bounded readback and scope validation. Missing response IDs cannot be recovered by adopting a later same-name directory or resubmitting mkdir. Scope is checked again before every remote mkdir/upload. Resume retains its saved scope; a current scope only adds restrictions, and downloads check that boundary before traversing the subtree.

Resume requires the same fixed tree and exact FID bindings. New source files, renamed/replaced directories and later unbound same-name directories are conflicts; they are not collected into the original transfer. Completed files require metadata/parent verification plus matching reliable remote hashes. When those hashes are unavailable, the CLI performs a read-only temporary download and SHA-256 comparison before skipping them. This verification uses the same bandwidth budget and can transfer the complete file. Downloaded complete outputs are also rehashed locally. Directory manifests preserve acknowledged work and unknown intents when a later file fails; deleting state is not a recovery operation. Recursive upload previews create no state or new lock files.

### Progress and bandwidth

`--progress` emits safe current-file/relative-path events to stderr, with sent/confirmed bytes, total when known, and speed. JSON stdout retains only result fields. Noninteractive terminals get complete lines; TTYs can update a line. Upload confirmation events follow their durable checkpoint. `--limit-rate` accepts a positive integer in bytes per second, uses actual payload bytes with a monotonic clock, and shrinks chunks for low limits. All files in a recursive operation share one cumulative budget; local hashing is not rate-limited. The bandwidth limit also applies to resumed completed-file verification downloads.

The CLI obtains download links from Quark's fixed API using the desktop-client User-Agent; the browser-style endpoint can reject large files with error 23018. Fresh single-file downloads of at least 8 MiB use up to 4 parallel HTTP byte-range requests by default; `--connections N` sets 1–16 connections (`1` forces a single stream, `--connections 16` restores the measured maximum). Parallel assembly requires the server to return exact 206 ranges with the same strong ETag and identity encoding for every segment. If the server ignores Range, that response is consumed as a regular stream; if the server supports Range but provides no strong ETag, the CLI falls back to a regular stream. `--resume` also supports bounded parallel range reads, in fixed 4 MiB segments: workers read at most N pending segments and one writer persists only the contiguous, fsynced prefix in the existing ledger. Old serial checkpoints remain compatible. Stopping or a failed segment preserves that confirmed prefix; changing `--connections` on the next attempt is supported. Recursive downloads still process files one at a time, while a qualifying file may use its own range workers. SVIP speed is an upper limit advertised by Quark, not a guaranteed rate; account policy, source delivery, route, and local network/disk determine the result. Parallel requests can improve throughput where one stream is the bottleneck, but cannot exceed those limits.

### Request pacing and cooldowns

Metadata and signed-URL requests from the CLI and from the PotPlayer bridge share one local request guard. It reserves a start slot at least 0.5 seconds after the previous one and refuses to open a new request while a persisted cooldown is still active. The state lives in `request_guard.json` (with a stable `request_guard.lock`) in the current configuration directory, so independently launched CLI runs and the plugin see the same cooldown across processes and restarts. It stores only wall-clock times and a fixed reason kind; no Cookie, account identity, token, or signed URL is ever written.

After an upstream rejection records a cooldown, later requests are refused locally before contacting Quark. The defaults are:

- HTTP 429 pauses at least 60 seconds.
- Account or QR-login API HTTP 403, or an explicit restriction/verification phrase in a failed business response, pauses at least 600 seconds.
- A longer `Retry-After` (delay-seconds or HTTP-date) is honored in full; HTTP 503 pauses only when it carries a valid `Retry-After`.

Repeated storage-level 403 on a media or subtitle signed URL, after the single allowed refetch, pauses new media/subtitle requests for 60 seconds. The 4 MiB serial media segments, 64 KiB forwarding, and the player's own embedded subtitle/audio handling are unchanged. Media payload requests check the cooldown without adding request spacing or a byte-rate limiter.

These typed cooldown errors (`rate_limited`, `access_restricted`) never clear the saved Cookie, open a QR login, or clear favorites/recent entries; a 401 credential expiry behaves exactly as before and still asks for a new login. Recovery is to wait out the cooldown and confirm the account in the official Quark client; do not clear state or rotate credentials to recover. If a cooldown cannot be persisted locally, the current process stops its automated requests with a clear local-storage error; a separate process or a restart cannot know the unpersisted cooldown, so fix local storage and verify the account before starting it again. Local health, the favorites/recent library, and logout stay usable throughout.

The spacing and cooldown values are conservative tool defaults, not official Quark safety thresholds; they cannot guarantee protection against account restrictions. `Retry-After` is defined in [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110#field.retry-after) (§10.2.3; `429 Too Many Requests` originates in RFC 6585 §4).

## Capacity, duplicates and batch renames

```sh
quark --scope '/工作资料' du '/工作资料/项目' --json
quark --scope '/工作资料' duplicates '/工作资料/项目' --json
quark rename-batch '/工作资料/项目' --pattern '^old-' --replacement 'new-' --json
quark rename-batch '/工作资料/项目' --pattern '^old-' --replacement 'new-' --apply --scope '/工作资料' --json
```

`du` accepts a directory or single file. It reads all directory pages and checks each exact FID, parent and type; queued objects are checked again before reading their children. Repeated/cyclic FIDs, inconsistent identities, failed reads or scope violations stop the command. The report gives `files`, `directories`, `known_bytes`, `unknown_size_files` and `complete`. Directory counts include the selected root and empty directories; a single file has zero directories. Missing, negative, boolean or otherwise invalid sizes count as unknown and are excluded from `known_bytes`; `complete` is false while any size is unknown. Failed traversal does not return a partial capacity report.

`duplicates` uses the same complete traversal. Its `confirmed` groups require a valid size and matching reliable top-level MD5 or SHA-1 values. Hashes must be 32 or 40 hexadecimal characters, respectively. A contradictory second hash cannot be ignored or bridged through a file missing that hash. `_extra` metadata and multipart ETags are not full-file hash evidence. `candidates` groups contain only size/name suggestions and specify `reason: "size"` or `"name"`; an identical group already confirmed is omitted. `hash_unavailable_files` counts files without usable hash evidence. File entries include only FID, absolute path, filename, valid size and valid hashes. This command never deletes anything.

`rename-batch` applies Python `re.sub` to the selected directory's direct children, including directories. It previews by default. `--apply` permits submission, while `--apply --dry-run` still previews. Regex/replacement references are validated before credentials or remote scope reads, including when the directory is empty. Invalid regex/replacement references, unsafe portable names, NFC/case collisions, occupied targets, duplicate results and swaps/rename chains fail the whole preflight. Unchanged entries remain in the report. Files need a known size and positive finite `updated_at`; directory aggregate timestamps are not file versions.

Each rename is serial and submitted once per FID. Before every write, the CLI verifies the root and the complete sibling set against the saved plan, including exact FIDs, parents, names, types and file size/version. Scope and its protected anchor are checked again. Confirmation reads the exact FID and its parent listing; a known index lag has a bounded read-only wait. A lost response can be resolved only by observing that exact object at the planned name. An old/unexpected name or unreadable identity remains unknown and stops later writes without resubmitting. Partial runs exit nonzero; safe per-item evidence appears in `error.operation` on stderr. Preserve that evidence for manual review before starting another batch.

## Single-direction backup

```sh
quark backup './中文 项目' --to '/工作资料/备份' --scope '/工作资料' --json
quark backup './中文 项目' --to '/工作资料/备份' --scope '/工作资料' --apply --progress --limit-rate 1048576
quark backup './中文 项目' --to '/工作资料/备份' --scope '/工作资料' --apply --update --json
```

Backup maps the local directory's **contents** into an existing remote directory, including empty directories. It previews by default; only `--apply` permits remote writes, and `--apply --dry-run` previews. Preview does not create a manifest, state directory or lock. Existing manifests and upload ledgers are read without changing ACLs or file modes; unsafe ownership or permissions stop preview. Apply verifies existing state ownership before tightening permissions. The entire local tree and target are checked before writing, using the recursive transfer policy for links, reparse points, portable names, collisions and empty files. Remote extra objects and locally deleted files are preserved. An existing untracked same-name object is a conflict, even when its size matches.

The user-private config directory stores `backups/` manifests and stable native locks. A manifest binds the canonical local root, exact target FID, saved scope, file hashes, exact remote identities and pending steps. The target lock covers the whole operation and is shared across local sources. Saved and current scope restrictions intersect; changing a same-name target or scope directory cannot rebind its FID. Damaged or untrusted state stops instead of becoming a fresh backup. Skipping a mapped file requires its complete local hash and verified remote identity/content; missing version evidence can require a full read-only verification download, using the same bandwidth budget.

Without `--update`, a changed managed file is a conflict. With it, backup durably records unique temporary/archive names, uploads and verifies the new version, preserves the exact old FID under the archive name, then renames the exact new FID to the original name. It never calls delete. Results list archive FIDs/names for later management. The sequence is not atomic across requests; failure may leave a new temporary object and old archive. An intent is saved before every mutation and receipts are saved before subsequent steps. Unknown operations retain their original evidence: unknown mkdir without a received FID cannot adopt a later same-name directory; uploads resume the original transfer task; rename recovery only reads known FIDs and never resends an uncertain rename. State-write failures stop later writes. Safe pending steps and associated FIDs/names appear in `error.operation`.

Pending `phase`, `fid` and `new_fid` reflect the last durable manifest. If a remote response supplies an exact FID but its first receipt cannot be saved, `error.operation.pending.unpersisted_fid` gives that in-memory FID for manual inspection. Recovery uses the durable manifest and original upload ledger as its authority.

`--progress` writes safe progress to stderr. `--limit-rate` is a positive integer in bytes/second and all payload and verification transfers share one budget. JSON stdout contains only report fields; raw transfer credentials and API messages remain private.

## PotPlayer plugin (夸克网盘)

Windows 10/11 x64 only. `QuarkPotPlayer-Setup.exe` installs a companion component that adds a 夸克网盘 entry to PotPlayer's media URL list, so you can browse your own drive and play files online without downloading them first. The plugin has its own login: it never reads, changes or deletes the CLI's saved Cookie.

Playback streams the original file. Nothing is written to disk — no video files, no segments, no cache directory — only bounded in-memory buffering plus the plugin credential and a small amount of runtime state. Large player requests are fetched as serial 4 MiB upstream ranges, each validated and forwarded 64 KiB at a time; the player still sees its original `200`/`206`, `Content-Length` and `Content-Range`. The component runs in the background for the current user only, is reachable from this machine only, and installs no Windows service and no start-up entry.

Install: close PotPlayer, run `QuarkPotPlayer-Setup.exe` and confirm (or browse to) the PotPlayer folder. The installer finds the player through its registry entries and usual install paths. The component, the setup copy and the manifest go under `%LOCALAPPDATA%`, so those need no administrator rights; the AngelScript files and the launcher go into the player's `Extension` folder, which must be writable — a player installed under `Program Files` needs an elevated run or a granted write permission. Full steps and silent switches: [安装说明](docs/PotPlayer安装说明.md).

First use: in PotPlayer's browser panel open the menu, choose 简索 → 简索地址…, expand 打开地址, and select 夸克网盘 我的网盘. The first time, a window shows a login QR code — scan it with the Quark mobile app and the directory appears. Then open a video to play it online and seek freely; the same-folder playlist is ordered naturally, and whether playback advances to the next episode depends on PotPlayer's own end-of-playback setting (the plugin changes no global player setting). Embedded subtitle and audio tracks keep playing through the player's own decoding and switching. The player's native subtitle search also lists the matching same-folder SRT/ASS files for manual selection; the plugin never auto-selects or replaces an embedded track, so a search with no candidates leaves the embedded track untouched. The bridge only reads the chosen subtitle text into memory — whether PotPlayer itself saves that subtitle to disk is native player behaviour. Share saving, poster libraries, transcoding and multiple accounts are not implemented.

The same entry also lists five more categories: 搜索网盘 searches the whole drive by file name (type the keyword in the player's search box), 收藏目录 keeps folders you mark from any directory's first page, 最近观看 replays a video that was successfully streamed before, 追剧中心（继续观看） groups the videos you recently watched (finished ones included) by the real folder they live in (verified folder names only, same-name folders kept apart, root or unclassified videos listed individually) through a purely local read, and 播放状态与诊断 opens a separate local status window — playback positions and states, actual forwarded bytes, 5-second average rate, recent response-header times and errors, credential/cooldown state — with an explicit export of a whitelisted JSON report. Opening a 追剧中心 group reads that one directory once to show 已看 x/y, a 继续观看 target and the next episode after the most recently watched listed one (no wrap past the last episode); every 最近观看 and 追剧中心 video row also carries an 操作：…（书签、记录） folder that opens that video's bookmark manager offline — list, rename and delete work without playback, while add and jump need the one live binding — or, after a confirmation page, deletes only that one local watch record; other history, favorites, watched marks and bookmarks stay, a running stream keeps playing, and only a later explicit play records it again. Selecting the current playback row and pressing 播放书签 in that window opens a per-video bookmark manager: add a bookmark at the player's real current position with an optional note (an empty note shows the timestamp), then jump, rename or delete. A bookmark is accepted only for a live binding of that same PID and full logical fid; adding always records the player's real current position, so a file that was replaced with a different size or duration can still receive a **new** bookmark, while **jumping** to an older bookmark whose saved size or duration no longer matches the live file is refused; without the native reader nothing is recorded or seeked; bookmarks are local only and never appear in the exported report. Opening a video from any directory, search result, recent entry or playlist resumes at the position saved last time. Progress is polled about every 2 seconds and persisted about every 10 seconds plus on pause, episode switch and close; positions under 15 seconds or within `min(30 s, 2% of duration)` of the end start from zero, a changed file size/duration discards the old position, and a player already past ~5 seconds when first read is left where it is (no forced jump back). Favorites, recent entries, saved positions and explicit watched marks live in one small JSON file next to the plugin credential (`config_directory()/potplayer/library.json`, schema version 4: recent rows carry the real parent directory FID and its name — shown as `directory / filename`, resolved only from a directory the bridge already listed or a favorite, never guessed from a window title and never costing an extra cloud request — and a separate marks list keeps manual watched flags past the 100-entry recent cap; `/v1/recent`, `/v1/continue`, `/v1/watch-center` and `/v1/series` expose read-only progress views); no video bytes, signed URLs or cookies are stored there. Playback bookmarks are a separate local file (`bookmark.json`) and are never part of the diagnostics report. Each directory's first page also offers a 继续观看 row — the latest unfinished episode, or the next unwatched one, or a completion notice when every episode is watched — and a 剧集观看状态 row that opens a per-episode watched/unwatched management list, including episodes never played; marking an episode watched suppresses automatic resume and hides it from 继续观看, and marking it unwatched clears that episode's saved position without seeking a running player. Position binding uses the player window's PID plus the full logical fid URL, never file names or window titles; a file opened in several player instances pauses progress writes for that fid until one closes. Resume needs the native Windows player — without it the bridge still works as a data source, and other platforms' CLI is unaffected.

Only an explicit plugin logout clears the plugin login, favorites, recent entries, saved positions, watched marks and playback bookmarks; an expired login does not, and upgrading keeps them. Uninstalling removes only the files the installer created and keeps the credential by default, with an explicit option to clear it.

The bridge shares the CLI's local request guard, so a rate-limited or restricted account pauses both tools at once (see [Request pacing and cooldowns](#request-pacing-and-cooldowns)). A plugin cooldown answers 429/403 with `Retry-After` and keeps the plugin login, favorites and recent lists intact; local health, library and logout stay usable. Recovery is to wait and confirm the account in the official Quark client, not to clear state or rotate credentials.

User guide: [docs/PotPlayer使用指南.md](docs/PotPlayer使用指南.md).

## Downloader window (夸克网盘下载器)

Windows 10/11 x64. `QuarkDriveDownloader.exe` is a single-file, windowed GUI to sign in, browse and search your drive or open a Quark share, check files and folders, choose a local output folder, and download the selection as one queue. Automatic overwrite, a live pause button, streaming/playback and account management are out of scope. The EXE needs no Python installation and no source tree.

The window uses the **same Cookie as the `quark` CLI**. Its `登录` button opens the same QR window as the plugin and stores `auth.credential_path()` — there is no second credential file, and `quark auth login` (or an exported `QUARK_COOKIE`/`QUARK_COOKIE_FILE`) is accepted as-is. Browsing and downloading go through the existing library calls, so the request guard, the signed-URL rules, the existing-file refusal and the atomic publish are exactly the CLI's. After the initial directory loads and after login, a background request to the fixed `https://pan.quark.cn/account/info` endpoint reads `data.nickname` for the top bar. Failure leaves browsing and downloading available; a new login clears the previous nickname, and stale responses cannot restore it.

Run it from a checkout:

```sh
python -m quark_cli.downloader
```

Build the standalone EXE (PyInstaller must be installed for the chosen interpreter):

```sh
python packaging/downloader/build.py
python packaging/downloader/build.py --python PATH --dist-dir dist
```

The build prints the path, its SHA-256 and its size; the product is `dist/QuarkDriveDownloader.exe`.

The keyword field filters the displayed names locally, without a cloud request; the type selector narrows the list to files, directories or both. Choose `全盘` or `当前目录及子目录` as the scope, then click the single `搜索` button. Both use case-insensitive substring matching and the CLI's existing paginated traversal and safety limits. Search results show their absolute drive paths, so same-name items in different folders remain distinguishable. Select results to download them directly, or double-click a directory result to browse it; returning upward follows its real parent directories. `返回目录` appears during a search session and returns to the folder where the search started. Navigation clears the old keyword and type filters to show the new folder's contents; refreshing a search repeats its query, and refreshing a normal directory keeps its filters. Searching keeps the window responsive but prevents another search, navigation or download until it ends. Large drives can take time to scan; there is no remote search index.

The bottom area groups the save folder (`更改` and `打开目录`), thread count, rate limit, `停止` and `下载（已选数量）`. `打开目录` opens the chosen local folder in Windows Explorer when clicked, including after a batch completes, fails or stops. It checks that the folder still exists, and reports an opening failure in the window. Downloads do not open Explorer automatically.

The editable `扩展名` selector filters the already loaded table by filename suffix, without requesting the drive or examining file contents. Choose `视频`, `音频`, `图片`, `文档`, `压缩包` or `无扩展名`, or enter suffixes such as `mp4, mkv` and `.tar.gz`. Matching is case-insensitive; commas (English or Chinese), spaces, semicolons and `、` separate custom suffixes. Multiple suffixes match any of them; name and file/directory filters still combine with this filter. An active suffix filter excludes folders. Blank input or `所有格式` restores all formats. Invalid input clears the visible matches and disables downloading stale checked rows until corrected. Filtering preserves hidden checks but only visible checked rows enter a batch. Refresh keeps the suffix filter; navigating folders or switching sources clears it. Busy operations disable this control, and suffix filters are not saved in preferences.

Click the `名称`, `大小` or `路径` column header to sort the visible file table; the first click uses ascending order and subsequent clicks toggle ascending/descending, marked by an arrow. Name and path comparison is case-insensitive; sizes use byte values. Folders stay first, and size sorting orders folders by name and puts files with unknown sizes after files with known sizes in either direction. Equal sort values keep their source order. Sorting works for account listings, search results and shares without a network request, preserving checks, highlights and keyboard focus by FID. The selected sort remains for this window session, including after filtering, refresh and navigation. Download order follows the sorted visible table, and busy operations freeze sorting.

Click a row's checkbox, or press Space on the focused row, to check it for downloading. `全选/取消全选` toggles the visible rows; Ctrl is unnecessary. Filtering preserves checks within the current view but downloads only the currently visible checked items. Changing folders or switching between your drive and a share clears the checks. Row highlighting is used for navigation and does not select download items.

Paste a direct `https://pan.quark.cn/s/SHARE_ID` link, a link containing `?pwd=CODE`, or a complete share message containing the link and `提取码`/`密码`. A recognised code fills the separate masked passcode field; edit it if needed, then click `打开分享`, which uses that field's current value. A bare link plus a separately entered code still works. Multiple Quark links or conflicting embedded codes are rejected until resolved, without saving anything. URLs retain the CLI's strict Quark validation. Previewing and navigating the share do not save its files. `我的网盘` returns to your drive. Local name/type filters work in the share view; account-wide and subtree search are available in your drive view only. Login is required for downloading shares.

The existing download API needs an account FID, so each checked share item is saved into a newly created `.quark-download-...` directory in your drive before downloading. Only that item is saved, and temporary local files and checkpoints are isolated from ordinary downloads. On completion, failure or stop, the downloader deletes its temporary cloud directory and removes only that directory's recycle record. It does not clear the recycle bin or delete the original share. A cleanup failure is reported with the temporary directory identity, including whether the local file was already downloaded; it is never reported as full success. Share downloads need available cloud space while running. If the process is forcibly terminated or the account/network becomes unavailable, cleanup cannot be guaranteed; inspect the reported temporary directory and recycle bin. A stopped share download starts over on the next attempt because its temporary FID and local checkpoint are discarded.

The downloader automatically remembers the output folder, thread count, rate limit and window dimensions in `config_directory()/downloader/preferences.json` (on Windows, normally `%APPDATA%/quark-drive-cli/downloader/preferences.json`; `QUARK_CONFIG_DIR` overrides the root). Choosing a folder or confirming the thread/rate fields saves immediately; a normal close saves the dimensions. An incomplete thread or rate entry keeps its last valid value. Existing version-1 preference files restore with an unlimited rate and are upgraded on the next successful save. Only these local preferences are stored; share text, passcodes, account data, sorting and task results are excluded. On startup, unavailable folders prompt you to choose again, and dimensions are limited to the current screen without restoring a screen position. Corrupt or unsafe preferences leave the window usable with defaults and are preserved for inspection, with automatic writes disabled for that session; fix or move that file before restarting to enable saving again. Write failures appear in the window log, and a failure during close is reported before the window exits. Writes use the existing private atomic JSON helper; if several windows are open, the last successful save wins.

The `下载任务` tab shows the current batch's waiting, running, completed, failed, stopped and unstarted items, with live progress and readable results. Select a row to see its full result and original save folder. `重试失败` submits failed items only; `继续未完成` submits failed, stopped and unstarted items, skipping completed items. Right-click a task row for `重试此项` or `打开保存目录`. Single-item retry accepts failed, stopped or unstarted tasks and leaves other results untouched. The folder action opens that task's original output folder, or the downloaded subfolder for a completed folder task; a missing folder or opening failure is reported. Right-clicking empty space performs no action, busy operations refuse the menu, and completed items cannot be retried. The retry actions keep the original order, output folder and share source even after navigating elsewhere or choosing another output folder, and use the current thread setting for the new attempt. Own-drive tasks reuse the library's safe checkpoints; shares start again through an isolated temporary copy and retain the cleanup rules above. Retries still refuse existing targets. If a share cleanup failure occurred after its file was published, that existing file is protected and the original cleanup error remains in the log for manual handling. Task results stay in memory for this window session; a new selection submitted through `下载` replaces them. A successful QR login invalidates retries of the previous account's recorded FIDs; browse and select again.

Behavior notes: browsing, searching and the file queue run on one background worker; download segments use the selected number of readers. Nickname lookup runs separately and shares the backend's request guard. All updates reach the Tk thread through a queue, so background work never operates on Tk controls. Navigation (including entering a folder by double-click) is refused while a batch runs, and a listing that arrives for a folder the window has already left is dropped instead of overwriting the current rows. One click freezes the checked items in the order the table shows them and serves them one at a time: the status line carries the item in flight (`（3/12）…`, phase, byte progress and rate), the log panel records one line per finished item, and the closing line reports the whole click (`全部下载完成：共 12 项`, or `下载结束：共 12 项，成功 10 项，失败 2 项`). Each file is downloaded into the chosen output folder and each folder into a **new** folder of the same name that preserves its tree. An existing target is never replaced: the library refuses it, that item fails alone with the conflict line, and the rest of the queue continues. Every failure (authentication, network, local path, name or conflict) is one readable line; no Cookie, share passcode or signed URL is ever displayed or logged.

`停止` ends the click safely: items that have not started are cancelled, and the item in flight is asked to stop at the checkpoint the library has already persisted. For your own drive, that checkpoint is a byte prefix written to the library's `part` file next to the target, and the ledger keeps the same offset, so the target is never published half-written and the same selection submitted again continues from where it stopped; once the file is published the part file is renamed onto the target and the ledger is kept with `phase=completed`, so a later `resume=True` recognises the published file instead of refusing it as an existing one. The window uses the library's bounded, stop-aware range transfer and shows the current connection mode and recent speed. A stop that arrives after the last item published is reported as the completion it turned out to be. While the stop is settling, closing the window and starting a new batch are both refused. If an own-drive download is forcibly killed, its part file and ledger allow the next submission of the same target to continue. Share downloads instead clean their temporary state and start over, as described above.

`总限速 MiB/s` defaults to `0` (unlimited). A positive value from 0.01 to 10240, including decimals such as `0.5`, sets one shared payload byte budget across the active connections, using the library's existing limiter. It applies to account files, recursive downloads and temporary share copies. The batch freezes the rate alongside its thread setting; manual retries use the current values. Preferences remember the rate. This controls average payload consumption; socket buffering and server behaviour can cause brief speed fluctuations, and it does not increase an account's server-side allowance.

The downloader's `线程数` setting defaults to 4 and accepts 1–16; 1 forces a single stream. It is restored from local preferences and frozen per batch. It applies to account downloads, share copies and files inside folders; the file queue remains sequential. Files below 8 MiB, or with only one remaining segment, use one connection. Unsupported Range responses fall back to a single stream. A fresh object without a strong ETag can finish as one response but cannot keep a safe resume checkpoint; its progress and stop result say that it must restart. Previously confirmed prefixes still require their matching strong ETag and are never appended to an unversioned response.

`tests/test_parallel_resume.py` checks overlap and connection limits, out-of-order reads, stop/resume with different limits, old serial checkpoints, ledger failures and mixed/truncated segments. A controlled offline 64 MiB transfer with each simulated connection paced at 16 MiB/s measured 13.73 / 54.30 / 98.33 / 147.38 MiB/s with 1 / 4 / 8 / 16 connections on the development machine; every output hash matched. This measures client concurrency, not Quark account speed or a way around account limits.

## Offline checks

From an installed checkout (including a virtual environment created above), install the package and the QR decoder used by the PNG regression test:

```sh
python -m pip install -e ".[test]"
python -m unittest discover -s tests -v
python -m quark_cli --help
quark --help
```

To run tests directly from the source tree without installing this project, first install its runtime and test dependencies:

```sh
python -m pip install "httpx>=0.27,<1" "qrcode[pil]>=7.4,<9" "zxing-cpp>=2.2,<4"
```

Then set `PYTHONPATH` for the test process:

PowerShell:

```powershell
$env:PYTHONPATH = 'src'
python -m unittest discover -s tests -v
python -m quark_cli --help
```

Linux:

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python -m quark_cli --help
```

The tests use `httpx.MockTransport` and a local QR decoder. They do not contact or modify a real Quark account.

`tests/test_downloader_core.py`, `tests/test_downloader_worker.py` and `tests/test_downloader_window.py` cover the download window without a network or an account: listing rows and their ordering, folder navigation, the batch queue and its closing summary, the local target and name of a file/folder download, progress text and its fraction, the error line, the worker's serialization, live stop hook and bounded shutdown, and the window's state machine driven with stand-in widgets. They also run the real library through the window's backend — one file installed under its remote name with no credential on the payload host, and one stop/resume cycle that checks the part file holds exactly the offset the ledger confirmed and that the resumed request carries `Range`/`If-Range` — plus one folder tree through the real recursive downloader, and, where a display is available, building the real window and browsing the root.

`tests/test_potplayer_extensions.py` covers the PotPlayer plugin without a real player: it tokenises both AngelScript files (terminated literals, balanced delimiters, no undefined calls, and no C-style array declaration such as `string roots[2];`), checks every native `Host*` call against the `api.txt` shipped with the installed player, checks the exported signatures and logic-link shape, asserts the search/favorites/recent/continue entries keep their native row shapes (`type=search`, idempotent favorite action tokens, idempotent per-episode watched/unwatched action tokens, no media URL on action rows), and drives the real loopback bridge to assert the JSON payloads the scripts parse — including the `/v1/search`, `/v1/favorites`, `/v1/recent`, `/v1/continue`, `/v1/series` and `/v1/mark` shapes. PotPlayer ships no standalone AngelScript toolchain, so the scripts are not compiled here. Real QR scanning, in-player playback/seeking/next-episode, the new entry screens and AngelScript compilation require a real player for manual verification.

Behavioral regressions cover exact paths and FID compatibility, cross-page ambiguity, tree/search output filtering, malformed pagination, forged/cyclic parent chains, source and destination scope boundaries, share paths and direct-name filters, empty selections, single-submit task readback, batch input/range preflight, partial failures and atomic reports, incremental identities and recursive mappings, durable unknown-operation recovery, native private permissions, independent-process kernel locks, safe JSON fields, zero mutation requests in previews, and decoding both PNG and rendered terminal QR matrices. Organization checks add unknown sizes, contradictory hashes, queued-directory scope changes, complete sibling preflight, regex/portable-name conflicts and single-submit rename readback. Terminal camera scanning requires an additional manual check.

## Implementation notes

The share downloader's offline tests in `tests/test_downloader_share.py` exercise real file and directory transfers, exact share selection, temporary-directory identity, failure/stop cleanup, delayed recycle records and protection of unrelated records. `tests/test_downloader_share_window.py` covers share navigation, checked download arguments, stale results and a real Tk share-view smoke check. Native ACL regression tests reproduce the old Win32 error 5 on newly created Modify-only targets and verify the repaired private DACL. These checks do not validate a real account's share permissions or remote cleanup.

- [QuarkPan](https://github.com/lich0821/QuarkPan) (MIT) was reviewed for list, CRUD, and upload request shapes. Exact-FID details use `GET /file/info?fid=...&_fetch_full_path=0`, cross-checked against this [public endpoint reference](https://github.com/Ry-Run/quark-automatic-quality/blob/main/%E5%A4%B8%E5%85%8B%E7%BD%91%E7%9B%98%E5%AD%97%E5%B9%95%E5%8C%B9%E9%85%8D-%E9%9C%80%E6%B1%82%E6%96%87%E6%A1%A3.md). `auth status` validates through `GET /file/sort` with a one-item root listing. No source code was copied into this project. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
- Quark QR login endpoints and cookie completion order were cross-checked against the MIT-licensed [QuarkPan login implementation](https://github.com/lich0821/QuarkPan/blob/master/quark_client/auth/api_login.py) and [QuarkDownloaderPro cookie flow](https://github.com/OP404OP/quarkdownloaderpro/blob/main/src-tauri/src/quark_client.rs). No source code was copied.
- Share token, detail pagination, save, and task request shapes were cross-checked against [Pearsoon/quark](https://github.com/Pearsoon/quark/blob/master/quark.py) and [QuarkPanTool](https://github.com/ihmily/QuarkPanTool/blob/main/quark.py). No source code was copied.
- Temporary share cleanup uses a targeted recycle-record removal. Quark documents that ordinary deletion moves files to the [recycle bin](https://www.quark.cn/documents/help/recycle-bin); the list/remove request shapes were cross-checked against this project's [Quark API notes](https://github.com/zhaocongqi/clouddrive-auto-save/blob/main/docs/quark_apis.md). No source code was copied.
- [Alibaba Cloud OSS multipart upload constraints](https://help.aliyun.com/zh/oss/user-guide/multipart-upload) document the allowed part sizes used in the upload implementation.
- Quark API endpoints, including QR login and share saving, are unofficial and can change without notice. Run `quark auth status` after future logins and use a disposable test folder for live checks.

### 客户端扩展进度

下载器的全盘搜索已改为服务端分页，子树搜索增量显示；新增取消搜索，导航和输入不再因搜索整体禁用。全盘结果路径按选中或打开时定位，暂停路径排序；部分失败明确标注结果不完整。下载引擎、续传及分享清理规则保持不变。

后续按 Electron 基础客户端、文件管理、上传与持久传输中心、分享与回收站、发布完善五个阶段推进。详见 [客户端功能路线图](docs/网盘下载器待实现.md)。

### Electron 桌面客户端

第二版客户端使用 Electron + React + TypeScript，保留 Python 下载引擎和私有凭证。Windows x64 的免安装包在 `desktop/out/make/zip/win32/x64/`，解压后运行 `QuarkDriveClient.exe`；需要保留同目录资源，不能只复制 EXE。不依赖系统 Python。旧的 `dist/QuarkDriveDownloader.exe` 仍可独立使用。

```powershell
cd desktop
npm ci
npm run typecheck
cd ..
python packaging/desktop/build.py
```

界面提供我的文件、分享链接、传输任务和设置；支持扫码登录、昵称、分页搜索与取消、虚拟文件列表、复选框、名称/大小/路径排序、类型与格式筛选、分享目录导航、多线程续传、停止、逐项重试和保存目录打开。分享输入自动填入提取码，手工修改优先；服务端全盘结果路径按需定位。文件、目录、分享下载使用同一引擎和清理规则。

Electron 主进程通过白名单 IPC 管理本地 Python 子进程；Renderer 无 Node 权限，Cookie、分享令牌与签名地址仅在后端处理。设置继承下载器配置；损坏文件不覆盖，本次有效设置可仅在会话使用并明确提示。

客户端离线测试：`python -m unittest tests.test_desktop_service -q`；构建后在 `desktop` 运行 `npm test`，包含浏览器中的受控交互检查和隔离配置的真实 Electron 启动检查。真实账号能力须与官方客户端对照，不将离线结果当作实盘验证。

第三版文件页支持新建目录、重命名、批量移动和移入回收站，右键可查看属性或复制路径。每次修改展示选中项与目标，后台核对当前对象，拒绝同名覆盖和移动到自身子目录，逐项反馈结果并刷新。分享文件只读，传输或登录期间不能修改网盘文件。

第四版增加文件/目录上传和持久传输中心。上传到当前网盘目录；传输中可浏览、搜索、追加下载或上传。任务页提供开始全部、暂停全部与清理已完成记录；每项使用开始/暂停、打开文件夹、删除记录三个图标按钮。删除记录先安全停止任务，不删除本地或网盘文件，保留引擎检查点。重启不会自动执行任务，恢复会核验登录上下文；分享任务重新输入提取码，任务记录不保存密码或授权令牌。目录上传拒绝无关同名目标，未知写入受理状态不自动重发。

当前免安装包版本为 **0.5.15**。已交付托盘与后台运行、拖拽上传、修改时间与排序小版，其余功能按用户要求暂缓，等待手动测试结果；详见现有客户端路线图与手动测试建议。

格式筛选使用固定下拉选项，支持全部格式、分类、无扩展名和自定义扩展名；可清除筛选恢复列表。Windows 标题栏与客户端统一跟随当前外观，保留系统最小化、最大化和关闭按钮。

第五版增加「我的分享」和「回收站」。勾选一个网盘文件或目录可创建分享，选择 1/7/30 天或永久有效期、四位提取码（留空为公开分享）；创建后核验所选根项目及分享参数，并支持复制文案。在「我的分享」可复制已有分享、确认取消选中链接，原网盘文件保留。分享浏览中的「永久转存到网盘」单独选择目标目录，明确确认后仅转存勾选成员并保留；下载仍使用临时转存并清理。

回收站支持完整分页、勾选恢复和永久删除选中记录，操作前展示名称与范围，逐项反馈并刷新；没有清空全部入口。恢复到原目录，同名不覆盖，原目录缺失或同 FID 有多个回收站记录时拒绝恢复。恢复接口参考开源客户端实现，尚未使用真实账号核验；服务端不支持或结果未知时明确反馈，写请求不自动重发。建议先用专用测试目录中的小文件实测。

本版输出在 `desktop/out/v0.5.15/`，完整解压运行包后启动 `QuarkDriveClient.exe`。在旧客户端运行时构建，可先设置 PowerShell `$env:QUARK_DESKTOP_OUT='out/v0.5.15'`；运行 Electron 测试时使用相同输出目录设置。此设置可避免覆盖正在运行的旧版本目录。启动新版前先退出旧客户端。

已完成下载的本地文件被移动或删除，且对应 part 不存在时，再次下载会从头获取当前远端文件。未完成任务若已确认的 part 丢失，仍拒绝伪造续传；本地文件存在时继续核验，不自动覆盖。

桌面创建目录分享不再反复遍历所有子文件，核验所选根 FID、名称/类型/大小、账号父目录归属、服务端完成与不完整标记、链接/期限/提取码；目录内部变化不会逐文件扫描。CLI 默认完整子树核验，受限 scope 不能使用根项目模式。创建对话框显示核对、提交、服务器生成、结果核验四个阶段，服务端实际耗时仍受网络与请求间隔影响。

复制、保存设置、文件操作、入队和失败等反馈使用顶部渐入提示，覆盖对话框上方；可手动关闭并自动消失，重复操作会重新显示。警告和失败单独呈现，不把未持久化设置提示成保存成功。

「我的分享」显示服务端 expired_at 对应的剩余小时，每秒本地更新并在窗口回到前台时校准；不足一小时、已过期和永久分别显示。鼠标悬停可查看到期日期时间。未提供有效到期时间时显示未知，不按创建日期加固定天数猜测；倒计时不产生额外服务器请求，刷新列表时重新读取服务端记录。

右键菜单第一小版：目录打开、下载、分享、剪切/粘贴、删除（移入回收站）、重命名、移动到、详细信息和复制路径；空白区域支持刷新、新建目录和粘贴。右键未勾选项会选择它，右键已勾选项保留多选。支持 Ctrl+X 剪切、Ctrl+V 粘贴、F2 重命名、Delete/Backspace 删除确认、Ctrl+Shift+C 复制路径、Ctrl+A 全选可见项、Esc 关闭菜单/取消剪切；输入框保留原生文字编辑快捷键。

剪切在当前后端内存中保存本次登录的选中项，不立即移动、不做目录请求，也不写入系统剪贴板。打开目标目录后粘贴，展示源项目与目标并确认；同名不覆盖，禁止移动到自身/子目录，同目录不重复移动。批次复用父目录与详情预检，一次提交多个有效 FID，提交后读取新的目标列表核验；未找到的成员单独定位。已成功项从剪切记录移除，已知失败项保留，未知结果阻止再次粘贴。登录开始时清除剪切记录，重启不保留；取消剪切只清除本地意图。

文件区左上方显示可点击路径，每段可直接跳转；分享路径在分享内导航。详情和列表大小统一使用 KB/MB/GB（按 1024 换算）。目录右键「设为常用文件夹」，侧栏可打开或移除；记录存于私有 `downloader/favorites.json`，不保存 Cookie。添加和移除是本地操作，打开时重新核实父目录链并更新改名后的名称。记录按当前登录凭证摘要隔离；凭证更新后可能不显示旧收藏，避免误用其他账号的 FID。失效目录明确报错，可手动移除。

交互式文件操作的请求间隔由 0.5 秒缩短为 0.1 秒，保留与普通浏览/传输共享的持久限流冷却。批量删除一次提交，批次内复用预检目录；所有写入后核验使用新数据，不扫描子目录。网络延迟、服务端任务和刷新耗时仍影响实际速度，模拟测试不能保证实盘低于一秒。

此小版完成后暂停，等待实测。多标签按用户要求不实现；云端文件复制和桌面快捷方式暂缓，隐私空间与发送到手机需核实接口。现有「复制路径」不等于云端文件复制。

文件区使用紧凑工具栏：上传合并为文件/目录下拉，新建目录保留；筛选按需展开，启用筛选时保留清除入口。上级/刷新使用带文字说明的图标，重命名、移动、删除、分享、详情、复制路径集中到右键菜单；鼠标悬停或键盘聚焦某行可使用「更多操作」。这些界面操作不增加网盘请求。

账号头像提供切换和退出。登录方式以[官方登录页](https://pan.quark.cn/)为准：夸克 App 扫码、手机验证码、微信登录；当前官方页面没有展示用户名/密码入口。手机与微信在独立的官方窗口输入或扫码，保留官方验证码、人机验证及绑定流程，由用户完成；窗口使用内存会话、沙箱、无 Node/无应用 preload，关闭后清理会话。主进程只收集夸克域的完整登录 Cookie，后端验证网盘访问后才保存，Cookie 不进入客户端 Renderer，密码/验证码不由本客户端收集或保存。

切换成功前保留原登录；取消或凭证验证失败不替换原账号。传输或文件写操作期间拒绝切换/退出。登录成功与退出均清除旧文件视图、搜索、分享句柄、剪切意图和任务有效性；旧任务仍可查看，不能直接恢复其他登录上下文的任务。退出属于本机退出，不撤销其他设备上的远端会话；会移除与 CLI 共用的本地扫码凭证，依赖它的 CLI 需重新登录。环境变量/外部 Cookie 文件不删除，私有 `downloader/session.json` 仅保存退出状态和凭证来源选择，确保重启不自动回退到环境中的旧账号；新登录优先采用本次保存的凭证。

本版已验证账号状态持久化、取消/失败/延迟结果、旧任务隔离和打包后的官方手机/微信窗口打开与关闭；真实验证码提交、微信确认/手机号绑定需本人手动测试。本轮不发送短信、不代替用户扫码登录，也不操作真实网盘文件。完成后暂停开发。

目录加载提示使用独立覆盖层，不占列表布局位置；加载时原列表和滚动区域保持稳定，旧行暂不可操作。提示延迟 120ms 渐入，快速返回减少闪现；尊重系统减少动画设置。读取失败保留原目录，成功进入不同目录从顶部显示，同目录刷新保留滚动位置。

设置页「外观」提供跟随系统、浅色、暗色三张预览卡，默认跟随系统；点击立即生效并自动保存，不必点击「保存设置」。外观保存在 Electron 用户数据目录的 `appearance.json`，无需登录，传输中也可切换；设置读取或保存失败时明确提示。Windows 主题变化由主进程统一更新内容区和原生标题栏按钮，避免系统主题判断不一致。官方登录网页由官方管理外观。

界面采用柔和灰白/深灰蓝、统一导航图标与按钮、细边线和可读的次要文字；主题覆盖文件列表、菜单、弹窗、任务与顶部提示，扫码图片保留白底。沿用固定行高和虚拟滚动，不增加网盘请求或列表切换动画。[Canva 三页设计参考](https://canva.link/ygzklbo063i4zor)用于浅色、暗色和设置页的视觉方向；参考来源包括 [Awwwards 简约设计集合](https://www.awwwards.com/websites/minimal/)，实际客户端仍按已验证的功能范围实现。

已验证三种主题、运行中主题事件、显式主题不随系统变化、重启保存、损坏/不可写设置反馈、传输中切换、860×600 窗口布局及真实打包 EXE 启动。未修改 Windows 全局外观设置；系统切换通过受控事件验证，请手动切换 Windows 浅色/暗色复测。本小版完成后暂停。

下载设置提供数字输入与滑块，双向联动。线程数保持 1–16；总限速滑块为 0–100 MiB/s，0 表示不限速，数字输入继续支持 0.01–10240 MiB/s，超过滑块范围的数值不会被自动改小。修改后点击「保存设置」，传输期间两种输入均禁用。

滑块获得焦点或拖动时不再显示包裹整条轨道的高亮框；普通数字输入仍保留焦点提示。当前界面、托盘和 Windows EXE 使用用户最新提供的白底彩色云盘图标。

「后台运行」可选择关闭窗口时最小化到托盘（默认）或安全退出，并开启/关闭批次完成通知（默认开启）；这些选项即时生效、自动保存到 Electron 用户数据目录的 `desktop.json`，与账号无关。托盘可恢复窗口、打开传输页、暂停全部、继续未完成和退出；继续任务沿用账号与分享授权核验规则。主进程复用任务事件更新菜单，进度更新不反复重建菜单、不轮询网盘。

完成通知使用 [Electron Windows 托盘通知](https://www.electronjs.org/docs/latest/api/tray#traydisplayballoonoptions)，适用于当前免安装包，不创建开始菜单快捷方式。只汇总本批次成功/失败数量，点击进入传输页；暂停、退出和关闭通知时不弹出，遵守系统静默设置。系统通知是否实际展示及点击效果需本人复测。

托盘「退出客户端」或设置中的退出按钮始终执行安全退出：界面显示等待提示、禁止新操作，结束后端输入并等待 Python 当前操作、传输检查点和分享清理完成后退出。排队项保存为已暂停，取消项仍保存为已取消；重启后不自动执行。不会按固定时限强杀后端，未返回的网络或写操作需要先结束；后端异常退出明确提示，重启时仍核验任务。用户提供的 Logo 已用于界面、托盘和 Windows EXE 图标。本小版交付后暂停。

文件页支持拖入本地文件、目录及混合多项。松开前显示当前上传目标；松开后只读取本地顶层信息，确认窗口锁定目标并展示名称、类型和总数，最多渲染前 50 项。点击「确认上传」后才加入现有上传队列；取消或 Esc 不会上传，迟到的预览结果会丢弃。传输中可追加，采用现有批次设置和同名冲突/停止续传规则；分享视图、搜索结果、目录读取中和未登录状态不接受拖拽上传，需进入明确的网盘目录。

拖拽路径通过预加载中的 [Electron webUtils.getPathForFile](https://www.electronjs.org/docs/latest/api/web-utils#webutilsgetpathforfilefile) 读取，并由主进程验证本地文件/目录；网页生成的 File 不作为本地来源。预览不扫描目录内容、不发送网盘请求；多个目录同时加入队列时，预检复用一次目标目录列表。实际上传仍逐项执行。

本版覆盖真实控件的拖入反馈、混合预览、取消/迟到结果、追加、固定目标、忙碌与只读门禁；打包 EXE 使用真实本地文件和目录验证原生拖拽路径桥接与失效文件反馈。网盘上传验收使用受控接口，未代用户进行实盘写入。请先在专用网盘目录中拖入一个小文件和一个测试目录，核对目标、取消、确认、后台追加及暂停续传。本版完成后暂停。

文件列表增加「修改时间」列，目录、搜索和分享统一使用已有列表的 `updated_at` 毫秒时间戳，通过 IPC 的 `modified_at` 传递。按电脑本地时间显示 `YYYY-MM-DD HH:mm`，缺失、无效或仅有创建时间时显示「未知」。点击表头先按最新在前，再点击按最早在前；保持目录优先，各类项目中未知时间始终在末尾，相同时间保留原顺序。时间排序仅作用于当前已加载列表，搜索新页到达时按当前顺序合并；不触发逐文件详情或额外网盘请求，保留复选框和虚拟滚动。

0.5.11 覆盖有效/无效时间、目录与搜索/分享的 IPC 元数据、请求数量、升降序、未知值、选中状态及最小窗口布局，并重新构建和启动打包客户端。真实账号的时间显示由本人实测，本小版交付后暂停。字段及显示约定参考[官方网页实现](https://g.alicdn.com/uc-cloud-drive-web-system/cloud-drive-web/4.6.7/index.js)。

窗口标题栏显示实际客户端版本号。拖入文件列表时，上传目标是当前路径对应的目录；拖到列表中的子目录行也不会改成该子目录，进入该子目录后再拖入才上传到其中。文件和目录混合拖入后，显示目标供确认；确认后加入现有上传队列。

若拖拽显示禁止标志，请先核对标题栏版本：0.5.9 没有拖拽功能；关闭窗口默认只是隐藏到托盘，旧进程仍在时新启动会恢复旧窗口。升级时先右键托盘选择「退出客户端」，等待退出后，完整解压当前包并运行其中的 `QuarkDriveClient.exe`，确认标题栏为 v0.5.15。原生打包测试覆盖实际文件拖到文件行/空白列表、当前目标、混合文件/目录和确认入队；网盘 RPC 使用受控回复，没有执行真实上传。

传输页分为「下载 / 上传」，每类再分「未完成 / 已完成」，显示实时数量并保留虚拟滚动。只有成功完成（done）归入已完成；等待、运行、失败、暂停和取消归入未完成，缺少方向的旧任务按下载显示。初次打开按最近任务方向选择分类，当前方向全部成功时显示已完成；手动选择不会随任务进度跳转。

「开始全部」将整个队列中可恢复的等待、失败、暂停任务加入队列，不重复提交已排队项；「暂停全部」暂停整个队列。「清理已完成记录」只删除成功完成的记录，跨上传/下载分类，传输中暂不可清理。单项开始/暂停不影响其他任务，打开文件夹指向上传源目录或下载目录；垃圾桶删除选中记录，运行项先显示安全停止，完成检查点及分享清理后移除，不删除本地或网盘文件。恢复仍核验账号与分享授权；分享失效任务使用开始按钮重新授权后开始。`tasks.pause(ids)` 暂停指定项，`tasks.remove(ids)` 删除指定记录，`tasks.clear(ids?)` 保留已有定向清理接口。

图标外围改为透明，界面 PNG、托盘和 Windows EXE 使用相同图形；16–256 像素 ICO 四角均验证为透明。若任务栏仍缓存旧图标，退出旧进程后使用新包启动；系统任务栏实际展示仍需本人复测。0.5.13 覆盖分类、状态变化、批量范围、旧账号门禁、千项虚拟列表、两种外观和图标资源，构建后暂停。

传输页新增进度条、已传输量/总量、近期速度、下载引擎实际使用连接数和预计剩余时间。运行中的百分比按已传输量计算，100% 仍需等待引擎完成核验及分享清理，任务成功以最终状态为准。暂停/失败/取消显示已确认量，隐藏速度和剩余时间；继续时清除旧实时快照，等待检查点核验后的进度事件。

目录使用既有扫描确认的总量显示整体进度，同时标明当前相对文件路径；扫描准备、临时转存、清理或总量未知时显示阶段/不定进度，不编造百分比与 ETA。空文件完成由最终结果确认，不除以零。剩余时间只在总量明确、近期速度为正且仍在传输时估算，不包含之后的校验/清理时间。

新增任务公开字段 `progress` 为本窗口会话内的结构化快照（名称、阶段、已传输/确认/总字节、速率、连接数和目录标记），旧任务或无进度事件时为 null。沿用现有事件推送和版本 1 私有任务记录，窗口与托盘都忽略旧 revision 的逐项事件，不增加网络请求、计时轮询或新的恢复账本；实时数值不持久化，重启后显示原结果文字，继续任务重新核验。0.5.14 覆盖上传/下载、目录、续传/停止、未知阶段、空文件、迟到事件和两种外观，完成构建后暂停供本人实测。


0.5.15 优化上传准备和分片间隔：首个分片完成授权后直接发送，后续 SHA-1 上下文按需计算，使用 Python/打包后端已有 OpenSSL 本地库加速，不新增二进制依赖；库不可用时沿用原算法。上传不再每个分片反复扫描整文件，仅检查本地大小、时间和文件身份，仍保留准备及合并/完成阶段完整哈希、分片 MD5/ETag、远端身份及未知写入状态保护。上传依旧逐文件、逐分片执行，不额外放宽请求冷却或账号限制。

准备时显示计算哈希、获取授权、分片上下文、核验或合并等实际阶段，首次准备使用不定进度而非持续 0%；已有字节进度在后续授权与核验阶段保留，不反复跳回不定状态。即刻进入准备并不表示免除服务端授权及整文件哈希；大文件或慢磁盘仍需相应处理时间。本机受控接口中 109 MiB 文件约 0.60 秒进入首个 PUT，1 MiB 上下文从约 0.89 秒缩短到 0.00042 秒；这些是本地/离线测量，不代表真实网盘速度。50 Mbps 上行理论上限约 6.25 MB/s（约 5.96 MiB/s），实际吞吐需本人同文件对照测试。

本版验证首段发送时机、完整扫描次数、OpenSSL/原算法一致性、同尺寸时间戳文件变化拒绝合并、暂停续传、单项暂停后队列继续、排队项恢复不重复、删除安全停止与迟到响应顺序，以及真实控件和无外部 Python 的打包窗口启动。构建后暂停，供本人实测。
