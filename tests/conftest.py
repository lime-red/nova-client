"""Pytest fixtures for nova_client integration tests."""

import asyncio
import socket
import threading
import time
from pathlib import Path
from typing import Generator

import pytest
import uvicorn

from .mock_server import app, get_storage, reset_storage


def find_free_port() -> int:
    """Find a free port for the mock server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockServerThread(threading.Thread):
    """Thread to run the mock server."""

    def __init__(self, host: str, port: int):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.server = None
        self._started = threading.Event()

    def run(self):
        """Run the uvicorn server."""
        config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level="warning",
        )
        self.server = uvicorn.Server(config)

        # Signal that we're starting
        self._started.set()

        # Run the server (blocks until shutdown)
        self.server.run()

    def wait_for_startup(self, timeout: float = 5.0):
        """Wait for the server to be ready."""
        self._started.wait(timeout=timeout)

        # Poll until server is accepting connections
        start = time.time()
        while time.time() - start < timeout:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(0.1)
                    s.connect((self.host, self.port))
                    return True
            except (ConnectionRefusedError, socket.timeout):
                time.sleep(0.05)

        raise RuntimeError(f"Mock server failed to start within {timeout}s")

    def shutdown(self):
        """Shutdown the server."""
        if self.server:
            self.server.should_exit = True


@pytest.fixture(scope="session")
def mock_server_url() -> Generator[str, None, None]:
    """Start mock server and yield its URL.

    This fixture is session-scoped for efficiency - the server runs
    for the entire test session.
    """
    host = "127.0.0.1"
    port = find_free_port()

    server_thread = MockServerThread(host, port)
    server_thread.start()
    server_thread.wait_for_startup()

    yield f"http://{host}:{port}"

    server_thread.shutdown()


@pytest.fixture
def mock_storage():
    """Get mock storage and reset it before each test."""
    reset_storage()
    return get_storage()


@pytest.fixture
def test_client_config(mock_storage):
    """Register a standard test client and return its config.

    Returns dict with client_id, client_secret, bbs_name, memberships.
    """
    config = {
        "client_id": "test_client",
        "client_secret": "test_secret",
        "bbs_name": "Test BBS",
        "memberships": [
            {"league_id": "555B", "bbs_index": 2},
        ],
    }

    mock_storage.add_client(
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        bbs_name=config["bbs_name"],
        memberships=config["memberships"],
    )

    return config


@pytest.fixture
def multi_bbs_config(mock_storage):
    """Register multiple test clients for multi-BBS testing.

    Returns dict with bbs1 and bbs2 configs.
    """
    bbs1 = {
        "client_id": "bbs1_client",
        "client_secret": "bbs1_secret",
        "bbs_name": "BBS One",
        "memberships": [{"league_id": "555B", "bbs_index": 1}],
    }

    bbs2 = {
        "client_id": "bbs2_client",
        "client_secret": "bbs2_secret",
        "bbs_name": "BBS Two",
        "memberships": [{"league_id": "555B", "bbs_index": 2}],
    }

    mock_storage.add_client(**bbs1)
    mock_storage.add_client(**bbs2)

    return {"bbs1": bbs1, "bbs2": bbs2}


@pytest.fixture
def tmp_league_dirs(tmp_path):
    """Create temporary inbound/outbound directories for a test league.

    Returns dict with inbound_dir and outbound_dir paths.
    """
    inbound = tmp_path / "inbound"
    outbound = tmp_path / "outbound"
    inbound.mkdir()
    outbound.mkdir()

    return {
        "inbound_dir": str(inbound),
        "outbound_dir": str(outbound),
    }


@pytest.fixture
def sample_config_file(tmp_path, mock_server_url, test_client_config, tmp_league_dirs):
    """Create a complete config.toml file for testing.

    Returns path to the config file.
    """
    config_content = f"""
[hub]
url = "{mock_server_url}"
client_id = "{test_client_config['client_id']}"
client_secret = "{test_client_config['client_secret']}"

[bbs]
name = "{test_client_config['bbs_name']}"

[sync]
sent_action = "delete"
max_retries = 1
retry_delay = 0

[leagues.BRE.555]
enabled = true
bbs_index = 2
inbound_dir = "{tmp_league_dirs['inbound_dir']}"
outbound_dir = "{tmp_league_dirs['outbound_dir']}"
"""

    config_path = tmp_path / "config.toml"
    config_path.write_text(config_content)

    return str(config_path)
