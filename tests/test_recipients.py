import pytest

from talkub.errors import ValidationError
from talkub.recipients import Recipient, parse, render

KEY_A = "age1qk2vu7xxezxcfhyhasnx8c7z5j479xwevu5tqq8tc2fjqjzdegjqvepymq"
KEY_B = "age1yubikey1qgw6x0m7l9t6tg3hvc8d0x3q7tnfq8m5h4k6s2r3v4w5x6y7z8a9bcdefg"


def test_render_parse_roundtrip():
    items = [Recipient("alice", KEY_A), Recipient("bob-yubikey", KEY_B)]
    text = render(items)
    assert text.splitlines()[1:] == ["# name: alice", KEY_A, "# name: bob-yubikey", KEY_B]
    assert parse(text) == items


def test_parse_ignores_comments_and_names_unnamed_keys():
    text = f"# some comment\n\n{KEY_A}\n"
    assert parse(text) == [Recipient("unnamed-3", KEY_A)]


def test_parse_rejects_non_age_line():
    with pytest.raises(ValidationError):
        parse("ssh-ed25519 AAAA\n")


def test_invalid_name_rejected():
    with pytest.raises(ValidationError):
        render([Recipient("with space", KEY_A)])
