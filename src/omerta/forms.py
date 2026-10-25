"""The forms in which a value is searched for: as written, and encoded in URLs, JSON and base64."""

from __future__ import annotations

import base64
import json
import re
import urllib.parse
from collections.abc import Iterator

PLAIN = "plain"
URL = "url"
JSON = "json"
BASE64 = "base64"

_ESCAPE = re.compile(r"%[0-9A-F]{2}")
_URL_SAFE = str.maketrans("+/", "-_")


def forms(value: str, *, encoded: bool, min_chars: int) -> dict[str, str]:
    """The forms of a value to search for, each with its kind.

    - plain: the value; a multi-line value also with ``\\r\\n`` line ends; a
      value with ``://`` also without one trailing slash, which a longer URL
      contains;
    - url, from each plain form: percent-encoded as a path and as a query, each
      also with lowercase escapes;
    - json, from each plain form: as in a JSON string, with non-ASCII escaped
      and not, each also with ``/`` written ``\\/``;
    - base64, from each plain form: the characters that its own UTF-8 bytes
      decide inside a longer encoded text, at each of the three alignments, in
      the standard and the URL-safe alphabets.

    Args:
        value: The value, with ``\\n`` line ends.
        encoded: Whether to search the encoded forms too.
        min_chars: The shortest form searched for, in characters.

    Returns:
        Each form, mapped to its kind: ``plain``, ``url``, ``json`` or
        ``base64``. A form that two kinds give keeps the first kind.
    """
    plain = list(dict.fromkeys(_plain(value)))
    found: dict[str, str] = {}
    kinds = [(PLAIN, _same)]
    if encoded:
        kinds += [(URL, _url), (JSON, _json), (BASE64, _base64)]
    for kind, encode in kinds:
        for form in plain:
            for each in encode(form):
                if len(each) >= min_chars:
                    found.setdefault(each, kind)
    return found


def _plain(value: str) -> Iterator[str]:
    """The plain forms of a value."""
    yield value
    if "\n" in value:
        yield value.replace("\n", "\r\n")
    if "://" in value and value.endswith("/"):
        yield value[:-1]


def _same(form: str) -> Iterator[str]:
    """A plain form as it is."""
    yield form


def _url(form: str) -> Iterator[str]:
    """A plain form, percent-encoded."""
    for escaped in (
        urllib.parse.quote(form, safe=""),
        urllib.parse.quote_plus(form, safe=""),
    ):
        yield escaped
        yield _ESCAPE.sub(lambda match: match[0].lower(), escaped)


def _json(form: str) -> Iterator[str]:
    """A plain form, as the text of a JSON string."""
    for text in (json.dumps(form)[1:-1], json.dumps(form, ensure_ascii=False)[1:-1]):
        yield text
        yield text.replace("/", "\\/")


def _base64(form: str) -> Iterator[str]:
    """The base64 characters that a plain form's own bytes decide."""
    data = form.encode("utf-8")
    for padding in range(3):
        text = base64.b64encode(bytes(padding) + data).decode("ascii")
        # A character stands for 6 bits: keep those whose bits all come from
        # the form's bytes, not from the padding before it or from what would
        # follow it in a longer text.
        first = -(-8 * padding // 6)
        last = 8 * (padding + len(data)) // 6
        yield text[first:last]
        yield text[first:last].translate(_URL_SAFE)
