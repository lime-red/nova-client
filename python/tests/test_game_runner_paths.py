"""Paths handed to the dosemu subprocess must be absolute.

The subprocess runs with cwd=game_folder, because BRE/FE have to be started
from their own directory. But dosemu_config_dir and log_dir default to "./..."
- relative to the *daemon's* working directory. Passing those through unchanged
sent script(1) looking for logs/ inside the game folder, where it died with
"cannot open logs/...: No such file or directory" and exit 1, before dosemu ran
at all and with no log written to say why.
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from game_runner import GameRunner  # noqa: E402


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A daemon cwd and a game folder that are deliberately different."""
    daemon_cwd = tmp_path / "client"
    game_folder = tmp_path / "drive_c" / "bbs" / "bre_015"
    daemon_cwd.mkdir(parents=True)
    game_folder.mkdir(parents=True)
    monkeypatch.chdir(daemon_cwd)
    return daemon_cwd, game_folder


def _runner(game_folder):
    config = {
        "daemon": {
            "dosemu_path": "/bin/true",
            "dosemu_config_dir": "./dosemu_configs",
            "log_dir": "./logs",
        }
    }
    runner = GameRunner(config, verbose=False)
    league_config = {
        "game_folder": str(game_folder),
        "game_dos_path": "C:\\BBS\\BRE",
        "game_command": "BRE",
        "maintenance_args": "PLANETARY",
    }
    return runner, league_config


@pytest.mark.skipif(sys.platform == "win32", reason="dosemu path is Linux-only")
def test_log_lands_under_the_daemon_cwd_not_the_game_folder(workspace):
    daemon_cwd, game_folder = workspace
    runner, league_config = _runner(game_folder)

    asyncio.run(runner.run_maintenance("BRE", "015", league_config, timeout=30))

    logs = list((daemon_cwd / "logs").glob("BRE_015_*.log"))
    assert logs, "no log written under the daemon's working directory"
    assert not (game_folder / "logs").exists(), (
        "log directory was created inside the game folder - the subprocess cwd "
        "leaked into a relative path again"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="dosemu path is Linux-only")
def test_dosemu_config_lands_under_the_daemon_cwd(workspace):
    daemon_cwd, game_folder = workspace
    runner, league_config = _runner(game_folder)

    asyncio.run(runner.run_maintenance("BRE", "015", league_config, timeout=30))

    assert (daemon_cwd / "dosemu_configs" / "bre_015.conf").exists()
    assert not (game_folder / "dosemu_configs").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="dosemu path is Linux-only")
def test_batch_file_is_cleaned_up(workspace):
    """NOVAMNT.BAT is written into the game folder and removed afterwards.

    Worth pinning: its absence after a failed run looked like evidence the
    batch file was never written, which sent the first diagnosis sideways.
    """
    _, game_folder = workspace
    runner, league_config = _runner(game_folder)

    asyncio.run(runner.run_maintenance("BRE", "015", league_config, timeout=30))

    assert not (game_folder / "NOVAMNT.BAT").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="dosemu path is Linux-only")
def test_term_is_pinned_for_the_subprocess(workspace, monkeypatch):
    """dosemu2 exits immediately under TERM=dumb, which is what script(1)
    supplies when there is no controlling terminal - i.e. under systemd."""
    _, game_folder = workspace
    runner, league_config = _runner(game_folder)

    captured = {}
    real_exec = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        captured["argv"] = args
        captured["env"] = kwargs.get("env")
        captured["cwd"] = kwargs.get("cwd")
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    asyncio.run(runner.run_maintenance("BRE", "015", league_config, timeout=30))

    assert captured["env"]["TERM"] == "linux"
    # And the game still has to be started from its own directory.
    assert Path(captured["cwd"]) == game_folder.resolve()


