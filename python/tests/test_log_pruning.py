"""Game logs are aged out of log_dir.

Every game run writes a log of its own, so a board running two leagues every
ten minutes gains nearly three hundred files a day; eclipse had 27,816 of them
(163 MB) by October 2026. daemon.log_days, default 30, ages them out; 0 keeps
them all, and nothing but a log this client wrote for a configured league is
ever touched.
"""
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import daemon as daemon_module  # noqa: E402
from game_runner import GameRunner  # noqa: E402

DAY = 86400


def _log(log_dir: Path, name: str, age_days: float) -> Path:
    path = log_dir / name
    path.write_text("Planetary Maintenance Complete\n")
    when = time.time() - age_days * DAY
    os.utime(path, (when, when))
    return path


@pytest.fixture
def logs(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    return log_dir


LEAGUES = {"BRE": {"015": {}}, "FE": {"015": {}}, "IB": {"902": {}}}


def _runner(log_dir, leagues=LEAGUES, **daemon):
    return GameRunner({"daemon": {"log_dir": str(log_dir), **daemon}, "leagues": leagues})


def test_old_game_logs_go_and_new_ones_stay(logs):
    old = [_log(logs, "BRE_015_20260806_000417.log", 40),
           _log(logs, "FE_015_20260806_000418.log", 31),
           _log(logs, "IB_902_20260901_120000.log", 35)]
    new = [_log(logs, "BRE_015_20261031_075417.log", 0),
           _log(logs, "FE_015_20261002_075417.log", 29)]
    assert _runner(logs).prune_logs() == 3
    assert not any(p.exists() for p in old)
    assert all(p.exists() for p in new)


def test_only_this_clients_logs_are_touched(logs):
    others = [_log(logs, name, 400) for name in
              ("daemon.log", "notes.txt", "BRE_015_old.log", "BRE_015_20260806_000417.log.gz",
               # Shaped like ours, but no configured league wrote them.
               "BRE_555_20260806_000417.log", "FE_902_20260806_000417.log",
               "bre_015_20260806_000417.log", "SYSOP_backup_20260806_000417.log",
               "BRE_015_2026080_0004170.log")]
    (logs / "FE_015_20260806_000418.log").mkdir()
    assert _runner(logs).prune_logs() == 0
    assert all(p.exists() for p in others)


def test_log_days_sets_the_age_and_zero_keeps_everything(logs):
    log = _log(logs, "BRE_015_20261020_000000.log", 10)
    assert _runner(logs, log_days=0).prune_logs() == 0
    assert _runner(logs, log_days=14).prune_logs() == 0 and log.exists()
    assert _runner(logs, log_days=7).prune_logs() == 1 and not log.exists()


def test_no_log_dir_yet_is_not_an_error(tmp_path):
    assert _runner(tmp_path / "missing").prune_logs() == 0


def test_a_league_dropped_from_the_config_keeps_its_logs(logs):
    log = _log(logs, "FE_015_20260806_000418.log", 40)
    assert _runner(logs, leagues={"BRE": {"015": {}}}).prune_logs() == 0 and log.exists()
    assert _runner(logs, leagues={}).prune_logs() == 0 and log.exists()


def test_failures_are_one_line_however_many(logs, monkeypatch, capsys):
    for i in range(50):
        _log(logs, f"BRE_015_20260806_{i:06d}.log", 40)

    def refuse(self, *a, **kw):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(Path, "unlink", refuse)
    assert _runner(logs).prune_logs() == 0
    warnings = [line for line in capsys.readouterr().out.splitlines() if "WARNING" in line]
    assert len(warnings) == 1, warnings
    assert "Could not prune 50 game log(s)" in warnings[0]
    assert "and 48 more" in warnings[0] and "Permission denied" in warnings[0]


def _daemon(tmp_path, extra=""):
    config = tmp_path / "config.toml"
    config.write_text(f'[hub]\nurl = "http://127.0.0.1:1"\n[daemon]\nlog_dir = "{tmp_path / "logs"}"\n{extra}')
    return config


def test_the_daemon_prunes_after_maintenance_at_most_hourly(tmp_path, monkeypatch):
    (tmp_path / "logs").mkdir()
    d = daemon_module.NovaDaemon.__new__(daemon_module.NovaDaemon)
    d.config = {"daemon": {"log_dir": str(tmp_path / "logs")}}
    d.game_runner = GameRunner(d.config)
    d._last_log_prune = None
    d.log = lambda *a: None
    calls = []
    monkeypatch.setattr(d.game_runner, "prune_logs", lambda: calls.append(1) or 0)
    clock = [1000.0]
    monkeypatch.setattr(daemon_module.time, "monotonic", lambda: clock[0])
    d._prune_logs()
    clock[0] += 60
    d._prune_logs()
    assert len(calls) == 1
    clock[0] += daemon_module.LOG_PRUNE_EVERY
    d._prune_logs()
    assert len(calls) == 2


@pytest.mark.parametrize("bad", ["30", -1, True, 1.5])
def test_a_bad_log_days_is_refused(tmp_path, bad):
    value = f'"{bad}"' if isinstance(bad, str) else str(bad).lower()
    with pytest.raises(ValueError, match="log_days"):
        daemon_module.NovaDaemon._load_config(None, str(_daemon(tmp_path, f"log_days = {value}\n")))
