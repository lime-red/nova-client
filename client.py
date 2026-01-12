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
from typing import Dict, List, Optional

import aiohttp
import toml

# Load environment variables
from dotenv import load_dotenv

load_dotenv()


class NovaHubClient:
    """One-shot client for syncing packets with Nova Hub"""

    def __init__(self, config_path: str = "config.toml"):
        self.config = self.load_config(config_path)
        self.metrics = {
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

    def log(self, level: str, message: str, league: str = None):
        """Simple logging"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_msg = f"[{timestamp}] [{level}] {message}"
        print(log_msg)

        if level == "ERROR":
            self.metrics["errors"].append(
                {"time": timestamp, "message": message, "league": league}
            )

    async def run(self):
        """Execute one complete sync run"""
        self.log("INFO", "Nova Hub Client starting")
        self.log("INFO", f"BBS: {self.config['bbs']['name']}")
        self.log("INFO", f"Hub: {self.config['hub']['url']}")

        async with aiohttp.ClientSession() as session:
            self.session = session

            # Get OAuth token
            token = await self.get_token()
            if not token:
                self.log("ERROR", "Failed to obtain OAuth token")
                return self.finalize()

            self.token = token

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
        """Upload a single packet to the hub using PUT with raw body"""

        # Convert game_type to single letter (BRE -> B, FE -> F)
        game_type_letter = "B" if game_type == "BRE" else "F"

        # Construct league_id with game type (e.g., "555B" or "555F")
        league_id = f"{league_number}{game_type_letter}"
        filename = packet_file.name

        # New URL structure with filename in path
        url = f"{self.config['hub']['url']}/api/v1/leagues/{league_id}/packets/{filename}"

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/octet-stream"
        }

        max_retries = self.config.get("sync", {}).get("max_retries", 3)
        retry_delay = self.config.get("sync", {}).get("retry_delay", 5)

        # Read file as raw bytes
        file_data = packet_file.read_bytes()

        for attempt in range(max_retries):
            try:
                # PUT request with raw body
                async with self.session.put(url, headers=headers, data=file_data) as resp:
                    if resp.status == 200:
                        self.log("INFO", f"Uploaded: {filename}", league_number)
                        return True
                    elif resp.status == 401:
                        self.log("ERROR", "Token rejected by server", league_number)
                        return False
                    else:
                        error = await resp.text()
                        self.log(
                            "ERROR",
                            f"Upload failed ({resp.status}): {error}",
                            league_number,
                        )
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
            filename = packet_info["filename"]
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

        url = f"{self.config['hub']['url']}/api/v1/leagues/{league_id}/packets"

        if unread:
            url += "?unread=true"

        headers = {"Authorization": f"Bearer {self.token}"}

        try:
            async with self.session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("packets", [])
                elif resp.status == 401:
                    self.log("ERROR", "Token rejected when listing packets")
                    return []
                else:
                    error = await resp.text()
                    self.log("ERROR", f"List packets failed ({resp.status}): {error}")
                    return []
        except Exception as e:
            self.log("ERROR", f"List packets exception: {e}")
            return []

    async def download_packet(
        self, game_type: str, league_number: str, filename: str, inbound_dir: Path
    ) -> bool:
        """Download a single packet from the hub"""
        # Convert game_type to single letter (BRE -> B, FE -> F)
        game_type_letter = "B" if game_type == "BRE" else "F"

        # Construct league_id with game type (e.g., "555B" or "555F")
        league_id = f"{league_number}{game_type_letter}"

        url = f"{self.config['hub']['url']}/api/v1/leagues/{league_id}/packets/{filename}"

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
                        self.log("ERROR", "Token rejected when downloading packet")
                        return False
                    else:
                        error = await resp.text()
                        self.log("ERROR", f"Download failed ({resp.status}): {error}")
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

            dest = archive_dir / packet_file.name
            packet_file.rename(dest)
            self.log("DEBUG", f"Archived: {packet_file.name}")

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
        url = f"{self.config['hub']['url']}/auth/token"

        data = {
            "grant_type": "client_credentials",
            "client_id": self.config["hub"]["client_id"],
            "client_secret": self.config["hub"]["client_secret"],
        }

        try:
            async with self.session.post(url, data=data) as resp:
                if resp.status == 200:
                    token_data = await resp.json()
                    self.log("INFO", "OAuth token obtained")
                    return token_data["access_token"]
                else:
                    error = await resp.text()
                    self.log("ERROR", f"Token request failed ({resp.status}): {error}")
                    return None

        except Exception as e:
            self.log("ERROR", f"Token request exception: {e}")
            return None

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

    args = parser.parse_args()

    try:
        client = NovaHubClient(args.config)
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
