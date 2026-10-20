# Changelog

## 0.2.0 — 2026-10-20

- Incremental jobs reuse parent verification during mapping preflight, while checking each exact destination FID and retaining fresh mutation guards.
- Job reports include confirmed `saved_entries`, sanitized failure messages and `next_action`; cooldown errors expose `retry_after_seconds`.
- Add `quark --version` without requiring credentials.
- Prepare MIT licensing, package metadata, contributor/security documentation, an agent workflow guide and Windows/Linux CI.
- Isolate the test package to prevent unrelated installed `tests` packages from shadowing regression imports.
- Assign the current user as owner of newly created private Windows directories, including under elevated tokens; existing directory ownership is not changed implicitly.

Earlier CLI development builds used version 0.1.0. Desktop and PotPlayer releases maintain independent version numbers.

## PotPlayer 0.12.10

- Restore playback progress replies when the player and bridge run at different elevation levels.
- Retain legacy private-state ownership recovery, service discovery after upgrades and window lifecycle fixes.
- Release asset: `QuarkPotPlayer-Setup.exe`, distributed under tag `potplayer-v0.12.10`.

## Desktop 0.5.29

- Reduce repeated download preparation requests while retaining exact identity, scope and resume checks.
- Add per-share revoke controls and refresh file views after upload and other file operations.
- Retain the single-file Chinese Windows installation wizard.
- Release asset: `QuarkDriveClient-0.5.29-Windows-x64-Setup.exe`, distributed under tag `desktop-v0.5.29`.
