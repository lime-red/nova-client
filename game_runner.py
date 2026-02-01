# game_runner.py
"""
Cross-platform game runner for BRE and Falcon's Eye.

Handles running game maintenance commands on both Windows (direct .EXE)
and Linux (via dosemu). Includes locking to prevent concurrent execution
with automatic queuing.
"""

import asyncio
import platform
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional


@dataclass
class GameRunResult:
    """Result of a game run attempt"""
    success: bool
    game_type: str
    league_id: str
    command: str
    output: str = ""
    error: str = ""
    return_code: int = 0
    duration_seconds: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    queued: bool = False  # True if this run was queued behind another


class GameRunner:
    """
    Cross-platform runner for BRE/FE game commands.

    Ensures only one instance of a game runs at a time per game folder.
    Additional requests are queued and executed after the current run completes.
    """

    def __init__(self, config: dict, verbose: bool = False):
        self.config = config
        self.verbose = verbose
        self.is_windows = platform.system() == "Windows"

        # Per-league locks to prevent concurrent execution
        # Key: game_folder path (normalized)
        self._locks: Dict[str, asyncio.Lock] = {}

        # Track pending queue counts per game folder
        self._queue_counts: Dict[str, int] = {}

    def _get_lock(self, game_folder: str) -> asyncio.Lock:
        """Get or create a lock for the given game folder"""
        normalized = str(Path(game_folder).resolve())
        if normalized not in self._locks:
            self._locks[normalized] = asyncio.Lock()
            self._queue_counts[normalized] = 0
        return self._locks[normalized]

    def _get_queue_count(self, game_folder: str) -> int:
        """Get the current queue count for a game folder"""
        normalized = str(Path(game_folder).resolve())
        return self._queue_counts.get(normalized, 0)

    def _increment_queue(self, game_folder: str):
        """Increment the queue count"""
        normalized = str(Path(game_folder).resolve())
        self._queue_counts[normalized] = self._queue_counts.get(normalized, 0) + 1

    def _decrement_queue(self, game_folder: str):
        """Decrement the queue count"""
        normalized = str(Path(game_folder).resolve())
        if normalized in self._queue_counts and self._queue_counts[normalized] > 0:
            self._queue_counts[normalized] -= 1

    def log(self, level: str, message: str):
        """Simple logging"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] [{level}] [GameRunner] {message}")

    async def run_maintenance(
        self,
        game_type: str,
        league_id: str,
        league_config: dict,
        timeout: float = 300.0
    ) -> GameRunResult:
        """
        Run maintenance command for a league.

        Args:
            game_type: "BRE" or "FE"
            league_id: League number (e.g., "555")
            league_config: League configuration from config.toml
            timeout: Maximum seconds to wait for game to complete

        Returns:
            GameRunResult with success status and output
        """
        game_folder = league_config.get("game_folder")
        if not game_folder:
            return GameRunResult(
                success=False,
                game_type=game_type,
                league_id=league_id,
                command="",
                error="game_folder not configured for this league"
            )

        game_folder_path = Path(game_folder)
        if not game_folder_path.exists():
            return GameRunResult(
                success=False,
                game_type=game_type,
                league_id=league_id,
                command="",
                error=f"game_folder does not exist: {game_folder}"
            )

        # Get command configuration
        game_command = league_config.get("game_command", game_type)
        maintenance_args = league_config.get("maintenance_args", "PLANETARY")

        # Build full command string for logging
        full_command = f"{game_command} {maintenance_args}"

        # Acquire lock (queue if another run is in progress)
        lock = self._get_lock(game_folder)
        was_queued = lock.locked()

        if was_queued:
            queue_pos = self._get_queue_count(game_folder) + 1
            self._increment_queue(game_folder)
            self.log("INFO", f"Queued {game_type} {league_id} maintenance (position {queue_pos})")

        start_time = datetime.now()

        async with lock:
            if was_queued:
                self._decrement_queue(game_folder)
                self.log("INFO", f"Starting queued {game_type} {league_id} maintenance")
            else:
                self.log("INFO", f"Starting {game_type} {league_id} maintenance: {full_command}")

            try:
                if self.is_windows:
                    result = await self._run_windows(
                        game_folder_path, game_command, maintenance_args, timeout
                    )
                else:
                    result = await self._run_linux_dosemu(
                        game_type, league_id, league_config, timeout
                    )

                duration = (datetime.now() - start_time).total_seconds()
                result.duration_seconds = duration
                result.queued = was_queued
                result.game_type = game_type
                result.league_id = league_id
                result.command = full_command

                if result.success:
                    self.log("INFO", f"Completed {game_type} {league_id} maintenance in {duration:.1f}s")
                else:
                    self.log("ERROR", f"Failed {game_type} {league_id} maintenance: {result.error}")

                return result

            except asyncio.TimeoutError:
                duration = (datetime.now() - start_time).total_seconds()
                self.log("ERROR", f"Timeout running {game_type} {league_id} after {timeout}s")
                return GameRunResult(
                    success=False,
                    game_type=game_type,
                    league_id=league_id,
                    command=full_command,
                    error=f"Timeout after {timeout} seconds",
                    duration_seconds=duration,
                    queued=was_queued
                )
            except Exception as e:
                duration = (datetime.now() - start_time).total_seconds()
                self.log("ERROR", f"Exception running {game_type} {league_id}: {e}")
                return GameRunResult(
                    success=False,
                    game_type=game_type,
                    league_id=league_id,
                    command=full_command,
                    error=str(e),
                    duration_seconds=duration,
                    queued=was_queued
                )

    async def _run_windows(
        self,
        game_folder: Path,
        game_command: str,
        args: str,
        timeout: float
    ) -> GameRunResult:
        """
        Run game on Windows via direct subprocess execution.

        The game must be run from its game folder (cwd) because it cannot
        handle being called with a full path.

        Uses synchronous subprocess.run in a thread executor because
        asyncio's subprocess has issues on Windows with console apps.
        """
        import subprocess

        # Build command - game_command might be just "BRE" or a full path
        exe_path: str
        if not game_command.lower().endswith('.exe'):
            # Assume it's in the game folder
            exe_path_candidate = game_folder / f"{game_command}.EXE"
            if not exe_path_candidate.exists():
                # Try without .EXE extension (might be in PATH)
                exe_path = game_command
            else:
                exe_path = str(exe_path_candidate)
        else:
            exe_path = game_command

        # Split args if it's a string
        if isinstance(args, str):
            cmd_args = args.split()
        else:
            cmd_args = list(args)

        # Build full command list
        full_cmd = [str(exe_path)] + cmd_args

        if self.verbose:
            self.log("DEBUG", f"Running: {' '.join(full_cmd)} in {game_folder}")

        def run_sync():
            """Run the subprocess synchronously (called in thread executor)"""
            try:
                # Use shell=True to run through cmd.exe for DOS/legacy program compatibility.
                # Without shell=True, subprocess.run fails with WinError 87 on DOS executables
                # because they can't handle the way Python sets up process I/O handles.
                cmd_str = ' '.join(full_cmd)

                result = subprocess.run(
                    cmd_str,
                    cwd=str(game_folder),
                    shell=True,
                    capture_output=True,
                    timeout=timeout,
                )
                return {
                    "success": result.returncode == 0,
                    "output": result.stdout.decode('utf-8', errors='replace') +
                              result.stderr.decode('utf-8', errors='replace'),
                    "return_code": result.returncode,
                    "error": "" if result.returncode == 0 else f"Exit code: {result.returncode}"
                }
            except subprocess.TimeoutExpired:
                return {
                    "success": False,
                    "output": "",
                    "return_code": -1,
                    "error": f"Timeout after {timeout} seconds"
                }
            except FileNotFoundError:
                return {
                    "success": False,
                    "output": "",
                    "return_code": -1,
                    "error": f"Executable not found: {exe_path}"
                }
            except OSError as e:
                return {
                    "success": False,
                    "output": "",
                    "return_code": -1,
                    "error": f"OS error: {e}"
                }

        # Run in thread executor to not block the event loop
        result = await asyncio.to_thread(run_sync)

        return GameRunResult(
            success=result["success"],
            game_type="",  # Will be filled in by caller
            league_id="",  # Will be filled in by caller
            command="",    # Will be filled in by caller
            output=result["output"],
            error=result["error"],
            return_code=result["return_code"]
        )

    async def _run_linux_dosemu(
        self,
        game_type: str,
        league_id: str,
        league_config: dict,
        timeout: float
    ) -> GameRunResult:
        """
        Run game on Linux via dosemu.

        Uses the same approach as nova-hub's dosemu_runner.
        """
        # Get dosemu configuration from daemon config
        daemon_config = self.config.get("daemon", {})
        dosemu_path = daemon_config.get("dosemu_path", "/usr/bin/dosemu")
        dosemu_config_dir = daemon_config.get("dosemu_config_dir", "./dosemu_configs")

        game_folder = Path(league_config["game_folder"])
        game_dos_path = league_config.get("game_dos_path", "C:\\GAMES\\BRE")
        game_command = league_config.get("game_command", game_type)
        maintenance_args = league_config.get("maintenance_args", "PLANETARY")

        full_command = f"{game_command} {maintenance_args}"

        # Ensure config directory exists
        config_dir = Path(dosemu_config_dir)
        config_dir.mkdir(parents=True, exist_ok=True)

        # Generate dosemu config
        conf_file = config_dir / f"{game_type.lower()}_{league_id}.conf"
        self._write_dosemu_config(conf_file)

        # Create batch file to run the command
        batch_file = game_folder / "NOVAMNT.BAT"
        self._create_batch_file(batch_file, full_command, game_dos_path)

        # Prepare log file for output capture
        log_dir = Path(daemon_config.get("log_dir", "./logs"))
        log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = log_dir / f"{game_type}_{league_id}_{timestamp}.log"

        try:
            # Run dosemu with the batch file
            cmd = [
                dosemu_path,
                "-f", str(conf_file),
                str(batch_file),
            ]

            # Use script command to capture output including ANSI codes
            import shlex
            dosemu_cmd = " ".join([shlex.quote(str(c)) for c in cmd])
            script_cmd = ["script", "-c", dosemu_cmd, str(log_file)]

            process = await asyncio.create_subprocess_exec(
                *script_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(game_folder),
            )

            try:
                stdout, _ = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise

            # Read output from log file
            output = ""
            if log_file.exists():
                output = log_file.read_text(errors='replace')

            return_code = process.returncode if process.returncode is not None else -1
            return GameRunResult(
                success=return_code == 0,
                game_type="",
                league_id="",
                command="",
                output=output,
                error="" if return_code == 0 else f"Exit code: {return_code}",
                return_code=return_code
            )

        finally:
            # Cleanup batch file
            batch_file.unlink(missing_ok=True)

    def _write_dosemu_config(self, conf_file: Path):
        """Write dosemu configuration file"""
        conf_content = """# Nova Client dosemu configuration
