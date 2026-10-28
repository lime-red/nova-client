# client.py

import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
import toml

from _version import USER_AGENT, __version__
from instance_lock import InstanceLock

# Load environment variables
from dotenv import load_dotenv

load_dotenv()

# Config sections name the game; the hub's league ids carry its letter.
GAME_LETTERS = {"BRE": "B", "FE": "F", "IB": "I"}

# The file each game reads its league roster from, in its own folder.
NODES_FILES = {"BRE": "brnodes.dat", "FE": "fenodes.dat", "IB": "ibnodes.dat"}

# Immortal Barons names a packet once and never again, so its sent archive
# grows forever unless pruned, and a name already in inbound is never
# replaced. BRE and FE wrap their sequence numbers and reuse names.
NAMES_NEVER_REPEAT = {"IB"}
DEFAULT_ARCHIVE_DAYS = 30

# The hub refuses larger uploads, so a larger download is not a packet.
MAX_PACKET_SIZE = 10 * 1024 * 1024

# Worth another try: the hub, or the proxy in front of it, is briefly
# unavailable or asking us to slow down. Anything else will fail the same way.
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
MAX_RETRY_AFTER = 60

# Re-authenticate this long before the hub says the token expires.
TOKEN_MARGIN = timedelta(seconds=60)

SENT_ACTIONS = ("delete", "archive")

# fullmatch, not match with $: $ also matches before a trailing newline.
# BRE and FE: 555B0201.001, league + game + source + dest (hex) . sequence
PACKET_NAME = re.compile(r"[0-9]{3}[BF][0-9A-F]{4}\.[0-9]{3}", re.IGNORECASE)
# Immortal Barons: L555-2-<sequence>-1.brp, nodes and a 13-digit sequence in
# base 36, without leading zeros on the nodes; destination 0 is a broadcast,
# which the hub hands to every board in the league.
IB_PACKET_NAME = re.compile(
    r"L(?P<league>[0-9]{3})-(?P<src>[1-9A-Z][0-9A-Z]?)-[0-9A-Z]{13}-(?:0|[1-9A-Z][0-9A-Z]?)\.BRP",
    re.IGNORECASE,
)
NODELIST_NAME = re.compile(r"(BR|FE|IB)NODES\.[0-9]{3}", re.IGNORECASE)


def is_packet_name(name: str) -> bool:
    """A packet of any game, by its filename alone."""
    return bool(PACKET_NAME.fullmatch(name) or IB_PACKET_NAME.fullmatch(name))


def hub_league_id(game_type: str, league_number: str) -> str:
    """The hub's id for a config league: [leagues.BRE.555] is "555B"."""
    letter = GAME_LETTERS.get(game_type.upper())
    if letter is None:
        raise ValueError(
            f"[leagues.{game_type}.{league_number}]: unknown game '{game_type}' (use BRE, FE or IB)"
        )
    return f"{league_number}{letter}"


def unprocessed_packets(inbound_dir) -> List[str]:
    """Packets and nodelists in inbound, by name.

    Straight after maintenance the game should have taken in everything here,
    so whatever is left it skipped: a game that cannot see its inbound folder
    (a non-8.3 path, say), or a packet it will not read (#15).
    """
    try:
        names = [p.name for p in Path(inbound_dir).iterdir() if p.is_file()]
    except OSError:
        return []
    return sorted(n for n in names if is_packet_name(n) or NODELIST_NAME.fullmatch(n))


class ResponseTooLarge(Exception):
    pass


