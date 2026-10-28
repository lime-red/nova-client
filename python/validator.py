"""
Configuration validator for Nova Hub Client

Validates:
- Game, inbound, and outbound directories exist
- No duplicate directories across league entries
- Nodes.dat files exist and are parseable
- BBS index and name match in nodes.dat
- Hub credentials are set, and each game command is safe to hand a shell
"""

import os
import platform
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import toml

from client import DEFAULT_ARCHIVE_DAYS, GAME_LETTERS, NODES_FILES, SENT_ACTIONS
from game_runner import GameRunner, check_command, is_native
from nodes_parser import NodesFileParser


class ValidationError:
    """Represents a single validation error"""

    def __init__(self, category: str, message: str, severity: str = "ERROR"):
        self.category = category
        self.message = message
        self.severity = severity

    def __str__(self):
        return f"[{self.severity}] {self.category}: {self.message}"


class ClientValidator:
    """Validates Nova Hub Client configuration"""

    def __init__(self, config_path: str = "config.toml"):
        self.config_path = config_path
        self.config: Optional[Dict[str, Any]] = None
        self.errors: List[ValidationError] = []
        self.warnings: List[ValidationError] = []

    def load_config(self) -> bool:
        """Load configuration file"""
        if not os.path.exists(self.config_path):
            self.errors.append(
                ValidationError("Config", f"Configuration file not found: {self.config_path}")
            )
            return False

        try:
            self.config = toml.load(self.config_path)
            return True
        except Exception as e:
            self.errors.append(
                ValidationError("Config", f"Failed to parse configuration: {e}")
            )
            return False

    def find_file_case_insensitive(self, directory: Path, filename: str) -> Path | None:
        """Find a file in directory with case-insensitive search"""
        if not directory.exists():
            return None

        filename_lower = filename.lower()
        try:
            for item in directory.iterdir():
                if item.is_file() and item.name.lower() == filename_lower:
                    return item
        except PermissionError:
            pass
        return None

    def validate_directory(self, dir_path: str, dir_type: str, league_key: str) -> bool:
        """Validate that a directory exists"""
        path = Path(dir_path)
        if not path.exists():
            hint = ""
            # A DOS path on Linux is the classic misconfiguration: game_folder
            # must be the path the *host* can open (the directory dosemu
            # presents as C:), while game_dos_path carries the DOS spelling.
            # Without this hint the message reads as a missing directory when
            # it is really the wrong kind of path, and no amount of mkdir helps.
            if platform.system() != "Windows" and re.match(r"^[A-Za-z]:[\\/]", str(dir_path)):
                hint = (
                    f" - that is a DOS path, but {dir_type} must be a path this"
                    " machine can open. On Linux use the directory dosemu"
                    " presents as C: (e.g. /home/you/.dosemu/drive_c/bbs/...)"
                    " and put the DOS spelling in game_dos_path."
                )
            self.errors.append(
                ValidationError(
                    "Directory",
                    f"{league_key}: {dir_type} directory does not exist: {dir_path}{hint}"
                )
            )
            return False
        if not path.is_dir():
            self.errors.append(
                ValidationError(
                    "Directory",
                    f"{league_key}: {dir_type} path is not a directory: {dir_path}"
                )
            )
            return False
        return True

    def check_duplicate_directories(self) -> None:
        """Check for duplicate directories across all league entries"""
        assert self.config is not None  # Called after load_config succeeds
        # Track all directories used
        dir_usage: Dict[str, List[Tuple[str, str]]] = {}  # dir_path -> [(league_key, dir_type)]

        leagues = self.config.get("leagues", {})
        for game_type, game_leagues in leagues.items():
            for league_id, league_config in game_leagues.items():
                if not league_config.get("enabled", True):
                    continue

                league_key = f"{game_type}.{league_id}"

                # Check game_folder if present
                game_folder = league_config.get("game_folder")
                if game_folder:
                    abs_path = str(Path(game_folder).resolve())
                    dir_usage.setdefault(abs_path, []).append((league_key, "game_folder"))

                # Check outbound_dir
                outbound_dir = league_config.get("outbound_dir")
                if outbound_dir:
                    abs_path = str(Path(outbound_dir).resolve())
                    dir_usage.setdefault(abs_path, []).append((league_key, "outbound_dir"))

                # Check inbound_dir
                inbound_dir = league_config.get("inbound_dir")
                if inbound_dir:
                    abs_path = str(Path(inbound_dir).resolve())
                    dir_usage.setdefault(abs_path, []).append((league_key, "inbound_dir"))

        # Report duplicates
        for dir_path, usages in dir_usage.items():
            if len(usages) > 1:
                usage_str = ", ".join([f"{key} ({dtype})" for key, dtype in usages])
                self.errors.append(
                    ValidationError(
                        "Directory",
                        f"Directory used multiple times: {dir_path} - {usage_str}"
                    )
                )

    def validate_nodes_file(self, game_type: str, league_id: str, league_config: dict) -> None:
        """Validate nodes.dat file for a league"""
        league_key = f"{game_type}.{league_id}"

        # Determine game folder
        game_folder = league_config.get("game_folder")
        if not game_folder:
            self.warnings.append(
                ValidationError(
                    "NodesFile",
                    f"{league_key}: game_folder not configured, skipping nodes file validation",
                    severity="WARNING"
                )
            )
            return

        game_folder_path = Path(game_folder)
        if not game_folder_path.exists():
            # Already reported by validate_directory
            return

        # Determine expected nodes file name
        nodes_filename = NODES_FILES.get(game_type.upper(), "brnodes.dat")

        # Find file case-insensitively
        nodes_file = self.find_file_case_insensitive(game_folder_path, nodes_filename)

        if not nodes_file:
            # A new node has none until the game takes in the hub's first
            # nodelist. Failing here stopped the daemon starting, so it never
            # fetched one.
            self.warnings.append(
                ValidationError(
                    "NodesFile",
                    f"{league_key}: {nodes_filename} not found in {game_folder} "
                    "(expected until the first nodelist from the hub is processed)",
                    severity="WARNING"
                )
            )
            return

        # Parse nodes file
        parser = NodesFileParser(nodes_file)
        if not parser.parse():
            for error in parser.errors:
                self.errors.append(
                    ValidationError(
                        "NodesFile",
                        f"{league_key}: {nodes_filename}: {error}"
                    )
                )
            return

        # Immortal Barons' roster has no HOST lines: every board sends to its
        # destination, and the client hands everything to the hub regardless.
        if game_type.upper() != "IB":
            self.check_routing(league_key, nodes_filename, parser)

        # Check for duplicate indices
        duplicates = parser.check_duplicate_indices()
        for dup in duplicates:
            self.errors.append(
                ValidationError(
                    "NodesFile",
                    f"{league_key}: {nodes_filename}: {dup}"
                )
            )

        # Validate that this BBS's index matches the name
        bbs_index = league_config.get("bbs_index")
        if bbs_index is None:
            self.errors.append(
                ValidationError(
                    "NodesFile",
                    f"{league_key}: bbs_index not configured"
                )
            )
            return

        # Get BBS name from config
        assert self.config is not None  # Called after load_config succeeds
        bbs_name = self.config.get("bbs", {}).get("name")
        if not bbs_name:
            self.errors.append(
                ValidationError(
                    "Config",
                    f"{league_key}: BBS name not configured in [bbs] section"
                )
            )
            return

        # Look up this BBS in nodes file
        node = parser.get_node_by_index(bbs_index)
        if not node:
            self.errors.append(
                ValidationError(
                    "NodesFile",
                    f"{league_key}: BBS index {bbs_index} not found in {nodes_filename}"
                )
            )
            return

        # Check that the name matches
        if node.bbs_name.strip().lower() != bbs_name.strip().lower():
            self.errors.append(
                ValidationError(
                    "NodesFile",
                    f"{league_key}: BBS name mismatch - config has '{bbs_name}' "
                    f"but {nodes_filename} has '{node.bbs_name}' for index {bbs_index}"
                )
            )

    def check_routing(self, league_key: str, nodes_filename: str, parser: NodesFileParser) -> None:
        """Warn about nodes no HOST line routes for (#19).

        "1 HOST 2 3" tells the game to send traffic for 2 and 3 through 1, the
        hub. A node outside every HOST line is sent to directly, past the hub.
        A sysop can still override this on their own side.
        """
        hosts = {n.bbs_index for n in parser.nodes if n.routing_targets}
        routed = {t for n in parser.nodes for t in n.routing_targets}
        direct = sorted(
            n.bbs_index for n in parser.nodes
            if n.bbs_index not in hosts and n.bbs_index not in routed
        )
        if direct:
            self.warnings.append(
                ValidationError(
                    "NodesFile",
                    f"{league_key}: {nodes_filename}: no HOST line routes for "
                    f"{', '.join(map(str, direct))}, so the game sends to "
                    f"{'it' if len(direct) == 1 else 'them'} directly rather than through the hub",
                    severity="WARNING"
                )
            )

    def validate_hub(self) -> None:
        """The hub section, as client.py will read it (environment first)"""
        assert self.config is not None
        hub = self.config.get("hub")
        if not isinstance(hub, dict):
            self.errors.append(ValidationError("Config", "Missing [hub] section"))
            return
        if not hub.get("url"):
            self.errors.append(ValidationError("Config", "hub.url not configured"))
        for key, env in (("client_id", "HUB_CLIENT_ID"), ("client_secret", "HUB_CLIENT_SECRET")):
            if not (os.getenv(env) or hub.get(key)):
                self.errors.append(
                    ValidationError("Config", f"hub.{key} not configured (nor {env} in the environment)")
                )

    def validate_league(self, game_type: str, league_id: str, league_config: dict) -> None:
        """What client.py would refuse to start with, and commands it would not run"""
        league_key = f"{game_type}.{league_id}"
        if game_type.upper() not in GAME_LETTERS:
            self.errors.append(
                ValidationError("Config", f"{league_key}: unknown game '{game_type}' (use BRE, FE or IB)")
            )
        runner = league_config.get("runner")
        if runner not in (None, "native"):
            self.errors.append(
                ValidationError("Config", f"{league_key}: runner must be \"native\" or left out, got {runner!r}")
            )
        if is_native(game_type, league_config) and league_config.get("game_folder"):
            # Run directly, so it has to be there to run; dosemu finds a DOS
            # game's command in the DOS drive instead.
            command = league_config.get("game_command", game_type)
            if not (Path(command).is_file() and os.access(command, os.X_OK)) and not shutil.which(command):
                self.errors.append(
                    ValidationError("Config", f"{league_key}: game_command {command!r} is not a program this machine can run")
                )
        bbs_index = league_config.get("bbs_index")
        # bool is an int to Python: bbs_index = true would be BBS 1
        if bbs_index is not None and (
            isinstance(bbs_index, bool) or not isinstance(bbs_index, int) or not 1 <= bbs_index <= 255
        ):
            self.errors.append(
                ValidationError("Config", f"{league_key}: bbs_index must be a whole number from 1 to 255, got {bbs_index!r}")
            )
        refused = check_command(league_config, game_type.upper())
        if refused:
            self.errors.append(ValidationError("Config", f"{league_key}: {refused}"))

    def validate(self) -> bool:
        """
        Run all validations.
        Returns True if all validations passed, False otherwise.
        """
        # Load config
        if not self.load_config():
            return False

        assert self.config is not None  # load_config succeeded

        # Check required sections
        if "bbs" not in self.config:
            self.errors.append(ValidationError("Config", "Missing [bbs] section"))
            return False

        self.validate_hub()

        sent_action = self.config.get("sync", {}).get("sent_action", "delete")
        if sent_action not in SENT_ACTIONS:
            self.errors.append(
                ValidationError("Config", f"sync.sent_action must be 'delete' or 'archive', got '{sent_action}'")
            )

        archive_days = self.config.get("sync", {}).get("archive_days", DEFAULT_ARCHIVE_DAYS)
        if isinstance(archive_days, bool) or not isinstance(archive_days, int) or archive_days < 0:
            self.errors.append(
                ValidationError("Config", f"sync.archive_days must be a whole number of days (0 keeps all), got {archive_days!r}")
            )

        drives = self.config.get("daemon", {}).get("dosemu_drives") or {}
        try:
            for letter, path in GameRunner._dosemu_drives(drives).items():
                if not Path(path).is_dir():
                    self.errors.append(
                        ValidationError("Config", f"daemon.dosemu_drives: {letter}: {path} does not exist")
                    )
        except ValueError as e:
            self.errors.append(ValidationError("Config", f"daemon.{e}"))

        # Validate each enabled league
        leagues = self.config.get("leagues", {})
        enabled_count = 0

        for game_type, game_leagues in leagues.items():
            for league_id, league_config in game_leagues.items():
                if not league_config.get("enabled", True):
                    continue

                enabled_count += 1
                league_key = f"{game_type}.{league_id}"
                self.validate_league(game_type, league_id, league_config)

                # Validate directories
                game_folder = league_config.get("game_folder")
                if game_folder:
                    self.validate_directory(game_folder, "game_folder", league_key)

                outbound_dir = league_config.get("outbound_dir")
                if outbound_dir:
                    self.validate_directory(outbound_dir, "outbound_dir", league_key)
                else:
                    self.errors.append(
                        ValidationError(
                            "Config",
                            f"{league_key}: outbound_dir not configured"
                        )
                    )

                inbound_dir = league_config.get("inbound_dir")
                if inbound_dir:
                    self.validate_directory(inbound_dir, "inbound_dir", league_key)
                else:
                    self.errors.append(
                        ValidationError(
                            "Config",
                            f"{league_key}: inbound_dir not configured"
                        )
                    )

                # Validate nodes file
                self.validate_nodes_file(game_type, league_id, league_config)

        if enabled_count == 0:
            self.warnings.append(
                ValidationError(
                    "Config",
                    "No enabled leagues configured, so nothing will sync. "
                    "client.py --test-connection checks the hub credentials meanwhile",
                    severity="WARNING"
                )
            )

        # Check for duplicate directories
        self.check_duplicate_directories()

        return len(self.errors) == 0

    def print_results(self) -> None:
        """Print validation results"""
        print("\n" + "=" * 70)
        print("Nova Hub Client - Configuration Validation")
        print("=" * 70)
        print()

        if self.warnings:
            print(f"Warnings: {len(self.warnings)}")
            for warning in self.warnings:
                print(f"  {warning}")
            print()

        if self.errors:
            print(f"Errors: {len(self.errors)}")
            for error in self.errors:
                print(f"  {error}")
            print()
        else:
            print("✓ All validation checks passed!")
            print()

        print("=" * 70)
        print()


def main() -> int:
    import argparse

    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(description="Nova Hub Client - Configuration validation")
    parser.add_argument("--config", default="config.toml", help="Path to configuration file (default: config.toml)")
    args = parser.parse_args()

    # client.py reads the credentials from .env too
    load_dotenv()
    validator = ClientValidator(args.config)
    success = validator.validate()
    validator.print_results()
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
