"""Immortal Barons leagues ([leagues.IB.<n>]), against the mock hub.

What differs from BRE and FE, and why:
- Packets are L555-<src>-<seq>-<dst>.brp, nodes and sequence in base 36; a
  destination of 0 is a broadcast, which the hub hands to every board.
- A name is never reused, so one already in inbound is never replaced (the
  game reads only *.brp, so an FTN-style .br1 rename would be lost), and the
  sent archive is pruned by age.
- The roster, IBNODES.<n> from the hub, is installed as ibnodes.dat in
  game_folder, IB's data directory: IB will not exchange packets without it.
  Only where there is none, though: IB rewrites the file itself from the
  Coordinator's signed roster, and the hub's copy must not replace that.
- The Coordinator's public key comes from the hub, recorded as coord.pub in
  game_folder as `-coord-key` would; only where there is none, like the roster.
- IB is a native program: maintenance runs it directly, never under dosemu.
"""

import asyncio
import datetime
import os
import sys
import time
from pathlib import Path

import pytest
import toml

sys.path.insert(0, str(Path(__file__).parent.parent))

from client import NovaHubClient, is_packet_name, unprocessed_packets  # noqa: E402
from game_runner import GameRunner, default_maintenance_args, is_native  # noqa: E402
from nodes_parser import NodesFileParser  # noqa: E402
from validator import ClientValidator  # noqa: E402

KEY = "ab" * 32


def ib_name(src: int, seq: int, dst: int, league: str = "555") -> str:
    """IB's own spelling: base 36, lower case."""
    def b36(n: int) -> str:
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"
        out = ""
        while True:
            n, r = divmod(n, 36)
            out = digits[r] + out
            if n == 0:
                return out
    return f"L{league}-{b36(src)}-{b36(seq).rjust(13, '0')}-{b36(dst)}.brp"


@pytest.fixture
def ib(tmp_path, mock_server_url, mock_storage):
    """A board at index 2 in league 555I, with boards 3 and 4 beside it."""
    mock_storage.add_client("ib_client", "ib_secret", "Board Two",
                            [{"league_id": "555I", "bbs_index": 2}])
    mock_storage.add_client("ib_three", "x", "Board Three", [{"league_id": "555I", "bbs_index": 3}])
    mock_storage.add_client("ib_four", "x", "Board Four", [{"league_id": "555I", "bbs_index": 4}])
    data = tmp_path / "ib_555"
    dirs = {"game_folder": data, "inbound_dir": data / "inbound", "outbound_dir": data / "outbound",
            "archive": tmp_path / "sent"}
    for d in dirs.values():
        d.mkdir(parents=True)
    config = {
        "hub": {"url": mock_server_url, "client_id": "ib_client", "client_secret": "ib_secret"},
        "bbs": {"name": "Board Two"},
        "sync": {"sent_action": "archive", "archive_dir": str(dirs["archive"])},
        "leagues": {"IB": {"555": {
            "enabled": True,
            "bbs_index": 2,
            "runner": "native",
            "game_folder": str(data),
            "inbound_dir": str(dirs["inbound_dir"]),
            "outbound_dir": str(dirs["outbound_dir"]),
        }}},
    }
    path = tmp_path / "config.toml"
    path.write_text(toml.dumps(config))
    return {"config": path, "storage": mock_storage, "tmp": tmp_path, **dirs}


def sync(ib) -> int:
    return asyncio.run(NovaHubClient(str(ib["config"])).run())


def queue(storage, name: str, data: bytes, dest: str, source: str = "03"):
    storage.packets.append({
        "id": storage.next_packet_id, "filename": name.upper(), "league_number": "555",
        "game_type": "I", "source": source, "dest": dest, "sequence": 1,
        "received_at": datetime.datetime.now(), "retrieved_at": None,
        "file_size": len(data), "file_data": data,
    })
    storage.next_packet_id += 1


# --- Names --------------------------------------------------------------------

