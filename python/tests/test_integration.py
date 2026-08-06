"""Integration tests for nova_client using mock server.

These tests verify the full client flow against a mock Nova Hub server,
testing authentication, packet upload, listing, and download functionality.
"""

import asyncio
import sys
from pathlib import Path

import aiohttp
import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from client import NovaHubClient


class TestAuthentication:
    """Test OAuth2 authentication flow."""

    @pytest.mark.asyncio
    async def test_get_token_success(
        self, mock_server_url, mock_storage, test_client_config
    ):
        """Test successful token acquisition."""
        async with aiohttp.ClientSession() as session:
            url = f"{mock_server_url}/service/api/v1/auth/token"
            data = {
                "grant_type": "client_credentials",
                "client_id": test_client_config["client_id"],
                "client_secret": test_client_config["client_secret"],
            }

            async with session.post(url, data=data) as resp:
                assert resp.status == 200
                token_data = await resp.json()
                assert "access_token" in token_data
                assert token_data["token_type"] == "bearer"

    @pytest.mark.asyncio
    async def test_get_token_invalid_credentials(self, mock_server_url, mock_storage):
        """Test token request with invalid credentials."""
        async with aiohttp.ClientSession() as session:
            url = f"{mock_server_url}/service/api/v1/auth/token"
            data = {
                "grant_type": "client_credentials",
                "client_id": "nonexistent",
                "client_secret": "wrong",
            }

            async with session.post(url, data=data) as resp:
                assert resp.status == 401

    @pytest.mark.asyncio
    async def test_verify_token(
        self, mock_server_url, mock_storage, test_client_config
    ):
        """Test token verification endpoint."""
        async with aiohttp.ClientSession() as session:
            # Get token first
            token_url = f"{mock_server_url}/service/api/v1/auth/token"
            data = {
                "grant_type": "client_credentials",
                "client_id": test_client_config["client_id"],
                "client_secret": test_client_config["client_secret"],
            }

            async with session.post(token_url, data=data) as resp:
                token_data = await resp.json()
                token = token_data["access_token"]

            # Verify token
            verify_url = f"{mock_server_url}/service/api/v1/auth/verify"
            headers = {"Authorization": f"Bearer {token}"}

            async with session.get(verify_url, headers=headers) as resp:
                assert resp.status == 200
                verify_data = await resp.json()
                assert verify_data["client_id"] == test_client_config["client_id"]
                assert verify_data["bbs_name"] == test_client_config["bbs_name"]


class TestPacketUpload:
    """Test packet upload functionality."""

    @pytest.mark.asyncio
    async def test_upload_packet_success(
        self, mock_server_url, mock_storage, test_client_config
    ):
        """Test successful packet upload."""
        async with aiohttp.ClientSession() as session:
            # Get token
            token_url = f"{mock_server_url}/service/api/v1/auth/token"
            data = {
                "grant_type": "client_credentials",
                "client_id": test_client_config["client_id"],
                "client_secret": test_client_config["client_secret"],
            }
            async with session.post(token_url, data=data) as resp:
                token = (await resp.json())["access_token"]

            # Upload packet (BBS 02 -> BBS 01)
            upload_url = f"{mock_server_url}/service/api/v1/leagues/555B/packets/555B0201.001"
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/octet-stream",
            }
            packet_data = b"Test packet content"

            async with session.put(upload_url, headers=headers, data=packet_data) as resp:
                assert resp.status == 200
                result = await resp.json()
                assert result["status"] == "received"
                assert result["filename"] == "555B0201.001"
                assert "packet_id" in result

    @pytest.mark.asyncio
    async def test_upload_wrong_source_bbs(
        self, mock_server_url, mock_storage, test_client_config
    ):
        """Test upload rejection when source BBS doesn't match client."""
        async with aiohttp.ClientSession() as session:
            # Get token
            token_url = f"{mock_server_url}/service/api/v1/auth/token"
            data = {
                "grant_type": "client_credentials",
                "client_id": test_client_config["client_id"],
                "client_secret": test_client_config["client_secret"],
            }
            async with session.post(token_url, data=data) as resp:
                token = (await resp.json())["access_token"]

            # Try to upload packet from BBS 03 (client is BBS 02)
            upload_url = f"{mock_server_url}/service/api/v1/leagues/555B/packets/555B0301.001"
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/octet-stream",
            }

            async with session.put(upload_url, headers=headers, data=b"data") as resp:
                assert resp.status == 403

    @pytest.mark.asyncio
    async def test_upload_nodelist_blocked(
        self, mock_server_url, mock_storage, test_client_config
    ):
        """Test that nodelist uploads are blocked."""
        async with aiohttp.ClientSession() as session:
            # Get token
            token_url = f"{mock_server_url}/service/api/v1/auth/token"
            data = {
                "grant_type": "client_credentials",
                "client_id": test_client_config["client_id"],
                "client_secret": test_client_config["client_secret"],
            }
            async with session.post(token_url, data=data) as resp:
                token = (await resp.json())["access_token"]

            # Try to upload nodelist
            upload_url = f"{mock_server_url}/service/api/v1/leagues/555B/packets/BRNODES.555"
            headers = {"Authorization": f"Bearer {token}"}

            async with session.put(upload_url, headers=headers, data=b"data") as resp:
                assert resp.status == 403


