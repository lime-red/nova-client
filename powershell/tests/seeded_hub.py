"""Mock Nova Hub, pre-seeded with inbound work for the PowerShell client tests.

Wraps ../../python/tests/mock_server.py - the same mock the Python client's own
test suite uses, so both implementations are tested against one definition of
the API. The only thing added here is seeded state.

Seeding has to happen in-process rather than over the API because a client
cannot create the situation we need to test: the hub refuses to let a client
upload a packet claiming a *different* BBS as the source, which is exactly the
packet we need waiting for us in order to test downloading.

Run:  python powershell/tests/seeded_hub.py
Then: test_client / test_secret, league 555B, BBS index 2
"""

import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "python" / "tests"))

from fastapi import Request  # noqa: E402

from mock_server import app, storage  # noqa: E402

# A packet from BBS 01 addressed to us (02), waiting to be collected.
PAYLOAD = bytes(range(256)) * 4


def seed() -> None:
    """Reset the hub to a known starting state.

    Called at startup and from POST /__test__/reset. Each test resets first, so
    tests do not depend on each other's ordering - downloading a packet marks it
    read hub-side, which would otherwise make the second test to run see nothing.
    """
    storage.reset()

    storage.add_client(
        client_id="test_client",
        client_secret="test_secret",
        bbs_name="Test BBS",
        memberships=[{"league_id": "555B", "bbs_index": 2}],
    )

    storage.packets.append(
        {
            "id": 1,
            "filename": "555B0102.007",
            "league_number": "555",
            "game_type": "B",
            "source": "01",
            "dest": "02",
            "sequence": 7,
            "received_at": datetime.now(),
            "retrieved_at": None,
            "file_size": len(PAYLOAD),
            "file_data": PAYLOAD,
        }
    )
    storage.next_packet_id = 2

    # Format is 6 lines per entry plus a blank separator.
    storage.add_nodelist("555B", b"2\nTest BBS\n1:2/3\n\n\n\n\n")


@app.post("/__test__/reset")
async def reset_for_test() -> dict:
    """Test-only. Exists on this wrapper, never on the real hub."""
    seed()
    return {"status": "reset"}


@app.post("/__test__/nodelist/{league_id}")
async def replace_nodelist_for_test(league_id: str, request: Request) -> dict:
    """Test-only. Clients may not upload nodelists, so tests cannot change one
    through the API - but they need to, to prove the client notices a real
    change rather than merely caching forever."""
    storage.add_nodelist(league_id.upper(), await request.body())
    return {"status": "replaced"}


seed()

# The tests compare the downloaded file against this, byte for byte.
(Path(__file__).parent / "expected_packet.bin").write_bytes(PAYLOAD)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
