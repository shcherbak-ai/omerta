"""What is searched for, and the search."""

from __future__ import annotations

import re
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from omerta import config
from omerta.envfiles import EnvFile
from omerta.search import Hit, Matcher, Target, line_of, redact, targets

_KEY = "sk-test-0123456789abcdef"
_OTHER = "sk-other-fedcba9876543210"


def _env(tmp_path: Path, name: str, text: str) -> EnvFile:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return EnvFile(name, path)


def test_targets_leave_out_short_ignored_and_missing_values(tmp_path: Path):
    env = _env(
        tmp_path,
        ".env",
        f"KEY={_KEY}\nSHORT=1234567\nEIGHT=12345678\nHOST=db.example.com\nNONE\nEMPTY=\n",
    )
    settings = config.Settings(patterns=(), min_chars=8, ignore=frozenset({"HOST"}), encoded=False)
    found = targets([env], settings)
    assert [(t.variable, t.env_file, t.env_order) for t in found] == [
        ("KEY", ".env", 0),
        ("EIGHT", ".env", 0),
    ]
    assert found[0].forms == ((_KEY.encode(), "plain"),)


def test_a_value_given_in_two_env_files_is_searched_for_with_the_first(tmp_path: Path):
    first = _env(tmp_path, ".env", f"KEY={_KEY}\n")
    second = _env(tmp_path, ".env.local", f"KEY={_KEY}\nKEY={_OTHER}\nKEY={_OTHER}\n")
    settings = config.Settings(patterns=(), min_chars=8, ignore=frozenset(), encoded=False)
    found = targets([first, second], settings)
    assert [(t.variable, t.env_file, t.forms) for t in found] == [
        ("KEY", ".env", ((_KEY.encode(), "plain"),)),
        ("KEY", ".env.local", ((_OTHER.encode(), "plain"),)),
    ]


def test_the_values_of_a_key_given_twice_are_both_searched_for(tmp_path: Path):
    env = _env(tmp_path, ".env", f"KEY={_KEY}\nKEY={_OTHER}\n")
    settings = config.Settings(patterns=(), min_chars=8, ignore=frozenset(), encoded=False)
    (target,) = targets([env], settings)
    assert target.forms == ((_KEY.encode(), "plain"), (_OTHER.encode(), "plain"))


def test_a_form_that_two_values_share_is_searched_for_once(tmp_path: Path):
    env = _env(tmp_path, ".env", "URL=https://res.example.com/\nURL=https://res.example.com\n")
    settings = config.Settings(patterns=(), min_chars=8, ignore=frozenset(), encoded=False)
    (target,) = targets([env], settings)
    assert target.forms == (
        (b"https://res.example.com/", "plain"),
        (b"https://res.example.com", "plain"),
    )


def _target(*forms: tuple[bytes, str], variable: str = "KEY", value: int = 0) -> Target:
    """A target whose forms all belong to one of its values."""
    members = tuple((rank, value, form, kind) for rank, (form, kind) in enumerate(forms))
    return Target(variable, ".env", 0, members)


def test_search_finds_the_first_occurrence_of_any_form():
    data = b"one\ntwo c2stdGVzdC0 three\nsk-test-0123456789abcdef"
    plain = _target((_KEY.encode(), "plain"), (b"c2stdGVzdC0", "base64"))
    missing = _target((b"absent-value", "plain"))
    assert Matcher([missing, plain]).search(data) == {1: Hit(8, "base64")}
    assert line_of(data, 8) == 2
    assert line_of(data, 0) == 1


def test_at_one_position_the_form_listed_first_wins():
    data = b"https://res.example.com/v1"
    target = _target((b"https://res.example.com", "plain"), (b"https://res", "plain"))
    assert Matcher([target]).search(data) == {0: Hit(0, "plain")}
    target = _target((b"https://res", "url"), (b"https://res.example.com", "plain"))
    assert Matcher([target]).search(data) == {0: Hit(0, "url")}


def test_a_form_that_two_targets_have_is_found_for_both():
    shared = b"sk-shared-0123456789"
    first = _target((shared, "plain"), variable="A")
    second = _target((b"unrelated-value", "plain"), (shared, "plain"), variable="B")
    assert Matcher([first, second]).search(b"x " + shared) == {
        0: Hit(2, "plain"),
        1: Hit(2, "plain"),
    }


def test_each_value_of_a_target_is_found_on_its_own():
    members = ((0, 0, _KEY.encode(), "plain"), (1, 1, _OTHER.encode(), "plain"))
    target = Target("KEY", ".env", 0, members)
    data = f"{_OTHER} {_KEY} {_OTHER}".encode()
    assert Matcher([target]).occurrences(data) == {
        0: {0: (len(_OTHER) + 1, 0, "plain"), 1: (0, 1, "plain")}
    }