def test_ib_names_are_packets():
    assert is_packet_name(ib_name(2, 1, 1))
    assert is_packet_name(ib_name(2, 1, 0).upper())
    assert is_packet_name(ib_name(35, 36 ** 5, 36))
    for bad in ("L555-02-0000000000001-1.brp",  # a leading zero
                "L555-2-001-1.brp",  # a short sequence
                ib_name(2, 1, 1) + ".tmp", ib_name(2, 1, 1)[:-1] + "1"):  # .br1
        assert not is_packet_name(bad), bad


def test_only_our_own_ib_packets_are_uploaded(tmp_path):
    client = NovaHubClient.__new__(NovaHubClient)
    league = {"bbs_index": 2}
    assert client.is_packet_file(ib_name(2, 7, 0), "IB", "555", league)
    assert client.is_packet_file(ib_name(2, 7, 1).upper(), "IB", "555", league)
    assert not client.is_packet_file(ib_name(3, 7, 1), "IB", "555", league)
    assert not client.is_packet_file(ib_name(2, 7, 1, league="556"), "IB", "555", league)
    assert not client.is_packet_file("555B0201.001", "IB", "555", league)
    # Base 36: board 37 is "11"
    assert client.is_packet_file(ib_name(37, 7, 1), "IB", "555", {"bbs_index": 37})


# --- Sync ---------------------------------------------------------------------

def test_a_broadcast_reaches_every_other_board(ib):
    name = ib_name(2, 1, 0)
    (ib["outbound_dir"] / name).write_bytes(b"to all")
    assert sync(ib) == 0
    assert not (ib["outbound_dir"] / name).exists()
    assert (ib["archive"] / name).exists()
    rows = [p for p in ib["storage"].packets if p["filename"] == name.upper()]
    assert sorted(p["dest"] for p in rows) == ["03", "04"]


def test_packets_download_into_inbound(ib):
    name = ib_name(3, 9, 2).upper()
    queue(ib["storage"], name, b"for two", "02")
    assert sync(ib) == 0
    assert (ib["inbound_dir"] / name).read_bytes() == b"for two"


def test_a_packet_already_waiting_is_never_replaced(ib):
    same, other = ib_name(3, 1, 2).upper(), ib_name(3, 2, 2).upper()
    (ib["inbound_dir"] / same).write_bytes(b"same")
    (ib["inbound_dir"] / other).write_bytes(b"what was here")
    queue(ib["storage"], same, b"same", "02")
    queue(ib["storage"], other, b"something else", "02")
    sync(ib)
    assert (ib["inbound_dir"] / same).read_bytes() == b"same"
    assert (ib["inbound_dir"] / other).read_bytes() == b"what was here"
    # Nothing renamed beside it, where IB would never look
    assert sorted(p.name for p in ib["inbound_dir"].iterdir()) == sorted([same, other])


def test_the_roster_is_installed_as_ibnodes_dat(ib):
    roster = b"1\r\nThe Hub\r\n1:1/1\r\nCity\r\nState\r\nCountry\r\n" + KEY.encode() + b"\r\n"
    ib["storage"].queue_nodelist("555I", roster, "02")
    assert sync(ib) == 0
    assert (ib["game_folder"] / "ibnodes.dat").read_bytes() == roster
    assert not list(ib["inbound_dir"].iterdir())


def test_the_games_own_roster_is_left_alone(ib):
    (ib["game_folder"] / "ibnodes.dat").write_bytes(b"the game's, signed")
    ib["storage"].queue_nodelist("555I", b"the hub's", "02")
    assert sync(ib) == 0
    assert (ib["game_folder"] / "ibnodes.dat").read_bytes() == b"the game's, signed"
    assert not list(ib["inbound_dir"].iterdir())
    # Downloaded all the same, so the hub counts it delivered
    roster = [p for p in ib["storage"].packets if p["filename"] == "IBNODES.555"]
    assert roster and all(p["retrieved_at"] for p in roster)


def test_a_packet_named_without_nodes_is_warned_about(ib, capsys):
    stray = ib["outbound_dir"] / "L555-ABCDEFGHIJKLMNOP.brp"
    stray.write_bytes(b"x")
    sync(ib)
    assert stray.exists()
    assert "no ibnodes.dat yet" in capsys.readouterr().out


COORD = "c0" * 32