$_layout = "us"
$_floppy_a = ""
$_xms = (8192)
$_ems = (8192)
$_X = ""
$_vga = "off"
$_graphics = "off"
$_com1 = ""
$_com2 = ""
$_quiet = (1)
"""
        conf_file.write_text(conf_content)

    def _create_batch_file(self, batch_file: Path, command: str, game_dos_path: str):
        """Create DOS batch file to run the game command"""
        # Extract drive letter if present
        drive_letter = ""
        if len(game_dos_path) >= 2 and game_dos_path[1] == ':':
            drive_letter = game_dos_path[:2]

        batch_content = "@ECHO OFF\n"
        batch_content += "ECHO Nova Client Maintenance Starting\n"

        if drive_letter:
            batch_content += f"{drive_letter}\n"
            batch_content += f"CD {game_dos_path}\n"
        else:
            batch_content += f"CD {game_dos_path}\n"

        batch_content += f"{command}\n"
        batch_content += "ECHO Nova Client Maintenance Complete\n"
        batch_content += "EXIT\n"

        batch_file.write_text(batch_content)

    async def run_all_maintenance(
        self,
        timeout_per_league: float = 300.0
    ) -> Dict[str, GameRunResult]:
        """
        Run maintenance for all enabled leagues.

        Returns:
            Dict mapping league keys (e.g., "BRE_555") to their results
        """
        results = {}
        leagues = self.config.get("leagues", {})

        for game_type, game_leagues in leagues.items():
            for league_id, league_config in game_leagues.items():
                if not league_config.get("enabled", True):
                    continue

                # Skip if no game_folder configured
                if not league_config.get("game_folder"):
                    self.log("DEBUG", f"Skipping {game_type}.{league_id} - no game_folder configured")
                    continue

                league_key = f"{game_type}_{league_id}"
                result = await self.run_maintenance(
                    game_type, league_id, league_config, timeout_per_league
                )
                results[league_key] = result

        return results
