# client.py

import asyncio
import hashlib
import json
import os
import re
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
import toml

from _version import USER_AGENT, __version__

# Load environment variables
from dotenv import load_dotenv

load_dotenv()

# Config sections name the game; the hub's league ids carry its letter.
GAME_LETTERS = {"BRE": "B", "FE": "F"}


class NovaHubClient:
    """One-shot client for syncing packets with Nova Hub"""

    def __init__(self, config_path: str = "config.toml", verbose: bool = False):
        self.verbose = verbose
        self.config = self.load_config(config_path)
        self.metrics: Dict[str, Any] = {
            "start_time": datetime.now().isoformat(),
            "leagues": {},
            "total_uploaded": 0,
            "total_downloaded": 0,
            "errors": [],
        }

    def load_config(self, config_path: str) -> dict:
        """Load configuration from TOML file"""
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Config file not found: {config_path}")

        config = toml.load(config_path)
        # A BBS can be set up before it joins any league
        config.setdefault("leagues", {})

        # Override with environment variables
        config["hub"]["client_id"] = os.getenv(
            "HUB_CLIENT_ID", config["hub"].get("client_id", "")
        )
        config["hub"]["client_secret"] = os.getenv(
            "HUB_CLIENT_SECRET", config["hub"].get("client_secret", "")
        )

        if not config["hub"]["client_id"] or not config["hub"]["client_secret"]:
            raise ValueError(
                "Client ID and Secret must be set in config or environment"
            )

        # Validate that each enabled league has bbs_index configured (as integer, 1-255)
        for game_type, leagues in config.get("leagues", {}).items():
            for league_number, league_config in leagues.items():
                if league_config.get("enabled", True):
                    bbs_index = league_config.get("bbs_index")
                    if bbs_index is None:
                        raise ValueError(
                            f"League {game_type}.{league_number} is enabled but missing 'bbs_index' configuration"
                        )
                    if not isinstance(bbs_index, int):
                        raise ValueError(
                            f"League {game_type}.{league_number} bbs_index must be an integer, got {type(bbs_index).__name__}"
                        )
                    if not (1 <= bbs_index <= 255):
                        raise ValueError(
                            f"League {game_type}.{league_number} bbs_index must be between 1 and 255, got {bbs_index}"
                        )

        return config

    def log(self, level: str, message: str, league: Optional[str] = None):
        """Simple logging"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_msg = f"[{timestamp}] [{level}] {message}"
        print(log_msg)

        if level == "ERROR":
            self.metrics["errors"].append(
                {"time": timestamp, "message": message, "league": league}
            )
            if not self.verbose:
                print("  (use --verbose to see more details)")

    def log_api_response(self, response_text: str, context: str = ""):
        """Log API response in verbose mode with pretty-printing"""
        if not self.verbose:
            return

        prefix = f"[API Response{': ' + context if context else ''}]"
        try:
            # Try to parse as JSON and pretty-print
            data = json.loads(response_text)
            print(f"{prefix}")
            print(json.dumps(data, indent=2))
        except (json.JSONDecodeError, TypeError):
            # Not JSON, print as-is
            print(f"{prefix} {response_text}")

    def sanitize_filename(self, filename: str) -> str:
        """
        Sanitize and validate a packet filename to prevent path traversal attacks.

        Args:
            filename: The filename to sanitize

        Returns:
            The sanitized filename (basename only)

        Raises:
            ValueError: If the filename is invalid or contains traversal attempts
        """
        if not filename:
            raise ValueError("Empty filename")

        # Strip any path components - defense against path traversal
        safe_name = os.path.basename(filename)

        # Check if path traversal was attempted
        if safe_name != filename:
            raise ValueError(f"Path traversal attempt detected in filename: {filename}")

        # Reject obviously dangerous names
        if safe_name in (".", "..", ""):
            raise ValueError(f"Invalid filename: {filename}")

        # Validate against expected filename patterns
        # Packet pattern: <league><game><source><dest>.<seq>
        # Example: 555B0102.001
        packet_pattern = r"^[0-9]{3}[BF][0-9A-Fa-f]{4}\.[0-9]{3}$"

        # Nodelist pattern: BRNODES.<league> or FENODES.<league>
        # Example: BRNODES.555, FENODES.013
        nodelist_pattern = r"^(BR|FE)NODES\.[0-9]{3}$"

        if not (re.match(packet_pattern, safe_name, re.IGNORECASE) or
                re.match(nodelist_pattern, safe_name, re.IGNORECASE)):
            raise ValueError(f"Filename does not match expected format: {filename}")

        return safe_name

    async def run(self):
        """Execute one complete sync run"""
        self.log("INFO", f"Nova Hub Client {__version__} starting")
        self.log("INFO", f"BBS: {self.config['bbs']['name']}")
        self.log("INFO", f"Hub: {self.config['hub']['url']}")

        # Identify the implementation in the hub's access logs, and set an
        # explicit timeout rather than inheriting aiohttp's 5-minute default.
        timeout = aiohttp.ClientTimeout(
            total=self.config.get("sync", {}).get("timeout_seconds", 120)
        )
        async with aiohttp.ClientSession(
            timeout=timeout, headers={"User-Agent": USER_AGENT}
        ) as session:
            self.session = session

            # Get OAuth token
            token = await self.get_token()
            if not token:
                self.log("ERROR", "Failed to obtain OAuth token")
                return self.finalize()

            self.token = token

            if not any(
                league_config.get("enabled", True)
                for leagues in self.config["leagues"].values()
                for league_config in leagues.values()
            ):
                self.log("INFO", "No leagues configured; nothing to sync")
                return self.finalize()

            # Process each league
            for game_type, leagues in self.config["leagues"].items():
                for league_number, league_config in leagues.items():
                    if not league_config.get("enabled", True):
                        continue

                    await self.sync_league(game_type, league_number, league_config)

        return self.finalize()

    async def sync_league(
        self, game_type: str, league_number: str, league_config: dict
    ):
        """Sync one league"""
        league_key = f"{game_type}_{league_number}"
        self.log("INFO", f"Syncing {game_type} League {league_number}")

        self.metrics["leagues"][league_key] = {
            "game_type": game_type,
            "league_number": league_number,
            "uploaded": 0,
            "downloaded": 0,
            "errors": 0,
        }

        # Step 1: Upload outbound packets
        uploaded = await self.upload_outbound(game_type, league_number, league_config)
        self.metrics["leagues"][league_key]["uploaded"] = uploaded
        self.metrics["total_uploaded"] += uploaded

        # Step 2: Download inbound packets
        downloaded = await self.download_inbound(
            game_type, league_number, league_config
        )
        self.metrics["leagues"][league_key]["downloaded"] = downloaded
        self.metrics["total_downloaded"] += downloaded

        self.log(
            "INFO",
            f"League {league_number}: Uploaded {uploaded}, Downloaded {downloaded}",
        )

    async def upload_outbound(
        self, game_type: str, league_number: str, league_config: dict
    ) -> int:
        """Upload outbound packets to hub"""
        outbound_dir = Path(league_config["outbound_dir"])

        if not outbound_dir.exists():
            self.log(
                "WARNING",
                f"Outbound directory does not exist: {outbound_dir}",
                league_number,
            )
            return 0

        # Find all packet files
        packet_files = []
        for file in outbound_dir.iterdir():
            if file.is_file() and self.is_packet_file(
                file.name, game_type, league_number, league_config
            ):
                packet_files.append(file)

        if not packet_files:
            self.log("DEBUG", f"No outbound packets in {outbound_dir}", league_number)
            return 0

        self.log("INFO", f"Found {len(packet_files)} outbound packet(s)", league_number)

        uploaded = 0
        for packet_file in packet_files:
            success = await self.upload_packet(game_type, league_number, packet_file)
            if success:
                uploaded += 1
                await self.handle_sent_file(packet_file)
            else:
                self.log(
                    "ERROR", f"Failed to upload: {packet_file.name}", league_number
                )
                self.metrics["leagues"][f"{game_type}_{league_number}"]["errors"] += 1

        return uploaded

    async def upload_packet(
        self, game_type: str, league_number: str, packet_file: Path
    ) -> bool:
        """Upload a single packet to the hub using PUT with streaming body"""
        # Defense-in-depth: validate filename before using in URL
        try:
            filename = self.sanitize_filename(packet_file.name)
        except ValueError as e:
            self.log("ERROR", f"Invalid filename for upload: {e}", league_number)
            return False

        # Max file size limit (10MB)
        MAX_SIZE = 10 * 1024 * 1024
        try:
            file_size = packet_file.stat().st_size
            if file_size > MAX_SIZE:
                self.log(
                    "ERROR",
                    f"File too large for upload: {filename} ({file_size} bytes)",
                    league_number,
                )
                return False
        except OSError as e:
            self.log("ERROR", f"Could not stat file: {e}", league_number)
            return False

        # Convert game_type to single letter (BRE -> B, FE -> F)
        game_type_letter = "B" if game_type == "BRE" else "F"

        # Construct league_id with game type (e.g., "555B" or "555F")
        league_id = f"{league_number}{game_type_letter}"

        # URL with validated filename
        url = f"{self.config['hub']['url']}/service/api/v1/leagues/{league_id}/packets/{filename}"

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/octet-stream"
        }

        max_retries = self.config.get("sync", {}).get("max_retries", 3)
        retry_delay = self.config.get("sync", {}).get("retry_delay", 5)

        for attempt in range(max_retries):
            try:
                # Open file in binary mode for streaming
                with open(packet_file, "rb") as f:
                    # PUT request with streaming body
                    async with self.session.put(url, headers=headers, data=f) as resp:
                        if resp.status == 200:
                            self.log("INFO", f"Uploaded: {filename}", league_number)
                            return True
                        elif resp.status == 401:
                            error = await resp.text()
                            self.log("ERROR", "Token rejected by server", league_number)
                            self.log_api_response(error, "upload auth error")
                            return False
                        else:
                            error = await resp.text()
                            self.log(
                                "ERROR",
                                f"Upload failed ({resp.status})",
                                league_number,
                            )
                            self.log_api_response(error, "upload error")
                            return False

            except Exception as e:
                self.log(
                    "ERROR",
                    f"Upload exception (attempt {attempt + 1}): {e}",
                    league_number,
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)

        return False

    async def download_inbound(
        self, game_type: str, league_number: str, league_config: dict
    ) -> int:
        """Download inbound packets from hub"""
        # Step 1: List available packets
        packets = await self.list_packets(game_type, league_number, unread=True)

        if not packets:
            self.log("DEBUG", "No inbound packets", league_number)
            return 0

        self.log("INFO", f"Found {len(packets)} inbound packet(s)", league_number)

        inbound_dir = Path(league_config["inbound_dir"])
        inbound_dir.mkdir(parents=True, exist_ok=True)

        # Step 2: Download each packet
        downloaded = 0
        for packet_info in packets:
            raw_filename = packet_info["filename"]

            # Sanitize filename from server to prevent path traversal
            try:
                filename = self.sanitize_filename(raw_filename)
            except ValueError as e:
                self.log("ERROR", f"Invalid filename from server: {e}", league_number)
                self.metrics["leagues"][f"{game_type}_{league_number}"]["errors"] += 1
                continue

            success = await self.download_packet(game_type, league_number, filename, inbound_dir)
            if success:
                downloaded += 1
            else:
                self.log("ERROR", f"Failed to download: {filename}", league_number)
                self.metrics["leagues"][f"{game_type}_{league_number}"]["errors"] += 1

        return downloaded

    async def list_packets(
        self, game_type: str, league_number: str, unread: bool = False
    ) -> List[dict]:
        """List packets available at the hub"""
        # Convert game_type to single letter (BRE -> B, FE -> F)
        game_type_letter = "B" if game_type == "BRE" else "F"

        # Construct league_id with game type (e.g., "555B" or "555F")
        league_id = f"{league_number}{game_type_letter}"

        url = f"{self.config['hub']['url']}/service/api/v1/leagues/{league_id}/packets"

        if unread:
            url += "?unread=true"

        headers = {"Authorization": f"Bearer {self.token}"}

        try:
            async with self.session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.log_api_response(json.dumps(data), "list packets")
                    return data.get("packets", [])
                elif resp.status == 401:
                    error = await resp.text()
                    self.log("ERROR", "Token rejected when listing packets")
                    self.log_api_response(error, "list packets auth error")
                    return []
                else:
                    error = await resp.text()
                    self.log("ERROR", f"List packets failed ({resp.status})")
                    self.log_api_response(error, "list packets error")
                    return []
        except Exception as e:
            self.log("ERROR", f"List packets exception: {e}")
            return []

    async def download_packet(
        self, game_type: str, league_number: str, filename: str, inbound_dir: Path
    ) -> bool:
        """Download a single packet from the hub"""
        # Defense-in-depth: validate filename even though caller should sanitize
        try:
            filename = self.sanitize_filename(filename)
        except ValueError as e:
            self.log("ERROR", f"Invalid filename in download_packet: {e}", league_number)
            return False

        # Convert game_type to single letter (BRE -> B, FE -> F)
        game_type_letter = "B" if game_type == "BRE" else "F"

        # Construct league_id with game type (e.g., "555B" or "555F")
        league_id = f"{league_number}{game_type_letter}"

        url = f"{self.config['hub']['url']}/service/api/v1/leagues/{league_id}/packets/{filename}"

        headers = {"Authorization": f"Bearer {self.token}"}

        dest_file = inbound_dir / filename

        max_retries = self.config.get("sync", {}).get("max_retries", 3)
        retry_delay = self.config.get("sync", {}).get("retry_delay", 5)

        for attempt in range(max_retries):
            try:
                async with self.session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        dest_file.write_bytes(content)
                        self.log(
                            "INFO",
                            f"Downloaded: {filename} ({len(content)} bytes)",
                            league_number,
                        )
                        return True
                    elif resp.status == 401:
                        error = await resp.text()
                        self.log("ERROR", "Token rejected when downloading packet")
                        self.log_api_response(error, "download auth error")
                        return False
                    else:
                        error = await resp.text()
                        self.log("ERROR", f"Download failed ({resp.status})")
                        self.log_api_response(error, "download error")
                        return False

            except Exception as e:
                self.log("ERROR", f"Download exception (attempt {attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)

        return False

    async def handle_sent_file(self, packet_file: Path):
        """Handle a packet file after successful upload"""
        action = self.config.get("sync", {}).get("sent_action", "delete")

        if action == "delete":
            packet_file.unlink()
            self.log("DEBUG", f"Deleted: {packet_file.name}")

        elif action == "archive":
            archive_dir = Path(self.config.get("sync", {}).get("archive_dir", "./sent"))
            archive_dir.mkdir(parents=True, exist_ok=True)

            # Defense-in-depth: validate filename before constructing archive path
            try:
                safe_name = self.sanitize_filename(packet_file.name)
            except ValueError as e:
                self.log("ERROR", f"Invalid filename for archive: {e}")
                return

            dest = archive_dir / safe_name
            packet_file.rename(dest)
            self.log("DEBUG", f"Archived: {safe_name}")

    def is_packet_file(
        self, filename: str, game_type: str, league_number: str, league_config: dict
    ) -> bool:
        """Check if a file is a valid packet file for this league"""
        # Pattern: <league><game><source><dest>.<seq>
        # Example: 555B0102.001
        pattern = (
            rf"^{league_number}{game_type[0]}([0-9A-F]{{2}})[0-9A-F]{{2}}\.\d{{3}}$"
        )
        match = re.match(pattern, filename, re.IGNORECASE)

        if not match:
            return False

        # Validate that source BBS index matches our league's BBS index
        source_hex = match.group(1).upper()
        our_bbs_index = league_config.get("bbs_index")
        our_bbs_hex = format(our_bbs_index, "02X") if our_bbs_index else None

        return source_hex == our_bbs_hex

    async def get_token(self) -> Optional[str]:
        """Get OAuth token from the hub"""
        url = f"{self.config['hub']['url']}/service/api/v1/auth/token"

        data = {
            "grant_type": "client_credentials",
            "client_id": self.config["hub"]["client_id"],
            "client_secret": self.config["hub"]["client_secret"],
        }

        try:
            async with self.session.post(url, data=data) as resp:
                if resp.status == 200:
                    token_data = await resp.json()
                    self.log("INFO", "Authenticated Successfully")
                    # Log token response (but mask the actual token for security)
                    if self.verbose:
                        masked = {**token_data, "access_token": "***masked***"}
                        self.log_api_response(json.dumps(masked), "token")
                    return token_data["access_token"]
                else:
                    error = await resp.text()
                    self.log("ERROR", f"Token request failed ({resp.status})")
                    self.log_api_response(error, "token error")
                    return None

        except Exception as e:
            self.log("ERROR", f"Token request exception: {e}")
            return None

    def configured_leagues(self) -> Dict[str, dict]:
        """Every league in the config, enabled or not, keyed by hub league id ("015B")."""
        result = {}
        for game_type, leagues in self.config["leagues"].items():
            letter = GAME_LETTERS.get(game_type.upper())
            if letter is None:
                continue
            for league_number, league_config in leagues.items():
                result[f"{league_number}{letter}"] = {
                    "section": f"leagues.{game_type}.{league_number}",
                    "enabled": league_config.get("enabled", True),
                    "bbs_index": league_config.get("bbs_index"),
                }
        return result

    async def check_connection(self) -> int:
        """Sign in to the hub, then compare this config's leagues with the hub's.

        Syncs nothing. Returns 0 when the credentials work and nothing in the
        config would make the hub refuse a packet.
        """
        hub = self.config["hub"]["url"].rstrip("/")
        print(f"Testing the connection to {hub} as {self.config['hub']['client_id']}")
        print()

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(
            timeout=timeout, headers={"User-Agent": USER_AGENT}
        ) as session:
            data = {
                "grant_type": "client_credentials",
                "client_id": self.config["hub"]["client_id"],
                "client_secret": self.config["hub"]["client_secret"],
            }
            try:
                async with session.post(f"{hub}/service/api/v1/auth/token", data=data) as resp:
                    status = resp.status
                    body = await resp.json() if status == 200 else None
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                print(f"FAILED   Could not reach the hub: {e or type(e).__name__}")
                print("         Check hub.url, and that this machine can reach it.")
                return 1

            if status != 200:
                print(f"FAILED   The hub did not authenticate this BBS (HTTP {status}).")
                print("         " + {
                    401: "The client ID or secret is wrong, or the BBS is disabled on the hub. "
                         "If they came from a claim link, copy them again; otherwise ask the hub admin.",
                    429: "Too many failed attempts from this address. Wait a few minutes and try again.",
                    404: "Nothing answered at that address. Check hub.url points at a Nova Hub.",
                }.get(status, "Check hub.url points at a Nova Hub."))
                return 1

            print("Authenticated Successfully")
            headers = {"Authorization": f"Bearer {body['access_token']}"}

            async with session.get(f"{hub}/service/api/v1/me", headers=headers) as resp:
                if resp.status == 404:
                    print()
                    print("This hub does not report league memberships (it is older than this")
                    print("client), so your leagues could not be checked against it.")
                    return 0
                if resp.status != 200:
                    print(f"FAILED   The hub would not report this BBS's leagues (HTTP {resp.status}).")
                    return 1
                account = await resp.json()

        return self.compare_with_hub(account)

    def compare_with_hub(self, account: dict) -> int:
        """Print how the config's leagues line up with the hub's; return 1 on any problem."""
        problems = 0
        print(f"The hub knows this BBS as {account['bbs_name']} ({account['client_id']})")

        configured_name = self.config.get("bbs", {}).get("name", "")
        if configured_name.strip().lower() != account["bbs_name"].strip().lower():
            print()
            print(f"WARNING  bbs.name is '{configured_name}', but the hub has '{account['bbs_name']}'.")
            print("         The nodelists the hub sends use its name; the games expect the two to match.")

        print()
        hub_leagues = {league["league_id"]: league for league in account["leagues"]}
        local = self.configured_leagues()

        if not hub_leagues and not any(l["enabled"] for l in local.values()):
            print("No leagues yet, on the hub or in this config. The connection works;")
            print("the hub admin will add your BBS to a league and tell you its BBS index.")
            return 0

        for league_id in sorted(set(hub_leagues) | set(local)):
            on_hub = hub_leagues.get(league_id)
            mine = local.get(league_id)
            if on_hub and mine is None:
                print(f"WARNING  {league_id}  The hub has this BBS in {league_id} as #{on_hub['bbs_index']}, "
                      f"but it is not in this config.")
            elif on_hub and not mine["enabled"]:
                print(f"NOTE     {league_id}  The hub has this BBS as #{on_hub['bbs_index']}; "
                      f"[{mine['section']}] is disabled.")
            elif on_hub and mine["bbs_index"] != on_hub["bbs_index"]:
                problems += 1
                print(f"ERROR    {league_id}  [{mine['section']}] has bbs_index = {mine['bbs_index']}, "
                      f"but the hub has this BBS as #{on_hub['bbs_index']}.")
                print("                The hub will refuse packets sent as the wrong index.")
            elif on_hub:
                print(f"OK       {league_id}  BBS #{on_hub['bbs_index']}")
            elif mine["enabled"]:
                problems += 1
                print(f"ERROR    {league_id}  [{mine['section']}] is enabled, but the hub does not have "
                      f"this BBS in {league_id}.")
                print("                The hub will refuse its packets. Ask the hub admin, or disable it.")

        print()
        if problems:
            print(f"Connection test found {problems} problem{'s' if problems != 1 else ''}.")
            return 1
        print("Connection test passed.")
        return 0

    def finalize(self) -> int:
        """Finalize the run and output metrics"""
        self.metrics["end_time"] = datetime.now().isoformat()
        self.metrics["success"] = len(self.metrics["errors"]) == 0

        # Output metrics as JSON
        metrics_file = Path(
            self.config.get("sync", {}).get("metrics_file", "metrics.json")
        )
        metrics_file.write_text(json.dumps(self.metrics, indent=2))

        self.log("INFO", "=== Run Summary ===")
        self.log("INFO", f"Total Uploaded: {self.metrics['total_uploaded']}")
        self.log("INFO", f"Total Downloaded: {self.metrics['total_downloaded']}")
        self.log("INFO", f"Total Errors: {len(self.metrics['errors'])}")
        self.log("INFO", f"Metrics written to: {metrics_file}")

        # Exit code: 0 if success, 1 if errors
        return 0 if self.metrics["success"] else 1


def main():
    """Main entry point"""
    import argparse

    parser = argparse.ArgumentParser(
        description="Nova Hub Client - One-shot packet sync"
    )
    parser.add_argument(
        "--config",
        default="config.toml",
        help="Path to configuration file (default: config.toml)",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate configuration without syncing packets"
    )
    parser.add_argument(
        "--test-connection",
        action="store_true",
        help="Sign in to the hub and check this config's leagues and BBS indexes "
             "against the hub's, without syncing packets"
    )

    args = parser.parse_args()

    # Handle validation mode
    if args.validate:
        from validator import ClientValidator

        validator = ClientValidator(args.config)
        success = validator.validate()
        validator.print_results()
        sys.exit(0 if success else 1)

    # Normal operation mode
    try:
        client = NovaHubClient(args.config, verbose=args.verbose)
        if args.test_connection:
            sys.exit(asyncio.run(client.check_connection()))
        exit_code = asyncio.run(client.run())
        sys.exit(exit_code)

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