def test_the_coordinator_key_is_recorded_as_coord_pub(ib):
    ib["storage"].coordinator_keys["555I"] = COORD
    assert sync(ib) == 0
    # Exactly what immortal-barons -coord-key writes
    assert (ib["game_folder"] / "coord.pub").read_text() == COORD + "\n"


def test_a_recorded_coordinator_key_is_left_alone_and_not_asked_for(ib):
    (ib["game_folder"] / "coord.pub").write_text("ab" * 32 + "\n")
    ib["storage"].coordinator_keys["555I"] = COORD
    sync(ib)
    assert (ib["game_folder"] / "coord.pub").read_text() == "ab" * 32 + "\n"
    assert ib["storage"].coordinator_key_requests == 0


def test_the_coordinator_itself_is_not_given_a_key(ib):
    (ib["game_folder"] / "coord.key").write_text("private")
    ib["storage"].coordinator_keys["555I"] = COORD
    sync(ib)
    assert not (ib["game_folder"] / "coord.pub").exists()
    assert ib["storage"].coordinator_key_requests == 0


def test_no_coordinator_key_at_the_hub_is_a_warning_not_a_failure(ib, capsys):
    assert sync(ib) == 0
    assert not (ib["game_folder"] / "coord.pub").exists()
    assert "no Coordinator key" in capsys.readouterr().out


def test_a_bad_coordinator_key_from_the_hub_is_not_written(ib):
    ib["storage"].coordinator_keys["555I"] = "not a key"
    sync(ib)
    assert not (ib["game_folder"] / "coord.pub").exists()


def test_old_archived_ib_packets_are_pruned(ib):
    old, recent = ib_name(2, 1, 1), ib_name(2, 2, 1)
    keep_sr = "555B0201.001"
    for name in (old, recent, keep_sr):
        (ib["archive"] / name).write_bytes(b"x")
    month_ago = time.time() - 31 * 86400
    for name in (old, keep_sr):
        os.utime(ib["archive"] / name, (month_ago, month_ago))
    (ib["outbound_dir"] / ib_name(2, 3, 1)).write_bytes(b"new")
    sync(ib)
    names = {p.name for p in ib["archive"].iterdir()}
    assert old not in names
    assert {recent, keep_sr, ib_name(2, 3, 1)} <= names


def test_archive_days_zero_keeps_everything(ib):
    config = toml.load(ib["config"])
    config["sync"]["archive_days"] = 0
    ib["config"].write_text(toml.dumps(config))
    old = ib["archive"] / ib_name(2, 1, 1)
    old.write_bytes(b"x")
    os.utime(old, (0, 0))
    (ib["outbound_dir"] / ib_name(2, 3, 1)).write_bytes(b"new")
    sync(ib)
    assert old.exists()


def test_unprocessed_counts_ib_packets(tmp_path):
    (tmp_path / ib_name(3, 1, 2)).write_bytes(b"x")
    (tmp_path / "ignore.tmp").write_bytes(b"x")
    assert unprocessed_packets(tmp_path) == [ib_name(3, 1, 2)]


# --- Maintenance --------------------------------------------------------------

def fake_ib(folder: Path, body: str) -> Path:
    script = folder / "immortal-barons"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    return script


@pytest.mark.skipif(sys.platform == "win32", reason="Linux runs IB natively, not through dosemu")
def test_maintenance_runs_ib_directly(tmp_path):
    game = tmp_path / "ib"
    game.mkdir()
    binary = fake_ib(game, 'echo "args: $*"; pwd > ran_in\n')
    runner = GameRunner({"daemon": {"log_dir": str(tmp_path / "logs"),
                                    "dosemu_path": "/nonexistent/dosemu"}})
    league = {"game_folder": str(game), "game_command": str(binary)}
    result = asyncio.run(runner.run_maintenance("IB", "555", league, timeout=10))
    assert result.success, result.error
    assert f"args: -maint -data {game}" in result.output
    assert (game / "ran_in").read_text().strip() == str(game.resolve())
    assert list((tmp_path / "logs").glob("IB_555_*.log"))


