"""game_folder must be a host path; game_dos_path carries the DOS spelling.

Both tests here cover the same real misconfiguration, found on a live Linux
node: game_folder held "C:\\BBS\\DOORS\\BRE_015". Nothing can stat that on
Linux, so maintenance failed at the existence check and never launched dosemu -
and the error just said the directory did not exist, which sends you off looking
for a missing directory rather than a wrong kind of path.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from validator import ClientValidator  # noqa: E402


@pytest.fixture
def validator():
    return ClientValidator(config_path="unused.toml")


def test_dos_path_on_linux_explains_itself(validator, monkeypatch):
    monkeypatch.setattr("validator.platform.system", lambda: "Linux")

    assert validator.validate_directory(
        "C:\\BBS\\DOORS\\BRE_015", "game_folder", "BRE.015"
    ) is False

    message = validator.errors[0].message
    assert "DOS path" in message
    assert "game_dos_path" in message


def test_forward_slash_dos_path_is_also_caught(validator, monkeypatch):
    """dosemu accepts C:/... too, and people write it."""
    monkeypatch.setattr("validator.platform.system", lambda: "Linux")

    assert validator.validate_directory("C:/BBS/BRE", "game_folder", "BRE.015") is False
    assert "DOS path" in validator.errors[0].message


def test_ordinary_missing_linux_dir_gets_no_dos_hint(validator, monkeypatch):
    """The hint must not fire for a plain typo, or it becomes noise."""
    monkeypatch.setattr("validator.platform.system", lambda: "Linux")

    assert validator.validate_directory(
        "/home/lime/.dosemu/drive_c/bbs/typo", "game_folder", "BRE.015"
    ) is False
    assert "DOS path" not in validator.errors[0].message


def test_no_dos_hint_on_windows(validator, monkeypatch):
    """On Windows a C:\\ path is simply a missing directory."""
    monkeypatch.setattr("validator.platform.system", lambda: "Windows")

    assert validator.validate_directory(
        "C:\\BBS\\DOORS\\BRE_015", "game_folder", "BRE.015"
    ) is False
    assert "DOS path" not in validator.errors[0].message


def test_existing_directory_passes(validator, tmp_path, monkeypatch):
    monkeypatch.setattr("validator.platform.system", lambda: "Linux")
    assert validator.validate_directory(str(tmp_path), "game_folder", "BRE.015") is True
    assert validator.errors == []


def test_daemon_defaults_pin_dosemu_term(tmp_path):
    """dosemu2 exits immediately under TERM=dumb, which is what script(1)
    supplies when there is no controlling terminal - i.e. under systemd.
    nova-hub hit this and pinned TERM; the client must default it too."""
    from daemon import NovaDaemon

    config_file = tmp_path / "config.toml"
    config_file.write_text("[daemon]\nsync_interval = 120\n")

    # __new__ rather than the constructor: _load_config never touches self, and
    # constructing a real daemon would set up an event loop we do not want here.
    daemon = NovaDaemon.__new__(NovaDaemon)
    config = daemon._load_config(str(config_file))

    assert config["daemon"]["dosemu_term"] == "linux"
