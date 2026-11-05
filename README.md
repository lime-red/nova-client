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

## Joining a league: start from a fresh install

Give each league its own **fresh install** of the game, set up for that league. Never
copy the folder of a league you already play, even if you change the league number in
`bbs.cfg` afterwards.

On Linux, [`tools/new-league-game.sh`](tools/new-league-game.sh) does all of what follows
from a golden image of BRE or FE. It takes everything about your board from the client's
`config.toml`: the league's `game_folder` and `game_dos_path` (or another league's, renamed,
if the league isn't there yet) and `[daemon] dosemu_drives`. It unpacks the image, writes
`bbs.cfg` from your other league's, installs the hub's nodelist, sets the folder's group,
and runs RESET for you to answer with inbound's files set aside. An existing folder is
moved aside, not deleted. Add the league to `config.toml` and let the client sync once
first, so the hub's nodelist is waiting; then:

```bash
tools/new-league-game.sh --image ~/bre-sj1-r2.tar.gz --league 969
```

It shows its plan and asks before changing anything; `--help` lists the options.

A fresh install is not a game yet: its `DATA/` is empty. Once `bbs.cfg` and the nodes file
are in place, run `BRE RESET` (or `FE RESET`) in the folder once. Answer **Y** to reset the
game, then press ESC to leave the Configuration Editor without changing anything: the
league's reset from the hub sets those values. A member board is not asked the
league-wide question. Until RESET has run, maintenance stops with "Game files could not be
opened. Please run BRE RESET", and leaves the hub's packets in inbound.

**RESET empties the inbound folder.** So run it *before* you add the league to the client's
config, and the hub's reset arrives afterwards into a game ready for it. If the client has
already downloaded files for the league, move them out of inbound, RESET, then put them
back.

The game files RESET writes must be writable by the client's game runs. On Linux the
simplest way is a shared group: put yourself in the client user's group, give the game
folder that group with the setgid bit on every directory, and use umask 002. Then files
you make by hand take the client's group too:

```bash
chgrp -R <client-group> bre_969 && chmod -R g+rwX bre_969
find bre_969 -type d -exec chmod g+s {} +
```

Unpacking an archive (tar, unzip) sets each directory's mode from the archive, which drops
the setgid bit, so run the `find` again after unpacking.

A game that has been played belongs to its league, and changing `bbs.cfg` does not change
that:

- **BRE and FE** record the League Coordinator they belong to in `DATA/GAME.DAT`. Every new
  league's coordinator has a key of its own, so a copy rejects the new league's first reset.
  `PROBLEMS.LOG` then says:
  ```
  Invalid Configuration Update #1 found and ignored
  Illegal Global Reset Attempted
  ```
  The copy also carries on numbering its packets from the old league (a first packet
  numbered `.027`, not `.001`). The hub reports the board as silent, because its game never
  hears from it.
- **Immortal Barons** keeps the old league's `coord.pub` and `ibnodes.dat`. The client fetches
  each only when the file is missing, so it never replaces the copied ones.

Copying a folder used to work because the hub's own games were copies too, and every league
shared one coordinator key. From hub 0.13.0 each new league gets a key of its own, so a
member's game has to start fresh as well.

**If you have already copied one:** move the copy aside, and make a fresh install in its
place. Bring over only what you set up for this league (`bbs.cfg`, the nodes file, and your
BBS's door files), and run RESET as above, minding inbound. Then ask the hub's operator to reset the
league again, unless a reset sent after the copy was moved aside is already waiting in
inbound. A copy that is still in place when the reset arrives rejects it again.

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
but only when there is none, since IB keeps that file current itself. The same goes for the
League Coordinator's public key: the clients fetch it from the hub and record it as
`coord.pub`, which is what `immortal-barons -coord-key` would do by hand.

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
