# Changelog

All notable changes to Nova Client are documented here.

Versions before 0.2.0 were never tagged; 0.2.0 was applied retroactively to the last
Python-only release, which is the code that had been running in production.

## [0.3.0] — 2026-08-05

Nova Client becomes multi-language. The Python client is unchanged in behaviour but has
moved; a PowerShell client is added for Windows nodes that would rather not install Python.

### Added

- **PowerShell client** (`powershell/`), Windows only, in two variants:
  - `NovaClient-WinPS5.ps1` for Windows PowerShell 5.1 — needs nothing installed.
  - `NovaClient-PS7.ps1` for PowerShell 7+.

  Both are self-contained single files at full parity with the Python client: one-shot sync,
  continuous daemon with independent sync and maintenance schedules, game maintenance, and
  configuration validation. They are byte-identical below their `HTTP TRANSPORT` section; a
  test enforces that.
- `config.psd1` configuration format for the PowerShell client — a PowerShell data file, read
  with `Import-PowerShellDataFile` so it cannot execute code. Single-quoted strings mean
  Windows paths need no backslash escaping.
- `Install-NovaClientTask.ps1` — registers the daemon as a Windows Scheduled Task that starts
  at boot, restarts on failure, and has no execution time limit.
- Pester test suite (`powershell/tests/`) that drives the real scripts against the existing
  Python mock hub, so both implementations are held to one definition of the API.
- `linux/nova-client.service` — a systemd unit for the Python daemon.
- `VERSION`, `python/_version.py` and `$Script:NovaClientVersion`, all reporting `0.3.0` and
  feeding a `User-Agent` (`nova-client-py/0.3.0`, `nova-client-ps/0.3.0`) so the hub's logs
  identify which implementation a node runs.
- CI now lints the PowerShell with PSScriptAnalyzer and runs Pester alongside pytest.

### Changed

- **Repository restructured.** The Python client moved from the repository root into
  `python/`; `sync_and_process.sh.example` moved into `linux/`. See the upgrade notes in
  [README.md](README.md) — deployments must update their working directory.
- The Python client now sets an explicit HTTP timeout (`sync.timeout_seconds`, default 120)
  instead of inheriting aiohttp's five-minute default.
- `.gitignore`: `outound/` was a typo and never matched `outbound/`. Also now ignores
  `config_*.toml`, `config.psd1`, `sent/`, `logs/` and `*.part`.

### Removed

- `run_nova_cycle.ps1`, an uncommitted pre-daemon PowerShell orchestrator. Superseded by
  daemon mode, and it assigned to `$matches` — a variable PowerShell's `-match` operator
  silently overwrites — so its config parsing read the wrong capture group.

### PowerShell client differences from the Python client

These are deliberate improvements, not translation drift:

- **Atomic downloads.** Packets are written to `<name>.part` and renamed into place only once
  complete. The Python client writes directly, so a crash mid-write can leave a truncated
  packet that the game then treats as real data.
- **Token caching and refresh.** The token is reused across sync cycles and refreshed before
  expiry, or once on a `401`. The Python daemon re-authenticates every cycle.
- **Retries cover more.** Transport failures, `5xx` and `429` are retried; `4xx` are not,
  because they are configuration errors that will fail identically forever. The Python client
  retries only transport exceptions, so a `502` from a restarting hub fails the whole packet.
- **Single-instance guard.** A named mutex per config file. Two Python daemons on one config
  will both claim the same packets and both run the game in the same folder.
- **Process-tree kill on maintenance timeout** (`taskkill /T /F`). Killing only `cmd.exe`
  leaves the game running with its data files open.

## [0.2.0] — 2026-02-01

Retroactive tag for the last Python-only release. `main` had never been updated to include
any of this — it was all sitting on `dev` while production ran it.

### Added

- **Daemon mode** (`daemon.py`) — continuous operation with independent sync and maintenance
  schedules, immediate maintenance on packet arrival, and graceful shutdown.
- **Game runner** (`game_runner.py`) — BRE/FE execution, native on Windows and via dosemu on
  Linux, with per-game-folder locking so two runs cannot overlap in one directory.
- Mock hub server and integration test suite; unit tests and the Gitea Actions workflow.
- Configuration validator (`validator.py`) — checks directories, cross-checks `nodes.dat`
  against the configured BBS index and name, and flags directories shared between leagues.

### Fixed

- **Path traversal and filename injection** — `sanitize_filename()` is now applied at every
  point where a filename becomes part of a path or a URL, including filenames returned by the
  server.
- `BRNODES.xxx` / `FENODES.xxx` added to the filename allowlist; nodelist downloads had been
  failing validation.
- Windows game execution: `CREATE_NO_WINDOW` removed and the call routed through the shell.
  DOS console programs need a real console; without one they fail with `WinError 87`.
- Streaming upload with a 10 MiB limit — packets are no longer read wholly into memory.

## [0.1.0] — 2026-01-11

Initial release. One-shot sync, OAuth2 client credentials, multi-league support.
