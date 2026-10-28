# Nova Client — Python

Async Python client for syncing packets with Nova Hub. Supports one-shot syncs and continuous daemon operation with automatic game maintenance.

> This is one of two implementations — see the [repository README](../README.md) for the
> PowerShell client, which is the better choice on a Windows node with no Python installed.
> For running this as a service on Linux, see [`../linux/`](../linux/).

## Features

- **OAuth2 Authentication**: Secure client credentials flow
- **One-Shot Sync**: Run once via cron or manual execution
- **Daemon Mode**: Continuous operation with scheduled sync and maintenance
- **Cross-Platform Game Execution**: Windows native or Linux via dosemu
- **Multi-League Support**: Handle multiple BRE, FE and Immortal Barons leagues simultaneously
- **Configuration Validation**: Verify directories, nodes.dat files, and BBS index consistency
- **Metrics Tracking**: JSON metrics output for monitoring

## Requirements

- Python 3.12+
- aiohttp, toml, python-dotenv

## Installation

```bash
# From the repo root: .venv there, with exactly the versions in uv.lock
cd nova-client
uv sync --no-dev

# Or with pip (python/requirements.txt is exported from uv.lock)
python -m venv .venv
.venv/bin/pip install -r python/requirements.txt   # .venv\Scripts\pip on Windows
```

## Configuration

Copy the example configuration and edit:

```bash
cp config.toml.example config.toml
```

### Hub Connection

```toml
[hub]
url = "https://hub.example.com"
client_id = "your_client_id"      # Get from hub admin
client_secret = "your_secret"     # Get from hub admin

[bbs]
name = "My BBS"
```

Credentials can also be set via environment variables:
- `HUB_CLIENT_ID`
- `HUB_CLIENT_SECRET`

### Sync Settings

```toml
[sync]
sent_action = "archive"       # "archive" or "delete"
archive_dir = "./sent"        # Where to move sent packets
max_retries = 3               # Attempts per request, for a dropped connection,
                              # 408, 429 or 5xx; others fail at once
retry_delay = 5               # Seconds between them, unless the hub sends Retry-After
timeout_seconds = 120         # Per-request HTTP timeout
metrics_file = "./metrics.json"
```

A downloaded packet is written to `<name>.part` and renamed once complete, so the game
never reads half a packet. Anything over 10 MB is refused.

### League Configuration

Each league requires:
- `bbs_index`: Your BBS ID for this league (integer 1-255, must match hub config)
- `outbound_dir`: Where your game writes outbound packets
- `inbound_dir`: Where to place downloaded packets

```toml
[leagues.BRE.555]
enabled = true
bbs_index = 2
outbound_dir = "/opt/bre/league555/outbound"
inbound_dir = "/opt/bre/league555/inbound"

# For daemon mode - game execution settings
game_folder = "/home/user/.dosemu/drive_c/BBS/BRE"
game_command = "BRE"
maintenance_args = "PLANETARY"
game_dos_path = "C:\\BBS\\BRE"  # DOS path inside dosemu
```

### Daemon Configuration

For continuous operation:

```toml
[daemon]
enabled = true
sync_interval = 120               # Seconds between hub syncs
maintenance_interval = 600        # Seconds between scheduled maintenance
maintenance_timeout = 300         # Max seconds for game execution
run_maintenance_on_download = true

# Linux dosemu settings
dosemu_path = "/usr/bin/dosemu"
dosemu_config_dir = "./dosemu_configs"
log_dir = "./logs"
# Optional: DOS drives the games were set up on, mapped with lredir
# dosemu_drives = { M = "/opt/bbs" }
```

If the BBS runs its games from a redirected drive (for example an `M:` that
lredir maps to the BBS tree) rather than from `drive_c`, list it in
`dosemu_drives`. BRE and FE store the drive paths they were set up with in
their own config files, so maintenance has to see the same drive; set
`game_dos_path` on that drive too.

## Usage

### One-Shot Sync

Run a single sync cycle:

```bash
python client.py
python client.py --config myconfig.toml --verbose
```

Schedule via cron:
```bash
# Sync every 5 minutes
*/5 * * * * cd /path/to/nova-client/python && ../.venv/bin/python client.py
```

### Daemon Mode

Continuous operation with automatic sync and game maintenance:

```bash
python daemon.py
python daemon.py --config config.toml --verbose
```

The daemon:
1. Syncs with Nova Hub at `sync_interval`, keeping one token until it expires
2. Runs game maintenance at `maintenance_interval`
3. Triggers immediate maintenance when packets are downloaded (if enabled), then
   uploads the replies it wrote
