"""The Python client's review findings (#28).

Packets the hub has handed over are the only copy this BBS gets, so a
download must not be lost to a 503 or a half-written file. A daemon must not
fetch a new token every cycle (#20), share its directories with a hand-run
client.py, or leave dosemu running after a timeout. And --validate has to be
runnable as the README says, and agree with what client.py will refuse.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import client as client_module  # noqa: E402
import daemon as daemon_module  # noqa: E402
from client import NovaHubClient  # noqa: E402
from game_runner import GameRunner, GameRunResult  # noqa: E402
from instance_lock import InstanceLock  # noqa: E402
from validator import ClientValidator  # noqa: E402

PYTHON_DIR = Path(__file__).resolve().parent.parent
PACKET = "555B0102.001"


class ScriptedHub:
    """A hub whose replies a test lays out in advance."""

    def __init__(self):
        self.tokens_issued = 0
        self.expires_in = None
        self.valid = set()
        self.download_replies = []   # statuses to give before the packet itself
        self.download_body = b"PACKETDATA"
        self.downloads = 0
        self.app = web.Application()
        self.app.router.add_post("/service/api/v1/auth/token", self.token)
        self.app.router.add_get("/service/api/v1/leagues/{league}/packets", self.listing)
        self.app.router.add_get("/service/api/v1/leagues/{league}/packets/{name}", self.download)
        self.app.router.add_put("/service/api/v1/leagues/{league}/packets/{name}", self.upload)

    async def token(self, request):
        self.tokens_issued += 1
        token = f"t{self.tokens_issued}"
        self.valid.add(token)
        body = {"access_token": token, "token_type": "bearer"}
        if self.expires_in is not None:
            body["expires_in"] = self.expires_in
        return web.json_response(body)

    def authorized(self, request):
        return request.headers.get("Authorization", "").removeprefix("Bearer ") in self.valid

    async def listing(self, request):
        if not self.authorized(request):
            return web.json_response({"detail": "expired"}, status=401)
        return web.json_response({"packets": [{"filename": PACKET}]})

    async def download(self, request):
        if not self.authorized(request):
            return web.json_response({"detail": "expired"}, status=401)
        self.downloads += 1
        if self.download_replies:
            status = self.download_replies.pop(0)
            return web.Response(status=status, headers={"Retry-After": "0"})
        return web.Response(body=self.download_body)

    async def upload(self, request):
        await request.read()
        return web.json_response({"status": "ok"})


@pytest_asyncio.fixture
async def hub():
    scripted = ScriptedHub()
    server = TestServer(scripted.app)
    await server.start_server()
    scripted.url = str(server.make_url("")).rstrip("/")
    yield scripted
    await server.close()


def _config(tmp_path, url, extra=""):
    inbound = tmp_path / "inbound"
    outbound = tmp_path / "outbound"
    inbound.mkdir(exist_ok=True)
    outbound.mkdir(exist_ok=True)
    path = tmp_path / "config.toml"
    path.write_text(f"""
[hub]
url = "{url}"
client_id = "c"
client_secret = "s"

[bbs]
name = "Test BBS"

[sync]
sent_action = "delete"
max_retries = 3
retry_delay = 0
metrics_file = "{tmp_path / 'metrics.json'}"
{extra}

