# Nova Hub Client

One-shot packet synchronization client for BBS nodes.

## Overview

This client connects to Nova Hub to:
1. Upload outbound packets from your BBS
2. Download inbound packets for your BBS
3. Log metrics for monitoring

## Installation

```bash
pip install -r requirements.txt
```

## Configuration

1. Copy configuration template:
   ```bash
   cp config.toml.example config.toml
   ```

2. Edit config.toml with your settings:
   - Hub URL
   - OAuth credentials (get from hub admin)
   - BBS information
   - League directories


## Usage

### One-time Run
Default config file is `config.toml`, or specify `--config <specific TOML file>.  One copy of the client can service many copies of BRE and FE, by having many config TOML files.
```bash
python client.py
```

### Scheduled via Cron
```bash
# Edit crontab
crontab -e

# Add line (runs every 5 minutes)
*/5 * * * * cd /path/to/client && /path/to/python client.py
```

### Bash Script Integration
```bash
#!/bin/bash
# sync_and_process.sh

cd /path/to/client
python client.py

# Check if packets received
if [ -n "$(ls -A /path/to/inbound 2>/dev/null)" ]; then
    # Process with your game
    dosemu -E "BRE.EXE PLANETARY"
fi
```

## Metrics

After each run, metrics.json contains:
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

## League Configuration

Each league needs:
- Unique identifier (game type + league number)
- Outbound directory path
- Inbound directory path

Example:
```toml
[leagues.BRE.555]
enabled = true
outbound_dir = "/opt/bre/league555/outbound"
inbound_dir = "/opt/bre/league555/inbound"
```

## File Handling

Sent files can be:
- **deleted**: Removed after successful upload
- **archived**: Moved to archive directory

Configure via `sent_action` in config.toml

## Troubleshooting

### Authentication Errors
- Verify client_id and client_secret
- Check hub is reachable
- Confirm client is active in hub admin

### File Not Found Errors
- Verify directory paths exist
- Check file permissions
- Ensure game has created outbound packets

### Connection Errors
- Check hub URL
- Verify firewall allows outbound HTTPS
- Test connectivity: `curl https://hub.example.com/health`

## Exit Codes

- 0: Success
- 1: Errors occurred (check metrics.json for details)

## Complete Implementation

The full client.py implementation is in the conversation history under
"Let's go with client implementation next". Copy the complete code from
that section.