@pytest.mark.skipif(sys.platform == "win32", reason="Linux runs IB natively, not through dosemu")
def test_a_fault_exit_says_why(tmp_path):
    game = tmp_path / "ib"
    game.mkdir()
    binary = fake_ib(game, 'echo "planetary run done"; echo "new transport fault: hub"; exit 1\n')
    runner = GameRunner({"daemon": {"log_dir": str(tmp_path / "logs")}})
    league = {"game_folder": str(game), "game_command": str(binary)}
    result = asyncio.run(runner.run_maintenance("IB", "555", league, timeout=10))
    assert not result.success
    assert result.return_code == 1
    assert "new transport fault: hub" in result.error


@pytest.mark.skipif(sys.platform == "win32", reason="Linux runs IB natively, not through dosemu")
def test_a_hung_game_is_killed(tmp_path):
    game = tmp_path / "ib"
    game.mkdir()
    binary = fake_ib(game, "sleep 30\n")
    runner = GameRunner({"daemon": {"log_dir": str(tmp_path / "logs")}})
    league = {"game_folder": str(game), "game_command": str(binary)}
    started = time.time()
    result = asyncio.run(runner.run_maintenance("IB", "555", league, timeout=1))
    assert not result.success and "Timeout" in result.error
    assert time.time() - started < 10


def test_ib_is_native_and_bre_is_not():
    assert is_native("IB", {})
    assert not is_native("BRE", {})
    assert is_native("BRE", {"runner": "native"})
    assert default_maintenance_args("IB", "/srv/ib") == "-maint -data /srv/ib"
    assert default_maintenance_args("BRE", "/srv/bre") == "PLANETARY"


# --- Roster and validation ----------------------------------------------------

def test_the_key_line_is_read_and_does_not_shift_entries(tmp_path):
    roster = tmp_path / "ibnodes.dat"
    roster.write_text(
        f"1\nThe Hub\n1:1/1\nCity\nState\nCountry\n{KEY}\n\n"
        "2\nBoard Two\n1:1/2\nCity\nState\nCountry\n\n"
        f"3\nBoard Three\n1:1/3\nCity\nState\nCountry\n{KEY.upper()}\n"
    )
    parser = NodesFileParser(roster)
    assert parser.parse(), parser.errors
    assert [(n.bbs_index, n.bbs_name, n.public_key) for n in parser.nodes] == [
        (1, "The Hub", KEY), (2, "Board Two", ""), (3, "Board Three", KEY.upper())]


def validate(ib, **league_changes):
    config = toml.load(ib["config"])
    config["leagues"]["IB"]["555"].update(league_changes)
    ib["config"].write_text(toml.dumps(config))
    v = ClientValidator(str(ib["config"]))
    v.validate()
    return [e.message for e in v.errors], [w.message for w in v.warnings]


@pytest.mark.skipif(sys.platform == "win32", reason="the fake game is a shell script")
def test_validator_accepts_an_ib_league(ib):
    binary = fake_ib(ib["tmp"], "exit 0\n")
    (ib["game_folder"] / "ibnodes.dat").write_text(
        f"1\nThe Hub\n1:1/1\nC\nS\nX\n{KEY}\n\n2\nBoard Two\n1:1/2\nC\nS\nX\n{KEY}\n")
    errors, warnings = validate(ib, game_command=str(binary))
    assert errors == []
    # No HOST lines is right for IB: not a routing warning
    assert not [w for w in warnings if "HOST" in w]


def test_validator_wants_a_game_command_it_can_run(ib):
    errors, _ = validate(ib, game_command=str(ib["tmp"] / "missing"))
    assert any("is not a program this machine can run" in e for e in errors)


def test_validator_says_ibnodes_dat_comes_from_the_hub(ib):
    _, warnings = validate(ib, game_command=sys.executable)
    assert any("ibnodes.dat not found" in w for w in warnings)


def test_validator_says_coord_pub_comes_from_the_hub(ib):
    _, warnings = validate(ib, game_command=sys.executable)
    assert any("no coord.pub" in w for w in warnings)
    (ib["game_folder"] / "coord.pub").write_text(KEY + "\n")
    _, warnings = validate(ib, game_command=sys.executable)
    assert not any("coord.pub" in w for w in warnings)


def test_validator_refuses_an_unknown_runner(ib):
    errors, _ = validate(ib, runner="wine", game_command=sys.executable)
    assert any("runner must be" in e for e in errors)
