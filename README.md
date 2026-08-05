# Nova Client

Syncs BBS door-game packets with a [Nova Hub](https://gitea-hl/lime/nova-hub), and runs
game maintenance when packets arrive. Supports **Barren Realms Elite** and **Falcon's Eye**
inter-BBS leagues.

There are two independent implementations. **Pick one — you do not need both.** They speak
the same API to the same hub and behave the same way; they differ only in what has to be
installed on the machine.

| | [`python/`](python/) | [`powershell/`](powershell/) |
|---|---|---|
| **Pick this if** | you run Linux, or you already have Python | you run Windows and would rather not install a runtime |
| **Runs on** | Linux, Windows | Windows |
| **Needs** | Python 3.12+, `pip install -r requirements.txt` | nothing — Windows PowerShell 5.1 ships with Windows |
| **Config file** | `config.toml` | `config.psd1` |
| **Runs the game via** | native `.EXE` on Windows, dosemu on Linux | native `.EXE` |
| **Runs unattended as** | systemd service ([`linux/`](linux/)) | Scheduled Task (`Install-NovaClientTask.ps1`) |

Both implementations:

- authenticate with OAuth2 client credentials,
- upload the packets your game has written for other BBSes,
- download the packets addressed to you,
- keep your league nodelist up to date,
- and (in daemon mode) run `BRE PLANETARY` / `FE PLANETARY` on a schedule or as soon as
  packets arrive.

## Getting started

You need three things from whoever runs the hub: a **client ID**, a **client secret**, and
your **BBS index** for each league you join. The BBS index is a number from 1 to 255 and it
can be different in each league.

### Windows, no Python

```powershell
cd powershell
Copy-Item config.psd1.example config.psd1
notepad config.psd1                       # fill in URL, credentials, BbsIndex, directories

.\NovaClient-WinPS5.ps1 -Validate         # check everything before touching the network
.\NovaClient-WinPS5.ps1 -Once -Verbose    # one sync, with detail
.\NovaClient-WinPS5.ps1 -Daemon           # run continuously
```

See [`powershell/README.md`](powershell/README.md). If you have PowerShell 7, use
`NovaClient-PS7.ps1` instead — same options.

### Python

```bash
cd python
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.toml.example config.toml        # then edit it

.venv/bin/python client.py --validate
.venv/bin/python client.py --verbose      # one sync
.venv/bin/python daemon.py --verbose      # run continuously
```

See [`python/README.md`](python/README.md), and [`linux/`](linux/) for the systemd unit.

## Packet naming

Packet filenames carry their own routing information:

```
555B0201.001
│  ││ │  └── sequence number, 000-999, wraps
│  ││ └───── destination BBS index, 2 hex digits
│  │└─────── source BBS index, 2 hex digits
│  └──────── game: B = Barren Realms Elite, F = Falcon's Eye
└─────────── league number, 3 digits
```

The hub checks that the **source** index matches the client uploading it, and that the
**destination** matches the client downloading it. If your `BbsIndex` is wrong, uploads are
rejected with a `403` — that is far and away the most common setup problem, and both clients'
`-Validate` / `--validate` modes check for it before you hit the network.

Nodelists are `BRNODES.<league>` / `FENODES.<league>`. The hub generates them; clients only
ever download them.

## Repository layout

```
python/       Python client — client.py (one-shot), daemon.py (continuous)
powershell/   PowerShell client — Windows only, two variants for 5.1 and 7+
linux/        systemd unit and shell helpers
VERSION       current release
CHANGELOG.md  what changed and when
```

## Upgrading from 0.2.0 or earlier

**The Python client moved into `python/`.** Nothing about how it works changed, but paths did.
After pulling this release, update wherever you launch it from:

| | Before | After |
|---|---|---|
| systemd | `WorkingDirectory=/home/lime/nova-client` | `WorkingDirectory=/home/lime/nova-client/python` |
| Windows | `cd C:\BBS\nova-client` | `cd C:\BBS\nova-client\python` |
| CI / scripts | `pytest` at the repo root | `cd python && pytest` |

Your `config.toml` is not affected. Keep it wherever it is and pass an absolute path with
`--config`, which is what [`linux/nova-client.service`](linux/nova-client.service) now does —
it means a future reshuffle of the tree cannot break your config again.

Pull, update the path, restart, in that order. A node that pulls while its service is running
will keep working until the next restart, but do not leave it in that state.

## Licence

See the repository for licence terms.
