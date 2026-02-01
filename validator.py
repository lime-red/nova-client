"""
Configuration validator for Nova Hub Client

Validates:
- Game, inbound, and outbound directories exist
- No duplicate directories across league entries
- Nodes.dat files exist and are parseable
- BBS index and name match in nodes.dat
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import toml

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
            self.errors.append(
                ValidationError(
                    "Directory",
                    f"{league_key}: {dir_type} directory does not exist: {dir_path}"
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
        nodes_filename = "brnodes.dat" if game_type.upper() == "BRE" else "fenodes.dat"

        # Find file case-insensitively
        nodes_file = self.find_file_case_insensitive(game_folder_path, nodes_filename)

        if not nodes_file:
            self.errors.append(
                ValidationError(
                    "NodesFile",
                    f"{league_key}: {nodes_filename} not found in {game_folder}"
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

        if "leagues" not in self.config:
            self.errors.append(ValidationError("Config", "Missing [leagues] section"))
            return False

        # Validate each enabled league
        leagues = self.config.get("leagues", {})
        enabled_count = 0

        for game_type, game_leagues in leagues.items():
            for league_id, league_config in game_leagues.items():
                if not league_config.get("enabled", True):
                    continue

                enabled_count += 1
                league_key = f"{game_type}.{league_id}"

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
                    "No enabled leagues found in configuration",
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
