"""Mock Nova Hub server for integration testing.

This mock server implements the Nova Hub Service API endpoints that nova_client
uses, allowing integration tests to run without a real server.

API matches: /service/api/v1/...
"""

import hashlib
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Path as PathParam, Query, Request
from fastapi.responses import Response
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel


# --- Pydantic Models (matching nova-hub schemas) ---

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class ClientVerifyResponse(BaseModel):
    client_id: str
    bbs_name: str
    is_active: bool


class PacketInfo(BaseModel):
    filename: str
    league: str
    game_type: str
    source: str
    dest: str
    sequence: int
    received_at: datetime
    retrieved_at: Optional[datetime] = None
    file_size: int


class PacketListResponse(BaseModel):
    packets: List[PacketInfo]


class PacketUploadResponse(BaseModel):
    status: str
    filename: str
    packet_id: int


# --- In-Memory Storage ---

class MockStorage:
    """In-memory storage for mock server state."""

    def __init__(self):
        self.reset()

    def reset(self):
        """Reset all storage to initial state."""
        # Registered clients: client_id -> {secret, bbs_name, memberships: [{league_id, bbs_index}]}
        self.clients: Dict[str, dict] = {}

        # Active tokens: token -> {client_id, expires}
        self.tokens: Dict[str, dict] = {}

        # Stored packets: list of packet info dicts with file_data
        self.packets: List[dict] = []

        # Auto-increment packet ID
        self.next_packet_id: int = 1

        # Nodelists: league_id -> bytes
        self.nodelists: Dict[str, bytes] = {}
        # False plays a hub from before GET /me existed
        self.serves_account: bool = True

    def add_client(
        self,
        client_id: str,
        client_secret: str,
        bbs_name: str,
        memberships: List[dict]
    ):
        """Register a test client.

        Args:
            client_id: OAuth client ID
            client_secret: OAuth client secret
            bbs_name: BBS name for display
            memberships: List of {league_id: "555B", bbs_index: 2} dicts
        """
        self.clients[client_id] = {
            "secret": client_secret,
            "bbs_name": bbs_name,
            "memberships": memberships,
        }

    def add_nodelist(self, league_id: str, content: bytes):
        """Add a nodelist file for testing downloads."""
        self.nodelists[league_id] = content

    def queue_nodelist(self, league_id: str, content: bytes, dest: str):
        """Publish a nodelist to one member as the hub does: a packet row."""
        number, letter = parse_league_id(league_id)
        self.nodelists[league_id.upper()] = content
        self.packets.append({
            "id": self.next_packet_id,
            "filename": f"{NODELIST_PREFIXES[letter]}.{number}",
            "league_number": number,
            "game_type": letter,
            "source": "01",
            "dest": dest.upper(),
            "sequence": 0,
            "received_at": datetime.now(),
            "retrieved_at": None,
            "file_size": len(content),
            "file_data": content,
        })
        self.next_packet_id += 1


# Global storage instance
storage = MockStorage()


# --- FastAPI App ---

# Service API sub-application (matches nova-hub structure)
service_app = FastAPI(title="Mock Nova Hub - Service API")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/service/api/v1/auth/token")

# As the hub's: a lower-case game letter is accepted too
LEAGUE_ID_PATTERN = r"^\d{3}[BFIbfi]$"

# Packet filename regexes, matched against the upper-cased name as the hub does
PACKET_REGEX = re.compile(
    r"^(\d{3})([BF])([0-9A-F]{2})([0-9A-F]{2})\.(\d{3})$", re.IGNORECASE
)
# Immortal Barons: L555-<src>-<seq>-<dst>.BRP, base 36; dst 0 is a broadcast
IB_PACKET_REGEX = re.compile(
    r"^L(\d{3})-([1-9A-Z][0-9A-Z]?)-([0-9A-Z]{13})-(0|[1-9A-Z][0-9A-Z]?)\.BRP$"
)

GAME_CODES = {"B": "BRE", "F": "FE", "I": "IB"}
NODELIST_PREFIXES = {"B": "BRNODES", "F": "FENODES", "I": "IBNODES"}


def is_nodelist_name(name: str) -> bool:
    return any(name.startswith(prefix + ".") for prefix in NODELIST_PREFIXES.values())


