"""The env files of the working tree, and the variables they set."""

from __future__ import annotations

import codecs
import errno
import fnmatch
import io
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from dotenv.parser import parse_stream

from omerta.errors import OmertaError
from omerta.git import WINDOWS
from omerta.output import quote
from omerta.patterns import Pattern, is_swap_file, is_template

# The reparse tag of a Windows junction (IO_REPARSE_TAG_MOUNT_POINT), which the
# `stat` module names on Windows only.
_JUNCTION = 0xA0000003

# The errors of following a link that lead nowhere: a loop, or a part that is a
# file; on Windows, a name it refuses (ERROR_INVALID_NAME) and too many links
# (ERROR_CANT_RESOLVE_FILENAME).
_NOWHERE = (errno.ELOOP, errno.ENOTDIR)
_NOWHERE_ON_WINDOWS = (123, 1921)


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
    nested repository), nor a symbolic link to a folder or a Windows junction;
    a symbolic link to a file is read through. A folder that matches a pattern
    is no env file, and neither is a template or an editor's swap file.

    Args:
        top: The root of the repository.
        patterns: The env file patterns, in order.
        tracked: The paths of the index entries, as git gives them.

    Returns:
        The env files in the order of the patterns, then of their paths.

    Raises:
        OmertaError: A folder on the way cannot be listed.
    """
    return [
        EnvFile(path, top.joinpath(*parts))
        for path, parts in _matching(top, patterns)
        if not is_swap_file(parts[-1]) and os.fsencode(path) not in tracked
    ]


def every(top: Path, patterns: tuple[Pattern, ...]) -> list[str]:
    """The paths of every env file of the working tree, tracked or not, as Docker sees them.

    They are found as :func:`find` finds them, but for a swap file, which holds
    the text of the file it is beside, and a folder that holds a ``.git`` of
    its own (a submodule, a nested repository), which is entered: Docker
    copies it as any other. A template is left out.

    Returns:
        The paths relative to the root, with ``/``, in the order of the
        patterns, then of the paths.

    Raises:
        OmertaError: A folder on the way cannot be listed.
    """
    return [path for path, _ in _matching(top, patterns, nested=True)]


def files(top: Path, folder: str) -> list[str]:
    """The names of the files of a folder, given from the root with ``/``, empty for the root.

    A symbolic link to a file is a file; a folder, and what is neither, is not.

    Raises:
        OmertaError: The folder cannot be listed.
    """
    parts = tuple(folder.split("/")) if folder else ()
    return [name for name, is_folder in _entries(top, parts) if not is_folder]


def _matching(
    top: Path, patterns: tuple[Pattern, ...], nested: bool = False
) -> Iterator[tuple[str, tuple[str, ...]]]:
    """Each file of the working tree that matches a pattern but is no template, once.

    ``nested`` enters the folders that hold a ``.git`` of their own.
    """
    seen: set[str] = set()
    for pattern in patterns:
        for parts in sorted(set(_walk(top, (), pattern.parts, nested))):
            path = "/".join(parts)
            if path not in seen and not is_template(parts[-1]):
                seen.add(path)
                yield path, parts


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


def _walk(
    top: Path, folder: tuple[str, ...], rest: tuple[str, ...], nested: bool
) -> Iterator[tuple[str, ...]]:
    """The files under ``folder`` whose paths match the remaining pattern parts."""
    head, tail = rest[0], rest[1:]
    entries = _entries(top, folder, nested)
    if head == "**":
        if tail:
            yield from _walk(top, folder, tail, nested)
        else:
            yield from ((*folder, name) for name, is_folder in entries if not is_folder)
        for name, is_folder in entries:
            if is_folder:
                yield from _walk(top, (*folder, name), rest, nested)
        return
    for name, is_folder in entries:
        if not fnmatch.fnmatchcase(name, head):
            continue
        if tail and is_folder:
            yield from _walk(top, (*folder, name), tail, nested)
        elif not tail and not is_folder:
            yield (*folder, name)


def _entries(top: Path, folder: tuple[str, ...], nested: bool = False) -> list[tuple[str, bool]]:
    """The folders and files of a folder: each name, and whether it is a folder.

    Left out are ``.git``, a folder holding a ``.git`` of its own unless
    ``nested``, a symbolic link to a folder, a Windows junction, and what is
    neither a folder nor a file.
    """
    path = top.joinpath(*folder)
    entries = []
    try:
        with os.scandir(path) as listing:
            for entry in listing:
                if entry.name == ".git":
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if not _is_junction(entry) and (
                        nested or not os.path.lexists(os.path.join(entry.path, ".git"))
                    ):
                        entries.append((entry.name, True))
                elif _is_file(entry):
                    entries.append((entry.name, False))
    except OSError as error:
        name = quote(os.fsencode("/".join(folder) or "."))
        raise OmertaError(f"folder {name} cannot be listed ({error.strerror})") from None
    return entries


def _is_file(entry: os.DirEntry[str]) -> bool:
    """Whether an entry is a file, or a symbolic link that leads to one.

    :meth:`os.DirEntry.is_file` answers ``False`` for a link to nothing, but
    raises for one that leads nowhere otherwise, which is no file either: a
    loop, a target through a file (``ENOTDIR``), or one that Windows cannot
    follow, its target written with ``/`` (WinError 123), or through too many
    links (1921). Any other error, such as a target that cannot be reached
    (``EACCES``), makes the entry a file, so that reading it names it and the
    error, rather than leave its values out of the search.
    """
    try:
        return entry.is_file()
    except OSError as error:
        nowhere = error.errno in _NOWHERE or getattr(error, "winerror", None) in _NOWHERE_ON_WINDOWS
        return not nowhere


def _is_junction(entry: os.DirEntry[str]) -> bool:
    """Whether a folder is a Windows junction, which leads to another folder as a link does.

    Python takes a junction for a folder, not for a symbolic link: one that
    leads to a folder above it would be walked without end.
    """
    # What Windows listed the folder with: read from the entry, without a call.
    return WINDOWS and getattr(entry.stat(follow_symlinks=False), "st_reparse_tag", 0) == _JUNCTION