[leagues.BRE.555]
bbs_index = 2
inbound_dir = "{inbound}"
outbound_dir = "{outbound}"
""")
    return str(path)


# -- downloads ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_503_is_retried_and_the_packet_lands_whole(hub, tmp_path):
    hub.download_replies = [503, 502]
    client = NovaHubClient(_config(tmp_path, hub.url))
    assert await client.run() == 0
    assert hub.downloads == 3
    inbound = tmp_path / "inbound"
    assert (inbound / PACKET).read_bytes() == b"PACKETDATA"
    assert not list(inbound.glob("*.part"))


@pytest.mark.asyncio
async def test_a_reply_bigger_than_a_packet_is_refused(hub, tmp_path):
    hub.download_body = b"x" * (client_module.MAX_PACKET_SIZE + 1)
    client = NovaHubClient(_config(tmp_path, hub.url))
    assert await client.run() == 1
    assert list((tmp_path / "inbound").iterdir()) == []


@pytest.mark.asyncio
async def test_a_failed_write_leaves_no_packet_for_the_game(hub, tmp_path, monkeypatch):
    def refuse(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(client_module.os, "replace", refuse)
    client = NovaHubClient(_config(tmp_path, hub.url))
    assert await client.run() == 1
    assert list((tmp_path / "inbound").iterdir()) == []


# -- tokens (#20) ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_daemon_reuses_one_token_across_syncs(hub, tmp_path):
    config = _config(tmp_path, hub.url)
    cache = {}
    for _ in range(3):
        assert await NovaHubClient(config, token_cache=cache).run() == 0
    assert hub.tokens_issued == 1


@pytest.mark.asyncio
async def test_a_refused_token_is_replaced_and_the_request_retried(hub, tmp_path):
    config = _config(tmp_path, hub.url)
    cache = {}
    assert await NovaHubClient(config, token_cache=cache).run() == 0
    hub.valid.clear()  # the hub restarted with a new signing key, say
    assert await NovaHubClient(config, token_cache=cache).run() == 0
    assert hub.tokens_issued == 2
    assert (tmp_path / "inbound" / PACKET).exists()


@pytest.mark.asyncio
async def test_a_token_near_its_expiry_is_not_used(hub, tmp_path):
    hub.expires_in = 30  # inside the margin
    config = _config(tmp_path, hub.url)
    cache = {}
    for _ in range(2):
        assert await NovaHubClient(config, token_cache=cache).run() == 0
    assert hub.tokens_issued == 2


# -- sent packets ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_archiving_over_an_earlier_packet_of_the_same_name(hub, tmp_path):
    archive = tmp_path / "sent"
    archive.mkdir()
    (archive / "555B0201.001").write_bytes(b"last cycle")
    config = _config(tmp_path, hub.url, f'archive_dir = "{archive}"')
    Path(config).write_text(Path(config).read_text().replace('"delete"', '"archive"'))
    (tmp_path / "outbound" / "555B0201.001").write_bytes(b"this cycle")
    assert await NovaHubClient(config).run() == 0
    assert (archive / "555B0201.001").read_bytes() == b"this cycle"
    assert not (tmp_path / "outbound" / "555B0201.001").exists()


@pytest.mark.asyncio
async def test_a_failure_to_archive_does_not_stop_the_download(hub, tmp_path, monkeypatch):
    async def broken(self, packet_file):
        raise PermissionError("archive is read-only")

    monkeypatch.setattr(NovaHubClient, "handle_sent_file", broken)
    client = NovaHubClient(_config(tmp_path, hub.url))
    (tmp_path / "outbound" / "555B0201.001").write_bytes(b"x")
    assert await client.run() == 1
    assert (tmp_path / "inbound" / PACKET).exists()


# -- one sync at a time ------------------------------------------------------------

@pytest.mark.asyncio
async def test_client_py_will_not_sync_alongside_the_daemon(hub, tmp_path):
    config = _config(tmp_path, hub.url)
    held = InstanceLock(config)
    assert held.acquire()
    try:
        assert await NovaHubClient(config).run() == 1
        assert hub.tokens_issued == 0
    finally:
        held.release()
    assert await NovaHubClient(config).run() == 0


# -- config ------------------------------------------------------------------------

def test_config_edge_cases(tmp_path, monkeypatch):
    base = _config(tmp_path, "https://hub.example/")
    text = Path(base).read_text()

    client = NovaHubClient(base)
    assert client.config["hub"]["url"] == "https://hub.example"

    monkeypatch.setenv("HUB_CLIENT_ID", "")
    assert NovaHubClient(base).config["hub"]["client_id"] == "c"

    Path(base).write_text(text.replace("bbs_index = 2", "bbs_index = true"))
    with pytest.raises(ValueError, match="integer"):
        NovaHubClient(base)

    Path(base).write_text(text.replace('"delete"', '"keep"'))
    with pytest.raises(ValueError, match="sent_action"):
        NovaHubClient(base)

    Path(base).write_text(text.replace("[leagues.BRE.555]", "[leagues.bre.555]"))
    assert client_module.hub_league_id("bre", "555") == "555B"
    assert NovaHubClient(base).is_packet_file(
        "555B0201.001", "bre", "555", {"bbs_index": 2})

    with pytest.raises(ValueError):
        client.sanitize_filename(PACKET + "\n")


# -- the daemon --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_daemon_idles_when_disabled(hub, tmp_path):
    config = _config(tmp_path, hub.url, "\n[daemon]\nenabled = false\n")
    daemon = daemon_module.NovaDaemon(config)
    task = asyncio.create_task(daemon.run())
    await asyncio.sleep(0.3)
    assert not task.done()
    daemon._handle_signal(daemon_module.signal.SIGTERM)
    await asyncio.wait_for(task, 5)
    assert hub.tokens_issued == 0


@pytest.mark.asyncio
async def test_replies_written_by_maintenance_go_out_at_once(hub, tmp_path, monkeypatch):
    config = _config(tmp_path, hub.url, "\n[daemon]\n")
    daemon = daemon_module.NovaDaemon(config)
    reply = tmp_path / "outbound" / "555B0201.002"

    async def maintenance(timeout_per_league, select=None):
        reply.write_bytes(b"reply")
        return {}

    monkeypatch.setattr(daemon.game_runner, "run_all_maintenance", maintenance)
    await daemon._run_sync()
    assert not reply.exists()
    assert daemon.stats["packets_uploaded"] == 1
    assert hub.tokens_issued == 1


def _with_ib(config, tmp_path, daemon="", league=""):
    """Add an IB league beside the BRE one, with its own settings"""
    (tmp_path / "ib").mkdir()
    path = Path(config)
    path.write_text(path.read_text() + f"""
