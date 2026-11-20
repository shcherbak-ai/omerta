"""A ``.dockerignore``, read and matched as Docker's BuildKit does.

BuildKit's Dockerfile frontend reads the file on the builder, which is Linux,
under Docker Desktop on Windows too (moby/patternmatcher's
``ignorefile.ReadAll``), and compiles its patterns there (patternmatcher's
``New``). The client that sends the build context compiles them again, under
its own platform's path rules, and leaves out what they match, walking the
context folder by folder: each path is matched with the results of its folder
(``MatchesUsingParentResults``, in tonistiigi/fsutil's ``filterFS.Walk``). Each
step is repeated here, as patternmatcher 0.6.1 and fsutil take it.

patternmatcher matches a pattern as a string when it can (whole, by its start
or by its end), and otherwise as a Go regular expression that it builds from
the pattern the first time it matches a path with it. That expression is read
here as Go reads it, Go's escapes and classes too (``\\d``, ``\\b``, ``\\x41``,
``[[:alpha:]]``), which a backslash makes of a letter on Linux and macOS, and
the alternations and repetitions that ``|``, ``{`` and ``}`` make, which
patternmatcher leaves unescaped, and written for Python's ``re``. A pattern
that Docker refuses is an error, and so is one with a Unicode class (``\\pL``),
which Python's ``re`` cannot write, once it is matched: omerta never guesses
what a pattern leaves out.
"""

from __future__ import annotations

import codecs
import re
from collections.abc import Callable
from dataclasses import dataclass

from omerta.errors import OmertaError
from omerta.git import WINDOWS

# What Go's strings.TrimSpace removes: the characters of unicode.IsSpace, which,
# unlike Python's str.isspace, leaves out \x1c to \x1f.
GO_SPACES = (
    "\t\n\v\f\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009"
    "\u200a\u2028\u2029\u202f\u205f\u3000"
)

# Go's bufio.Scanner, which ReadAll reads lines with, refuses a line of 64 KiB
# or more, and ReadAll the whole file with it. BuildKit's Dockerfile parser
# reads with one too, and stops there (docker.py).
LONGEST_LINE = 64 * 1024 - 1

# The characters that patternmatcher escapes in a regular expression. It means to
# escape `|`, `{` and `}` too, but the table it keeps them in drops every
# character from 64 up, so they reach Go as they are: `a|b` is an alternation,
# and `a{2}` a repetition, as Docker builds show.
_ESCAPED = ".+()$"

# A repetition as Go reads it, without leading zeros; any other `{` is itself.
_REPEAT = re.compile(r"\{(0|[1-9][0-9]*)(?:(,)(0|[1-9][0-9]*)?)?\}")

# The largest count of a repetition that Go takes.
_MOST_REPEATS = 1000

# What makes an exception more than a path for fsutil, short of a trailing `/**`
# or `/*`; and the backslash too, but on Windows, where it separates folders.
_GLOB = "*[]?^"

_DRIVE = re.compile(r"[A-Za-z]:")

Ranges = tuple[tuple[int, int], ...]
"""Characters as ranges of code points, each from the first to the last."""

_LAST = 0x10FFFF

# The classes of Go's regular expressions, ASCII only: Perl's (`\d`, `\s`, `\w`,
# and their capitals for the rest), where `\s` leaves out `\v`, unlike Python's,
# and POSIX's (`[[:alpha:]]`, `[[:^alpha:]]` for the rest).
_PERL: dict[str, Ranges] = {
    "d": ((0x30, 0x39),),
    "s": ((0x09, 0x0A), (0x0C, 0x0D), (0x20, 0x20)),
    "w": ((0x30, 0x39), (0x41, 0x5A), (0x5F, 0x5F), (0x61, 0x7A)),
}
_POSIX: dict[str, Ranges] = {
    "alnum": ((0x30, 0x39), (0x41, 0x5A), (0x61, 0x7A)),
    "alpha": ((0x41, 0x5A), (0x61, 0x7A)),
    "ascii": ((0x00, 0x7F),),
    "blank": ((0x09, 0x09), (0x20, 0x20)),
    "cntrl": ((0x00, 0x1F), (0x7F, 0x7F)),
    "digit": ((0x30, 0x39),),
    "graph": ((0x21, 0x7E),),
    "lower": ((0x61, 0x7A),),
    "print": ((0x20, 0x7E),),
    "punct": ((0x21, 0x2F), (0x3A, 0x40), (0x5B, 0x60), (0x7B, 0x7E)),
    "space": ((0x09, 0x0D), (0x20, 0x20)),
    "upper": ((0x41, 0x5A),),
    "word": ((0x30, 0x39), (0x41, 0x5A), (0x5F, 0x5F), (0x61, 0x7A)),
    "xdigit": ((0x30, 0x39), (0x41, 0x46), (0x61, 0x66)),
}