class TestPacketListing:
    """Test packet listing functionality."""

    @pytest.mark.asyncio
    async def test_list_packets_empty(
        self, mock_server_url, mock_storage, test_client_config
    ):
        """Test listing packets when none available."""
        async with aiohttp.ClientSession() as session:
            # Get token
            token_url = f"{mock_server_url}/service/api/v1/auth/token"
            data = {
                "grant_type": "client_credentials",
                "client_id": test_client_config["client_id"],
                "client_secret": test_client_config["client_secret"],
            }
            async with session.post(token_url, data=data) as resp:
                token = (await resp.json())["access_token"]

            # List packets
            list_url = f"{mock_server_url}/service/api/v1/leagues/555B/packets"
            headers = {"Authorization": f"Bearer {token}"}

            async with session.get(list_url, headers=headers) as resp:
                assert resp.status == 200
                result = await resp.json()
                assert result["packets"] == []

    @pytest.mark.asyncio
    async def test_list_packets_filters_by_destination(
        self, mock_server_url, mock_storage, multi_bbs_config
    ):
        """Test that listing only shows packets for the client's BBS."""
        async with aiohttp.ClientSession() as session:
            # Get token for BBS 1
            token_url = f"{mock_server_url}/service/api/v1/auth/token"
            data = {
                "grant_type": "client_credentials",
                "client_id": multi_bbs_config["bbs1"]["client_id"],
                "client_secret": multi_bbs_config["bbs1"]["client_secret"],
            }
            async with session.post(token_url, data=data) as resp:
                bbs1_token = (await resp.json())["access_token"]

            # Get token for BBS 2
            data = {
                "grant_type": "client_credentials",
                "client_id": multi_bbs_config["bbs2"]["client_id"],
                "client_secret": multi_bbs_config["bbs2"]["client_secret"],
            }
            async with session.post(token_url, data=data) as resp:
                bbs2_token = (await resp.json())["access_token"]

            # BBS 1 uploads packet to BBS 2
            upload_url = f"{mock_server_url}/service/api/v1/leagues/555B/packets/555B0102.001"
            headers = {
                "Authorization": f"Bearer {bbs1_token}",
                "Content-Type": "application/octet-stream",
            }
            async with session.put(upload_url, headers=headers, data=b"packet") as resp:
                assert resp.status == 200

            # BBS 2 should see the packet
            list_url = f"{mock_server_url}/service/api/v1/leagues/555B/packets"
            headers = {"Authorization": f"Bearer {bbs2_token}"}
            async with session.get(list_url, headers=headers) as resp:
                result = await resp.json()
                assert len(result["packets"]) == 1
                assert result["packets"][0]["filename"] == "555B0102.001"

            # BBS 1 should NOT see the packet (it's not destined for them)
            headers = {"Authorization": f"Bearer {bbs1_token}"}
            async with session.get(list_url, headers=headers) as resp:
                result = await resp.json()
                assert len(result["packets"]) == 0


