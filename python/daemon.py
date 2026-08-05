#!/usr/bin/env python3
# daemon.py
"""
Nova Client Daemon - Continuous synchronization and game maintenance.

This daemon runs in the background and:
1. Periodically syncs packets with Nova Hub (configurable interval)
2. Runs game maintenance (PLANETARY) on a schedule
3. Triggers immediate maintenance when new packets are downloaded
4. Supports both Windows and Linux platforms

Usage:
    python daemon.py [--config config.toml] [--verbose]

Configuration (config.toml):
    [daemon]
    enabled = true
    sync_interval = 120        # seconds between hub syncs
    maintenance_interval = 600 # seconds between scheduled maintenance runs
"""

import argparse
import asyncio
import signal
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import toml

from _version import __version__
from client import NovaHubClient
from game_runner import GameRunner, GameRunResult


class NovaDaemon:
    """
    Nova Client Daemon for continuous operation.

    Manages two independent schedules:
    - Sync schedule: Polls Nova Hub for packets
    - Maintenance schedule: Runs game maintenance (PLANETARY)

    Also triggers immediate maintenance when packets are downloaded.
    """

    def __init__(self, config_path: str = "config.toml", verbose: bool = False):
        self.config_path = config_path
        self.verbose = verbose
        self.config = self._load_config(config_path)
        self.game_runner = GameRunner(self.config, verbose=verbose)

        # Shutdown flag
        self._shutdown = False
        self._shutdown_event = asyncio.Event()

        # Timing state
        self._last_sync: Optional[datetime] = None
        self._last_maintenance: Optional[datetime] = None

        # Statistics
        self.stats: Dict[str, Any] = {
            "start_time": None,
            "sync_count": 0,
            "maintenance_count": 0,
            "packets_uploaded": 0,
            "packets_downloaded": 0,
            "errors": 0,
        }

    def _load_config(self, config_path: str) -> dict:
        """Load and validate configuration"""
        if not Path(config_path).exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        config = toml.load(config_path)

        # Validate daemon section exists
        if "daemon" not in config:
            raise ValueError(
                "Missing [daemon] section in config. "
                "Add [daemon] with sync_interval and maintenance_interval settings."
            )

        daemon = config["daemon"]

        # Set defaults
        daemon.setdefault("enabled", True)
        daemon.setdefault("sync_interval", 120)
        daemon.setdefault("maintenance_interval", 600)
        daemon.setdefault("maintenance_timeout", 300)
        daemon.setdefault("run_maintenance_on_download", True)
        daemon.setdefault("dosemu_path", "/usr/bin/dosemu")
        daemon.setdefault("dosemu_config_dir", "./dosemu_configs")
        daemon.setdefault("log_dir", "./logs")

        return config

    def log(self, level: str, message: str):
        """Simple logging with timestamp"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] [{level}] [Daemon] {message}")

    async def run(self):
        """Main daemon loop"""
        self.stats["start_time"] = datetime.now().isoformat()
        self.log("INFO", f"Nova Client Daemon {__version__} starting")
        self.log("INFO", f"BBS: {self.config.get('bbs', {}).get('name', 'Unknown')}")
        self.log("INFO", f"Hub: {self.config.get('hub', {}).get('url', 'Unknown')}")

        daemon_config = self.config["daemon"]
        sync_interval = daemon_config["sync_interval"]
        maintenance_interval = daemon_config["maintenance_interval"]

        self.log("INFO", f"Sync interval: {sync_interval}s")
        self.log("INFO", f"Maintenance interval: {maintenance_interval}s")

        # Setup signal handlers
        self._setup_signal_handlers()

        # Run initial sync immediately
        self.log("INFO", "Running initial sync...")
        await self._run_sync()

        # Main loop
        while not self._shutdown:
            try:
                # Calculate time until next events
                now = datetime.now()

                # Time until next sync
                if self._last_sync:
                    next_sync = self._last_sync + timedelta(seconds=sync_interval)
                    sync_wait = max(0, (next_sync - now).total_seconds())
                else:
                    sync_wait = 0

                # Time until next maintenance
                if self._last_maintenance:
                    next_maint = self._last_maintenance + timedelta(seconds=maintenance_interval)
                    maint_wait = max(0, (next_maint - now).total_seconds())
                else:
                    # Run initial maintenance after first sync
                    maint_wait = maintenance_interval

                # Wait for the shorter interval or shutdown
                wait_time = min(sync_wait, maint_wait, 10)  # Check at least every 10s

                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=wait_time
                    )
                    # Shutdown event was set
                    break
                except asyncio.TimeoutError:
                    pass  # Normal timeout, check what needs to run

                if self._shutdown:
                    break

                # Check if sync is due
                now = datetime.now()
                if self._last_sync:
                    time_since_sync = (now - self._last_sync).total_seconds()
                    if time_since_sync >= sync_interval:
                        await self._run_sync()

                # Check if maintenance is due
                if self._last_maintenance:
                    time_since_maint = (now - self._last_maintenance).total_seconds()
                    if time_since_maint >= maintenance_interval:
                        await self._run_scheduled_maintenance()
                elif self._last_sync:
                    # First maintenance after first sync
                    await self._run_scheduled_maintenance()

            except Exception as e:
                self.log("ERROR", f"Error in main loop: {e}")
                self.stats["errors"] += 1
                if self.verbose:
                    traceback.print_exc()
                # Brief pause before retrying
                await asyncio.sleep(5)

        self.log("INFO", "Daemon shutting down...")
        self._print_stats()

    async def _run_sync(self):
        """Run a single sync cycle"""
        self.log("INFO", "=" * 50)
        self.log("INFO", "Starting sync cycle")

        try:
            # Create a new client instance for each sync
            client = NovaHubClient(self.config_path, verbose=self.verbose)
            exit_code = await client.run()

            self._last_sync = datetime.now()
            self.stats["sync_count"] += 1

            # Extract metrics
            downloaded = client.metrics.get("total_downloaded", 0)
            uploaded = client.metrics.get("total_uploaded", 0)
            errors = len(client.metrics.get("errors", []))

            self.stats["packets_downloaded"] += downloaded
            self.stats["packets_uploaded"] += uploaded
            self.stats["errors"] += errors

            self.log(
                "INFO",
                f"Sync complete: uploaded={uploaded}, downloaded={downloaded}, errors={errors}"
            )

            # Trigger immediate maintenance if packets were downloaded
            if downloaded > 0 and self.config["daemon"].get("run_maintenance_on_download", True):
                self.log("INFO", "Packets downloaded - triggering immediate maintenance")
                await self._run_triggered_maintenance()

        except Exception as e:
            self.log("ERROR", f"Sync failed: {e}")
            self.stats["errors"] += 1
            if self.verbose:
                traceback.print_exc()

    async def _run_scheduled_maintenance(self):
        """Run scheduled maintenance for all leagues"""
        self.log("INFO", "=" * 50)
        self.log("INFO", "Starting scheduled maintenance")
        await self._run_maintenance("scheduled")
        self._last_maintenance = datetime.now()

    async def _run_triggered_maintenance(self):
        """Run maintenance triggered by packet download"""
        self.log("INFO", "Starting triggered maintenance (packets arrived)")
        await self._run_maintenance("packet-triggered")
        # Also update last_maintenance to reset the schedule
        self._last_maintenance = datetime.now()

    async def _run_maintenance(self, reason: str):
        """Run maintenance for all enabled leagues"""
        timeout = self.config["daemon"].get("maintenance_timeout", 300)

        try:
            results = await self.game_runner.run_all_maintenance(
                timeout_per_league=timeout
            )

            success_count = sum(1 for r in results.values() if r.success)
            fail_count = sum(1 for r in results.values() if not r.success)

            self.stats["maintenance_count"] += 1

            self.log(
                "INFO",
                f"Maintenance complete ({reason}): success={success_count}, failed={fail_count}"
            )

            # Log individual results
            for league_key, result in results.items():
                if result.success:
                    self.log(
                        "INFO",
                        f"  {league_key}: OK ({result.duration_seconds:.1f}s)"
                    )
                else:
                    self.log(
                        "ERROR",
                        f"  {league_key}: FAILED - {result.error}"
                    )
                    self.stats["errors"] += 1

        except Exception as e:
            self.log("ERROR", f"Maintenance failed: {e}")
            self.stats["errors"] += 1
            if self.verbose:
                traceback.print_exc()

    def _setup_signal_handlers(self):
        """Setup handlers for graceful shutdown"""
        self._loop = asyncio.get_event_loop()

        if sys.platform != "win32":
            # Unix-like systems
            for sig in (signal.SIGINT, signal.SIGTERM):
                self._loop.add_signal_handler(sig, self._handle_signal, sig)
        else:
            # Windows - use signal module (works for SIGINT/Ctrl+C)
            signal.signal(signal.SIGINT, self._handle_signal_windows)

    def _handle_signal(self, sig):
        """Handle shutdown signal (Unix)"""
        self.log("INFO", f"Received signal {sig.name}, initiating shutdown...")
        self._shutdown = True
        self._shutdown_event.set()

    def _handle_signal_windows(self, signum, frame):
        """Handle shutdown signal (Windows)"""
        self.log("INFO", f"Received signal {signum}, initiating shutdown...")
        self._shutdown = True
        # Use call_soon_threadsafe since signal handler runs in different context
        self._loop.call_soon_threadsafe(self._shutdown_event.set)

    def _print_stats(self):
        """Print daemon statistics"""
        self.log("INFO", "=" * 50)
        self.log("INFO", "Daemon Statistics:")
        self.log("INFO", f"  Started: {self.stats['start_time']}")
        self.log("INFO", f"  Sync cycles: {self.stats['sync_count']}")
        self.log("INFO", f"  Maintenance runs: {self.stats['maintenance_count']}")
        self.log("INFO", f"  Packets uploaded: {self.stats['packets_uploaded']}")
        self.log("INFO", f"  Packets downloaded: {self.stats['packets_downloaded']}")
        self.log("INFO", f"  Errors: {self.stats['errors']}")
        self.log("INFO", "=" * 50)


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="Nova Client Daemon - Continuous sync and game maintenance"
    )
    parser.add_argument(
        "--config",
        default="config.toml",
        help="Path to configuration file (default: config.toml)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose output"
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate configuration without running"
    )

    args = parser.parse_args()

    # Validation mode
    if args.validate:
        try:
            config = toml.load(args.config)
            if "daemon" not in config:
                print("ERROR: Missing [daemon] section in config")
                sys.exit(1)

            daemon = config["daemon"]
            print(f"Configuration: {args.config}")
            print(f"  sync_interval: {daemon.get('sync_interval', 120)}s")
            print(f"  maintenance_interval: {daemon.get('maintenance_interval', 600)}s")
            print(f"  run_maintenance_on_download: {daemon.get('run_maintenance_on_download', True)}")

            # Also run client validation
            from validator import ClientValidator
            validator = ClientValidator(args.config)
            success = validator.validate()
            validator.print_results()
            sys.exit(0 if success else 1)

        except Exception as e:
            print(f"ERROR: {e}")
            sys.exit(1)

    # Normal daemon mode
    try:
        daemon = NovaDaemon(args.config, verbose=args.verbose)
        asyncio.run(daemon.run())
        sys.exit(0)

    except KeyboardInterrupt:
        print("\nInterrupted by user")
        sys.exit(130)
    except Exception as e:
        print(f"\n{'='*60}", file=sys.stderr)
        print(f"FATAL ERROR", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)
        print(f"Exception type: {type(e).__name__}", file=sys.stderr)
        print(f"Exception message: {e}", file=sys.stderr)
        print(f"\nFull traceback:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
