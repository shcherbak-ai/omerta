"""How findings are written and ordered."""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from omerta.output import Finding, json_line, quote, sort_key, text_line

_COMMIT = "a" * 40
_BLOB = "b" * 40


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        (b"src/app.py", "src/app.py"),
        (b"caf\xc3\xa9/r\xc3\xa9sum\xc3\xa9.txt", "café/résumé.txt"),
        (b"with space.txt", "with space.txt"),
        (b'a"b', '"a\\"b"'),
        (b"a\\b", '"a\\\\b"'),
        (b"tab\there", '"tab\\there"'),
        (b"\x07\x08\x09\x0a\x0b\x0c\x0d", '"\\a\\b\\t\\n\\v\\f\\r"'),
        (b"esc\x1b[31m", '"esc\\033[31m"'),
        (b"del\x7f", '"del\\177"'),
        (b"latin\xe9", '"latin\\351"'),
        (b"\xff\xfe", '"\\377\\376"'),
    ],
)
def test_a_path_is_quoted_as_git_quotes_paths_where_it_has_to_be(raw: bytes, shown: str):
    assert quote(raw) == shown


_UNESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def _unquote(shown: str) -> bytes:
    """A path as a reader of git's quoting takes it back: the oracle of the test below."""
    if not shown.startswith('"'):
        return shown.encode("utf-8")
    body, raw, position = shown[1:-1], bytearray(), 0
    while position < len(body):
        if body[position] != "\\":
            raw += body[position].encode("utf-8")
            position += 1
        elif body[position + 1] in _UNESCAPES:
            raw.append(_UNESCAPES[body[position + 1]])
            position += 2
        else:
            raw.append(int(body[position + 1 : position + 4], 8))
            position += 4
    return bytes(raw)


# Each byte that makes a path quoted, at the edge of a rule: control
# characters, DEL, a quote, a backslash, a byte that is not UTF-8, and a UTF-8
# surrogate, which is not UTF-8 either.
_EDGES = [b"\x00", b"\x07", b"\x1b", b"\x1f", b"\x7f", b'"', b"\\", b"\xff", b"\xed\xa0\x80"]
_TEXT = st.text(st.characters(exclude_categories=["Cc", "Cs"]), max_size=4).map(str.encode)
# Paths of text with one such byte in it, or with none, which stay unquoted, or
# of anything at all.
_PATHS = st.one_of(
    st.tuples(_TEXT, st.sampled_from([b"", *_EDGES]), _TEXT).map(b"".join),
    st.lists(st.one_of(st.sampled_from(_EDGES), st.binary(max_size=3), _TEXT)).map(b"".join),
)


@given(_PATHS)
def test_a_quoted_path_reads_back_as_its_bytes_and_stays_on_one_line(raw: bytes):
    shown = quote(raw)
    assert _unquote(shown) == raw
    assert not any(ord(char) < 0x20 or ord(char) == 0x7F for char in shown)


def test_check_findings_as_text_and_json():
    value = Finding(
        path=b"src/a b.py",
        line=3,
        variable="API_KEY",
        env_file=".env.local",
        form="url",
    )
    assert text_line(value) == "src/a b.py:3: API_KEY (from .env.local)"
    assert json.loads(json_line(value)) == {
        "path": "src/a b.py",
        "line": 3,
        "variable": "API_KEY",
        "env_file": ".env.local",
        "form": "url",
    }
    assert list(json.loads(json_line(value))) == ["path", "line", "variable", "env_file", "form"]
    tracked = Finding(path=b".env")
    assert text_line(tracked) == ".env: tracked env file"
    assert json_line(tracked) == '{"path": ".env", "tracked_env_file": true}'


def test_history_findings_as_text_and_json():
    in_commit = Finding(path=b"a.py", commit=_COMMIT, variable="K", env_file=".env", form="plain")
    assert text_line(in_commit) == f"{_COMMIT} a.py: K (from .env)"
    assert list(json.loads(json_line(in_commit))) == [
        "path",
        "commit",
        "variable",
        "env_file",
        "form",
    ]
    in_blob = Finding(path=b"a.py", blob=_BLOB, variable="K", env_file=".env", form="json")
    assert text_line(in_blob) == f"{_BLOB} a.py: K (from .env)"
    pathless = Finding(blob=_BLOB, variable="K", env_file=".env", form="plain")
    assert text_line(pathless) == f"{_BLOB}: K (from .env)"
    assert json.loads(json_line(pathless))["path"] is None
    message = Finding(commit=_COMMIT, message=True, variable="K", env_file=".env", form="plain")
    assert text_line(message) == f"{_COMMIT} (message): K (from .env)"
    assert json_line(message) == (
        f'{{"commit": "{_COMMIT}", "message": true, "variable": "K", "env_file": ".env", '
        '"form": "plain"}'
    )
    tag = Finding(tag=_BLOB, message=True, variable="K", env_file=".env", form="base64")
    assert text_line(tag) == f"{_BLOB} (message): K (from .env)"
    assert json.loads(json_line(tag))["tag"] == _BLOB
    tracked = Finding(path=b".env", commit=_COMMIT)
    assert text_line(tracked) == f"{_COMMIT} .env: tracked env file"
    assert json.loads(json_line(tracked)) == {
        "path": ".env",
        "commit": _COMMIT,
        "tracked_env_file": True,
    }


def test_names_and_env_files_are_quoted_too_in_both_formats():
    odd = Finding(path=b"a\nb", line=1, variable='K"1', env_file=".env\x01", form="plain")
    assert text_line(odd) == '"a\\nb":1: "K\\"1" (from ".env\\001")'
    assert json.loads(json_line(odd))["path"] == '"a\\nb"'
    assert json.loads(json_line(odd))["variable"] == '"K\\"1"'
    assert json.loads(json_line(odd))["env_file"] == '".env\\001"'
    assert "é" in json_line(Finding(path="é".encode(), variable="K", env_file=".env"))


def test_findings_are_sorted_by_path_then_variable_then_env_file_then_log_order():
    def value(path: bytes, variable: str, env_order: int = 0, **place) -> Finding:
        return Finding(path=path, variable=variable, env_file=".env", env_order=env_order, **place)

    findings = [
        Finding(commit=_COMMIT, message=True, variable="A", env_file=".env"),
        Finding(blob=_BLOB, variable="A", env_file=".env"),
        value(b"b.py", "A", blob=_BLOB),
        value(b"b.py", "A", commit=_COMMIT, order=1),
        value(b"b.py", "A", commit=_COMMIT, order=0),
        value(b"b.py", "A", 1),
        value(b"b.py", "B"),
        Finding(path=b"b.py"),
        value(b"B.py", "Z"),
        value(b"a.py", "Z"),
    ]
    ordered = sorted(findings, key=sort_key)
    assert ordered == [
        findings[8],  # B.py: by bytes, upper case first
        findings[9],
        findings[7],  # b.py: its tracked env file line first
        findings[4],
        findings[3],
        findings[2],
        findings[5],
        findings[6],
        findings[0],  # no path: by object ID
        findings[1],
    ]
