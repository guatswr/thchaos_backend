from integrations.astrbot_plugin_thchaos.logic import (
    default_umo,
    format_snapshot,
    parse_vote_choice,
    pseudonymous_voter_id,
)


def test_plugin_vote_parser_and_pseudonym():
    assert parse_vote_choice(" 2 ") == 2
    assert parse_vote_choice("12") is None
    assert parse_vote_choice("投2") is None
    first = pseudonymous_voter_id("secret", "123")
    assert first == pseudonymous_voter_id("secret", "123")
    assert first != pseudonymous_voter_id("secret", "124")
    assert len(first) <= 63 and len(first) == 63 and first.isascii() and " " not in first


def test_snapshot_format_and_umo():
    assert default_umo("123456") == "aiocqhttp:GroupMessage:123456"
    text = format_snapshot(
        {
            "round_id": 3,
            "options": [{"choice": 1, "votes": 2}, {"choice": 2, "votes": 0}, {"choice": 3, "votes": 1}],
            "paused": True,
        }
    )
    assert "#3" in text and "1:2票" in text and "冻结" in text