# The one-letter Unicode classes (`\pL`), which Python's `re` has no way to write.
_UNICODE_LETTERS = "CLMNPSZ"

# The characters of Go's C escapes (`\t`); there is no `\b`, a word boundary.
_C_ESCAPES = {"a": 0x07, "f": 0x0C, "n": 0x0A, "r": 0x0D, "t": 0x09, "v": 0x0B}

# The assertions of Go's regular expressions, outside a class, for Python's `re`
# with its ASCII flag, under which `\b` is a boundary of `[0-9A-Za-z_]`, as in Go.
_ASSERTIONS = {"A": r"\A", "b": r"\b", "B": r"\B", "z": r"\Z"}

# What a path has where its name is not Unicode: os.scandir gives a byte that is
# not UTF-8 as a surrogate on Linux and macOS, and an unpaired UTF-16 surrogate
# as itself on Windows. Go's regular expressions read each as U+FFFD.
_UNDECODED = re.compile("[\ud800-\udfff]")


class _RefusedError(Exception):
    """A pattern that Docker refuses."""


class _UnsupportedError(Exception):
    """A pattern that omerta cannot match as Docker does: one with a Unicode class or a BOM."""


@dataclass(frozen=True)
class _Pattern:
    """A pattern as patternmatcher compiles it: its text, without a leading ``!``."""

    text: str
    exclusion: bool
    line: int
    matches: Callable[[str], bool]
    """Whether a path matches it.

    Raises:
        _RefusedError: Go refuses its regular expression, built at the first call.
        _UnsupportedError: Its regular expression has a Unicode class.
    """


class Ignore:
    """The patterns of an ignore file, which leave paths out of a build context.

    Args:
        data: The file's bytes; empty where there is no file.
        name: The file's path, as an error names it.

    Raises:
        OmertaError: A line is not UTF-8 or is longer than Docker reads, or a
            pattern is one that Docker refuses or has a byte order mark inside.
            The error names the file and the line, never the pattern.
    """

    def __init__(self, data: bytes, name: str) -> None:
        self._name = name
        # The client's separator, which its patterns and paths have.
        self._separator = "\\" if WINDOWS else "/"
        self._patterns: list[_Pattern] = []
        for number, text in _read(data, name):
            try:
                pattern = _compiled(text, number)
            except (_RefusedError, _UnsupportedError) as error:
                raise _failure(name, number, error) from None
            if pattern is not None:
                self._patterns.append(pattern)
        # Each exception as fsutil takes it: without a trailing `/**`, then `/*`.
        separator = self._separator
        self._exceptions = [
            pattern.text.removesuffix(f"{separator}**").removesuffix(f"{separator}*")
            for pattern in self._patterns
            if pattern.exclusion
        ]
        glob = _GLOB if WINDOWS else f"{_GLOB}\\"
        self._paths_only = not any(
            char in glob for exception in self._exceptions for char in exception
        )

    def leaves_out(self, path: str) -> bool:
        """Whether the build context leaves out a file: ``path``, from its root, with ``/``.

        Each folder on the way is matched first, with the results of the
        folder above it, as BuildKit walks the context. A folder that is left
        out is not entered where no exception could bring back a path under
        it: when every exception is a path, short of a trailing ``/**`` or
        ``/*``, and none of them starts with the folder's.

        Raises:
            OmertaError: A pattern that the path is matched with is one whose
                regular expression Docker refuses, or one that omerta does not
                match as Docker does.
        """
        parts = path.split("/")
        results: list[bool] | None = None
        for depth in range(1, len(parts)):
            folder = self._separator.join(parts[:depth])
            left_out, results = self._match(folder, results)
            if left_out and self._skips(folder):
                return True
        return self._match(self._separator.join(parts), results)[0]

    def _match(self, path: str, above: list[bool] | None) -> tuple[bool, list[bool]]:
        """Whether a path is left out, and which patterns match it, from those of its folder.

        A pattern that matched the folder matches the path. Another is tried
        only where it could change the outcome: an exclusion once the path is
        left out, any other pattern while it is not.
        """
        left_out = False
        matched = []
        for index, pattern in enumerate(self._patterns):
            match = above is not None and above[index]
            if not match and pattern.exclusion == left_out:
                try:
                    match = pattern.matches(path)
                except (_RefusedError, _UnsupportedError) as error:
                    raise _failure(self._name, pattern.line, error) from None
            matched.append(match)
            if match:
                left_out = not pattern.exclusion
        return left_out, matched

    def _skips(self, folder: str) -> bool:
        """Whether BuildKit leaves a folder that is left out unentered."""
        if not self._paths_only:
            return False
        if not self._exceptions:
            return True
        separator = self._separator
        return not any(
            f"{exception}{separator}".startswith(f"{folder}{separator}")
            for exception in self._exceptions
        )


