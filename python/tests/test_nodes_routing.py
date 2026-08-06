"""The index line may carry routing info: "1 HOST 2 3 4".

That is how the hub writes its own entry, so every real nodelist has one.
nova-hub's parser has handled it since the format was introduced; the client's
did not, and rejected the file outright - which failed validation on a live node
and would have stopped the daemon starting.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nodes_parser import NodesFileParser  # noqa: E402

# Line endings are CRLF here on purpose: these files come from DOS.
REAL_WORLD = (
    "1 HOST 2 3 4\r\nNova Hub\r\n135:1/1\r\nBrisbane\r\nQLD\r\nAUS\r\n\r\n"
    "2\r\nStarship Junkyard\r\n135:1/2\r\nBrisbane\r\nQLD\r\nAUS\r\n\r\n"
)


def _parse(tmp_path, text):
    target = tmp_path / "BRNODES.015"
    target.write_bytes(text.encode("ascii"))
    parser = NodesFileParser(target)
    return parser, parser.parse()


def test_host_routing_line_parses(tmp_path):
    parser, ok = _parse(tmp_path, REAL_WORLD)
    assert ok, parser.errors
    assert parser.errors == []
    assert [n.bbs_index for n in parser.nodes] == [1, 2]
    assert parser.nodes[0].bbs_name == "Nova Hub"


def test_routing_targets_are_captured(tmp_path):
    parser, _ = _parse(tmp_path, REAL_WORLD)
    assert parser.nodes[0].routing_targets == [2, 3, 4]
    assert parser.nodes[1].routing_targets == []


def test_plain_index_still_parses(tmp_path):
    parser, ok = _parse(
        tmp_path, "7\r\nSolo BBS\r\n1:2/3\r\nCity\r\nState\r\nCountry\r\n\r\n"
    )
    assert ok, parser.errors
    assert parser.nodes[0].bbs_index == 7
    assert parser.nodes[0].routing_targets == []


def test_lowercase_host_parses(tmp_path):
    parser, ok = _parse(
        tmp_path, "1 host 2\r\nNova Hub\r\n1:2/3\r\nCity\r\nState\r\nCountry\r\n\r\n"
    )
    assert ok, parser.errors
    assert parser.nodes[0].routing_targets == [2]


def test_malformed_routing_tokens_are_skipped_not_fatal(tmp_path):
    """A bad routing target must not cost us the node itself."""
    parser, ok = _parse(
        tmp_path, "1 HOST 2 x 4\r\nNova Hub\r\n1:2/3\r\nCity\r\nState\r\nCountry\r\n\r\n"
    )
    assert ok, parser.errors
    assert parser.nodes[0].bbs_index == 1
    assert parser.nodes[0].routing_targets == [2, 4]


def test_genuinely_bad_index_still_reported(tmp_path):
    """Leniency about routing must not become leniency about the index."""
    parser, ok = _parse(
        tmp_path, "HOST 2 3\r\nNova Hub\r\n1:2/3\r\nCity\r\nState\r\nCountry\r\n\r\n"
    )
    assert not ok
    assert "Invalid BBS index" in parser.errors[0]
    # The message quotes the whole line, which is what the operator sees in the file.
    assert "HOST 2 3" in parser.errors[0]
