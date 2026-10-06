"""client.py --test-connection, and a BBS that has no leagues yet.

A sysop who has just claimed their credentials, or whose BBS is not in a
league yet, should be able to prove the credentials work, and find out that a
league or BBS index is wrong before the hub starts refusing their packets.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from client import NovaHubClient
from validator import ClientValidator


def _config(tmp_path, url, leagues="", secret="test_secret", name="Test BBS"):
    path = tmp_path / "config.toml"
    path.write_text(f"""
[hub]
url = "{url}"
client_id = "test_client"
client_secret = "{secret}"

[bbs]
name = "{name}"

[sync]
metrics_file = "{tmp_path / 'metrics.json'}"
{leagues}
""")
    return str(path)


def _league(number="555", bbs_index=2, enabled="true", game="BRE"):
    return f"""
[leagues.{game}.{number}]
enabled = {enabled}
bbs_index = {bbs_index}
inbound_dir = "."
outbound_dir = "."
"""


async def _check(path, capsys):
    code = await NovaHubClient(path).check_connection()
    return code, capsys.readouterr().out


@pytest.mark.asyncio
async def test_matching_config_passes(tmp_path, mock_server_url, test_client_config, capsys):
    code, out = await _check(_config(tmp_path, mock_server_url, _league()), capsys)

    assert code == 0
    assert "Authenticated Successfully" in out
    assert "OK       555B  BBS #2" in out
    assert "Connection test passed." in out


@pytest.mark.asyncio
async def test_wrong_bbs_index_fails(tmp_path, mock_server_url, test_client_config, capsys):
    code, out = await _check(_config(tmp_path, mock_server_url, _league(bbs_index=3)), capsys)

    assert code == 1
    assert "[leagues.BRE.555] has bbs_index = 3, but the hub has this BBS as #2" in out


@pytest.mark.asyncio
async def test_league_the_hub_does_not_have_fails(tmp_path, mock_server_url, test_client_config, capsys):
    leagues = _league() + _league(number="013", game="FE")
    code, out = await _check(_config(tmp_path, mock_server_url, leagues), capsys)

    assert code == 1
    assert "ERROR    013F  [leagues.FE.013] is enabled, but the hub does not have this BBS in 013F" in out


@pytest.mark.asyncio
async def test_disabled_league_the_hub_does_not_have_is_ignored(
        tmp_path, mock_server_url, test_client_config, capsys):
    leagues = _league() + _league(number="013", game="FE", enabled="false")
    code, out = await _check(_config(tmp_path, mock_server_url, leagues), capsys)

    assert code == 0
    assert "013F" not in out


@pytest.mark.asyncio
async def test_hub_league_missing_from_config_only_warns(
        tmp_path, mock_server_url, mock_storage, capsys):
    mock_storage.add_client("test_client", "test_secret", "Test BBS",
                            [{"league_id": "555B", "bbs_index": 2}, {"league_id": "013F", "bbs_index": 2}])

    code, out = await _check(_config(tmp_path, mock_server_url, _league()), capsys)

    assert code == 0
    assert "WARNING  013F  The hub has this BBS in 013F as #2, but it is not in this config." in out


@pytest.mark.asyncio
async def test_no_leagues_anywhere_still_proves_the_credentials(
        tmp_path, mock_server_url, mock_storage, capsys):
    mock_storage.add_client("test_client", "test_secret", "Test BBS", [])

    code, out = await _check(_config(tmp_path, mock_server_url), capsys)

    assert code == 0
    assert "Authenticated Successfully" in out
    assert "No leagues yet, on the hub or in this config." in out


@pytest.mark.asyncio
async def test_bbs_name_mismatch_warns(tmp_path, mock_server_url, test_client_config, capsys):
    code, out = await _check(_config(tmp_path, mock_server_url, _league(), name="Other"), capsys)

    assert code == 0
    assert "bbs.name is 'Other', but the hub has 'Test BBS'" in out


@pytest.mark.asyncio
async def test_wrong_secret_says_so(tmp_path, mock_server_url, test_client_config, capsys):
    code, out = await _check(_config(tmp_path, mock_server_url, _league(), secret="nope"), capsys)

    assert code == 1
    assert "Authenticated Successfully" not in out
    assert "client ID or secret is wrong" in out


@pytest.mark.asyncio
async def test_unreachable_hub_says_so(tmp_path, test_client_config, capsys):
    code, out = await _check(_config(tmp_path, "http://127.0.0.1:9", _league()), capsys)

    assert code == 1
    assert "Could not reach the hub" in out


@pytest.mark.asyncio
async def test_older_hub_skips_the_league_check(
        tmp_path, mock_server_url, test_client_config, mock_storage, capsys):
    mock_storage.serves_account = False

    code, out = await _check(_config(tmp_path, mock_server_url, _league()), capsys)

    assert code == 0
    assert "Authenticated Successfully" in out
    assert "could not be checked" in out


@pytest.mark.asyncio
async def test_sync_with_no_leagues_signs_in_and_stops(
        tmp_path, mock_server_url, mock_storage, capsys):
    mock_storage.add_client("test_client", "test_secret", "Test BBS", [])

    code = await NovaHubClient(_config(tmp_path, mock_server_url)).run()
    out = capsys.readouterr().out

    assert code == 0
    assert "Authenticated Successfully" in out
    assert "No leagues configured; nothing to sync" in out


def test_validator_accepts_a_config_with_no_leagues(tmp_path):
    validator = ClientValidator(_config(tmp_path, "http://hub.invalid"))

    assert validator.validate()
    assert any("No enabled leagues configured" in str(w) for w in validator.warnings)
