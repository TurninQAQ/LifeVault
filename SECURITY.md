# Security Policy

## Supported Release

Security fixes are applied to the current `1.x` release line. LifeVault v1.0 is a local, single-user, POSIX application; it is not an authenticated network service.

## Security Boundary

- `lifevault serve` only accepts loopback addresses. Do not expose Streamlit through a reverse proxy or public bind without adding a separate authentication and TLS design.
- The model produces candidates only. Record IDs, versions, confirmation flags, idempotency keys, database writes, reminder delivery, archive/restore, and backup authority remain in deterministic code.
- MCP is scoped to the configured local user and exposes no physical deletion, desktop notification, backup password, file-path, or database replacement tool.
- Active SQLite databases are plaintext local files protected by operating-system permissions. `.lvbackup` files are separately authenticated and encrypted with a mandatory password.
- Backup passwords are never accepted from command arguments, environment variables, config, MCP, or audit logs. Losing the password permanently loses access to that backup.
- Audit summaries use action-specific allowlists and exclude raw text, titles, notes, messages, search terms, idempotency keys, passwords, and exception details.

## Threats Not Covered

LifeVault does not protect against a compromised operating-system account, malware with access to the user's files or process memory, screen capture, a malicious Python dependency, or a weak backup password. It does not provide multi-user authorization, remote synchronization, cloud key management, or secure physical deletion.

## Reporting

Use GitHub private vulnerability reporting for the repository when available. Do not attach a real vault, backup password, database, raw personal text, or other secrets. Include the LifeVault version, operating system, reproduction steps using synthetic data, and the affected security boundary.