def _failure(name: str, line: int, error: _RefusedError | _UnsupportedError) -> OmertaError:
    """The error for a pattern at a line of an ignore file, which never shows the pattern."""
    if isinstance(error, _RefusedError):
        return OmertaError(f"{name}, line {line}: Docker refuses this pattern")
    return OmertaError(
        f"{name}, line {line}: omerta cannot match this pattern as Docker does: it has a "
        "Unicode class (such as \\pL) or a byte order mark"
    )


def _read(data: bytes, name: str) -> list[tuple[int, str]]:
    """The patterns of an ignore file, each with its line, as ``ignorefile.ReadAll`` gives them.

    A line that starts with ``#`` is a comment, but not one with a space
    before it. The rest is trimmed and cleaned as a path, under Linux's rules,
    and one leading ``/`` is dropped; a ``!`` stays in front.
    """
    lines = data.split(b"\n")
    if not lines[-1]:
        lines.pop()
    found = []
    for number, raw in enumerate(lines, 1):
        if len(raw) > LONGEST_LINE:
            raise OmertaError(f"{name}, line {number}: Docker refuses a line of 64 KiB or more")
        raw = raw.removesuffix(b"\r")
        if number == 1:
            raw = raw.removeprefix(codecs.BOM_UTF8)
        line = _decode(raw)
        if line is None:
            raise OmertaError(f"{name}, line {number} is not UTF-8 text")
        if line.startswith("#"):
            continue
        pattern = line.strip(GO_SPACES)
        if not pattern:
            continue
        exception = pattern.startswith("!")
        if exception:
            pattern = pattern[1:].strip(GO_SPACES)
        if pattern:
            pattern = _clean(pattern)
            if len(pattern) > 1 and pattern.startswith("/"):
                pattern = pattern[1:]
        found.append((number, f"!{pattern}" if exception else pattern))
    return found


def _decode(raw: bytes) -> str | None:
    """The text of a line, or ``None`` where it is not UTF-8."""
    # The caller raises its error outside of the handler, so that the decoding
    # error, which holds the bytes in question, is not its context.
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _clean(path: str) -> str:
    """A path as Go's ``path.Clean`` makes it.

    Empty and ``.`` parts go, a ``..`` takes the part before it away, or
    stays at the start of a relative path, and a trailing ``/`` goes; an empty
    path is ``.``.
    """
    rooted = path.startswith("/")
    parts: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts and parts[-1] != "..":
                parts.pop()
            elif not rooted:
                parts.append(part)
            continue
        parts.append(part)
    cleaned = "/".join(parts)
    return f"/{cleaned}" if rooted else cleaned or "."


