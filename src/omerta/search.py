"""What is searched for, the search itself, and the redaction of what is printed."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from omerta.config import Settings
from omerta.envfiles import EnvFile, read
from omerta.forms import BASE64, forms

# The characters that no encoding of omerta's changes: percent-encoding and
# JSON leave them as they are. Every plain, url and json form of a value holds
# the value's longest run of them.
_UNCHANGED = re.compile(rb"[A-Za-z0-9_.~-]+")
# The base64 characters that both alphabets share: a form's standard and
# URL-safe spellings hold the same longest run of them.
_SHARED = re.compile(rb"[A-Za-z0-9]+")
# A shorter run is found too often to save a search.
_MIN_CORE = 4

Member = tuple[int, int, bytes, str]
"""A form: its rank among the target's forms, its value's index, its bytes and its kind."""

Group = tuple[bytes, tuple[Member, ...]]
"""Forms that all hold one core: the core, and the forms."""

Occurrence = tuple[int, int, str]
"""Where a value is first found: the position, the rank of the form there, and its kind."""


@dataclass(frozen=True)
class Target:
    """A variable of an env file, with the forms of each value it has there.

    The forms are grouped by a core that each form of a group holds: content
    without the core holds none of them, which one search tells.
    """

    variable: str
    env_file: str
    """The env file's path relative to the root, with ``/``."""

    env_order: int
    """The env file's place in the order of the patterns, then of the paths."""

    groups: tuple[Group, ...] = field(repr=False)
    """The forms as UTF-8 bytes, by their cores. Never printed."""

    @property
    def forms(self) -> tuple[tuple[bytes, str], ...]:
        """Each form with its kind, in their order."""
        ranked = sorted(member for _, members in self.groups for member in members)
        return tuple((form, kind) for _, _, form, kind in ranked)


@dataclass(frozen=True)
class Hit:
    """The first occurrence of a target in some content."""

    position: int
    form: str


def targets(env_files: list[EnvFile], settings: Settings) -> list[Target]:
    """The variables to search for, from the env files in their order.

    Left out are a key without a value, the variables of ``ignore``, and values
    shorter than ``min_chars`` characters. When env files give one variable the
    same value, the first of them has it.

    Raises:
        OmertaError: An env file cannot be read or parsed.
    """
    seen: set[tuple[str, str]] = set()
    values: dict[tuple[int, str], list[str]] = {}
    for order, env_file in enumerate(env_files):
        for variable, value in read(env_file):
            if value is None or variable in settings.ignore or len(value) < settings.min_chars:
                continue
            if (variable, value) not in seen:
                seen.add((variable, value))
                values.setdefault((order, variable), []).append(value)
    found = []
    for (order, variable), each in values.items():
        ranks: dict[bytes, int] = {}
        groups: dict[bytes, list[Member]] = {}
        for number, value in enumerate(each):
            unchanged = _core(value.encode("utf-8"), _UNCHANGED)
            for text, kind in forms(
                value, encoded=settings.encoded, min_chars=settings.min_chars
            ).items():
                form = text.encode("utf-8")
                if form in ranks:
                    continue
                ranks[form] = len(ranks)
                core = _core(form, _SHARED) if kind == BASE64 else unchanged
                groups.setdefault(core, []).append((ranks[form], number, form, kind))
        found.append(
            Target(
                variable=variable,
                env_file=env_files[order].path,
                env_order=order,
                groups=tuple((core, tuple(members)) for core, members in groups.items()),
            )
        )
    return found


def _core(text: bytes, run: re.Pattern[bytes]) -> bytes:
    """The longest run of ``text``, or nothing where it is too short to search for."""
    core = max(run.findall(text), key=len, default=b"")
    return core if len(core) >= _MIN_CORE else b""


def occurrences(data: bytes, searched: list[Target]) -> dict[int, dict[int, Occurrence]]:
    """Where each value of each target is first found in ``data``.

    Returns:
        For each target found, by its index: for each of its values found, by
        the value's index, its first occurrence. At one position, the form
        listed first wins.
    """
    found = {}
    for index, target in enumerate(searched):
        values: dict[int, Occurrence] = {}
        for core, members in target.groups:
            if core and core not in data:
                continue
            for rank, value, form, kind in members:
                position = data.find(form)
                if position >= 0 and (value not in values or (position, rank) < values[value][:2]):
                    values[value] = (position, rank, kind)
        if values:
            found[index] = values
    return found


def first(values: dict[int, Occurrence]) -> Hit:
    """The first occurrence of any of the values of a target."""
    position, _, kind = min(values.values())
    return Hit(position, kind)


def search(data: bytes, searched: list[Target]) -> dict[int, Hit]:
    """The first occurrence of each target found in ``data``, by its index."""
    return {index: first(values) for index, values in occurrences(data, searched).items()}


def line_of(data: bytes, position: int) -> int:
    """The line of a position, counted from 1."""
    return data.count(b"\n", 0, position) + 1


def redact(data: bytes, searched: list[Target]) -> bytes:
    """``data`` with each occurrence of a form replaced by its variable's name.

    Overlapping occurrences are replaced together, by the names of their
    variables (``<A|B>``), so that no part of a value is left.
    """
    spans = []
    for target in searched:
        for form, _ in target.forms:
            start = data.find(form)
            while start >= 0:
                spans.append((start, start + len(form), target.variable))
                start = data.find(form, start + 1)
    pieces = []
    done = 0
    merged: list[tuple[int, int, list[str]]] = []
    for start, end, variable in sorted(spans):
        if merged and start < merged[-1][1]:
            last_start, last_end, names = merged[-1]
            merged[-1] = (last_start, max(last_end, end), names)
            if variable not in names:
                names.append(variable)
        else:
            merged.append((start, end, [variable]))
    for start, end, names in merged:
        pieces += [data[done:start], f"<{'|'.join(names)}>".encode()]
        done = end
    return b"".join(pieces) + data[done:]
