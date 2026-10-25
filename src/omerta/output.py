"""Findings, and how they are written: as lines of text, or as JSON Lines."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# The escapes git writes in a quoted path; other control characters are octal.
_ESCAPES = {
    0x07: "\\a",
    0x08: "\\b",
    0x09: "\\t",
    0x0A: "\\n",
    0x0B: "\\v",
    0x0C: "\\f",
    0x0D: "\\r",
    0x22: '\\"',
    0x5C: "\\\\",
}


def quote(raw: bytes) -> str:
    """A path or a name as git quotes paths, where it has to be.

    One that has ``"``, ``\\``, a control character (a byte below 0x20, or DEL)
    or bytes that are not UTF-8 goes in double quotes, with git's escapes and
    octal ones for the other control characters and the bytes that are not
    UTF-8. Any other, non-ASCII included, is as it is.
    """
    text = raw.decode("utf-8", "surrogateescape")
    if not any(_special(char) for char in text):
        return text
    quoted = []
    for char in text:
        code = ord(char)
        if 0xDC80 <= code <= 0xDCFF:
            quoted.append(f"\\{code - 0xDC00:03o}")
        elif code in _ESCAPES:
            quoted.append(_ESCAPES[code])
        elif code < 0x20 or code == 0x7F:
            quoted.append(f"\\{code:03o}")
        else:
            quoted.append(char)
    return '"' + "".join(quoted) + '"'


def _special(char: str) -> bool:
    """Whether a character makes a path or a name quoted."""
    code = ord(char)
    return code < 0x20 or code == 0x7F or char in '"\\' or 0xDC80 <= code <= 0xDCFF


@dataclass(frozen=True)
class Finding:
    """A value found, or a tracked env file.

    ``path`` is ``None`` for a message and for a blob that has no path;
    ``variable`` is ``None`` for a tracked env file. ``commit``, ``blob`` and
    ``tag`` name where ``history`` found it, ``line`` where ``check`` found it
    in a file's content; ``in_path`` is a value in the path itself.
    """

    path: bytes | None = None
    line: int | None = None
    commit: str | None = None
    blob: str | None = None
    tag: str | None = None
    message: bool = False
    in_path: bool = False
    variable: str | None = None
    env_file: str | None = None
    env_order: int = 0
    form: str | None = None
    order: int = 0
    """For ``history``, the place of the commit in the order of ``git log``."""


def _same(raw: bytes) -> bytes:
    """A path or a name as it is."""
    return raw


Hide = Callable[[bytes], bytes]
"""What a path or an env file's name is shown as: with each value replaced by its variable."""


def text_line(finding: Finding, hide: Hide = _same) -> str:
    """A finding as a line of text, with ``hide`` applied to its path and env file."""
    where = finding.commit or finding.blob or finding.tag
    if finding.message:
        place = f"{where} (message)"
    elif finding.path is None:
        place = f"{where}"
    else:
        path = quote(hide(finding.path))
        place = path if where is None else f"{where} {path}"
        if finding.line is not None:
            place = f"{place}:{finding.line}"
        if finding.in_path:
            place = f"{place} (path)"
    if finding.variable is None:
        return f"{place}: tracked env file"
    variable = quote(finding.variable.encode("utf-8"))
    env_file = quote(hide(os.fsencode(finding.env_file or "")))
    return f"{place}: {variable} (from {env_file})"


def json_line(finding: Finding, hide: Hide = _same) -> str:
    """A finding as a JSON object on one line, with ``hide`` applied to its path and env file."""
    record: dict[str, Any] = {}
    if not finding.message:
        record["path"] = None if finding.path is None else quote(hide(finding.path))
    if finding.line is not None:
        record["line"] = finding.line
    for key in ("commit", "blob", "tag"):
        if getattr(finding, key) is not None:
            record[key] = getattr(finding, key)
    if finding.message:
        record["message"] = True
    if finding.in_path:
        record["in_path"] = True
    if finding.variable is None:
        record["tracked_env_file"] = True
    else:
        record["variable"] = quote(finding.variable.encode("utf-8"))
        record["env_file"] = quote(hide(os.fsencode(finding.env_file or "")))
        record["form"] = finding.form
    return json.dumps(record, ensure_ascii=False)


def sort_key(finding: Finding) -> tuple:
    """Where a finding goes among the others.

    By path, as bytes, with a path's tracked env file first and a value in the
    path next, then by variable, then env file, then the order of ``git log``,
    blob IDs last; the lines without a path come after the others, by object ID.
    """
    variable = finding.variable or ""
    if finding.path is None:
        where = finding.commit or finding.blob or finding.tag or ""
        return (1, b"", where, variable, finding.env_order)
    kind = 0 if finding.variable is None else 1 if finding.in_path else 2
    commit = (0, finding.order, "") if finding.blob is None else (1, 0, finding.blob)
    return (0, finding.path, "", kind, variable, finding.env_order, commit)
