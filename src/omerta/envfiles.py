"""The env files of the working tree, and the variables they set."""

from __future__ import annotations

import codecs
import fnmatch
import io
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from dotenv.parser import parse_stream

from omerta.errors import OmertaError
from omerta.output import quote
from omerta.patterns import Pattern, is_template


@dataclass(frozen=True)
class EnvFile:
    """An env file that git does not track, a source of values."""

    path: str
    """Its path relative to the root of the repository, with ``/``."""

    absolute: Path


def find(top: Path, patterns: tuple[Pattern, ...], tracked: set[bytes]) -> list[EnvFile]:
    """The env files of the working tree that git does not track.

    Each pattern's folders are walked with :func:`os.scandir`: ``.git`` is never
    entered, nor a folder that holds a ``.git`` of its own (a submodule, a
    nested repository), nor a symbolic link to a folder; a symbolic link to a
    file is read through. A folder that matches a pattern is no env file, and
    neither is a template.

    Args:
        top: The root of the repository.
        patterns: The env file patterns, in order.
        tracked: The paths of the index entries, as git gives them.

    Returns:
        The env files in the order of the patterns, then of their paths.

    Raises:
        OmertaError: A folder on the way cannot be listed.
    """
    found: dict[str, EnvFile] = {}
    for pattern in patterns:
        for parts in sorted(set(_walk(top, (), pattern.parts))):
            path = "/".join(parts)
            if path in found or is_template(parts[-1]) or os.fsencode(path) in tracked:
                continue
            found[path] = EnvFile(path, top.joinpath(*parts))
    return list(found.values())


def read(env_file: EnvFile) -> list[tuple[str, str | None]]:
    """The variables an env file sets, with their values, in the order of the file.

    The file's bytes are decoded as UTF-16 after a UTF-16 byte order mark, and
    as UTF-8 otherwise, with or without one; line ends are then made ``\\n``, as
    Python's text mode reads a file. python-dotenv parses the text: quotes,
    ``export`` and comments, with no ``${VAR}`` expanded. A variable given twice
    is listed twice. A key without ``=`` has the value ``None``.

    Raises:
        OmertaError: The file cannot be read, is not UTF-8 or UTF-16, or has a
            statement that python-dotenv cannot parse. The error names the file
            and the line, never its text.
    """
    name = quote(os.fsencode(env_file.path))
    try:
        data = env_file.absolute.read_bytes()
    except OSError as error:
        raise OmertaError(f"{name} cannot be read ({error.strerror})") from None
    text = _decode(data)
    if text is None:
        raise OmertaError(f"{name} is neither UTF-8 nor UTF-16 text")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    bindings = []
    for binding in parse_stream(io.StringIO(text)):
        if binding.error:
            # python-dotenv counts the line from before the blank lines that
            # come ahead of the statement.
            original = binding.original.string
            blank = original[: len(original) - len(original.lstrip())]
            line = binding.original.line + blank.count("\n")
            raise OmertaError(f"{name}, line {line}: python-dotenv cannot parse this statement")
        if binding.key is not None:
            bindings.append((binding.key, binding.value))
    return bindings


def _decode(data: bytes) -> str | None:
    """The text of an env file's bytes, or ``None`` where they do not decode."""
    # The caller raises its own error after this returns, outside of the
    # handler: the decoding error, which holds the bytes in question, is then
    # not its context.
    try:
        if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            return data.decode("utf-16")
        return data.removeprefix(codecs.BOM_UTF8).decode("utf-8")
    except UnicodeDecodeError:
        return None


def _walk(top: Path, folder: tuple[str, ...], rest: tuple[str, ...]) -> Iterator[tuple[str, ...]]:
    """The files under ``folder`` whose paths match the remaining pattern parts."""
    head, tail = rest[0], rest[1:]
    entries = _entries(top, folder)
    if head == "**":
        if tail:
            yield from _walk(top, folder, tail)
        else:
            yield from ((*folder, name) for name, is_folder in entries if not is_folder)
        for name, is_folder in entries:
            if is_folder:
                yield from _walk(top, (*folder, name), rest)
        return
    for name, is_folder in entries:
        if not fnmatch.fnmatchcase(name, head):
            continue
        if tail and is_folder:
            yield from _walk(top, (*folder, name), tail)
        elif not tail and not is_folder:
            yield (*folder, name)


def _entries(top: Path, folder: tuple[str, ...]) -> list[tuple[str, bool]]:
    """The folders and files of a folder: each name, and whether it is a folder.

    Left out are ``.git``, a folder holding a ``.git`` of its own, a symbolic
    link to a folder, and what is neither a folder nor a file.
    """
    path = top.joinpath(*folder)
    entries = []
    try:
        with os.scandir(path) as listing:
            for entry in listing:
                if entry.name == ".git":
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if not os.path.lexists(os.path.join(entry.path, ".git")):
                        entries.append((entry.name, True))
                elif entry.is_file():
                    entries.append((entry.name, False))
    except OSError as error:
        name = quote(os.fsencode("/".join(folder) or "."))
        raise OmertaError(f"folder {name} cannot be listed ({error.strerror})") from None
    return entries
