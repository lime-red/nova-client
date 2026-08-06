import pytest
import os
import re
from pathlib import Path
from client import NovaHubClient

@pytest.fixture
def client(tmp_path):
    # Create a dummy config
    config_file = tmp_path / "config.toml"
    config_file.write_text("""
[hub]
url = "http://localhost:8000"
client_id = "test_id"
client_secret = "test_secret"

[bbs]
name = "Test BBS"

[sync]
sent_action = "archive"
archive_dir = "./sent"

[leagues.BRE.555]
enabled = true
bbs_index = 2
outbound_dir = "./outbound"
inbound_dir = "./inbound"
""")
    return NovaHubClient(str(config_file))

def test_sanitize_filename_valid_packets(client):
    # Standard packet filenames
    assert client.sanitize_filename("555B0102.001") == "555B0102.001"
    assert client.sanitize_filename("123FABCD.999") == "123FABCD.999"

def test_sanitize_filename_valid_nodelists(client):
    # Nodelist filenames (BRNODES.xxx and FENODES.xxx)
    assert client.sanitize_filename("BRNODES.555") == "BRNODES.555"
    assert client.sanitize_filename("FENODES.013") == "FENODES.013"
    # Case insensitive
    assert client.sanitize_filename("brnodes.555") == "brnodes.555"
    assert client.sanitize_filename("BrNodes.123") == "BrNodes.123"

def test_sanitize_filename_traversal(client):
    with pytest.raises(ValueError, match="Path traversal attempt"):
        client.sanitize_filename("../etc/passwd")
    
    with pytest.raises(ValueError, match="Path traversal attempt"):
        client.sanitize_filename("subdir/555B0102.001")

def test_sanitize_filename_invalid_format(client):
    with pytest.raises(ValueError, match="does not match expected format"):
        client.sanitize_filename("invalid.txt")

    with pytest.raises(ValueError, match="does not match expected format"):
        client.sanitize_filename("555X0102.001")  # Invalid game type X

    with pytest.raises(ValueError, match="does not match expected format"):
        client.sanitize_filename("BRNODES.55")  # League must be 3 digits

    with pytest.raises(ValueError, match="does not match expected format"):
        client.sanitize_filename("XXNODES.555")  # Invalid prefix

def test_is_packet_file(client):
    league_config = {"bbs_index": 2}
    
    # Valid packet for this league (BBS index 2 = 02 hex)
    assert client.is_packet_file("555B0201.001", "BRE", "555", league_config) == True
    
    # Wrong league number
    assert client.is_packet_file("123B0201.001", "BRE", "555", league_config) == False
    
    # Wrong game type
    assert client.is_packet_file("555F0201.001", "BRE", "555", league_config) == False
    
    # Wrong source BBS index (03 instead of 02)
    assert client.is_packet_file("555B0301.001", "BRE", "555", league_config) == False

def test_load_config_missing_bbs_index(tmp_path):
    config_file = tmp_path / "bad_config.toml"
    config_file.write_text("""
[hub]
url = "http://localhost:8000"
client_id = "test_id"
client_secret = "test_secret"
[bbs]
name = "Test"
[leagues.BRE.555]
enabled = true
""")
    with pytest.raises(ValueError, match="missing 'bbs_index'"):
        NovaHubClient(str(config_file))


def test_verbose_flag_default(client):
    # Default verbose should be False
    assert client.verbose == False


def test_verbose_flag_enabled(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text("""
[hub]
url = "http://localhost:8000"
client_id = "test_id"
client_secret = "test_secret"
[bbs]
name = "Test BBS"
[leagues.BRE.555]
enabled = true
bbs_index = 2
outbound_dir = "./outbound"
inbound_dir = "./inbound"
""")
    client = NovaHubClient(str(config_file), verbose=True)
    assert client.verbose == True


def test_log_api_response_verbose(client, capsys):
    # When not verbose, log_api_response should not print anything
    client.verbose = False
    client.log_api_response('{"test": "data"}', "context")
    captured = capsys.readouterr()
    assert captured.out == ""

    # When verbose, log_api_response should pretty-print JSON
    client.verbose = True
    client.log_api_response('{"test": "data"}', "context")
    captured = capsys.readouterr()
    assert "[API Response: context]" in captured.out
    assert '"test": "data"' in captured.out


def test_log_error_shows_verbose_hint(client, capsys):
    # When not verbose, errors should show the verbose hint
    client.verbose = False
    client.log("ERROR", "Test error message")
    captured = capsys.readouterr()
    assert "(use --verbose to see more details)" in captured.out

    # When verbose, errors should NOT show the hint
    client.verbose = True
    client.log("ERROR", "Another error")
    captured = capsys.readouterr()
    assert "(use --verbose to see more details)" not in captured.out