def parse_packet_name(name: str) -> Optional[tuple]:
    """(league, game letter, source hex, dest hex, sequence) of an upper-cased name."""
    match = PACKET_REGEX.match(name)
    if match:
        league, game, source, dest, seq = match.groups()
        return league, game.upper(), source, dest, int(seq)
    match = IB_PACKET_REGEX.match(name)
    if match:
        league, source, seq, dest = match.groups()
        return league, "I", format(int(source, 36), "02X"), format(int(dest, 36), "02X"), int(seq, 36)
    return None


def parse_league_id(league_id: str) -> tuple:
    """Parse league_id like '555B' into ('555', 'B')."""
    match = re.match(r'^(\d{3})([BFI])$', league_id.upper())
    if not match:
        raise HTTPException(400, f"Invalid league_id format: {league_id}")
    return match.groups()


def members_of(league_id: str) -> List[str]:
    """Every member's index in a league, as two hex digits."""
    return sorted(
        format(m["bbs_index"], "02X")
        for client in storage.clients.values()
        for m in client["memberships"]
        if m["league_id"].upper() == league_id.upper()
    )


def verify_token(token: str = Depends(oauth2_scheme)) -> dict:
    """Verify OAuth token and return client info."""
    if token not in storage.tokens:
        raise HTTPException(401, "Invalid token", headers={"WWW-Authenticate": "Bearer"})

    token_data = storage.tokens[token]
    if datetime.now() > token_data["expires"]:
        raise HTTPException(401, "Token expired", headers={"WWW-Authenticate": "Bearer"})

    client_id = token_data["client_id"]
    if client_id not in storage.clients:
        raise HTTPException(401, "Client not found", headers={"WWW-Authenticate": "Bearer"})

    return {
        "client_id": client_id,
        **storage.clients[client_id]
    }


# --- Auth Endpoints ---

@service_app.post("/auth/token", response_model=TokenResponse)
async def get_token(
    grant_type: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
):
    """OAuth2 client credentials token endpoint."""
    if grant_type != "client_credentials":
        raise HTTPException(400, "Unsupported grant_type")

    if client_id not in storage.clients:
        raise HTTPException(401, "Invalid client credentials")

    client = storage.clients[client_id]
    if client["secret"] != client_secret:
        raise HTTPException(401, "Invalid client credentials")

    # Generate token
    token = secrets.token_urlsafe(32)
    storage.tokens[token] = {
        "client_id": client_id,
        "expires": datetime.now() + timedelta(hours=1),
    }

    return TokenResponse(access_token=token, token_type="bearer", expires_in=3600)


@service_app.get("/auth/verify", response_model=ClientVerifyResponse)
async def verify_token_endpoint(client_info: dict = Depends(verify_token)):
    """Verify token and return client info."""
    return ClientVerifyResponse(
        client_id=client_info["client_id"],
        bbs_name=client_info["bbs_name"],
        is_active=True,
    )


@service_app.get("/me")
async def who_am_i(client_info: dict = Depends(verify_token)):
    """The BBS and its leagues, as nova-hub's GET /me reports them."""
    if not storage.serves_account:
        raise HTTPException(404, "Not Found")
    leagues = []
    for membership in client_info["memberships"]:
        number, letter = parse_league_id(membership["league_id"])
        leagues.append({
            "league_id": membership["league_id"],
            "game": GAME_CODES[letter],
            "number": number,
            "name": membership["league_id"],
            "bbs_index": membership["bbs_index"],
            "fidonet_address": None,
        })
    return {"client_id": client_info["client_id"], "bbs_name": client_info["bbs_name"],
            "leagues": sorted(leagues, key=lambda l: l["league_id"])}


# --- Packet Endpoints ---