def _clean_here(path: str) -> str:
    """A path as Go's ``filepath.Clean`` makes it on this platform.

    On Windows, ``/`` and ``\\`` both separate folders, and the result has
    ``\\``; a drive (``C:``) or a network share (``\\\\server``) stays at the
    start. A first part with a ``:`` in it gets ``.\\`` in front, so that it
    is not taken for a drive, but only where cleaning changed the path (a
    ``/`` made ``\\``, a ``..`` resolved), not where it left it as it was or
    only cut it short: Go builds the result only once a byte differs. So
    ``!.env.[[:digit:]]`` stays an exception, and ``![[:digit:]]/.env`` turns
    into ``.\\![[:digit:]]\\.env``, which is none, as Docker's Windows client
    shows. Such a pattern matches no path of a build context, which never has
    a ``:`` and never starts with a separator on Windows.
    """
    if not WINDOWS:
        return _clean(path)
    given = path
    path = path.replace("\\", "/")
    if path.startswith("//"):
        return path.replace("/", "\\")
    if _DRIVE.match(path):
        cleaned = path[:2] + (_clean(path[2:]) if len(path) > 2 else ".")
        return cleaned.replace("/", "\\")
    cleaned = _clean(path).replace("/", "\\")
    if ":" in cleaned.split("\\")[0] and not given.startswith(cleaned):
        cleaned = f".\\{cleaned}"
    return cleaned


def _compiled(text: str, line: int) -> _Pattern | None:
    """A pattern as patternmatcher's ``New`` compiles it, or ``None`` where it is blank.

    On Windows, the builder compiles it first, under Linux's rules, and
    refuses what they refuse, such as a trailing ``\\``.

    Raises:
        _RefusedError: Docker refuses the pattern.
        _UnsupportedError: It has a byte order mark, which Go reads one way when it
            compares the pattern as a string and another in a regular
            expression.
    """
    text = text.strip(GO_SPACES)
    if not text:
        return None
    if WINDOWS:
        on_builder = _clean(text)
        if on_builder == "!":
            raise _RefusedError
        _check(on_builder.removeprefix("!"), escapes=True)
    text = _clean_here(text)
    exclusion = text.startswith("!")
    if exclusion:
        if text == "!":
            raise _RefusedError
        text = text[1:]
    if "\ufeff" in text:
        raise _UnsupportedError
    _check(text, escapes=not WINDOWS)
    return _Pattern(text, exclusion, line, _compile(text, "\\" if WINDOWS else "/"))


def _check(pattern: str, *, escapes: bool) -> None:
    """Check a pattern as patternmatcher does, with Go's ``filepath.Match(pattern, ".")``.

    Go checks each part of the pattern between two stars as it matches it to
    ``.``, and stops at the first that does not match: the parts after it go
    unchecked here, and are checked when the regular expression is built. A
    backslash escapes the character after it, but on Windows, where it is a
    separator (``escapes`` false).

    Raises:
        _RefusedError: A part that is checked is malformed: a ``[`` without its
            ``]``, a class that is empty or starts or ends with ``-``, or an
            escaping ``\\`` at its end.
    """
    name = "."
    while pattern:
        star, chunk, pattern = _scan_chunk(pattern, escapes)
        if star and not chunk:
            return
        rest = _match_chunk(chunk, name, escapes)
        if rest is None or (rest and not pattern):
            return
        name = rest


def _scan_chunk(pattern: str, escapes: bool) -> tuple[bool, str, str]:
    """The next part of a pattern: whether stars come before it, the part, and the rest."""
    star = False
    while pattern.startswith("*"):
        pattern = pattern[1:]
        star = True
    in_class = False
    position = 0
    while position < len(pattern):
        char = pattern[position]
        if char == "\\" and escapes and position + 1 < len(pattern):
            position += 1
        elif char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "*" and not in_class:
            return star, pattern[:position], pattern[position:]
        position += 1
    return star, pattern, ""


def _match_chunk(chunk: str, name: str, escapes: bool) -> str | None:
    """What is left of ``name`` after the part matches its start, or ``None``.

    The whole part is read, whether or not it matches.

    Raises:
        _RefusedError: The part is malformed.
    """
    failed = False
    while chunk:
        failed = failed or not name
        char = chunk[0]
        if char == "[":
            current = "\0"
            if not failed:
                current, name = name[0], name[1:]
            chunk = chunk[1:]
            negated = chunk.startswith("^")
            if negated:
                chunk = chunk[1:]
            match = False
            ranges = 0
            while not (chunk.startswith("]") and ranges):
                low, chunk = _class_end(chunk, escapes)
                high = low
                if chunk[0] == "-":
                    high, chunk = _class_end(chunk[1:], escapes)
                match = match or low <= current <= high
                ranges += 1
            chunk = chunk[1:]
            failed = failed or match == negated
        elif char == "?":
            # A separator, which `?` does not match, is never in ".".
            if not failed:
                name = name[1:]
            chunk = chunk[1:]
        else:
            if char == "\\" and escapes:
                chunk = chunk[1:]
                if not chunk:
                    raise _RefusedError
            if not failed:
                failed = chunk[0] != name[0]
                name = name[1:]
            chunk = chunk[1:]
    return None if failed else name


