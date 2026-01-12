#!/bin/bash
# sync_and_process.sh - Example BBS operator script

LEAGUE="555"

echo "=== Nova Hub Sync ==="

# Run the client
cd /opt/nova-hub-client
./venv/bin/python client.py --config config.toml

# Check exit code
if [ $? -ne 0 ]; then
    echo "ERROR: Client sync failed"
    exit 1
fi

# Check if we received any packets
INBOUND_DIR="/opt/bre/league${LEAGUE}/inbound"
if [ -n "$(ls -A $INBOUND_DIR 2>/dev/null)" ]; then
    echo "Processing received packets..."

    # Run BRE in Dosemu to process inbound packets
    cd /opt/bre
    dosemu -f dosemu.conf "BRE.EXE PLANETARY"

    echo "Processing complete"
else
    echo "No packets received"
fi

echo "=== Sync Complete ==="
