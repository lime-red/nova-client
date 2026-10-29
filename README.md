# Nova Client

Syncs BBS door-game packets with a [Nova Hub](https://github.com/lime-red/nova-hub), and runs
game maintenance when packets arrive. Supports **Barren Realms Elite**, **Falcon's Eye** and
**Immortal Barons** inter-BBS leagues.

There are two independent implementations. **Pick one — you do not need both.** They speak
the same API to the same hub and behave the same way; they differ only in what has to be
installed on the machine.

| | [`python/`](python/) | [`powershell/`](powershell/) |
|---|---|---|
| **Pick this if** | you run Linux, or you already have Python | you run Windows and would rather not install a runtime |
| **Runs on** | Linux, Windows | Windows |
| **Needs** | Python 3.12+, `pip install -r requirements.txt` | nothing — Windows PowerShell 5.1 ships with Windows |
| **Config file** | `config.toml` | `config.psd1` |
| **Runs the game via** | native `.EXE` on Windows, dosemu on Linux (IB natively) | native `.EXE` |
| **Runs unattended as** | systemd service ([`linux/`](linux/)) | Scheduled Task (`Install-NovaClientTask.ps1`) |

Both implementations:

- authenticate with OAuth2 client credentials,
- upload the packets your game has written for other BBSes,
- download the packets addressed to you,
- keep your league nodelist up to date,
- and (in daemon mode) run `BRE PLANETARY` / `FE PLANETARY` / `immortal-barons -maint` on a
  schedule or as soon as packets arrive.

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
.\NovaClient-WinPS5.ps1 -TestConnection   # sign in, and check leagues and indexes with the hub
.\NovaClient-WinPS5.ps1 -Once -Verbose    # one sync, with detail
.\NovaClient-WinPS5.ps1 -Daemon           # run continuously
```

See [`powershell/README.md`](powershell/README.md). If you have PowerShell 7, use
`NovaClient-PS7.ps1` instead — same options.

### Python

```bash
uv sync --no-dev                          # .venv at the repo root, exactly the versions in uv.lock
cd python
cp config.toml.example config.toml        # then edit it

../.venv/bin/python client.py --validate
../.venv/bin/python client.py --test-connection   # sign in, and check leagues and indexes with the hub
../.venv/bin/python client.py --verbose      # one sync
../.venv/bin/python daemon.py --verbose      # run continuously
```

No uv? `python -m venv .venv && .venv/bin/pip install -r python/requirements.txt` at
the repo root does the same: that file is exported from `uv.lock`.

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

Immortal Barons names its packets differently, and never uses a name twice:

```
L555-2-000000000002s-1.brp
 │   │ │             └── destination board, base 36; 0 = every board (a broadcast)
 │   │ └──────────────── sequence number, 13 digits in base 36
 │   └────────────────── source board, base 36
 └────────────────────── league number, 3 digits
```

So the clients never replace an IB packet already waiting in inbound, and prune IB packets
from the sent archive after `archive_days` (default 30) instead of letting names overwrite.

Nodelists are `BRNODES.<league>` / `FENODES.<league>` / `IBNODES.<league>`. The hub generates
them; clients only ever download them. BRE and FE take theirs in from inbound; the clients
install IB's as `ibnodes.dat` in the game folder, IB's data directory, where IB reads it --
but only when there is none, since IB keeps that file current itself.

## Repository layout

```
python/       Python client — client.py (one-shot), daemon.py (continuous)
powershell/   PowerShell client — Windows only. Two launchers (5.1 and 7+) over
              one shared NovaClient.Common.ps1; deploy the folder, not a file.
linux/        systemd unit and shell helpers
openapi/      the hub's Service API spec, generated from the hub (see openapi/README.md)
VERSION       current release
CHANGELOG.md  what changed and when
```

## Upgrading

Read the release notes for every version between yours and the latest, in
[CHANGELOG.md](CHANGELOG.md) or on the Releases page. A release that needs you to do something
says so under **Upgrading**.

## Licence

MIT -- see [LICENSE](LICENSE).