def _class_end(chunk: str, escapes: bool) -> tuple[str, str]:
    """A character of a class, unescaped, and the rest of the part after it.

    Raises:
        _RefusedError: The class ends here, or the character is ``-`` or ``]``.
    """
    if not chunk or chunk[0] in "-]":
        raise _RefusedError
    if chunk[0] == "\\" and escapes:
        chunk = chunk[1:]
        if not chunk:
            raise _RefusedError
    if len(chunk) == 1:
        raise _RefusedError
    return chunk[0], chunk[1:]


def _compile(text: str, separator: str) -> Callable[[str], bool]:
    """How a pattern matches a path, as patternmatcher's ``compile`` decides it.

    A pattern without ``*``, ``?``, ``[``, ``]`` or an escape matches a path
    equal to it; one that ends in ``**`` and has none of these before it, a
    path that starts with what comes before; one that starts with ``**`` and
    has none of these after it, a path that ends with what comes after, or
    equals it without the separator in front. Any other is a regular
    expression: ``*`` and ``?`` within a folder, ``**`` across folders. As Go
    does, it is built the first time it matches a path, and so are its errors.
    On Windows, where ``separator`` is ``\\``, a backslash is a separator,
    never an escape.
    """
    # The separator in a regular expression.
    written = "\\\\" if separator == "\\" else separator
    regex = "^"
    kind = "exact"
    position = 0
    while position < len(text):
        first = position == 0
        char = text[position]
        position += 1
        if char == "*":
            if text.startswith("*", position):
                position += 1
                if text.startswith(separator, position):
                    position += 1
                if position == len(text):
                    if kind == "exact":
                        kind = "prefix"
                    else:
                        regex += ".*"
                        kind = "regex"
                else:
                    regex += f"(.*{written})?"
                    kind = "regex"
                if first:
                    kind = "suffix"
            else:
                regex += f"[^{written}]*"
                kind = "regex"
        elif char == "?":
            regex += f"[^{written}]"
            kind = "regex"
        elif char in _ESCAPED:
            regex += f"\\{char}"
        elif char == "\\" and separator == "\\":
            regex += written
        elif char == "\\":
            if position < len(text):
                regex += f"\\{text[position]}"
                position += 1
                kind = "regex"
            else:
                regex += "\\"
        elif char in "[]":
            regex += char
            kind = "regex"
        else:
            regex += char
    if kind == "exact":
        return lambda path: path == text
    if kind == "prefix":
        start = text[:-2]
        return lambda path: path.startswith(start)
    if kind == "suffix":
        end = text[2:]
        return lambda path: path.endswith(end) or (end.startswith(separator) and path == end[1:])
    built: list[re.Pattern[str]] = []

    def matches(path: str) -> bool:
        """Whether the regular expression, built at the first call, matches a path."""
        if not built:
            built.append(re.compile(_python(f"{regex}$"), re.ASCII))
        return built[0].search(_UNDECODED.sub("\ufffd", path)) is not None

    return matches


def _python(source: str) -> str:
    """A regular expression of patternmatcher's, written for Python's ``re`` to read as Go does.

    Outside a class, it has anchors (``^``, and ``$`` at its end), ``.``,
    groups, alternations and repetitions, classes, and escapes, which are
    written out as Go reads them; any other character stands for itself. It is
    compiled with ``re.ASCII``.

    Raises:
        _RefusedError: Go refuses it.
        _UnsupportedError: It has a Unicode class (``\\pL``).
    """
    return _sequence(source, 0)[0]


