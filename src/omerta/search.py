"""What is searched for, the search itself, and the redaction of what is printed."""

from __future__ import annotations

from dataclasses import dataclass, field

import hyperscan

from omerta.config import Settings
from omerta.envfiles import EnvFile, read
from omerta.forms import forms

Member = tuple[int, int, bytes, str]
"""A form: its rank among the target's forms, its value's index, its bytes and its kind."""

Occurrence = tuple[int, int, str]
"""Where a value is first found: the position, the rank of the form there, and its kind."""


@dataclass(frozen=True)
class Target:
    """A variable of an env file, with the forms of each value it has there."""

    variable: str
    env_file: str
    """The env file's path relative to the root, with ``/``."""

    env_order: int
    """The env file's place in the order of the patterns, then of the paths."""

    members: tuple[Member, ...] = field(repr=False)
    """The forms as UTF-8 bytes, in their order. Never printed."""

    @property
    def forms(self) -> tuple[tuple[bytes, str], ...]:
        """Each form with its kind, in their order."""
        return tuple((form, kind) for _, _, form, kind in self.members)


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
        members: dict[bytes, Member] = {}
        for number, value in enumerate(each):
            for text, kind in forms(
                value, encoded=settings.encoded, min_chars=settings.min_chars
            ).items():
                form = text.encode("utf-8")
                members.setdefault(form, (len(members), number, form, kind))
        found.append(
            Target(
                variable=variable,
                env_file=env_files[order].path,
                env_order=order,
                members=tuple(members.values()),
            )
        )
    return found


class Matcher:
    """Every form of every target in one Hyperscan database, which one pass over content searches.

    Each form is a literal reported at its first occurrence only, which is all a
    search needs: a form's first end, less its length, is its first position.
    """

    def __init__(self, searched: list[Target]) -> None:
        self._forms: list[bytes] = []
        # For each form, every target that has it: the target's index, the
        # value's index, the form's rank in the target, and its kind.
        self._owners: list[list[tuple[int, int, int, str]]] = []
        numbers: dict[bytes, int] = {}
        for index, target in enumerate(searched):
            for rank, value, form, kind in target.members:
                if form not in numbers:
                    numbers[form] = len(self._forms)
                    self._forms.append(form)
                    self._owners.append([])
                self._owners[numbers[form]].append((index, value, rank, kind))
        self._database = None
        if self._forms:
            self._database = hyperscan.Database(mode=hyperscan.HS_MODE_BLOCK)
            self._database.compile(
                expressions=self._forms,
                ids=list(range(len(self._forms))),
                elements=len(self._forms),
                flags=[hyperscan.HS_FLAG_SINGLEMATCH] * len(self._forms),
                literal=True,
            )

    def occurrences(self, data: bytes) -> dict[int, dict[int, Occurrence]]:
        """Where each value of each target is first found in ``data``.

        Returns:
            For each target found, by its index: for each of its values found, by
            the value's index, its first occurrence. At one position, the form
            listed first wins.
        """
        if self._database is None:
            return {}
        starts: dict[int, int] = {}

        def matched(form: int, start: int, end: int, *_: object) -> None:
            """Note where a form is first found (its flags and context are not used)."""
            starts.setdefault(form, end - len(self._forms[form]))

        self._database.scan(data, match_event_handler=matched)
        found: dict[int, dict[int, Occurrence]] = {}
        for form, position in starts.items():
            for index, value, rank, kind in self._owners[form]:
                values = found.setdefault(index, {})
                if value not in values or (position, rank) < values[value][:2]:
                    values[value] = (position, rank, kind)
        return found

    def search(self, data: bytes) -> dict[int, Hit]:
        """The first occurrence of each target found in ``data``, by its index."""
        return {index: first(values) for index, values in self.occurrences(data).items()}


def first(values: dict[int, Occurrence]) -> Hit:
    """The first occurrence of any of the values of a target."""
    position, _, kind = min(values.values())
    return Hit(position, kind)


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