class TestPacketDownload:
    """Test packet download functionality."""

    @pytest.mark.asyncio
    async def test_download_packet_success(
        self, mock_server_url, mock_storage, multi_bbs_config
    ):
        """Test successful packet download."""
        async with aiohttp.ClientSession() as session:
            # Get tokens
            token_url = f"{mock_server_url}/service/api/v1/auth/token"

            data = {
                "grant_type": "client_credentials",
                "client_id": multi_bbs_config["bbs1"]["client_id"],
                "client_secret": multi_bbs_config["bbs1"]["client_secret"],
            }
            async with session.post(token_url, data=data) as resp:
                bbs1_token = (await resp.json())["access_token"]

            data = {
                "grant_type": "client_credentials",
                "client_id": multi_bbs_config["bbs2"]["client_id"],
                "client_secret": multi_bbs_config["bbs2"]["client_secret"],
            }
            async with session.post(token_url, data=data) as resp:
                bbs2_token = (await resp.json())["access_token"]

            # BBS 1 uploads packet to BBS 2
            packet_content = b"Test packet data for download"
            upload_url = f"{mock_server_url}/service/api/v1/leagues/555B/packets/555B0102.001"
            headers = {
                "Authorization": f"Bearer {bbs1_token}",
                "Content-Type": "application/octet-stream",
            }
            async with session.put(upload_url, headers=headers, data=packet_content) as resp:
                assert resp.status == 200

            # BBS 2 downloads the packet
            download_url = f"{mock_server_url}/service/api/v1/leagues/555B/packets/555B0102.001"
            headers = {"Authorization": f"Bearer {bbs2_token}"}
            async with session.get(download_url, headers=headers) as resp:
                assert resp.status == 200
                downloaded = await resp.read()
                assert downloaded == packet_content

    @pytest.mark.asyncio
    async def test_download_marks_as_retrieved(
        self, mock_server_url, mock_storage, multi_bbs_config
    ):
        """Test that downloading marks packet as retrieved."""
        async with aiohttp.ClientSession() as session:
            # Get tokens
            token_url = f"{mock_server_url}/service/api/v1/auth/token"

            data = {
                "grant_type": "client_credentials",
                "client_id": multi_bbs_config["bbs1"]["client_id"],
                "client_secret": multi_bbs_config["bbs1"]["client_secret"],
            }
            async with session.post(token_url, data=data) as resp:
                bbs1_token = (await resp.json())["access_token"]

            data = {
                "grant_type": "client_credentials",
                "client_id": multi_bbs_config["bbs2"]["client_id"],
                "client_secret": multi_bbs_config["bbs2"]["client_secret"],
            }
            async with session.post(token_url, data=data) as resp:
                bbs2_token = (await resp.json())["access_token"]

            # BBS 1 uploads packet to BBS 2
            upload_url = f"{mock_server_url}/service/api/v1/leagues/555B/packets/555B0102.001"
            headers = {
                "Authorization": f"Bearer {bbs1_token}",
                "Content-Type": "application/octet-stream",
            }
            async with session.put(upload_url, headers=headers, data=b"data") as resp:
                assert resp.status == 200

            # BBS 2 lists unread - should see packet
            list_url = f"{mock_server_url}/service/api/v1/leagues/555B/packets?unread=true"
            headers = {"Authorization": f"Bearer {bbs2_token}"}
            async with session.get(list_url, headers=headers) as resp:
                result = await resp.json()
                assert len(result["packets"]) == 1

            # Download the packet
            download_url = f"{mock_server_url}/service/api/v1/leagues/555B/packets/555B0102.001"
            async with session.get(download_url, headers=headers) as resp:
                assert resp.status == 200

            # Now listing unread should be empty
            async with session.get(list_url, headers=headers) as resp:
                result = await resp.json()
                assert len(result["packets"]) == 0


class TestFullClientFlow:
    """Test the full NovaHubClient flow."""

    @pytest.mark.asyncio
    async def test_client_upload_flow(
        self, sample_config_file, mock_storage, tmp_league_dirs
    ):
        """Test client uploads outbound packets."""
        # Create a packet file in the outbound directory
        outbound = Path(tmp_league_dirs["outbound_dir"])
        packet_file = outbound / "555B0201.001"
        packet_file.write_bytes(b"Outbound packet content")

        # Run the client
        client = NovaHubClient(sample_config_file)
        exit_code = await client.run()

        # Should succeed
        assert exit_code == 0

        # Packet should be uploaded (and deleted due to sent_action=delete)
        assert not packet_file.exists()

        # Mock storage should have the packet
        assert len(mock_storage.packets) == 1
        assert mock_storage.packets[0]["filename"] == "555B0201.001"

    @pytest.mark.asyncio
    async def test_client_download_flow(
        self, sample_config_file, mock_storage, tmp_league_dirs, test_client_config
    ):
        """Test client downloads inbound packets."""
        # Add another BBS that will send packets to us
        mock_storage.add_client(
            client_id="other_bbs",
            client_secret="other_secret",
            bbs_name="Other BBS",
            memberships=[{"league_id": "555B", "bbs_index": 1}],
        )

        # Manually add a packet destined for BBS 02 (our test client)
        mock_storage.packets.append({
            "id": 1,
            "filename": "555B0102.001",
            "league_number": "555",
            "game_type": "B",
            "source": "01",
            "dest": "02",
            "sequence": 1,
            "received_at": __import__("datetime").datetime.now(),
            "retrieved_at": None,
            "file_size": 20,
            "file_data": b"Inbound packet data",
        })
        mock_storage.next_packet_id = 2

        # Run the client
        client = NovaHubClient(sample_config_file)
        exit_code = await client.run()

        # Should succeed
        assert exit_code == 0

        # Packet should be downloaded to inbound directory
        inbound = Path(tmp_league_dirs["inbound_dir"])
        downloaded_file = inbound / "555B0102.001"
        assert downloaded_file.exists()
        assert downloaded_file.read_bytes() == b"Inbound packet data"