def _sequence(source: str, position: int) -> tuple[str, int]:
    """The items of a regular expression, from a position to the end or to a ``)``.

    A repetition (``*``, ``?``, or ``{2}``, ``{2,}``, ``{2,5}``) applies to the
    item before it, which Python is given in a group of its own: Python refuses
    to repeat an assertion such as ``^``, which Go repeats. As in Go, one with
    no item before it, or right after another, is an error.

    Returns:
        The expression for Python, and the position after its ``)``.

    Raises:
        _RefusedError: Go refuses it.
        _UnsupportedError: It has a Unicode class (``\\pL``).
    """
    items: list[str] = []
    after_repeat = False
    while position < len(source):
        char = source[position]
        if char == ")":
            return "".join(items), position + 1
        repeat = _repeat(source, position)
        if repeat is not None:
            if after_repeat or not items or items[-1] == "|":
                raise _RefusedError
            quantifier, position = repeat
            items[-1] = f"(?:{items[-1]}){quantifier}"
            after_repeat = True
            continue
        after_repeat = False
        if char == "\\":
            pieces, position = _python_escape(source, position)
            items.extend(pieces)
        elif char == "[":
            text, position = _python_class(source, position + 1)
            items.append(text)
        elif char == "(":
            text, position = _sequence(source, position + 1)
            items.append(f"(?:{text})")
        else:
            items.append(r"\Z" if char == "$" else char if char in "^.|" else _literal(ord(char)))
            position += 1
    return "".join(items), position


def _repeat(source: str, position: int) -> tuple[str, int] | None:
    """A repetition at a position, for Python, and where it ends; ``None`` for none.

    Raises:
        _RefusedError: Its counts are over 1000, or the second is the smaller.
    """
    char = source[position]
    if char in "*?":
        return char, position + 1
    match = _REPEAT.match(source, position) if char == "{" else None
    if match is None:
        return None
    least, comma, most = match.groups()
    lowest = int(least)
    highest = lowest if comma is None else -1 if most is None else int(most)
    if lowest > _MOST_REPEATS or highest > _MOST_REPEATS or lowest > highest >= 0:
        raise _RefusedError
    if comma is None:
        return f"{{{lowest}}}", match.end()
    return f"{{{lowest},{'' if highest < 0 else highest}}}", match.end()


def _python_escape(source: str, position: int) -> tuple[list[str], int]:
    """An escape of a Go regular expression, outside a class, as items for Python; and its end.

    As Go reads it: an assertion (``\\A``, ``\\b``, ``\\B``, ``\\z``), literal
    characters up to ``\\E`` or the end (``\\Q``), each an item, so that a
    repetition after them repeats the last, a Unicode class, a Perl class
    (``\\d``, ``\\s``, ``\\w`` and their capitals), or one character.

    Raises:
        _RefusedError: Go refuses the escape.
        _UnsupportedError: It is a Unicode class.
    """
    letter = source[position + 1]
    if letter in _ASSERTIONS:
        return [_ASSERTIONS[letter]], position + 2
    if letter == "Q":
        end = source.find("\\E", position + 2)
        stop = len(source) if end < 0 else end
        literals = [_literal(ord(char)) for char in source[position + 2 : stop]]
        return literals, len(source) if end < 0 else end + 2
    group = _group(source, position)
    if group is not None:
        ranges, position = group
        return [_class_text(ranges, negated=False)], position
    code, position = _escape_code(source, position)
    return [_literal(code)], position


def _python_class(source: str, position: int) -> tuple[str, int]:
    """A class of a Go regular expression, from after its ``[``, for Python; and where it ends.

    As Go reads it: a ``^`` first negates it, a ``]`` first stands for itself,
    a POSIX class (``[:alpha:]``), a Unicode or a Perl class is a set of its
    own, a ``-`` between two characters makes a range, and anywhere else
    stands for itself.

    Raises:
        _RefusedError: The class has no ``]``, a range ends before it starts,
            or Go refuses a class or an escape in it.
        _UnsupportedError: It has a Unicode class.
    """
    negated = source.startswith("^", position)
    if negated:
        position += 1
    ranges: list[tuple[int, int]] = []
    first = True
    while True:
        if position == len(source):
            raise _RefusedError
        if source[position] == "]" and not first:
            break
        first = False
        end = source.find(":]", position + 2) if source.startswith("[:", position) else -1
        if end >= 0 and len(source) - position > 2:
            ranges.extend(_posix(source[position : end + 2]))
            position = end + 2
            continue
        group = _group(source, position) if source[position] == "\\" else None
        if group is not None:
            ranges.extend(group[0])
            position = group[1]
            continue
        low, position = _class_character(source, position)
        high = low
        if source.startswith("-", position) and source[position + 1 : position + 2] not in (
            "",
            "]",
        ):
            high, position = _class_character(source, position + 1)
            if high < low:
                raise _RefusedError
        ranges.append((low, high))
    return _class_text(tuple(ranges), negated=negated), position + 1


