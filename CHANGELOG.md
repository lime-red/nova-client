# Changelog

All notable changes to Nova Client are documented here.

Versions before 0.2.0 were never tagged; 0.2.0 was applied retroactively to the last
Python-only release, which is the code that had been running in production.

## [0.3.0] — 2026-08-06

Nova Client becomes multi-language. The Python client is unchanged in behaviour but has
moved; a PowerShell client is added for Windows nodes that would rather not install Python.

### Added

- **PowerShell client** (`powershell/`), Windows only, in two variants:
  - `NovaClient-WinPS5.ps1` for Windows PowerShell 5.1 — needs nothing installed.
  - `NovaClient-PS7.ps1` for PowerShell 7+.

  Full parity with the Python client: one-shot sync, continuous daemon with independent sync
  and maintenance schedules, game maintenance, and configuration validation.

  Each of those files holds only its own HTTP layer — the one place 5.1 and 7 genuinely
  differ. Everything else lives once in `NovaClient.Common.ps1`, dot-sourced by both, so
  **the whole `powershell/` folder must be deployed together**; a launcher alone exits 2 with
  an explanation. `NovaClient.Common.ps1` is held to Windows PowerShell 5.1 syntax, enforced
  in CI by PSScriptAnalyzer's `PSUseCompatibleSyntax`; the file documents how to fork it if
  that ever stops being possible.
- **Nodelist handling (PowerShell).** The client no longer polls `GET .../nodelist` on every
  sync. The hub queues a regenerated nodelist as an ordinary packet addressed to each member
  BBS, so it already arrives through the normal unread-packet flow — which is the point of
  that design, since a nodelist changes once or twice a year. New `Sync.NodelistCheck`:

  - `'bootstrap'` (default) — fetch directly only when this node has no nodelist at all, so
    a newly registered node is not blind until the next hub-side regeneration.
  - `'always'` — poll every sync. Sends `If-None-Match` and expects a `304`; against a hub
    without conditional support it falls back to comparing bytes, so an unchanged nodelist is
    still never rewritten and never logged at INFO. The ETag lives in `nodelist-etags.json`
    beside `metrics.json`, deliberately not in the game folder.
  - `'never'` — rely purely on the packet queue.
- **Fixed: the daemon re-authenticated and reprinted its banner every cycle (PowerShell).**
  `Invoke-NovaSync` passed `-Force` to the token fetch, so the cache existed but nothing ever
  hit it — a token good for 24 hours was refetched every two minutes, exactly the behaviour
  the cache was added to avoid. The startup banner lived in the sync pass too, so a daemon
  reintroduced itself every cycle and the log read like a series of restarts. The banner now
  prints once per process.
- **Fixed: `-Daemon` threw on every cycle before maintenance had run once (PowerShell).** The
  sleep calculation used `[Math]::Max(1, $wait)`, and until `$lastMaintenance` was set it was
  about -6.4e10 — correct, meaning "long overdue", but the literal `1` selected the `Int32`
  overload and the conversion threw. The daemon's own error handling caught it, so packets kept
  moving and only an error line every cycle gave it away. Clamped with plain comparisons now,
  keeping everything `[double]` until one cast at the end.
- **Fixed: nodes.dat entries with `HOST` routing were rejected (Python) or silently dropped
  (PowerShell).** The index line may read `1 HOST 2 3 4`, which is how the hub writes its own
  entry — so every real nodelist has one. `nova-hub`'s parser has always handled it; neither
  client did. Python failed the whole file, blocking `--validate` on a live node; PowerShell
  skipped the entry with no message at all, which is why it looked fine. Both now take the
  first token, and Python records the routing targets like the hub does.
- **Fixed: Linux maintenance never ran — `script(1)` died before dosemu started (Python).**
  `dosemu_config_dir` and `log_dir` default to `./...`, relative to the daemon's working
  directory, but the subprocess runs with `cwd=game_folder` because BRE/FE must start from
  their own directory. So `script` looked for `logs/` *inside the game folder*, failed with
  `cannot open logs/...: No such file or directory`, and exited 1 — instantly, and with no log
  to explain it. All three paths are resolved to absolute now.
- **Maintenance failures now say why (Python).** `script(1)`'s output was captured and then
  discarded, so a failed run reported only `Exit code: 1` — with no log to consult, because
  being unable to write the log was often the failure itself. The error now carries script's
  own stderr (or the tail of the transcript), names the transcript path, and flags a
  suspiciously small transcript as dosemu never having booted. Collapsed to a single line,
  since journald splits on newlines. The daemon already forwards this to the journal.
- **UNC path rejection (PowerShell).** `-Validate` now fails a `GameFolder` under `\\server\share`
  and warns for inbound/outbound directories. BRE and FE are DOS programs and DOS has no
  concept of UNC, so the game cannot run from one however happily PowerShell reads it. This
  also corrects the Scheduled Task guidance, which previously suggested UNC as the workaround
  for SYSTEM not seeing mapped drives — the fix is to run the task as an account that has the
  drive mapped, or to keep the game local.
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
- **Token caching and refresh.** The token is reused across sync cycles and refreshed a minute
  before expiry, or once on a `401`. The Python daemon re-authenticates every cycle.
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
