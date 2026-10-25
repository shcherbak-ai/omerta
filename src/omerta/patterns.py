"""Env file patterns, matched part by part against paths relative to the repository's root."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Sequence

from omerta.errors import OmertaError

DEFAULT_PATTERNS = (".env", ".env.*")

# A file whose name matches one of these is a template, never a source and
# never a leak: `.env.example` documents the variables, without their values.
TEMPLATES = ("*.example", "*.sample", "*.template")

_DRIVE = re.compile(r"[A-Za-z]:")


class Pattern:
    """An env file pattern, such as ``.env``, ``.env.*`` or ``apps/*/.env``.

    Each part between the ``/`` is matched with :func:`fnmatch.fnmatchcase`, so
    ``*`` and ``?`` stay within a part and case counts on every platform; a part
    that is exactly ``**`` matches any number of folders.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.parts = _parse(text)

    def matches(self, parts: Sequence[str]) -> bool:
        """Whether a path, given as its parts, matches the pattern."""
        return _match(self.parts, tuple(parts))


def is_template(name: str) -> bool:
    """Whether a file name is that of a template, such as ``.env.example``."""
    return any(fnmatch.fnmatchcase(name, template) for template in TEMPLATES)


def is_env_file(path: bytes, patterns: Sequence[Pattern]) -> bool:
    """Whether a path, as git gives it, is that of an env file, not a template."""
    parts = path.decode("utf-8", "surrogateescape").split("/")
    return not is_template(parts[-1]) and any(pattern.matches(parts) for pattern in patterns)


def _parse(text: str) -> tuple[str, ...]:
    """The parts of a pattern, or an error that says what is wrong with it."""
    if not text:
        raise OmertaError("an env file pattern is empty")
    if text.startswith("/") or _DRIVE.match(text):
        raise OmertaError(f"env file pattern {text!r} is absolute: write it from the root")
    if "\\" in text:
        raise OmertaError(f"env file pattern {text!r} has a backslash: separate folders with /")
    parts = tuple(text.split("/"))
    for part in parts:
        if part in ("", "."):
            raise OmertaError(f"env file pattern {text!r} has an empty or '.' part")
        if part == "..":
            raise OmertaError(f"env file pattern {text!r} has a '..' part")
        if "**" in part and part != "**":
            raise OmertaError(f"env file pattern {text!r} has '**' within a part")
    return parts


def _match(pattern: tuple[str, ...], path: tuple[str, ...]) -> bool:
    """Whether the parts of a path match those of a pattern."""
    if not pattern:
        return not path
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        return any(_match(rest, path[start:]) for start in range(len(path) + 1))
    return bool(path) and fnmatch.fnmatchcase(path[0], head) and _match(rest, path[1:])
