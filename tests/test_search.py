"""What is searched for, and the search."""

from __future__ import annotations

import json
import random
from pathlib import Path

from omerta import config
from omerta.envfiles import EnvFile
from omerta.search import Hit, Target, line_of, redact, search, targets

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


def _target(*forms: tuple[bytes, str], variable: str = "KEY") -> Target:
    """A target whose forms are searched one by one, with no core to rule them out."""
    members = tuple((rank, 0, form, kind) for rank, (form, kind) in enumerate(forms))
    return Target(variable, ".env", 0, ((b"", members),))


def test_search_finds_the_first_occurrence_of_any_form():
    data = b"one\ntwo c2stdGVzdC0 three\nsk-test-0123456789abcdef"
    plain = _target((_KEY.encode(), "plain"), (b"c2stdGVzdC0", "base64"))
    missing = _target((b"absent-value", "plain"))
    assert search(data, [missing, plain]) == {1: Hit(8, "base64")}
    assert line_of(data, 8) == 2
    assert line_of(data, 0) == 1


def test_at_one_position_the_form_listed_first_wins():
    data = b"https://res.example.com/v1"
    target = _target((b"https://res.example.com", "plain"), (b"https://res", "plain"))
    assert search(data, [target]) == {0: Hit(0, "plain")}
    target = _target((b"https://res", "url"), (b"https://res.example.com", "plain"))
    assert search(data, [target]) == {0: Hit(0, "url")}


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


def test_forms_are_grouped_by_a_core_that_each_of_them_holds(tmp_path: Path):
    env = _env(tmp_path, ".env", "URL=https://user:p@ss@res.example.com/v1?x=é\nPUNCT=!@#$%^&*()\n")
    settings = config.Settings(patterns=(), min_chars=8, ignore=frozenset(), encoded=True)
    url, punct = targets([env], settings)
    cores = [core for core, _ in url.groups]
    assert cores[0] == b"res.example.com"
    for core, members in url.groups:
        assert core
        assert all(core in form for _, _, form, _ in members)
    # A value without a long enough run of unchanged characters is searched
    # for form by form.
    assert punct.groups[0][0] == b""


def _naive(data: bytes, target: Target) -> Hit | None:
    """The first occurrence of any form, one find per form, the earlier form first."""
    found = [(data.find(form), rank, kind) for rank, (form, kind) in enumerate(target.forms)]
    found = [each for each in found if each[0] >= 0]
    return Hit(min(found)[0], min(found)[2]) if found else None


def test_the_grouped_search_finds_what_a_search_of_every_form_finds(tmp_path: Path):
    alphabet = 'abcXYZ0189-_.~/:+=@ %"\\\né'
    generator = random.Random(20261025)
    settings = config.Settings(patterns=(), min_chars=8, ignore=frozenset(), encoded=True)
    for _ in range(60):
        value = "".join(generator.choice(alphabet) for _ in range(generator.randint(8, 24)))
        line = json.dumps(value)[1:-1].replace("\\n", "\\\\n")
        env = _env(tmp_path, ".env", f'KEY="{line}"\n')
        (target,) = targets([env], settings)
        forms = [form for form, _ in target.forms]
        for _ in range(10):
            pieces = [generator.choice(forms) for _ in range(generator.randint(0, 2))]
            noise = [bytes(generator.choice(b"abc019%\\/+") for _ in range(5)) for _ in range(3)]
            parts = pieces + noise
            generator.shuffle(parts)
            data = b"".join(
                part[: generator.randint(1, len(part))] if i % 2 else part
                for i, part in enumerate(parts)
            )
            assert search(data, [target]).get(0) == _naive(data, target)