def test_with_nothing_to_search_for_nothing_is_found():
    assert Matcher([]).search(b"anything at all") == {}
    assert Matcher([_target((b"absent-value", "plain"))]).search(b"") == {}


def test_redact_replaces_each_occurrence_by_the_variable_name():
    target = _target((b"secret", "plain"), (b"secret-long", "plain"), variable="API_KEY")
    assert redact(b"x secret-long y secret", [target]) == b"x <API_KEY> y <API_KEY>"
    assert redact(b"nothing here", [target]) == b"nothing here"


def test_redact_leaves_no_part_of_overlapping_values():
    first = _target((b"abcdefgh12345678", "plain"), variable="A")
    second = _target((b"12345678zyxwvuts", "plain"), variable="B")
    third = _target((b"zyxw", "plain"), variable="C")
    assert redact(b"<abcdefgh12345678zyxwvuts>", [first, second, third]) == b"<<A|B|C>>"
    assert redact(b"abcdefgh12345678 zyxw", [first, third]) == b"<A> <C>"


def test_a_target_never_shows_its_forms():
    assert _KEY not in repr(_target((_KEY.encode(), "plain")))


# The forms of an example are cut from one short text of two letters, often
# the whole of it, so that they overlap each other and themselves (`aba` in
# `ababa`), share prefixes, lie inside each other, start at one position and
# repeat. No `<`, `|`, `>` or capital letter: those are the redaction's own,
# and variables are named in capitals.
_KINDS = st.sampled_from(["plain", "url", "json", "base64"])
_NOISE = st.text("ab%", min_size=1, max_size=3).map(str.encode)


@st.composite
def _example(draw: st.DrawFn) -> tuple[list[Target], bytes]:
    """Targets of one or two values each, built as ``targets`` builds them, and content."""
    text = draw(st.text("ab", min_size=3, max_size=8)).encode()
    parts = {
        text[start:end] for start in range(len(text)) for end in range(start + 1, len(text) + 1)
    }
    cuts = st.one_of(st.just(text), st.sampled_from(sorted(parts)))
    searched = []
    for variable in draw(st.lists(st.sampled_from(["A", "B", "AB"]), min_size=1, max_size=3)):
        members: dict[bytes, tuple[int, int, bytes, str]] = {}
        for value in range(draw(st.integers(1, 2))):
            for form in draw(st.lists(cuts, min_size=1, max_size=3)):
                members.setdefault(form, (len(members), value, form, draw(_KINDS)))
        searched.append(Target(variable, ".env", 0, tuple(members.values())))
    content = b"".join(draw(st.lists(st.one_of(cuts, _NOISE), max_size=8)))
    return searched, content


def _naive(data: bytes, target: Target) -> dict[int, tuple[int, int, str]]:
    """Where each value is first found, one find per form; at a tie, the earlier form."""
    found: dict[int, tuple[int, int, str]] = {}
    for rank, value, form, kind in target.members:
        position = data.find(form)
        if position >= 0 and (value not in found or position < found[value][0]):
            found[value] = (position, rank, kind)
    return found


@given(_example())
def test_the_matcher_finds_what_a_search_of_every_form_finds(example: tuple[list[Target], bytes]):
    searched, content = example
    matcher = Matcher(searched)
    expected = {}
    for index, target in enumerate(searched):
        if found := _naive(content, target):
            expected[index] = found
    assert matcher.occurrences(content) == expected
    hits = matcher.search(content)
    assert hits.keys() == expected.keys()
    for index, values in expected.items():
        position, _, kind = min(values.values())
        assert hits[index] == Hit(position, kind)


def _covered(data: bytes, searched: list[Target]) -> list[bool]:
    """For each byte, whether an occurrence of a form covers it."""
    covered = [False] * len(data)
    for target in searched:
        for form, _ in target.forms:
            for start in range(len(data) - len(form) + 1):
                if data.startswith(form, start):
                    covered[start : start + len(form)] = [True] * len(form)
    return covered


@given(_example())
def test_redact_replaces_every_byte_of_every_occurrence_and_keeps_the_rest(
    example: tuple[list[Target], bytes],
):
    searched, content = example
    shown = redact(content, searched)
    covered = _covered(content, searched)
    kept = bytes(byte for byte, hidden in zip(content, covered, strict=True) if not hidden)
    assert re.sub(rb"<[A-Z|]+>", b"", shown) == kept
    for target in searched:
        for form, _ in target.forms:
            assert form not in shown