[leagues.IB.016]
bbs_index = 2
runner = "native"
game_folder = "{tmp_path / 'ib'}"
inbound_dir = "{tmp_path / 'ib'}"
outbound_dir = "{tmp_path / 'ib'}"
{league}

[daemon]
{daemon}
""")
    return config


def _recording_runs(daemon, monkeypatch):
    ran = []

    async def run_maintenance(game_type, league_id, league_config, timeout):
        ran.append(f"{game_type}_{league_id}")
        return GameRunResult(success=True, game_type=game_type, league_id=league_id, command="x")

    monkeypatch.setattr(daemon.game_runner, "run_maintenance", run_maintenance)
    return ran


@pytest.mark.asyncio
async def test_a_league_can_wait_for_the_schedule_when_packets_arrive(hub, tmp_path, monkeypatch):
    config = _with_ib(_config(tmp_path, hub.url), tmp_path,
                      league="run_maintenance_on_download = false")
    (tmp_path / "bre").mkdir()
    Path(config).write_text(Path(config).read_text().replace(
        "[leagues.BRE.555]\n", f"[leagues.BRE.555]\ngame_folder = \"{tmp_path / 'bre'}\"\n"))
    daemon = daemon_module.NovaDaemon(config)
    ran = _recording_runs(daemon, monkeypatch)

    await daemon._run_sync()
    assert ran == ["BRE_555"]
    # IB was left out, so its schedule is not reset by this run
    assert daemon._last_maintenance is None

    await daemon._run_scheduled_maintenance()
    assert ran == ["BRE_555", "BRE_555", "IB_016"]


@pytest.mark.asyncio
async def test_a_league_can_run_when_packets_arrive_though_the_daemon_does_not(
        hub, tmp_path, monkeypatch, capsys):
    config = _with_ib(_config(tmp_path, hub.url), tmp_path,
                      daemon="run_maintenance_on_download = false",
                      league="run_maintenance_on_download = true")
    daemon = daemon_module.NovaDaemon(config)
    ran = _recording_runs(daemon, monkeypatch)

    await daemon._run_sync()
    # BRE_555 has no game_folder here, so IB is all that can run
    assert ran == ["IB_016"]
    assert daemon._last_maintenance is not None


@pytest.mark.asyncio
async def test_no_maintenance_on_download_when_every_league_waits(hub, tmp_path, monkeypatch, capsys):
    config = _with_ib(_config(tmp_path, hub.url), tmp_path,
                      daemon="run_maintenance_on_download = false")
    daemon = daemon_module.NovaDaemon(config)
    ran = _recording_runs(daemon, monkeypatch)

    await daemon._run_sync()
    assert ran == []
    assert "maintenance waits for its schedule" in capsys.readouterr().out
    assert daemon.stats["packets_downloaded"] > 0


def test_run_maintenance_on_download_must_be_true_or_false(tmp_path):
    config = _with_ib(_config(tmp_path, "http://hub"), tmp_path,
                      league='run_maintenance_on_download = "no"')
    with pytest.raises(ValueError, match="leagues.IB.016.run_maintenance_on_download"):
        daemon_module.NovaDaemon(config)


# -- what maintenance leaves in inbound (#15) -----------------------------------------

def _maintenance_that(takes_in, inbound):
    async def maintenance(timeout_per_league, select=None):
        if takes_in:
            for p in inbound.iterdir():
                if p.name.upper() == PACKET:
                    p.unlink()
        return {"BRE_555": GameRunResult(success=True, game_type="BRE", league_id="555",
                                         command="BRE")}
    return maintenance


@pytest.mark.asyncio
async def test_a_packet_maintenance_left_in_inbound_is_named_and_counted(
        hub, tmp_path, monkeypatch, capsys):
    config = _config(tmp_path, hub.url, "\n[daemon]\n")
    inbound = tmp_path / "inbound"
    (inbound / "README.TXT").write_text("not a packet")
    daemon = daemon_module.NovaDaemon(config)
    monkeypatch.setattr(daemon.game_runner, "run_all_maintenance",
                        _maintenance_that(False, inbound))
    await daemon._run_sync()

    assert f"BRE_555: 1 file(s) still in inbound after maintenance: {PACKET}" in capsys.readouterr().out
    # The upload pass straight after maintenance writes it out
    metrics = json.loads((tmp_path / "metrics.json").read_text())
    assert metrics["unprocessed"] == {"BRE_555": [PACKET]}
    assert metrics["total_unprocessed"] == 1


@pytest.mark.asyncio
async def test_an_inbound_the_game_emptied_reports_nothing(hub, tmp_path, monkeypatch, capsys):
    config = _config(tmp_path, hub.url, "\n[daemon]\n")
    daemon = daemon_module.NovaDaemon(config)
    monkeypatch.setattr(daemon.game_runner, "run_all_maintenance",
                        _maintenance_that(True, tmp_path / "inbound"))
    await daemon._run_sync()

    assert "still in inbound" not in capsys.readouterr().out
    metrics = json.loads((tmp_path / "metrics.json").read_text())
    assert metrics["unprocessed"] == {"BRE_555": []}
    assert metrics["total_unprocessed"] == 0


@pytest.mark.asyncio
async def test_a_one_shot_sync_makes_no_claim_about_inbound(hub, tmp_path):
    # No maintenance ran, so a packet just downloaded is not "unprocessed"
    client = NovaHubClient(_config(tmp_path, hub.url))
    await client.run()
    metrics = json.loads((tmp_path / "metrics.json").read_text())
    assert metrics["total_downloaded"] == 1
    assert "unprocessed" not in metrics and "total_unprocessed" not in metrics


# -- game commands -----------------------------------------------------------------

def _league(tmp_path, **overrides):
    folder = tmp_path / "game"
    folder.mkdir(exist_ok=True)
    league = {"game_folder": str(folder), "game_dos_path": "C:\\BRE",
              "game_command": "BRE", "maintenance_args": "PLANETARY"}
    league.update(overrides)
    return league


def test_a_command_the_shell_would_act_on_is_not_run(tmp_path, monkeypatch):
    runner = GameRunner({"daemon": {}})

    async def must_not_run(*a, **kw):
        raise AssertionError("ran a refused command")

    monkeypatch.setattr(runner, "_run_windows", must_not_run)
    monkeypatch.setattr(runner, "_run_linux_dosemu", must_not_run)
    for bad in ({"maintenance_args": "PLANETARY & del *.*"},
                {"game_command": "BRE|evil"},
                {"game_dos_path": "C:\\BRE\r\nDEL *.*"}):
        result = asyncio.run(runner.run_maintenance("BRE", "555", _league(tmp_path, **bad)))
        assert not result.success and "would act on" in result.error


def test_windows_quotes_the_folder_and_kills_the_tree_on_timeout(tmp_path, monkeypatch):
    folder = tmp_path / "Game Folder"
    folder.mkdir()
    (folder / "BRE.EXE").write_bytes(b"")
    calls = {}

    class Hung:
        pid = 4242
        returncode = None

        def __init__(self, cmd, **kw):
            calls["cmd"] = cmd
            self.waits = 0

        def communicate(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("cmd", timeout)
            return b"", b""

        def kill(self):
            pass

    real_run = subprocess.run

    def run(argv, **kw):
        if argv[0] == "taskkill":
            calls["taskkill"] = argv
            return subprocess.CompletedProcess(argv, 0)
        return real_run(argv, **kw)

    monkeypatch.setattr(subprocess, "Popen", Hung)
    monkeypatch.setattr(subprocess, "run", run)
    runner = GameRunner({"daemon": {}})
    result = asyncio.run(runner._run_windows(folder, "BRE", "PLANETARY", timeout=1))
    assert calls["cmd"].startswith(f'"{folder / "BRE.EXE"}" PLANETARY')
    assert calls["taskkill"] == ["taskkill", "/F", "/T", "/PID", "4242"]
    assert not result.success and "Timeout" in result.error


def _running(pid: int) -> bool:
    """Alive and not a zombie: an orphan in a container may never be reaped."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, IndexError):
        return False
    return state != "Z"