@pytest.mark.skipif(sys.platform == "win32", reason="dosemu path is Linux-only")
def test_every_path_in_the_command_is_absolute(workspace, monkeypatch):
    """Asserted on the argv itself, not on where files ended up.

    The dosemu config is *written* relative to the daemon cwd, which is right,
    but it is *passed* to a process running in the game folder - so checking
    only that the file was created cannot catch the mismatch. dosemu would
    silently start with default settings instead of ours.
    """
    _, game_folder = workspace
    runner, league_config = _runner(game_folder)

    captured = {}
    real_exec = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        captured["argv"] = args
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    asyncio.run(runner.run_maintenance("BRE", "015", league_config, timeout=30))

    # script -c "<dosemu cmd>" <logfile>; the dosemu command is one quoted arg.
    argv = captured["argv"]
    path_like = [a for a in argv if "/" in str(a)]
    assert path_like, "expected paths in the command line"
    for arg in path_like:
        for token in str(arg).split():
            token = token.strip("'\"")
            if token.startswith("./") or (
                "/" in token and not token.startswith("/")
            ):
                pytest.fail(f"relative path passed to a subprocess with a different cwd: {token}")


# --- Failure reporting ------------------------------------------------------
# script(1) merges its stderr into what we capture. That was captured and then
# dropped, so a run that failed before writing a transcript reported only
# "Exit code: 1" - no reason, and no log to look in, because being unable to
# write the log WAS the reason. The daemon puts result.error straight into its
# ERROR line, so whatever lands here is what reaches the journal.


def test_failure_says_when_no_transcript_was_written(tmp_path):
    missing = tmp_path / "logs" / "BRE_015.log"
    error = GameRunner._describe_failure(
        1, missing, "script: cannot open logs/BRE_015.log: No such file or directory", ""
    )

    assert "Exit code: 1" in error
    assert "no transcript written" in error
    assert "cannot open" in error, "script's own stderr must survive into the error"


def test_failure_quotes_the_tail_of_the_transcript(tmp_path):
    log = tmp_path / "run.log"
    log.write_text(
        "Script started\n"
        "\n"
        "ERROR: KVM: error opening /dev/kvm\n"
        "Your terminal lacks the ability to clear the screen\n"
    )
    error = GameRunner._describe_failure(1, log, "", log.read_text())

    assert "terminal lacks the ability" in error
    assert str(log) in error


def test_short_transcript_is_called_out_as_a_dead_dosemu(tmp_path):
    """~330 bytes means dosemu never booted; a healthy run is 19-26 KB."""
    log = tmp_path / "run.log"
    log.write_text("x" * 330)
    error = GameRunner._describe_failure(1, log, "", log.read_text())

    assert "never booted" in error


def test_healthy_sized_transcript_is_not_called_dead(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("y" * 20_000)
    error = GameRunner._describe_failure(1, log, "", log.read_text())

    assert "never booted" not in error


def test_detail_is_truncated(tmp_path):
    """A 26 KB transcript must not be pasted wholesale into a journal line."""
    log = tmp_path / "run.log"
    log.write_text("z" * 20_000)
    error = GameRunner._describe_failure(1, log, "q" * 20_000, log.read_text())

    assert len(error) < 800


def test_detail_is_one_line(tmp_path):
    """journald splits on newlines; a failure spread over several entries is
    much harder to read than one long one."""
    log = tmp_path / "run.log"
    log.write_text("first line\nsecond line\nthird line\n")
    error = GameRunner._describe_failure(1, log, "", log.read_text())

    assert "\n" not in error
    assert "second line | third line" in error


def test_script_boilerplate_is_dropped(tmp_path):
    """'Script started/done' is script(1) bookkeeping and says nothing."""
    missing = tmp_path / "logs" / "run.log"
    captured = (
        "Script started, output log file is 'run.log'.\n"
        "script: cannot open run.log: Permission denied\n"
        "Script done.\n"
    )
    error = GameRunner._describe_failure(1, missing, captured, "")

    assert "Script started" not in error
    assert "Script done" not in error
    assert "Permission denied" in error