@service_app.put("/leagues/{league_id}/packets/{filename}", response_model=PacketUploadResponse)
async def upload_packet(
    league_id: str = PathParam(..., pattern=LEAGUE_ID_PATTERN),
    filename: str = PathParam(...),
    request: Request = None,
    client_info: dict = Depends(verify_token),
):
    """Upload a packet."""
    league_number, league_game_type = parse_league_id(league_id)
    normalized_filename = filename.upper()

    # Block nodelist uploads
    if is_nodelist_name(normalized_filename):
        raise HTTPException(403, "Nodelist files cannot be uploaded by clients")

    # Parse filename
    parsed = parse_packet_name(normalized_filename)
    if not parsed:
        raise HTTPException(400, f"Invalid packet filename: {normalized_filename}")

    file_league, file_game, source, dest, seq = parsed

    # Verify league matches
    if file_league != league_number:
        raise HTTPException(400, f"League number mismatch: filename={file_league}, URL={league_number}")

    if file_game.upper() != league_game_type:
        raise HTTPException(400, f"Game type mismatch: filename={file_game}, URL={league_game_type}")

    # Check membership
    membership = None
    for m in client_info["memberships"]:
        if m["league_id"].upper() == league_id.upper():
            membership = m
            break

    if not membership:
        raise HTTPException(403, f"Client is not a member of league {league_id}")

    # Verify source BBS
    expected_source = format(membership["bbs_index"], "02X")
    if source.upper() != expected_source:
        raise HTTPException(
            403,
            f"Client BBS index {membership['bbs_index']} (0x{expected_source}) cannot upload from BBS {source}"
        )

    # Read body
    content = await request.body()
    if not content:
        raise HTTPException(400, "Empty request body")

    # IB never reuses a name: the same bytes again are a retry of the same
    # packet, and different bytes under that name are refused (409).
    if league_game_type == "I":
        previous = [p for p in storage.packets
                    if p["filename"] == normalized_filename and p["league_number"] == league_number]
        if previous:
            if previous[-1]["file_data"] != content:
                raise HTTPException(409, f"The hub already has a different packet named {normalized_filename}")
            return PacketUploadResponse(status="received", filename=normalized_filename,
                                        packet_id=previous[-1]["id"])

    # A row per recipient: an IB broadcast (dest 00) goes to every other member
    if league_game_type == "I" and dest == "00":
        recipients = [d for d in members_of(league_id) if d != source.upper()]
    else:
        recipients = [dest.upper()]

    for recipient in recipients:
        packet_id = storage.next_packet_id
        storage.next_packet_id += 1
        storage.packets.append({
            "id": packet_id,
            "filename": normalized_filename,
            "league_number": league_number,
            "game_type": league_game_type,
            "source": source.upper(),
            "dest": recipient,
            "sequence": seq,
            "received_at": datetime.now(),
            "retrieved_at": None,
            "file_size": len(content),
            "file_data": content,
        })

    return PacketUploadResponse(
        status="received",
        filename=normalized_filename,
        packet_id=packet_id,
    )


@service_app.get("/leagues/{league_id}/packets", response_model=PacketListResponse)
async def list_packets(
    league_id: str = PathParam(..., pattern=LEAGUE_ID_PATTERN),
    unread: bool = Query(False),
    client_info: dict = Depends(verify_token),
):
    """List packets available for this client."""
    league_number, league_game_type = parse_league_id(league_id)

    # Find client's BBS index for this league
    membership = None
    for m in client_info["memberships"]:
        if m["league_id"].upper() == league_id.upper():
            membership = m
            break

    if not membership:
        return PacketListResponse(packets=[])

    dest_hex = format(membership["bbs_index"], "02X")

    # Filter packets
    result = []
    for p in storage.packets:
        # Match league and destination
        if p["league_number"] != league_number:
            continue
        if p["game_type"] != league_game_type:
            continue
        if p["dest"] != dest_hex:
            continue

        # Filter unread if requested
        if unread and p["retrieved_at"] is not None:
            continue

        result.append(PacketInfo(
            filename=p["filename"],
            league=p["league_number"],
            game_type=p["game_type"],
            source=p["source"],
            dest=p["dest"],
            sequence=p["sequence"],
            received_at=p["received_at"],
            retrieved_at=p["retrieved_at"],
            file_size=p["file_size"],
        ))

    return PacketListResponse(packets=result)