4. With `enabled = false` it starts, syncs nothing, and waits to be stopped

The daemon and `client.py` take a lock beside the config (`config.toml.lock`), so only one
of them syncs that config at a time. A `client.py` run while the daemon is mid-cycle says so
and exits 1; the daemon waits for a hand-run `client.py` to finish.

### Configuration Validation

Validate your config before running:

```bash
python validator.py
python validator.py --config myconfig.toml
```

Checks:
- `[hub]` has a URL, and a client ID and secret (or `HUB_CLIENT_ID` / `HUB_CLIENT_SECRET`)
- `sync.sent_action` is `delete` or `archive`
- Directory paths exist and are accessible
- No duplicate directories across leagues
- nodes.dat files are parseable. A missing one warns: a new node has none until it has
  processed the hub's first nodelist
- Every node is behind a `HOST` line (a warning if one would be sent to directly)
- BBS index consistency
- `game_command`, `maintenance_args` and `game_dos_path` hold nothing the command shell
  would act on (`& | < > ^ % "` or a line break). The daemon refuses to run one that does.

A BBS with no leagues yet passes, with a warning: it can sign in, it just has nothing to sync.

### Connection Test

```bash
python client.py --test-connection
```

Signs in to the hub with the configured credentials, then compares every league and
`bbs_index` in the config with what the hub has for this BBS. Syncs nothing, and works
whether or not any leagues are configured. Exits 1 if the hub would refuse something: a wrong
index, or an enabled league the BBS is not a member of. A league the hub has but the config
does not is only a warning.

## Components

| File | Description |
|------|-------------|
| `client.py` | One-shot sync client with OAuth |
| `daemon.py` | Continuous operation daemon |
| `game_runner.py` | Cross-platform BRE/FE execution, and native IB |
| `validator.py` | Configuration validation |
| `nodes_parser.py` | BRE/FE nodes.dat (and IB ibnodes.dat) file parser |
| `_version.py` | Version string, shared with the User-Agent header |

## Packet Format

Packets follow the naming convention: `<league><game><source><dest>.<seq>`

Example: `555B0201.001`
- League: `555`
- Game: `B` (BRE) or `F` (FE)
- Source BBS: `02` (hex)
- Destination BBS: `01` (hex)
- Sequence: `001` (000-999, wraps)

Immortal Barons packets are `L<league>-<source>-<sequence>-<dest>.brp`, nodes and sequence in
base 36 (see the top-level README).

## Metrics

After each sync, `metrics.json` contains:

```json
{
  "start_time": "2025-01-06T10:00:00",
  "end_time": "2025-01-06T10:00:05",
  "total_uploaded": 3,
  "total_downloaded": 2,
  "success": true,
  "errors": [],
  "leagues": {
    "BRE_555": {
      "uploaded": 3,
      "downloaded": 2,
      "errors": 0
    }
  },
  "unprocessed": {
    "BRE_555": ["555B0102.001"]
  },
  "total_unprocessed": 1
}
```

`unprocessed` lists the packets and nodelists still in each league's inbound after the
daemon's last maintenance run. The game should have taken those in, so a name here means it
skipped them. The daemon also logs a WARNING naming them after each maintenance. Two common
causes are an inbound path the game can't see (BRE ignores one with a directory name longer
than 8.3) and a packet the game won't read. A one-shot `client.py`, or the daemon before its
first maintenance, runs none, so these two fields are left out. The PowerShell client writes
the same fields.

## Testing

```bash
# Install test dependencies (the dev group), from the repo root
uv sync

# Run the suite. The integration tests start their own mock hub
# (tests/mock_server.py) on a free port, so nothing else needs to be running.
pytest
```

The same mock hub backs the PowerShell client's Pester tests, so both implementations are
tested against one definition of the API.

## Troubleshooting

### Authentication Errors
- Verify `client_id` and `client_secret` match hub admin settings
- Check hub is reachable: `curl https://hub.example.com/service/docs`
- Confirm client is marked active in hub admin

### Directory Errors
- Run `python validator.py` to check all paths
- Verify game has created outbound directory
- Check file permissions

### Connection Errors
- Check hub URL (include port if non-standard)
- Verify firewall allows outbound HTTPS
- Test connectivity: `curl https://hub.example.com/health`

### Dosemu Issues (Linux)
- Ensure dosemu 2.x is installed
- Verify `dosemu_path` points to correct binary
- Check `dosemu_config_dir` contains valid configs
- Review logs in configured `log_dir`

## Exit Codes

- `0`: Success
- `1`: Errors occurred (check metrics.json and console output)

## License

MIT License - See LICENSE file