class NovaHubClient:
    """One-shot client for syncing packets with Nova Hub"""

    def __init__(
        self,
        config_path: str = "config.toml",
        verbose: bool = False,
        token_cache: Optional[dict] = None,
        unprocessed: Optional[Dict[str, List[str]]] = None,
    ):
        self.verbose = verbose
        self.config_path = config_path
        self.config = self.load_config(config_path)
        # The daemon passes the same dict every cycle, so one token lasts
        # until it expires or the hub refuses it.
        self.token_cache = token_cache if token_cache is not None else {}
        # What the daemon's last maintenance left in each league's inbound,
        # for metrics.json. None from a one-shot run, which has no maintenance
        # to judge by, so the keys are left out rather than written as 0.
        self.unprocessed = unprocessed
        self.upload_only = False
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

        hub = config.setdefault("hub", {})
        if not hub.get("url"):
            raise ValueError("hub.url must be set in config")
        # A trailing slash would make every path //service/..., which the hub 404s
        hub["url"] = hub["url"].rstrip("/")

        # Override with environment variables. An empty one, as an unset
        # entry in an EnvironmentFile leaves, does not count.
        hub["client_id"] = os.getenv("HUB_CLIENT_ID") or hub.get("client_id", "")
        hub["client_secret"] = os.getenv("HUB_CLIENT_SECRET") or hub.get("client_secret", "")

        if not hub["client_id"] or not hub["client_secret"]:
            raise ValueError(
                "Client ID and Secret must be set in config or environment"
            )

        sent_action = config.get("sync", {}).get("sent_action", "delete")
        if sent_action not in SENT_ACTIONS:
            # Anything else left each packet in outbound, to be sent again every sync
            raise ValueError(
                f"sync.sent_action must be 'delete' or 'archive', got '{sent_action}'"
            )
        archive_days = config.get("sync", {}).get("archive_days", DEFAULT_ARCHIVE_DAYS)
        if isinstance(archive_days, bool) or not isinstance(archive_days, int) or archive_days < 0:
            raise ValueError(
                f"sync.archive_days must be a whole number of days (0 keeps all), got {archive_days!r}"
            )

        # Validate that each enabled league has bbs_index configured (as integer, 1-255)
        for game_type, leagues in config.get("leagues", {}).items():
            for league_number, league_config in leagues.items():
                if league_config.get("enabled", True):
                    hub_league_id(game_type, league_number)
                    bbs_index = league_config.get("bbs_index")
                    if bbs_index is None:
                        raise ValueError(
                            f"League {game_type}.{league_number} is enabled but missing 'bbs_index' configuration"
                        )
                    # bool is an int to Python: bbs_index = true would be BBS 1
                    if isinstance(bbs_index, bool) or not isinstance(bbs_index, int):
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

        # A packet (555B0102.001, L555-2-<seq>-1.BRP) or a nodelist (BRNODES.555)
        if not (is_packet_name(safe_name) or NODELIST_NAME.fullmatch(safe_name)):
            raise ValueError(f"Filename does not match expected format: {filename}")

        return safe_name

    async def run(self, locked: bool = False, upload_only: bool = False):
        """Execute one complete sync run.

        locked: the caller (the daemon) already holds the instance lock.
        upload_only: send outbound packets and download nothing, for the pass
        after maintenance has written replies.
        """
        if locked:
            return await self._run(upload_only)

        lock = InstanceLock(self.config_path)
        try:
            acquired = lock.acquire()
        except OSError as e:
            # Not being able to lock must not stop a node syncing
            self.log("WARNING", f"Could not open lock file {lock.path} ({e}); syncing unlocked")
            return await self._run(upload_only)
        if not acquired:
            self.log(
                "ERROR",
                "Another nova-client (the daemon, or another client.py) is syncing "
                "with this config. Try again when it has finished.",
            )
            return 1
        try:
            return await self._run(upload_only)
        finally:
            lock.release()

    async def _run(self, upload_only: bool = False):
        self.upload_only = upload_only
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

            if not self._cached_token():
                token = await self.get_token()
                if not token:
                    self.log("ERROR", "Failed to obtain OAuth token")
                    return self.finalize()

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
        downloaded = 0
        if not self.upload_only:
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
            if not file.is_file():
                continue
            if self.is_packet_file(file.name, game_type, league_number, league_config):
                packet_files.append(file)
            elif game_type.upper() == "IB" and file.suffix.lower() == ".brp":
                # The game names a packet without node numbers until it has a
                # roster, and the hub refuses those; say so, or it sits here.
                self.log(
                    "WARNING",
                    f"Not sending {file.name}: it is not named as a packet from BBS "
                    f"#{league_config.get('bbs_index')} of league {league_number}. A name "
                    f"without node numbers means the game has no ibnodes.dat yet.",
                    league_number,
                )

        if not packet_files:
            self.log("DEBUG", f"No outbound packets in {outbound_dir}", league_number)
            return 0

        self.log("INFO", f"Found {len(packet_files)} outbound packet(s)", league_number)

        uploaded = 0
        for packet_file in packet_files:
            success = await self.upload_packet(game_type, league_number, packet_file)
            if success:
                uploaded += 1
                # The hub has the packet. Failing to tidy it away must not
                # stop this sync before it downloads anything.
                try:
                    await self.handle_sent_file(packet_file)
                except OSError as e:
                    self.log(
                        "ERROR",
                        f"Uploaded {packet_file.name}, but could not remove it from outbound: {e}",
                        league_number,
                    )
                    self.metrics["leagues"][f"{game_type}_{league_number}"]["errors"] += 1
            else:
                self.log(
                    "ERROR", f"Failed to upload: {packet_file.name}", league_number
                )
                self.metrics["leagues"][f"{game_type}_{league_number}"]["errors"] += 1

        if uploaded and game_type.upper() in NAMES_NEVER_REPEAT:
            self.prune_archive(league_number)

        return uploaded

    def _cached_token(self) -> Optional[str]:
        """The token from an earlier sync, if the hub has not said it has expired."""
        token = self.token_cache.get("access_token")
        expires_at = self.token_cache.get("expires_at")
        if token and (expires_at is None or datetime.now() + TOKEN_MARGIN < expires_at):
            return token
        return None

    @property
    def token(self) -> Optional[str]:
        return self.token_cache.get("access_token")

    async def _request(
        self,
        method: str,
        path: str,
        league_number: Optional[str] = None,
        upload: Optional[Path] = None,
        max_bytes: int = MAX_PACKET_SIZE,
    ):
        """Send one API request, retrying where another try can help.

        Retries a connection failure and the statuses in RETRY_STATUSES, up to
        sync.max_retries attempts, waiting the hub's Retry-After when it gives
        one. A 401 gets one fresh token and one more try, without spending an
        attempt: a cached token can expire between syncs.

        Returns (status, body); status is None if every attempt failed to
        connect. Raises ResponseTooLarge if the body exceeds max_bytes.
        """
        url = f"{self.config['hub']['url']}/service/api/v1{path}"
        max_retries = max(1, self.config.get("sync", {}).get("max_retries", 3))
        retry_delay = self.config.get("sync", {}).get("retry_delay", 5)
        reauthenticated = False
        attempt = 0

        while True:
            attempt += 1
            headers = {"Authorization": f"Bearer {self.token}"}
            body_file = None
            wait = retry_delay
            try:
                if upload is not None:
                    headers["Content-Type"] = "application/octet-stream"
                    body_file = open(upload, "rb")
                async with self.session.request(
                    method, url, headers=headers, data=body_file
                ) as resp:
                    status = resp.status
                    body = await self._read_capped(resp, max_bytes)
                    retry_after = resp.headers.get("Retry-After", "")
            except ResponseTooLarge:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                self.log(
                    "ERROR",
                    f"{method} {path} failed (attempt {attempt}): {e or type(e).__name__}",
                    league_number,
                )
                if attempt >= max_retries:
                    return None, b""
                await asyncio.sleep(wait)
                continue
            finally:
                if body_file is not None:
                    body_file.close()

            if status == 401 and not reauthenticated:
                reauthenticated = True
                attempt -= 1
                self.token_cache.clear()
                if await self.get_token():
                    continue
                return status, body

            if status in RETRY_STATUSES and attempt < max_retries:
                if retry_after.isdigit():
                    wait = min(int(retry_after), MAX_RETRY_AFTER)
                self.log(
                    "WARNING",
                    f"{method} {path}: HTTP {status}, retrying in {wait}s",
                    league_number,
                )
                await asyncio.sleep(wait)
                continue

            return status, body

    @staticmethod
    async def _read_capped(resp, max_bytes: int) -> bytes:
        declared = resp.content_length
        if declared is not None and declared > max_bytes:
            raise ResponseTooLarge(f"{declared} bytes")
        chunks = []
        size = 0
        async for chunk in resp.content.iter_chunked(64 * 1024):
            size += len(chunk)
            if size > max_bytes:
                raise ResponseTooLarge(f"over {max_bytes} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

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

        try:
            file_size = packet_file.stat().st_size
            if file_size > MAX_PACKET_SIZE:
                self.log(
                    "ERROR",
                    f"File too large for upload: {filename} ({file_size} bytes)",
                    league_number,
                )
                return False
        except OSError as e:
            self.log("ERROR", f"Could not stat file: {e}", league_number)
            return False

        league_id = hub_league_id(game_type, league_number)
        status, body = await self._request(
            "PUT", f"/leagues/{league_id}/packets/{filename}",
            league_number, upload=packet_file, max_bytes=1024 * 1024,
        )
        if status == 200:
            self.log("INFO", f"Uploaded: {filename}", league_number)
            return True
        if status is not None:
            self.log("ERROR", f"Upload failed ({status})", league_number)
            self.log_api_response(body.decode(errors="replace"), "upload error")
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

            dest_file = self.download_destination(game_type, filename, inbound_dir, league_config)
            if dest_file is None:
                self.log(
                    "ERROR",
                    f"Not downloading {filename}: game_folder is not set, and "
                    f"{NODES_FILES[game_type.upper()]} belongs there",
                    league_number,
                )
                self.metrics["leagues"][f"{game_type}_{league_number}"]["errors"] += 1
                continue
            success = await self.download_packet(
                game_type, league_number, filename, inbound_dir, dest_file=dest_file
            )
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
        league_id = hub_league_id(game_type, league_number)
        path = f"/leagues/{league_id}/packets" + ("?unread=true" if unread else "")

        try:
            status, body = await self._request("GET", path, league_number)
        except ResponseTooLarge as e:
            self.log("ERROR", f"Packet list too large ({e})", league_number)
            return []
        if status == 200:
            try:
                data = json.loads(body)
            except ValueError:
                self.log("ERROR", "List packets: the hub's reply is not JSON", league_number)
                return []
            self.log_api_response(json.dumps(data), "list packets")
            return data.get("packets", [])
        if status is not None:
            self.log("ERROR", f"List packets failed ({status})", league_number)
            self.log_api_response(body.decode(errors="replace"), "list packets error")
        return []

    @staticmethod
    def download_destination(
        game_type: str, filename: str, inbound_dir: Path, league_config: dict
    ) -> Optional[Path]:
        """Where a file from the hub goes: inbound, except an IB roster.

        BRE and FE take their nodelist in from inbound. Immortal Barons reads
        packets only (*.brp) and its roster from ibnodes.dat in its data
        directory, which is game_folder; it will not exchange packets without
        one, and keeps it current itself once it has one. None if that is
        where it belongs and game_folder is not set.
        """
        if game_type.upper() == "IB" and NODELIST_NAME.fullmatch(filename):
            game_folder = league_config.get("game_folder")
            return Path(game_folder) / NODES_FILES["IB"] if game_folder else None
        return Path(inbound_dir) / filename

    async def download_packet(
        self, game_type: str, league_number: str, filename: str, inbound_dir: Path,
        dest_file: Optional[Path] = None,
    ) -> bool:
        """Download a single packet from the hub.

        The hub counts a packet as delivered once it has sent it, so a
        truncated file would be the only copy this BBS ever gets. It is written
        to a .part file and renamed into place only once it is complete; the
        game never sees a partial packet.

        A game whose packet names never repeat (IB) never has one replaced:
        the same bytes already waiting are the same packet, and different
        bytes under that name are kept and the download refused, rather than
        renamed (.br1) where the game would never read it.
        """
        # Defense-in-depth: validate filename even though caller should sanitize
        try:
            filename = self.sanitize_filename(filename)
        except ValueError as e:
            self.log("ERROR", f"Invalid filename in download_packet: {e}", league_number)
            return False

        league_id = hub_league_id(game_type, league_number)
        try:
            status, body = await self._request(
                "GET", f"/leagues/{league_id}/packets/{filename}", league_number
            )
        except ResponseTooLarge as e:
            self.log("ERROR", f"Refused {filename}: larger than a packet can be ({e})", league_number)
            return False

        if status != 200:
            if status is not None:
                self.log("ERROR", f"Download failed ({status})", league_number)
                self.log_api_response(body.decode(errors="replace"), "download error")
            return False

        if dest_file is None:
            dest_file = inbound_dir / filename
        if game_type.upper() == "IB" and NODELIST_NAME.fullmatch(filename) and dest_file.exists():
            # IB rewrites ibnodes.dat itself from the Coordinator's signed
            # roster, so the hub's copy only starts a board that has none.
            # The download still counts: it is what marks the roster delivered.
            self.log("INFO", f"{filename}: the game keeps its own {dest_file.name}; left it as it is", league_number)
            return True
        if game_type.upper() in NAMES_NEVER_REPEAT and is_packet_name(filename) and dest_file.exists():
            try:
                same = dest_file.read_bytes() == body
            except OSError as e:
                self.log("ERROR", f"Could not read {dest_file}: {e}", league_number)
                return False
            if same:
                self.log("INFO", f"Downloaded: {filename}, already in inbound", league_number)
                return True
            self.log(
                "ERROR",
                f"{filename} is already in inbound with different contents; kept that one. "
                "The hub keeps its copy",
                league_number,
            )
            return False
        part_file = dest_file.with_name(dest_file.name + ".part")
        try:
            part_file.write_bytes(body)
            os.replace(part_file, dest_file)
        except OSError as e:
            self.log("ERROR", f"Could not write {dest_file}: {e}", league_number)
            part_file.unlink(missing_ok=True)
            return False
        self.log("INFO", f"Downloaded: {filename} ({len(body)} bytes)", league_number)
        return True

    async def handle_sent_file(self, packet_file: Path):
        """Handle a packet file after successful upload"""
        action = self.config.get("sync", {}).get("sent_action", "delete")

        if action == "delete":
            packet_file.unlink(missing_ok=True)
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
            # BRE and FE sequence numbers wrap at 999 and restart after a game
            # reset, so the archive often has this name already. os.replace overwrites
            # it, where rename raises on Windows; shutil.move handles an
            # archive on another drive or mount.
            try:
                os.replace(packet_file, dest)
            except OSError:
                shutil.move(str(packet_file), str(dest))
            self.log("DEBUG", f"Archived: {safe_name}")

    def prune_archive(self, league_number: Optional[str] = None) -> int:
        """Delete archived packets of games that never reuse a name, once old.

        BRE and FE overwrite their archive as names come round again; IB's
        would grow forever. sync.archive_days, default 30; 0 keeps them all.
        """
        sync = self.config.get("sync", {})
        if sync.get("sent_action", "delete") != "archive":
            return 0
        days = sync.get("archive_days", DEFAULT_ARCHIVE_DAYS)
        if not days:
            return 0
        archive_dir = Path(sync.get("archive_dir", "./sent"))
        cutoff = time.time() - days * 86400
        removed = 0
        try:
            entries = list(archive_dir.iterdir())
        except OSError:
            return 0
        for entry in entries:
            if not IB_PACKET_NAME.fullmatch(entry.name):
                continue
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    entry.unlink()
                    removed += 1
            except OSError as e:
                self.log("WARNING", f"Could not prune {entry}: {e}", league_number)
        if removed:
            self.log("INFO", f"Pruned {removed} archived packet(s) older than {days} days", league_number)
        return removed

    def is_packet_file(
        self, filename: str, game_type: str, league_number: str, league_config: dict
    ) -> bool:
        """Check if a file is a valid packet file for this league"""
        letter = GAME_LETTERS.get(game_type.upper())
        if letter is None:
            return False
        our_bbs_index = league_config.get("bbs_index")
        if letter == "I":
            # L555-2-<seq>-1.brp: the source is in base 36
            match = IB_PACKET_NAME.fullmatch(filename)
            if not match or match["league"] != league_number:
                return False
            return our_bbs_index is not None and int(match["src"], 36) == our_bbs_index
        # Pattern: <league><game><source><dest>.<seq>
        # Example: 555B0102.001
        pattern = rf"{league_number}{letter}([0-9A-F]{{2}})[0-9A-F]{{2}}\.\d{{3}}"
        match = re.fullmatch(pattern, filename, re.IGNORECASE)

        if not match:
            return False

        # Validate that source BBS index matches our league's BBS index
        source_hex = match.group(1).upper()
        our_bbs_hex = format(our_bbs_index, "02X") if our_bbs_index else None

        return source_hex == our_bbs_hex

    async def get_token(self) -> Optional[str]:
        """Get OAuth token from the hub, and keep it for later syncs"""
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
                    self.token_cache.clear()
                    self.token_cache["access_token"] = token_data["access_token"]
                    # Older hubs do not say; the token is then kept
                    # until the hub refuses it.
                    expires_in = token_data.get("expires_in")
                    if isinstance(expires_in, int) and not isinstance(expires_in, bool):
                        self.token_cache["expires_at"] = datetime.now() + timedelta(
                            seconds=expires_in
                        )
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
        if self.unprocessed is not None:
            self.metrics["unprocessed"] = dict(self.unprocessed)
            self.metrics["total_unprocessed"] = sum(len(v) for v in self.unprocessed.values())

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
