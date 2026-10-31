# Changelog

All notable changes to Nova Client are documented here.

Versions before 0.2.0 were never tagged; 0.2.0 was applied retroactively to the last
Python-only release, which is the code that had been running in production.

## [Unreleased]

### Added
- **The daemon ages out its game logs** (Python). Every game run writes a log
  of its own in `log_dir`, which nothing ever deleted: eclipse had 27,816 of them
  (163 MB) after three months. Logs older than `daemon.log_days` (default 30; 0
  keeps them all) are deleted after maintenance, checked at most once an hour.
  Only a log this client wrote for a league in its config is touched; a league
  dropped from the config keeps its logs. Files it can't delete are reported in
  one warning. The PowerShell client writes no per-run logs, so has nothing to
  prune.

## [0.6.0] — 2026-10-30

Immortal Barons leagues, both clients. Needs nova-hub 0.10.0 or later for an IB league;
BRE and FE leagues work against any hub they did before.

### Upgrading

Nothing to do for BRE and FE leagues: no dependency, config or unit changes. An IB league
needs its `[leagues.IB.<n>]` section (see `config.toml.example`) and, usually,
`run_maintenance_on_download = false` in it. Check the config with `daemon.py --validate`.

### Added

- **Immortal Barons** (`[leagues.IB.<n>]`, `Game = 'IB'`), nova-hub #100:
  - Its packets, `L555-2-<seq>-1.brp`, upload and download beside BRE and FE. A broadcast
    (destination 0) is uploaded once; the hub hands it to every board.
  - The hub's roster, `IBNODES.<n>`, is installed as `ibnodes.dat` in the game folder, IB's
    data directory: IB reads it there and won't exchange packets without one. Only where
    there is none: IB keeps the file current itself from the Coordinator's signed roster,
    so an existing one is left alone (from andy5995's lime-red/nova-client#1).
  - A `.brp` in outbound that is not named as this board's packet gets a warning: IB names
    one without node numbers until it has a roster (also from andy5995's #1).
  - The League Coordinator's public key, which IB needs before it accepts the league's
    signed rules and roster, is fetched from the hub (`GET .../coordinator-key`) and
    recorded as `coord.pub` in the game folder, as `immortal-barons -coord-key` would. Only
    where there is none: an existing key is never replaced. Validation warns until it is there.
  - IB never reuses a packet name, so one already in inbound is never replaced (the same
    bytes count as delivered; different bytes are kept and logged), and IB packets in the
    sent archive are deleted after `sync.archive_days` / `Sync.ArchiveDays` (default 30;
    0 keeps them).
  - Maintenance runs IB directly, on Linux too: `runner = "native"`, default arguments
    `-maint -data <game_folder>`. A non-zero exit, as IB gives after a new transport fault,
    fails the run with IB's last lines; the Linux log is in `log_dir`.
  - Validation knows IB: `ibnodes.dat` with its optional key line, no HOST-line warning,
    and a `game_command` that has to exist.
  - `run_maintenance_on_download` / `RunMaintenanceOnDownload` can be set per league,
    overriding `[daemon]`'s / `Daemon`'s. IB sends packets on every run and the hub answers each upload, so an IB
    league that runs on every download keeps a packet going round with the hub every
    sync; set it to `false` there and its packets wait for the scheduled run. Leagues
    left out of a download's run keep their schedule, which such a run no longer resets.

### Changed

- `openapi/service-api.json` is regenerated from nova-hub v0.10.0, and the mock hub follows it: league ids ending `I`, IB names, a row per
  board for a broadcast, 409 for a different packet under a used name, and `expires_in`.

## [0.5.0] — 2026-10-28

Linux nodes whose BBS runs its games from a redirected DOS drive can now run maintenance.

### Upgrading

Nothing to do unless you need the new setting: no dependency, config or unit changes. On
Linux, rebuild nothing; check the config with `daemon.py --validate` after pulling, as usual.

### Added

- `[daemon] dosemu_drives` (Linux): DOS drives mapped with lredir for maintenance runs, for
  games set up on a redirected drive such as an `M:` holding the BBS tree. Their config files
  keep those drive paths, so PLANETARY run from `drive_c` alone can't find its own files.
  `--validate` checks the letters and that each directory exists.

## [0.4.0] — 2026-10-26

The review release. Downloads survive a 502 or a full disk, a daemon keeps one token and
shares nothing with a hand-run client, a hung game is killed whole, and the PowerShell
client's first nodelist reaches the game. Both daemons now say when a game leaves packets
unprocessed in inbound.

### Upgrading

**Python dependencies come from the lock.** aiohttp moves to 3.14.3. Python itself doesn't
change: 3.12 or later, as before. From the repo root, with the daemon stopped:

```bash
mv .venv .venv-0.3.0                # keep it for a rollback
uv sync --no-dev --frozen           # or: pip install -r python/requirements.txt
cd python && ../.venv/bin/python daemon.py --validate --config config.toml
```

Keep the old venv until the new one has run for a while. To go back, put it back. Rebuilding
0.3.0's from its `requirements.txt` asks for aiohttp 3.10.0, which has no wheels for Python
3.13 and fails to build without a compiler.

Validate before restarting. An unknown `sync.sent_action` and a malformed `bbs_index` are now
refused at start, and a game command containing a character the shell would act on is not run.

Stop the daemon between maintenance runs. Its stop timeout is shorter than a PLANETARY run.

The daemon now keeps a lock file beside its config (`config.toml.lock`), so the account it runs
as must be able to write that folder. An installed systemd unit needs no change; the one in
`linux/` assumes `/opt/nova-client` and a `nova` user, so don't copy it over a working one.

**PowerShell:** pull and restart. Re-running `Install-NovaClientTask.ps1` is optional; it now
warns if ordinary users can change what the SYSTEM task runs.

### Fixed (PowerShell client)

- **The bootstrap nodelist went where the game never looked.** A node with no nodelist fetched
  `BRNODES.<league>` into the game folder, then counted that file as having one, so the game
  never took it in and it was never fetched again. It now goes in the inbound folder, where a
  nodelist the hub sends as a packet lands. A node counts as having a nodelist when the game's
  `brnodes.dat` / `fenodes.dat` exists, or one is waiting in inbound.
- **On a FIPS-mode Windows host, the client could not start.** The single-instance check
  hashed the config path with MD5, which .NET Framework refuses there. It uses SHA256.
- **`-Once` could run alongside the scheduled task.** The task, as SYSTEM, creates its mutex
  with security an ordinary account cannot open, and the client then fell back to a mutex of
  its own. It now says another instance is running under another account, and exits 3.
- **Windows PowerShell 5.1 turned TLS 1.3 off and TLS 1.1 on.** Where .NET leaves the choice
  to Windows, so does the client; otherwise TLS 1.2 is added and SSL 3, TLS 1.0 and 1.1 are
  removed.
- `-Validate` read the nodes file by blank lines, so a blank city split an entry in two; it now
  reads fixed six-line records, as the hub and the Python client do.
- A maintenance run that failed was not counted in the daemon's errors.
- A packet name with a trailing newline passed the name check.

### Added (PowerShell client)

- `-Validate` warns about a nodes-file index line that is not a number, and about nodes no
  `HOST` line routes through the hub (#19).
- `GameCommand` and `MaintenanceArgs` holding `& | < > ^ % "` or a line break are refused, by
  `-Validate` and by maintenance.
- After the maintenance that new packets trigger, the daemon uploads the replies at once.
- `Install-NovaClientTask.ps1` warns when the task runs as SYSTEM and Everyone, Users,
  Authenticated Users or INTERACTIVE can change the launcher, the shared script, the config or
  a game folder: any of them could run code as SYSTEM.

### Fixed (Python client)

- **A download interrupted by a 502, 503 or a full disk could lose the packet.** The hub
  counts a packet delivered once it has sent it, but the client gave up on any non-200 reply
  at once and wrote straight into the inbound directory. Requests now retry 408, 429 (waiting
  the hub's `Retry-After`) and 5xx as well as dropped connections, up to `sync.max_retries`.
  Packets are written to `.part` and renamed into place, and a reply over 10 MB is refused.
- **The daemon fetched a new token every sync cycle** (#20). It now keeps one until the hub's
  `expires_in` is near or the hub refuses it, and a refused token is replaced and the request
  retried once, in `client.py` too.
- **Archiving a sent packet whose name was already in `archive_dir` crashed the sync on
  Windows**, before it downloaded anything, and every later sync sent the packet again.
  Sequence numbers wrap and restart after a game reset, so this was a matter of time. The
  archive is overwritten, a different drive works, and a failure to tidy up a sent packet is
  logged without stopping the sync.
- **On Windows, a maintenance timeout could hang the daemon for good** (#22). Killing
  `cmd.exe` left the game running with the output pipe open, and Python waited on it. The
  whole process tree is killed. A game folder with a space in it is quoted.
- **On Linux, a timeout or shutdown left dosemu running.** script(1) puts dosemu in a session
  of its own, so killing script never reached it. The whole tree is killed now.
- `[leagues.bre.555]` was treated as Falcon's Eye, and `bbs_index = true` as BBS 1. A
  trailing slash on `hub.url` made every request 404, and an empty `HUB_CLIENT_ID` in the
  environment overrode the config. A packet name with a trailing newline passed the name
  check.
- `python validator.py`, as the README says to run it, did nothing and exited 0.

### Added (Python client)

- `client.py` and the daemon take a lock beside the config (`config.toml.lock`), so a
  hand-run sync cannot run alongside the daemon's. The daemon waits; `client.py` exits 1.
- After the maintenance that new packets trigger, the daemon uploads the replies at once
  rather than at the next sync.
- `daemon.enabled = false` is honoured: the daemon starts and waits, without syncing.
  (Exiting would only have been restarted by systemd.)
- The validator checks `[hub]`, `sync.sent_action`, the game type and `bbs_index`, as
  `client.py` does. It warns about nodes no `HOST` line routes through the hub (#19). It
  refuses game commands, arguments or DOS paths with `& | < > ^ % "` or a line break, and so
  does the daemon.

### Changed (Python client)

- A missing `brnodes.dat` / `fenodes.dat` is a warning rather than an error, as it is in the
  PowerShell client. A new node has none until it has processed the hub's first nodelist, and
  the error stopped the systemd unit starting, so it never fetched one.
- An unknown `sync.sent_action` is refused at start. It used to leave every packet in
  outbound, to be sent again each sync.
- **aiohttp 3.10.0 → 3.14.3**, the version the hub runs. 3.10.0 has no wheels for Python
  3.13 or later, so pip there tried to build it from source and failed without a compiler.
  It also picks up the aiohttp security fixes since 3.10.0. A pip install gets it from
  `python/requirements.txt`; `uv sync --no-dev` from `uv.lock`. (#27)

### Changed

- **Python dependencies are locked with uv.** `pyproject.toml` at the repo root lists
  them, `uv.lock` holds the exact versions, and `uv sync --no-dev` builds `.venv` at the
  repo root, where `linux/nova-client.service` already expected it. `python/requirements.txt`
  is now exported from the lock with every indirect dependency pinned, so a pip install
  gets exactly what CI tested. `python/requirements-dev.txt` is gone: the test
  dependencies are the lock's dev group (`uv sync`). No version changed.

### Added

- **What maintenance left in inbound is reported** (both clients, #15). After each maintenance
  run the daemon lists any packet or nodelist still in a league's inbound, which the game
  should have taken in, as a WARNING naming the files. It also writes them to metrics.json as
  `unprocessed` (by league) and `total_unprocessed`. A game that can't see its inbound folder,
  or won't read a packet, used to show nothing at all. A `-Once` / one-shot sync runs no
  maintenance and leaves both fields out.
- **Connection test**: `client.py --test-connection` and `-TestConnection`. Signs in to the
  hub, says "Authenticated Successfully" or why not (unreachable, wrong ID or secret, rate
  limited), then compares each league and BBS index in the config with what the hub has for
  this BBS (the hub's new `GET /service/api/v1/me`). A wrong index or a league the BBS is not
  in fails; a league missing from the config, or a `bbs.name` that differs from the hub's,
  warns. Syncs nothing. Against an older hub without `/me` it signs in and skips the league
  check.
- MIT licence, and `tools/release.sh` to publish a tag's CHANGELOG section as a release.
- `openapi/service-api.json`, the hub's Service API spec, generated by the hub's
  `export_openapi.py`. CI checks the mock hub both client suites run against has the same
  routes and returns bodies of the shape the spec gives, so a mock that drifts from the hub
  fails here rather than on a live node.

### Changed

- A config with no leagues is valid: a BBS that has claimed its credentials but is not in a
  league yet can sign in. `--validate` and `-Validate` warn instead of failing, and a sync
  signs in, logs "No leagues configured; nothing to sync" and exits 0.
- A successful sign-in logs "Authenticated Successfully" (was "OAuth token obtained").
- Upgrade steps live in the release notes (CHANGELOG.md and the Releases page) instead of
  the README. The 0.3.0 steps moved into its entry.
- The systemd unit and `linux/README.md` assume a checkout at `/opt/nova-client`, run as a
  user called `nova`, instead of one person's home directory. Adjust both to your install.

### Fixed

- Python: a config with no `[leagues]` section crashed with a KeyError straight after
  signing in.

## [0.3.0] — 2026-08-06

Nova Client becomes multi-language. The Python client is unchanged in behaviour but has
moved; a PowerShell client is added for Windows nodes that would rather not install Python.

### Upgrading

**The Python client moved into `python/`.** Nothing about how it works changed, but paths did.
After pulling this release, update wherever you launch it from:

| | Before | After |
|---|---|---|
| systemd | `WorkingDirectory=/opt/nova-client` | `WorkingDirectory=/opt/nova-client/python` |
| Windows | `cd C:\BBS\nova-client` | `cd C:\BBS\nova-client\python` |
| CI / scripts | `pytest` at the repo root | `cd python && pytest` |
| Config | `config.toml` at the repo root | `python/config.toml` |

The paths assume a checkout at `/opt/nova-client`; use wherever yours is.

Move your config with the code — it belongs next to the client that reads it, which is where
`client.py --validate` looks by default:

```bash
mv /opt/nova-client/config.toml /opt/nova-client/python/
```

[`linux/nova-client.service`](linux/nova-client.service) still passes it by absolute path, so a
wrong `WorkingDirectory` fails loudly rather than quietly loading a different file.

Pull, update the path, restart, in that order. A node that pulls while its service is running
will keep working until the next restart, but do not leave it in that state.

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
  `python/`; `sync_and_process.sh.example` moved into `linux/`. Deployments must update
  their working directory: see **Upgrading** below.
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