@service_app.get("/leagues/{league_id}/packets/{filename}")
async def download_packet(
    league_id: str = PathParam(..., pattern=LEAGUE_ID_PATTERN),
    filename: str = PathParam(...),
    client_info: dict = Depends(verify_token),
):
    """Download a specific packet."""
    league_number, league_game_type = parse_league_id(league_id)
    normalized_filename = filename.upper()

    # Find client membership
    membership = None
    for m in client_info["memberships"]:
        if m["league_id"].upper() == league_id.upper():
            membership = m
            break

    # Check if nodelist
    if is_nodelist_name(normalized_filename):
        # One the hub published to this member, as a packet row
        member_hex = format(membership["bbs_index"], "02X") if membership else None
        for p in storage.packets:
            if p["filename"] == normalized_filename and p["dest"] == member_hex:
                p["retrieved_at"] = datetime.now()
                return Response(content=p["file_data"], media_type="application/octet-stream",
                                headers={"Content-Disposition": f"attachment; filename={normalized_filename}"})
        # Return nodelist if available
        if league_id.upper() in storage.nodelists:
            return Response(
                content=storage.nodelists[league_id.upper()],
                media_type="application/octet-stream",
                headers={"Content-Disposition": f"attachment; filename={normalized_filename}"}
            )
        raise HTTPException(404, "Nodelist not found")

    # Parse filename
    parsed = parse_packet_name(normalized_filename)
    if not parsed:
        raise HTTPException(400, "Invalid packet filename")

    file_league, file_game, source, dest, seq = parsed

    if not membership:
        raise HTTPException(403, "Client is not a member of this league")

    # Verify destination (a broadcast's name says 00; its rows say who)
    expected_dest = format(membership["bbs_index"], "02X")
    if dest.upper() not in (expected_dest, "00"):
        raise HTTPException(403, f"Cannot download packets for BBS {dest}")

    # Find packet
    packet = None
    for p in storage.packets:
        if p["filename"] == normalized_filename and p["dest"] == expected_dest:
            packet = p
            break

    if not packet:
        raise HTTPException(404, "Packet not found")

    # Mark as downloaded
    packet["retrieved_at"] = datetime.now()

    return Response(
        content=packet["file_data"],
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename={normalized_filename}"}
    )


@service_app.get("/leagues/{league_id}/nodelist")
async def download_nodelist(
    league_id: str = PathParam(..., pattern=LEAGUE_ID_PATTERN),
    request: Request = None,
    client_info: dict = Depends(verify_token),
):
    """Download nodelist for a league."""
    league_number, league_game_type = parse_league_id(league_id)

    # Check membership
    membership = None
    for m in client_info["memberships"]:
        if m["league_id"].upper() == league_id.upper():
            membership = m
            break

    if not membership:
        raise HTTPException(403, f"Client is not a member of league {league_id}")

    # Get nodelist
    if league_id.upper() not in storage.nodelists:
        raise HTTPException(404, f"Nodelist not available for league {league_id}")

    # Determine filename
    filename = f"{NODELIST_PREFIXES[league_game_type]}.{league_number}"

    # Mirrors the real hub: ETag plus 304 on a matching If-None-Match. Clients
    # poll far more often than nodelists change, so this is the path they take
    # almost every cycle and the mock has to cover it.
    content = storage.nodelists[league_id.upper()]
    etag = f'"{hashlib.md5(content).hexdigest()}"'

    if_none_match = request.headers.get("if-none-match") if request else None
    if if_none_match:
        candidates = {
            tag.strip().removeprefix("W/").strip('"')
            for tag in if_none_match.split(",")
        }
        if "*" in candidates or etag.strip('"') in candidates:
            return Response(status_code=304, headers={"ETag": etag})

    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "ETag": etag,
        },
    )


# --- Main Application ---

app = FastAPI(title="Mock Nova Hub")
app.mount("/service/api/v1", service_app)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy", "mock": True}


def get_storage() -> MockStorage:
    """Get the global storage instance for test setup."""
    return storage


def reset_storage():
    """Reset storage between tests."""
    storage.reset()


if __name__ == "__main__":
    import uvicorn

    # Add a test client for manual testing
    storage.add_client(
        client_id="test_client",
        client_secret="test_secret",
        bbs_name="Test BBS",
        memberships=[{"league_id": "555B", "bbs_index": 2}]
    )

    print("Starting Mock Nova Hub...")
    print("Test credentials: test_client / test_secret")
    print("League membership: 555B, BBS index 2")
    uvicorn.run(app, host="127.0.0.1", port=8000)
