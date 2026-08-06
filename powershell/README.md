# Nova Client — PowerShell

A Windows-only reference client for the Nova Hub Service API. No Python, no runtime install,
no build step: copy the folder onto the BBS machine and run it.

## Which script?

| Script | Requires | Use when |
|---|---|---|
| `NovaClient-WinPS5.ps1` | Windows PowerShell 5.1 | **Default.** 5.1 ships with every Windows since 7, so this needs nothing installed. |
| `NovaClient-PS7.ps1` | PowerShell 7.0+ | You already have PowerShell 7 and prefer it. |

They are the same client. Each of those two files is a launcher holding nothing but its own
HTTP layer — that is the only part that genuinely differs, because 5.1 turns every non-2xx
response into a terminating exception whose body has to be dug out of a response stream,
while 7 can just hand you the status code. Everything else — sync, daemon, game maintenance,
validation — lives once in **`NovaClient.Common.ps1`**, which both dot-source.

So: **copy the whole `powershell/` folder, not one script.** A launcher on its own exits 2
and tells you what is missing.

`NovaClient.Common.ps1` must stay runnable on Windows PowerShell 5.1, the lowest version any
launcher supports. CI enforces that with PSScriptAnalyzer's `PSUseCompatibleSyntax` rule, so
a `??` or a ternary that slips in fails the build rather than the BBS. If the shared code ever
genuinely needs something 5.1 cannot do, fork it rather than adding version probes — the
procedure is written at the top of the file.

## Setup

```powershell
Copy-Item config.psd1.example config.psd1
notepad config.psd1
```

You need from the hub sysop: the hub URL, a **client ID** and **secret**, and your **BBS index**
in each league (1–255, and it can differ per league).

Then, in this order:

```powershell
.\NovaClient-WinPS5.ps1 -Validate         # no network calls; checks config and nodes.dat
.\NovaClient-WinPS5.ps1 -Once -Verbose    # a single sync, showing everything
.\NovaClient-WinPS5.ps1 -Daemon           # run continuously until Ctrl+C
```

If PowerShell refuses to run the script at all, it is the execution policy, not the script:

```powershell
powershell -ExecutionPolicy Bypass -File .\NovaClient-WinPS5.ps1 -Validate
```

## Options

| Option | Meaning |
|---|---|
| `-Config <path>` | Config file. Defaults to `config.psd1` beside the script. |
| `-Validate` | Check the config and exit. Makes no network calls. |
| `-Once` | One sync, then exit. The default if no mode is given. |
| `-Daemon` | Run continuously. |
| `-Verbose` | Show DEBUG lines and full API responses. |
| `-ShowVersion` | Print the version and exit. |

Exit codes: `0` success · `1` errors occurred · `2` invalid config · `3` another instance is
already running · `4` interrupted.

## Configuration

`config.psd1` is a PowerShell **data** file. It holds data only and cannot execute code, so
it is read with `Import-PowerShellDataFile` rather than being dot-sourced.

Use **single quotes** for paths. In a single-quoted PowerShell string a backslash is just a
backslash:

```powershell
GameFolder = 'C:\BBS\DOORS\BRE_015'      # right
GameFolder = "C:\\BBS\\DOORS\\BRE_015"   # unnecessary — this is not TOML or JSON
```

See `config.psd1.example`, which documents every key. `config.psd1` is gitignored.

Credentials can come from the environment instead of the file, which is better if other
people can read the disk:

```powershell
$env:HUB_CLIENT_ID     = '...'
$env:HUB_CLIENT_SECRET = '...'
```

Environment variables win over the file.

### Nodelists

You do not need to do anything to keep your nodelist current, and the client does not poll for
it. When the hub regenerates a league's nodelist it queues it as an ordinary packet addressed
to every member BBS, so it arrives with your normal game traffic. A nodelist changes once or
twice a year; polling for that would be pure waste.

The one gap is a node that has *never* had a nodelist — freshly registered, or a rebuilt game
directory — which would otherwise sit blind until the next regeneration. So `Sync.NodelistCheck`
defaults to `'bootstrap'`: ask the hub directly only when there is no local nodelist at all.
Set it to `'always'` to poll every sync anyway, or `'never'` to rely purely on the packet queue.

## Running unattended

```powershell
# from an elevated prompt
.\Install-NovaClientTask.ps1 -WhatIf      # show what it would do
.\Install-NovaClientTask.ps1
Start-ScheduledTask -TaskName NovaClient
```

This registers a Scheduled Task that starts at boot, restarts on failure, and has no
execution time limit (without that last part Windows silently kills it after three days).

It runs as `SYSTEM` by default. **If your game directories are on a mapped drive, that will
not work** — SYSTEM cannot see per-user drive mappings. Install the task under an account
that does have the drive mapped:

```powershell
.\Install-NovaClientTask.ps1 -User 'MYBBS\sysop'
```

**Do not reach for a UNC path to work around this.** BRE and FE are DOS programs, and DOS has
no concept of UNC — `\\server\share` cannot be the game's working directory, no matter how
happily PowerShell reads it. `-Validate` rejects a UNC `GameFolder` outright for that reason,
and warns about a UNC inbound/outbound directory. Map a drive letter, or keep the game local.

To remove it: `Unregister-ScheduledTask -TaskName NovaClient -Confirm:$false`

## Running the game

In `-Daemon` mode the client runs e.g. `BRE PLANETARY` in the game folder, either on
`MaintenanceIntervalSeconds` or immediately when packets arrive (`RunMaintenanceOnDownload`).

The game is launched through `cmd.exe` **with its own console window**, and its output is not
redirected. That looks wasteful and it is deliberate: BRE and FE are DOS-era console programs
that need a real console. Suppressing the window or redirecting stdio makes them fail with
`WinError 87` — the Python client spent three commits rediscovering this. The console flash
during a maintenance run is expected. Read results from the game's own log files.

If a run exceeds `MaintenanceTimeoutSeconds` the whole process tree is killed with
`taskkill /T /F`, because killing only `cmd.exe` leaves the game running and holding its data
files open.

## Troubleshooting

**`403` on upload.** Your `BbsIndex` does not match what the hub has for you in that league,
or your client is not an active member of it. Run `-Validate`, then check with the sysop.

**`Could not create SSL/TLS secure channel`** on 5.1 for an `https://` hub. The script forces
TLS 1.2, so if you still see this the machine is old enough to need
[the .NET TLS 1.2 update](https://learn.microsoft.com/en-us/security/engineering/solving-tls1-problem).

**Nothing uploads, and no error.** The client only sends files whose *source* index is yours —
a packet from another BBS sitting in your outbound folder is ignored on purpose. Check the
filename: `555B0201.001` means league 555, BRE, from BBS `02`, to BBS `01`.

**The daemon exits immediately with code 3.** Another copy is already running against the same
config. That is the single-instance guard; two daemons on one config would fight over the same
packets and the same game folder.

**Files named `*.part` in the inbound folder.** A download was interrupted. They are harmless
and get cleaned up on the next attempt — packets are written to `.part` first and only renamed
into place once complete, so the game never sees a half-written file.

## Tests

```powershell
Invoke-Pester -Path tests
```

The tests start the Python mock hub from `../python/tests/mock_server.py` and drive the real
script against it, so both implementations are held to one definition of the API.
