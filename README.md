# Nova Client

Async Python client for syncing packets with Nova Hub. Supports one-shot syncs and continuous daemon operation with automatic game maintenance.

## Features

- **OAuth2 Authentication**: Secure client credentials flow
- **One-Shot Sync**: Run once via cron or manual execution
- **Daemon Mode**: Continuous operation with scheduled sync and maintenance
- **Cross-Platform Game Execution**: Windows native or Linux via dosemu
- **Multi-League Support**: Handle multiple BRE and FE leagues simultaneously
- **Configuration Validation**: Verify directories, nodes.dat files, and BBS index consistency
- **Metrics Tracking**: JSON metrics output for monitoring

## Requirements

- Python 3.12+
- aiohttp, toml, python-dotenv

## Installation

```bash
# Clone or copy nova-client directory
cd nova-client

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows

# Install dependencies
pip install -r requirements.txt
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
max_retries = 3
retry_delay = 5
metrics_file = "./metrics.json"
```

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
```

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
*/5 * * * * cd /path/to/nova-client && .venv/bin/python client.py
```

### Daemon Mode

Continuous operation with automatic sync and game maintenance:

```bash
python daemon.py
python daemon.py --config config.toml --verbose
```

The daemon:
1. Syncs with Nova Hub at `sync_interval`
2. Runs game maintenance at `maintenance_interval`
3. Triggers immediate maintenance when packets are downloaded (if enabled)

### Configuration Validation

Validate your config before running:

```bash
python validator.py
python validator.py --config myconfig.toml
```

Checks:
- Directory paths exist and are accessible
- No duplicate directories across leagues
- nodes.dat files are parseable (if present)
- BBS index consistency

## Components

| File | Description |
|------|-------------|
| `client.py` | One-shot sync client with OAuth |
| `daemon.py` | Continuous operation daemon |
| `game_runner.py` | Cross-platform BRE/FE execution |
| `validator.py` | Configuration validation |
| `nodes_parser.py` | BRE/FE nodes.dat file parser |

## Packet Format

Packets follow the naming convention: `<league><game><source><dest>.<seq>`

Example: `555B0201.001`
- League: `555`
- Game: `B` (BRE) or `F` (FE)
- Source BBS: `02` (hex)
- Destination BBS: `01` (hex)
- Sequence: `001` (000-999, wraps)

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
  }
}
```

## Testing

```bash
# Install test dependencies
pip install -r requirements-dev.txt

# Run tests (requires mock server from nova-hub)
cd ../nova-hub && .venv/bin/python tests/mock_hub.py &
cd ../nova-client && pytest tests/
```

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
