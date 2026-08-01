# Changelog

All notable changes to LifeVault are documented here.

## [1.0.1] - 2026-08-01

### Fixed

- Recognize purchase return windows written as `N 天内可以退货`, `N 天以内可退货`, or `N 天支持退货` instead of incorrectly interrupting for a missing `return_deadline`.

### Verified

- Added the exact web-demo report as a unit and extraction-evaluation regression case; fallback extraction passes 73/73 cases and 460/460 expected fields.

## [1.0.0] - 2026-08-01

### Added

- Natural-language creation, search, controlled update, status change, archive, and restore for purchase, subscription, and bill records.
- Persisted LangGraph Human-in-the-loop flows for missing fields, duplicate review, record review, reminder review, target selection, and update confirmation.
- A 21-tool Personal Vault MCP boundary with confirmation, idempotency, optimistic locking, user scope, privacy-filtered audit, and real stdio smoke coverage.
- Deterministic return, warranty, renewal, bill due-date, reminder, quiet-hours, snooze, and recurring-subscription calculations.
- Streamlit record, reminder, audit, preference, archive, and encrypted backup/restore management.
- Mandatory-password full-vault `.lvbackup` snapshots using scrypt and AES-256-GCM, strict archive/schema validation, pre-restore safety backup, dual-database recovery journal, generation reload, and Worker pause.
- Installable wheel, packaged Skills/evaluation data, `lifevault` and `lifevault-mcp` entry points, supervised `lifevault serve`, and read-only `lifevault doctor` diagnostics.

### Verified

- Fallback and local Qwen creation evaluation: 72/72 cases and 448/448 expected fields.
- Fallback and local Qwen natural-update evaluation: 36/36 cases and 87/87 expected fields.
- Fresh dependency installation, wheel execution outside the repository, MCP stdio smoke, Streamlit health, desktop/mobile browser rendering, crash recovery, and complete automated regression suite.

### Compatibility

- Python 3.10 or newer on POSIX systems with `fcntl.flock`.
- Existing v0.18/v0.19 schema-v1 vaults, checkpoints, and `.lvbackup` files remain supported.
- OCR/PDF import, cloud sync, remote access, multi-user authorization, PostgreSQL, external payment/cancellation, physical deletion, and scheduled/cloud/incremental backup remain outside the v1.0 local MVP.
