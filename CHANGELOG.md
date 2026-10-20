# Changelog

## 0.2.0 — 2026-10-20

- Incremental jobs reuse parent verification during mapping preflight, while checking each exact destination FID and retaining fresh mutation guards.
- Job reports include confirmed `saved_entries`, sanitized failure messages and `next_action`; cooldown errors expose `retry_after_seconds`.
- Add `quark --version` without requiring credentials.
- Prepare MIT licensing, package metadata, contributor/security documentation, an agent workflow guide and Windows/Linux CI.
- Isolate the test package to prevent unrelated installed `tests` packages from shadowing regression imports.

Earlier CLI development builds used version 0.1.0. Desktop and PotPlayer releases maintain independent version numbers.
