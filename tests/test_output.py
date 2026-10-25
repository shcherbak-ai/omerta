"""How findings are written and ordered."""

from __future__ import annotations

import json

import pytest

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