@pytest.mark.skipif(not Path("/proc").exists(), reason="needs /proc and script(1)")
def test_a_timeout_kills_dosemu_not_just_script(tmp_path):
    pidfile = tmp_path / "dosemu.pid"
    fake = tmp_path / "dosemu"
    # Deaf to the pty's hangup, as dosemu may be: only an explicit kill works
    fake.write_text(f"#!/bin/sh\ntrap '' HUP\nsleep 300 &\necho $! > {pidfile}\nwait\n")
    fake.chmod(0o755)
    runner = GameRunner({"daemon": {"dosemu_path": str(fake),
                                    "dosemu_config_dir": str(tmp_path / "conf"),
                                    "log_dir": str(tmp_path / "logs")}})
    result = asyncio.run(runner.run_maintenance("BRE", "555", _league(tmp_path), timeout=2))
    assert not result.success and "Timeout" in result.error
    pid = int(pidfile.read_text())
    deadline = time.time() + 5
    while _running(pid) and time.time() < deadline:
        time.sleep(0.1)
    assert not _running(pid), "dosemu's child outlived the timeout"


# -- validator ---------------------------------------------------------------------

NODES = "1 HOST 2\r\nNova Hub\r\n1:1/1\r\n\r\n\r\n\r\n\r\n2\r\nTest BBS\r\n1:1/2\r\nX\r\nY\r\nZ\r\n\r\n"