def _class_character(source: str, position: int) -> tuple[int, int]:
    """A character of a class, as its code point, and where the next one starts.

    Raises:
        _RefusedError: Go refuses its escape.
    """
    if source[position] == "\\":
        return _escape_code(source, position)
    return ord(source[position]), position + 1


def _posix(name: str) -> Ranges:
    """The characters of a POSIX class, such as ``[:alpha:]``, or of its negation ``[:^alpha:]``.

    Raises:
        _RefusedError: Go knows no class of that name.
    """
    inner = name[2:-2]
    negated = inner.startswith("^")
    ranges = _POSIX.get(inner[1:] if negated else inner)
    if ranges is None:
        raise _RefusedError
    return _complement(ranges) if negated else ranges


def _group(source: str, position: int) -> tuple[Ranges, int] | None:
    """A Perl class at a backslash, with where it ends; ``None`` for another escape.

    Raises:
        _RefusedError: It is ``\\p`` or ``\\P`` with a name that Go refuses.
        _UnsupportedError: It is a Unicode class.
    """
    letter = source[position + 1]
    if letter in "pP":
        # The name is one letter, or in braces, and may start with "^"; Go
        # refuses one that it has no class of.
        if source[position + 2] == "{":
            end = source.find("}", position)
            if end < 0 or not source[position + 3 : end].removeprefix("^"):
                raise _RefusedError
        elif source[position + 2] not in _UNICODE_LETTERS:
            raise _RefusedError
        raise _UnsupportedError
    ranges = _PERL.get(letter.lower())
    if ranges is None:
        return None
    return (ranges if letter.islower() else _complement(ranges)), position + 2


def _escape_code(source: str, position: int) -> tuple[int, int]:
    """The character of a one-character escape at a backslash, and where it ends.

    As Go's ``parseEscape`` reads it: a punctuation character stands for
    itself; up to three octal digits, the first ``0`` unless a second follows;
    ``\\x`` and two hexadecimal digits, or one or more in braces (``\\x{41}``);
    or a C escape (``\\t``).

    Raises:
        _RefusedError: Go refuses the escape: a non-ASCII character, a letter
            or digit that makes none of these, or a lone ``\\1`` to ``\\7``.
    """
    char = source[position + 1]
    rest = position + 2
    if char.isascii() and not char.isalnum():
        return ord(char), rest
    if char in "01234567":
        if char != "0" and not _octal(source, rest):
            raise _RefusedError
        code = int(char)
        for _ in range(2):
            if not _octal(source, rest):
                break
            code = code * 8 + int(source[rest])
            rest += 1
        return code, rest
    if char == "x":
        braced = source.startswith("{", rest)
        end = source.find("}", rest) if braced else rest + 2
        digits = source[rest + 1 : end] if braced else source[rest:end]
        if end < 0 or len(digits) < (1 if braced else 2):
            raise _RefusedError
        if any(digit not in "0123456789abcdefABCDEF" for digit in digits):
            raise _RefusedError
        code = int(digits, 16)
        if code > _LAST:
            raise _RefusedError
        return code, end + 1 if braced else end
    if char in _C_ESCAPES:
        return _C_ESCAPES[char], rest
    raise _RefusedError


def _octal(source: str, position: int) -> bool:
    """Whether an octal digit is at a position."""
    return position < len(source) and source[position] in "01234567"


def _complement(ranges: Ranges) -> Ranges:
    """Every character that is in none of the ranges, which are ASCII ones."""
    out = []
    start = 0
    for low, high in sorted(ranges):
        if low > start:
            out.append((start, low - 1))
        start = max(start, high + 1)
    out.append((start, _LAST))
    return tuple(out)


def _class_text(ranges: Ranges, *, negated: bool) -> str:
    """A class of Python's ``re`` that holds the ranges, or all but them."""
    body = "".join(
        _literal(low) if low == high else f"{_literal(low)}-{_literal(high)}"
        for low, high in ranges
    )
    return f"[{'^' if negated else ''}{body}]"


def _literal(code: int) -> str:
    """A character in a Python regular expression, by its code point, in or outside a class."""
    return f"\\U{code:08x}"
