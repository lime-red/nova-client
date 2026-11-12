"""A game run holds NOVARUN.LCK in the game folder, visible to other programs.

eqx (the BBS) checks it when a caller finds the game's INUSE.FLG, to tell
"turn maintenance is running" from "someone else is playing".
"""

import asyncio
import fcntl
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from game_runner import RUN_LOCK_NAME, GameRunner, GameRunResult  # noqa: E402


def held_by_someone_else(path: Path) -> bool:
    """The check eqx makes: a shared, non-blocking flock, let go at once."""
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def _setup(tmp_path, monkeypatch):
    game = tmp_path / "bre_015"
    game.mkdir()
    runner = GameRunner({"daemon": {}}, verbose=False)
    runner.is_windows = False
    seen = {}

    async def fake_run(game_type, league_id, league_config, timeout):
        lock = game / RUN_LOCK_NAME
        seen["exists"] = lock.exists()
        # The lock is an flock on another open file description, so even this
        # process's own probe sees it held.
        seen["held"] = seen["exists"] and held_by_someone_else(lock)
        return GameRunResult(success=True, game_type=game_type, league_id=league_id, command="")

    monkeypatch.setattr(runner, "_run_linux_dosemu", fake_run)
    league = {"game_folder": str(game), "game_command": "BRE", "maintenance_args": "PLANETARY"}
    return runner, league, game, seen


def test_lock_is_held_during_the_run_and_released_after(tmp_path, monkeypatch):
    runner, league, game, seen = _setup(tmp_path, monkeypatch)
    result = asyncio.run(runner.run_maintenance("BRE", "015", league))
    assert result.success
    assert seen == {"exists": True, "held": True}
    assert not held_by_someone_else(game / RUN_LOCK_NAME)


def test_lock_is_released_when_the_run_raises(tmp_path, monkeypatch):
    runner, league, game, _ = _setup(tmp_path, monkeypatch)

    async def boom(*a, **k):
        raise RuntimeError("dosemu fell over")

    monkeypatch.setattr(runner, "_run_linux_dosemu", boom)
    result = asyncio.run(runner.run_maintenance("BRE", "015", league))
    assert not result.success
    assert not held_by_someone_else(game / RUN_LOCK_NAME)


def test_a_momentary_outside_check_does_not_fail_the_run(tmp_path, monkeypatch):
    """eqx's check holds the lock shared for an instant; the run waits it out."""
    runner, league, game, seen = _setup(tmp_path, monkeypatch)
    lock = game / RUN_LOCK_NAME
    lock.touch()
    fd = os.open(lock, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_SH)

    async def go():
        loop = asyncio.get_running_loop()
        loop.call_later(0.2, os.close, fd)
        return await runner.run_maintenance("BRE", "015", league)

    result = asyncio.run(go())
    assert result.success and seen["held"]


def test_an_unwritable_game_folder_still_runs(tmp_path, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    runner, league, game, seen = _setup(tmp_path, monkeypatch)
    game.chmod(0o555)
    try:
        result = asyncio.run(runner.run_maintenance("BRE", "015", league))
    finally:
        game.chmod(0o755)
    assert result.success
    assert seen["exists"] is False