def _validate(tmp_path, nodes=None, extra_league=""):
    config = Path(_config(tmp_path, "https://hub.example"))
    game = tmp_path / "game"
    game.mkdir(exist_ok=True)
    if nodes is not None:
        (game / "BRNODES.DAT").write_bytes(nodes.encode())
    config.write_text(config.read_text() + f'game_folder = "{game}"\n' + extra_league)
    validator = ClientValidator(str(config))
    return validator, validator.validate()


def test_validator_runs_as_the_readme_says(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[bbs]\nname = "X"\n[hub]\nurl = "https://hub.example"\n')
    env = {k: v for k, v in os.environ.items() if not k.startswith("HUB_")}
    result = subprocess.run(
        [sys.executable, "validator.py", "--config", str(config)],
        cwd=PYTHON_DIR, capture_output=True, text=True, env=env,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "hub.client_id not configured" in result.stdout


def test_a_new_node_without_a_nodes_file_still_validates(tmp_path):
    validator, ok = _validate(tmp_path)
    assert ok, [str(e) for e in validator.errors]
    assert any("not found" in str(w) for w in validator.warnings)


def test_routing_through_the_hub_passes_and_direct_routing_warns(tmp_path):
    validator, ok = _validate(tmp_path, NODES)
    assert ok, [str(e) for e in validator.errors]
    assert not [w for w in validator.warnings if "HOST" in str(w)]

    direct = NODES.replace("1 HOST 2", "1")
    validator, ok = _validate(tmp_path, direct)
    assert ok
    assert any("no HOST line routes for 1, 2" in str(w) for w in validator.warnings)


def test_validator_refuses_what_client_py_would(tmp_path):
    validator, ok = _validate(tmp_path, NODES, 'maintenance_args = "PLANETARY > NUL"\n')
    assert not ok
    assert any("maintenance_args" in str(e) for e in validator.errors)
