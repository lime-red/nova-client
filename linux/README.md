# Nova Client — Linux

Files for running the Python client as a service on Linux. The client itself lives in
[`../python/`](../python/); there is no separate Linux implementation.

## systemd

```bash
sudo cp nova-client.service /etc/systemd/system/
sudo systemctl edit --full nova-client       # adjust User, Group and the three paths
sudo systemctl daemon-reload
sudo systemctl enable --now nova-client

journalctl -u nova-client -f
```

The unit assumes:

- the repo checked out at `/home/lime/nova-client`,
- its virtualenv at the repo root (`/home/lime/nova-client/.venv`),
- the Python client in `python/` (this is where it moved in 0.3.0),
- `config.toml` in `python/` alongside the code — the same place `python/README.md` tells you
  to create it, and where `client.py --validate` looks by default. The unit still passes it by
  absolute path, so a wrong `WorkingDirectory` fails loudly instead of quietly loading a
  different file.

If you are upgrading from 0.2.0, your `config.toml` was at the repo root. Move it:

```bash
mv /home/lime/nova-client/config.toml /home/lime/nova-client/python/
```

### Keeping credentials out of the checkout

`client.py` prefers `HUB_CLIENT_ID` / `HUB_CLIENT_SECRET` from the environment over whatever
is in `config.toml`, so:

```bash
sudo install -m 600 -o root -g root /dev/null /etc/default/nova-client
printf 'HUB_CLIENT_ID=...\nHUB_CLIENT_SECRET=...\n' | sudo tee /etc/default/nova-client
```

The unit reads that file if it exists and starts anyway if it does not.

### Things in the unit that are load-bearing

- **`PYTHONUNBUFFERED=1`** — without it Python buffers stdout when it is a pipe and journald
  receives your logs in 8 KB bursts. `journalctl -f` then sits silent for an hour and dumps
  everything at once.
- **`StartLimitIntervalSec` / `StartLimitBurst`** — systemd's default is 5 starts in 10
  seconds, after which it gives up *permanently*. With `Restart=always`, any fault that kills
  the daemon quickly would silently take the node offline until someone noticed.
- **`KillMode=mixed`** — SIGTERM goes to the daemon only, which handles it and shuts down
  cleanly. The default (`control-group`) would SIGTERM a running dosemu child mid-game.
- **`ExecStartPre=... --validate`** — a bad config fails at start with a readable message
  instead of crash-looping with the reason buried in a restart storm.
- **The mild hardening is deliberate.** dosemu2 needs `/dev` access, a writable tmp and a
  `TERM` that can position the cursor, so `ProtectSystem=strict`, `PrivateDevices`,
  `PrivateTmp` and `MemoryDenyWriteExecute` are all left off. Tightening them produces a
  game that silently stops processing turns — do not change them without testing a real
  `PLANETARY` run afterwards.

Check your edits before restarting:

```bash
systemd-analyze verify /etc/systemd/system/nova-client.service
```

## sync_and_process.sh.example

A pre-daemon shell wrapper: run one sync, then run the game if packets arrived. Superseded by
`daemon.py --daemon`, but kept for anyone who would rather drive the client from cron.
