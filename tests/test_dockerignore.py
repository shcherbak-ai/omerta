"""``.dockerignore``: read and matched as Docker's BuildKit reads and matches it.

The cases of moby/patternmatcher's own tests come first, then BuildKit's walk of
a build context, the reading of the file, Windows' path rules, and patterns
that Docker refuses or that omerta does not match.
"""

from __future__ import annotations

import posixpath

import pytest
from hypothesis import given
from hypothesis import strategies as st

from omerta import dockerignore
from omerta.dockerignore import Ignore, _clean, _compile
from omerta.errors import OmertaError


@pytest.fixture(autouse=True)
def _linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """Match under the path rules of Linux and macOS, whatever this platform; a test can switch."""
    monkeypatch.setattr(dockerignore, "WINDOWS", False)


def _left_out(lines: list[str], path: str) -> bool:
    return Ignore("\n".join(lines).encode("utf-8"), ".dockerignore").leaves_out(path)


def _error(data: bytes) -> str:
    with pytest.raises(OmertaError) as caught:
        Ignore(data, ".dockerignore")
    return str(caught.value)


# moby/patternmatcher's TestMatches, but for paths with a trailing slash, which
# a file of a build context never has.
@pytest.mark.parametrize(
    ("pattern", "path", "left_out"),
    [
        ("**", "file", True),
        ("**/", "file", True),
        ("**", "dir/file", True),
        ("**/", "dir/file", True),
        ("**/**", "dir/file", True),
        ("dir/**", "dir/file", True),
        ("dir/**", "dir/dir2/file", True),
        ("**/dir", "dir", True),
        ("**/dir", "dir/file", True),
        ("**/dir2/*", "dir/dir2/file", True),
        ("**/dir2/**", "dir/dir2/dir3/file", True),
        ("**file", "file", True),
        ("**file", "dir/file", True),
        ("**/file", "dir/file", True),
        ("**file", "dir/dir/file", True),
        ("**/file", "dir/dir/file", True),
        ("**/file*", "dir/dir/file", True),
        ("**/file*", "dir/dir/file.txt", True),
        ("**/file*txt", "dir/dir/file.txt", True),
        ("**/file*.txt", "dir/dir/file.txt", True),
        ("**/file*.txt*", "dir/dir/file.txt", True),
        ("**/**/*.txt", "dir/dir/file.txt", True),
        ("**/**/*.txt2", "dir/dir/file.txt", False),
        ("**/*.txt", "file.txt", True),
        ("**/**/*.txt", "file.txt", True),
        ("a**/*.txt", "a/file.txt", True),
        ("a**/*.txt", "a/dir/file.txt", True),
        ("a**/*.txt", "a/dir/dir/file.txt", True),
        ("a/*.txt", "a/dir/file.txt", False),
        ("a/*.txt", "a/file.txt", True),
        ("a/*.txt**", "a/file.txt", True),
        ("a[b-d]e", "ae", False),
        ("a[b-d]e", "ace", True),
        ("a[b-d]e", "aae", False),
        ("a[^b-d]e", "aze", True),
        (".*", ".foo", True),
        (".*", "foo", False),
        ("abc.def", "abcdef", False),
        ("abc.def", "abc.def", True),
        ("abc.def", "abcZdef", False),
        ("abc?def", "abcZdef", True),
        ("abc?def", "abcdef", False),
        ("a\\\\", "a\\", True),
        ("a\\*b", "a*b", True),
        ("**/foo/bar", "foo/bar", True),
        ("**/foo/bar", "dir/foo/bar", True),
        ("**/foo/bar", "dir/dir2/foo/bar", True),
        ("abc/**", "abc", False),
        ("abc/**", "abc/def", True),
        ("abc/**", "abc/def/ghi", True),
        ("**/.foo", ".foo", True),
        ("**/.foo", "bar.foo", False),
        ("a(b)c/def", "a(b)c/def", True),
        ("a(b)c/def", "a(b)c/xyz", False),
        ("a.|)$(}+{bc", "a.|)$(}+{bc", True),
        ("dist/*.whl", "dist/proxy.py-2.4.0rc3.dev36+g08acad9-py3-none-any.whl", True),
    ],
)
def test_a_pattern_matches_as_patternmatcher_matches_it(pattern: str, path: str, left_out: bool):
    assert _left_out([pattern], path) is left_out


@pytest.mark.parametrize(
    ("patterns", "path", "left_out"),
    [
        (["**", "!util/docker/web"], "util/docker/web/foo", False),
        (["**", "!util/docker/web", "util/docker/web/foo"], "util/docker/web/foo", True),
        (["**", "!dist/*.whl"], "dist/proxy.py-2.4.0rc3.dev36+g08acad9-py3-none-any.whl", False),
    ],
)
def test_the_last_pattern_that_matches_decides(patterns: list[str], path: str, left_out: bool):
    assert _left_out(patterns, path) is left_out


@pytest.mark.parametrize(
    ("patterns", "path", "left_out"),
    [
        # Docker's documented example: the README files but one come back.
        (["*.md", "!README*.md", "README-secret.md"], "README.md", False),
        (["*.md", "!README*.md", "README-secret.md"], "README-secret.md", True),
        (["*.md", "!README*.md", "README-secret.md"], "notes.md", True),
        # `*` and `?` stay within a folder, and a pattern is from the root.
        (["*/.env"], "api/.env", True),
        (["*/.env"], "api/v1/.env", False),
        ([".env"], "api/.env", False),
    ],
)
def test_docker_s_documented_examples_hold(patterns: list[str], path: str, left_out: bool):
    assert _left_out(patterns, path) is left_out


def test_a_pattern_starting_with_two_stars_and_no_wildcard_after_matches_any_end():
    # `**.env` is matched by its end, not as a regular expression, which would
    # need a folder before `.env`.
    assert _left_out(["**.env"], "x.env")
    assert _left_out(["**.env"], "dir/a.env")
    assert not _left_out(["**.env"], "x.envy")


def test_a_pattern_ending_in_two_stars_with_no_wildcard_before_matches_any_start():
    assert _left_out(["dir**"], "directory/a")
    assert not _left_out(["dir**"], "di")


def test_a_caret_is_an_anchor_in_a_regular_expression_but_a_character_otherwise():
    assert _left_out(["a^b"], "a^b")
    assert not _left_out(["a*^b"], "ax^b")


def test_a_trailing_backslash_in_a_part_go_leaves_unchecked_escapes_the_end():
    # Go checks only the part before the first star that does not match "."; the
    # regular expression then ends in `\$`, a literal `$` with no anchor after it.
    assert _left_out(["a*b\\"], "axb$")
    assert _left_out(["a*b\\"], "axb$tail")
    assert not _left_out(["a*b\\"], "axb")


@pytest.mark.parametrize(
    ("pattern", "path", "left_out"),
    [
        ("[\\]a]", "]", True),
        ("[\\-x]", "-", True),
        ("[x\\-]", "x", True),
        (".e[n]v", ".env", True),
        ("?env", "xenv", True),
        ("\\.env", ".env", True),
        # Go checks only the first part: `[]a]` after a star is read as Go reads
        # a regular expression, where a `]` first in a class stands for itself,
        # and so does a `-` that cannot make a range.
        ("x*[]a]", "xa", True),
        ("x*[]a]", "x]", True),
        ("x*[]a]", "xb", False),
        ("x*[--0]", "x.", True),
        ("x*[--0]", "x1", False),
        ("x*[a-]", "x-", True),
        ("x*[a-]", "xb", False),
        # A star inside a class becomes part of the regular expression there.
        ("[*]", "]", True),
        ("[*]", "[^]", True),
        ("[*]", "*", False),
        # `[:` without a `:]` after it is a `[`.
        ("a*[[:]", "ab[", True),
        ("a*[[:]", "ab:", True),
    ],
)
def test_a_class_is_read_as_go_reads_it(pattern: str, path: str, left_out: bool):
    assert _left_out([pattern], path) is left_out


def test_a_byte_that_is_not_utf8_is_one_character_to_a_regular_expression():
    # As os.scandir gives a name that is not UTF-8 on Linux.
    assert _left_out(["caf?.env"], "caf\udce9.env")
    assert not _left_out(["caf??.env"], "caf\udce9.env")
    assert _left_out(["caf**"], "caf\udce9.env")
    assert not _left_out(["caf.env"], "caf\udce9.env")


# BuildKit's walk: each folder is matched before the paths in it, with its results.


def test_a_folder_left_out_leaves_out_what_it_holds():
    assert _left_out(["dir"], "dir/sub/.env")
    assert _left_out(["dir", "!other"], "dir/.env")


def test_an_exception_brings_back_a_path_in_a_folder_left_out():
    assert not _left_out(["dir", "!dir/.env"], "dir/.env")
    assert not _left_out(["dir", "!**/.env"], "dir/.env")
    assert _left_out(["dir", "!**/.env"], "dir/.envy")


def test_a_pattern_is_tried_on_a_path_with_the_results_of_its_folder():
    # patternmatcher's MatchesOrParentMatches would bring `a/x` back, as `!a`
    # matches its folder; BuildKit, which matches `a` first, did not try `!a`
    # there, where nothing was left out yet, and so does not bring it back.
    assert _left_out(["a/x", "!a"], "a/x")
    assert not _left_out(["a/x", "!a"], "a/y")


# Reading the file, as ignorefile.ReadAll reads it.


def test_an_empty_file_leaves_nothing_out():
    assert not Ignore(b"", ".dockerignore").leaves_out(".env")


def test_a_comment_starts_the_line_and_a_hash_after_a_space_is_a_pattern():
    assert not _left_out(["#.env"], "#.env")
    assert _left_out([" #.env"], "#.env")


def test_lines_are_trimmed_of_go_s_spaces_only():
    assert _left_out(["\u00a0.env\t "], ".env")
    # Python's strip would take away \x1c, which Go's TrimSpace keeps.
    assert not _left_out(["\x1c.env"], ".env")
    assert _left_out(["!  .env", "*"], ".env")
    assert not _left_out(["*", "!  .env"], ".env")


def test_a_pattern_is_cleaned_as_a_path_and_a_leading_slash_dropped():
    assert _left_out(["/.env"], ".env")
    assert _left_out(["//dir//./.env/"], "dir/.env")
    assert _left_out(["dir/../.env"], ".env")
    assert _left_out(["*", "!/.env"], "x")
    assert not _left_out(["*", "!/.env"], ".env")
    assert not _left_out(["../.env"], ".env")
    assert not _left_out(["/"], ".env")


def test_a_pattern_that_cleans_to_blank_is_skipped():
    # Clean keeps the " " of the last part, which patternmatcher's New trims away.
    assert not _left_out(["a/../ /"], " ")


def test_a_byte_order_mark_and_line_ends_are_read_as_go_reads_them():
    ignore = Ignore(b"\xef\xbb\xbf.env\r\n\r\n \t\n.env.local\r\n", ".dockerignore")
    assert ignore.leaves_out(".env")
    assert ignore.leaves_out(".env.local")
    assert "line 2: omerta cannot match" in _error(b".env\n\xef\xbb\xbf.env.local\n")


def test_a_line_of_64_kib_is_an_error_as_in_docker():
    assert Ignore(b"x" * (64 * 1024 - 1) + b"\n", ".dockerignore")
    assert _error(b".env\n" + b"x" * (64 * 1024)) == (
        ".dockerignore, line 2: Docker refuses a line of 64 KiB or more"
    )


def test_a_line_that_is_not_utf8_is_an_error_without_its_bytes():
    assert _error(b".env\nsecret-\xff\n") == ".dockerignore, line 2 is not UTF-8 text"


# Windows: the client matches with backslashes as separators, after the builder
# (Linux) has read the file.


def test_on_windows_a_backslash_separates_folders(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(dockerignore, "WINDOWS", True)
    assert _left_out(["dir\\.env"], "dir/.env")
    assert _left_out(["dir\\sub\\..\\.env"], "dir/.env")
    assert not _left_out(["a\\*b"], "a*b")


def test_a_class_matches_the_separator_of_the_platform(monkeypatch: pytest.MonkeyPatch):
    # Found against Docker: `[.-0]` holds `/` (0x2F), which separates folders
    # elsewhere, but not `\` (0x5C), which does on Windows; `[A-z]` holds `\`.
    assert _left_out(["*[.-0]env"], "sub/env")
    assert not _left_out(["*[A-z]env"], "sub/env")
    assert _left_out(["a[/]b"], "a/b")
    monkeypatch.setattr(dockerignore, "WINDOWS", True)
    assert not _left_out(["*[.-0]env"], "sub/env")
    assert _left_out(["*[A-z]env"], "sub/env")
    # Go's Clean makes the `/` in the class a `\` there.
    assert _left_out(["a[/]b"], "a/b")


def test_on_windows_a_rooted_pattern_or_a_drive_matches_nothing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(dockerignore, "WINDOWS", True)
    # The builder drops a leading "/", but not a leading "\".
    assert _left_out(["/.env"], ".env")
    assert not _left_out(["\\.env"], ".env")
    assert not _left_out(["C:\\.env"], ".env")
    assert not _left_out(["C:"], "C:")
    assert not _left_out(["C:\\..\\.env"], ".env")
    # Go puts `./` before a first part with a colon, so that it is not a drive.
    assert not _left_out(["ab:*"], "ab:x")


def test_on_windows_a_part_the_builder_cleaned_away_stays_away(monkeypatch: pytest.MonkeyPatch):
    # The builder cleans `a\b/..` under Linux's rules, to ".", before Windows'
    # rules could read it as `a`.
    monkeypatch.setattr(dockerignore, "WINDOWS", True)
    assert not _left_out(["a\\b/.."], "a/x")


def test_on_windows_a_network_share_matches_nothing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(dockerignore, "WINDOWS", True)
    assert not _left_out(["\\\\server\\share"], "server/share")
    assert not _left_out(["\\\\**"], "x")


@pytest.mark.parametrize("pattern", ["\\", "*\\", ".env\\", "!", "!/"])
def test_on_windows_the_builder_refuses_what_linux_s_rules_refuse(
    monkeypatch: pytest.MonkeyPatch, pattern: str
):
    # The builder compiles the patterns under Linux's rules, where a trailing
    # backslash escapes nothing, before the client reads it as a separator.
    monkeypatch.setattr(dockerignore, "WINDOWS", True)
    assert _error(pattern.encode()) == ".dockerignore, line 1: Docker refuses this pattern"


def test_elsewhere_a_drive_and_a_colon_are_characters():
    assert _left_out(["ab:*"], "ab:x")
    assert _left_out(["C:\\.env"], "C:.env")


# Patterns that Docker refuses, and those that omerta does not match.


@pytest.mark.parametrize(
    "pattern",
    [
        # patternmatcher's TestMatch, for the patterns that filepath.Match refuses.
        "[]a]",
        "[-]",
        "[x-]",
        "[-x]",
        "\\",
        "[a-b-c]",
        "[",
        "[^",
        "[^bc",
        "a[",
        "[\\",
        "*\\",
        # New refuses a lone exclamation mark, also as a `!/` cleaned.
        "!",
        "!/",
    ],
)
def test_a_pattern_docker_refuses_is_an_error(pattern: str):
    message = _error(f".env\n{pattern}\n".encode())
    assert message == ".dockerignore, line 2: Docker refuses this pattern"


def _error_on_match(lines: list[str], path: str) -> str:
    ignore = Ignore("\n".join(lines).encode(), ".dockerignore")
    with pytest.raises(OmertaError) as caught:
        ignore.leaves_out(path)
    return str(caught.value)


@pytest.mark.parametrize(
    "pattern",
    # Go's regular expressions refuse a range that ends before it starts
    # (moby/moby#52203), a class without its end, and a non-ASCII escape.
    ["[Local-Only]/", "[z-a]", "a*[", "a*[b", "a*\\é"],
)
def test_a_regular_expression_go_refuses_is_an_error_once_a_path_is_matched_with_it(
    pattern: str,
):
    # `.env` is left out by the first line: the second is not tried on it.
    assert _left_out([".env", pattern], ".env")
    message = _error_on_match([".env", pattern], "x")
    assert message == ".dockerignore, line 2: Docker refuses this pattern"


def test_a_regular_expression_that_is_never_built_is_no_error():
    # As in Docker, which builds a pattern's expression when it first tries it.
    assert _left_out(["**", "[z-a]"], "x")
    assert not _left_out(["!a*["], "x")


@pytest.mark.parametrize(
    ("pattern", "path", "left_out"),
    [
        # Perl classes, ASCII only as in Go, where `\s` has no `\v`; capitals
        # for the rest, which holds the separator too.
        ("\\d.env", "1.env", True),
        ("\\d.env", "a.env", False),
        ("\\d.env", "\N{ARABIC-INDIC DIGIT ONE}.env", False),
        ("x\\sy", "x\ty", True),
        ("x\\sy", "x\vy", False),
        ("x\\wy", "x_y", True),
        ("x\\wy", "x-y", False),
        ("x\\Sy", "xay", True),
        ("x\\Wy", "x y", True),
        ("x\\Dy", "x/y", True),
        # A pattern written with Windows' separators means something else on
        # Linux: `\D` is any character but a digit, `\s` a space.
        ("bin\\Debug", "bin/ebug", True),
        ("bin\\Debug", "bin/Debug", False),
        ("dir\\sub", "dir ub", True),
        ("dir\\sub", "dir/sub", False),
        # Assertions, a word boundary as Go draws it.
        ("*\\bfoo", "a.foo", True),
        ("*\\bfoo", "afoo", False),
        ("*\\Bfoo", "afoo", True),
        ("\\Afoo", "foo", True),
        ("foo\\z", "foo", True),
        # C, octal and hexadecimal escapes.
        ("a\\tb", "a\tb", True),
        ("a\\nb", "a\nb", True),
        ("\\101.env", "A.env", True),
        ("x\\0101", "x\b1", True),
        ("x\\07y", "x\ay", True),
        ("\\1234", "S4", True),
        ("\\x41.env", "A.env", True),
        ("\\x4a", "J", True),
        # `\Q` makes literal what comes up to `\E`, the escapes that
        # patternmatcher writes in too; without `\E`, up to the end, `$` too.
        ("\\Qab\\E.env", "ab.env", True),
        ("\\Qa.b\\E", "a\\.b", True),
        ("\\Qa.b\\E", "a.b", False),
        ("\\Qa*", "a[^/]*$", True),
        ("\\Qa*", "ab", False),
        # Classes hold Perl's, POSIX's and escaped characters.
        ("[\\d]x", "1x", True),
        ("[\\D]x", "ax", True),
        ("[\\D]x", "1x", False),
        ("[\\s_]x", " x", True),
        ("[\\d-z]", "-", True),
        ("[\\d-z]", "y", False),
        ("[[:digit:]]x", "1x", True),
        ("[[:^alpha:]]x", "1x", True),
        ("[[:^alpha:]]x", "ax", False),
        ("[[:^cntrl:]]x", "ax", True),
        ("[[:^cntrl:]]x", "\tx", False),
        ("[\\x41-\\x43]", "B", True),
        ("[\\x41-\\x43]", "D", False),
    ],
)
def test_go_s_escapes_and_classes_mean_what_they_mean_to_go(
    pattern: str, path: str, left_out: bool
):
    assert _left_out([pattern], path) is left_out


@pytest.mark.parametrize(
    "pattern",
    [
        "\\pu",
        "\\p{}",
        "\\p{^}",
        "\\p{L",
        "\\E",
        "\\1",
        "\\x",
        "\\xZZ",
        "\\x{}",
        "\\x{4G}",
        "\\x{41",
        "\\x{110000}",
        "\\C",
        "\\y",
        "[\\b]",
        "[[:bogus:]]",
        "[[:^^alpha:]]",
    ],
)
def test_an_escape_or_class_go_refuses_is_an_error_once_tried(pattern: str):
    assert _error_on_match([pattern], "x") == (".dockerignore, line 1: Docker refuses this pattern")


@pytest.mark.parametrize(
    ("pattern", "path", "left_out"),
    [
        # patternmatcher means to escape `|`, `{` and `}`, but does not: Go
        # reads an alternation, and a repetition of the item before it.
        ("x*a{2}", "xaa", True),
        ("x*a{2}", "xa{2}", False),
        ("x*a{2}", "xa", False),
        ("x*a{1,2}", "xa", True),
        ("x*a{2,}", "xaaa", True),
        ("x*a{0}", "x", True),
        ("*.md|*.txt", "b.txt", True),
        ("*.md|*.txt", "c.js", False),
        # The alternatives split the anchors: the first matches a start, and an
        # empty last one matches every path.
        ("*.md|x", "notes.md.bak", True),
        ("x*a|", "anything", True),
        # Go repeats an assertion, as the `^` that starts every expression.
        ("{2}*", "x", True),
        # Not a repetition to Go, so the characters themselves.
        ("x*a{01}", "xa{01}", True),
        ("x*a{,2}", "xa{,2}", True),
        ("x*a{x}", "xa{x}", True),
        ("x*a}", "xa}", True),
        # In braces, a hexadecimal escape takes any number of digits.
        ("\\x{41}.env", "A.env", True),
        ("\\x{0041}", "A", True),
        # `\Q\E` ends no repetition, so the next repeats the one before.
        ("x*a{2}\\Q\\E{2}", "xaaaa", True),
    ],
)
def test_bars_and_braces_reach_go_as_they_are(pattern: str, path: str, left_out: bool):
    assert _left_out([pattern], path) is left_out


@pytest.mark.parametrize(
    "pattern", ["x*a{2}{3}", "*{2}", "x*|{2}", "x*a{1001}", "x*a{3,2}", "x*a{100000000}"]
)
def test_a_repetition_go_refuses_is_an_error_once_tried(pattern: str):
    assert _error_on_match([pattern], "x") == ".dockerignore, line 1: Docker refuses this pattern"


@pytest.mark.parametrize("pattern", ["\\pL", "\\PN", "\\p{Greek}", "\\p{^Greek}", "[\\pN]x"])
def test_a_unicode_class_is_an_error_once_tried(pattern: str):
    assert _error_on_match([pattern], "x") == (
        ".dockerignore, line 1: omerta cannot match this pattern as Docker does: it has a "
        "Unicode class (such as \\pL) or a byte order mark"
    )


# Properties.

_CHARACTERS = "ab.-_é☺"


def _glob_match(pattern: str, name: str) -> bool:
    """A glob, outside Windows, as patternmatcher's regular expression reads it: the oracle below.

    Read as Go's filepath.Match reads it, chunk by chunk, but trying every way
    to match, as a regular expression does, where filepath.Match takes each
    chunk's first match: the two differ where a class matches `/`, as
    `aa*[^a]*` matches `aa?/` only with `/` for its class.
    """
    if not pattern:
        return not name
    star = pattern.startswith("*")
    pattern = pattern.lstrip("*")
    end = len(pattern)
    in_class, position = False, 0
    while position < len(pattern):
        if pattern[position] == "\\":
            position += 1
        elif pattern[position] == "[":
            in_class = True
        elif pattern[position] == "]":
            in_class = False
        elif pattern[position] == "*" and not in_class:
            end = position
            break
        position += 1
    chunk, pattern = pattern[:end], pattern[end:]
    if star and not chunk:
        return "/" not in name
    for skip in range(len(name) + 1) if star else (0,):
        if skip and name[skip - 1] == "/":
            break
        rest = _go_chunk(chunk, name[skip:])
        if rest is not None and _glob_match(pattern, rest):
            return True
    return False


def _go_chunk(chunk: str, name: str) -> str | None:
    """What is left of ``name`` after ``chunk`` matches its start, or ``None``."""
    while chunk:
        if not name:
            return None
        char = chunk[0]
        if char == "[":
            chunk, negated = chunk[1:], chunk[1:2] == "^"
            chunk = chunk[1:] if negated else chunk
            match, first = False, True
            while first or chunk[0] != "]":
                first = False
                low = chunk[1] if chunk[0] == "\\" else chunk[0]
                chunk = chunk[2:] if chunk[0] == "\\" else chunk[1:]
                high = low
                if chunk[0] == "-":
                    high = chunk[2] if chunk[1] == "\\" else chunk[1]
                    chunk = chunk[3:] if chunk[1] == "\\" else chunk[2:]
                match = match or low <= name[0] <= high
            if match == negated:
                return None
            chunk, name = chunk[1:], name[1:]
        elif char == "?":
            if name[0] == "/":
                return None
            chunk, name = chunk[1:], name[1:]
        else:
            if char == "\\":
                chunk = chunk[1:]
            if chunk[0] != name[0]:
                return None
            chunk, name = chunk[1:], name[1:]
    return name


_class = st.builds(
    lambda negated, items: "[" + ("^" if negated else "") + "".join(items) + "]",
    st.booleans(),
    st.lists(
        st.one_of(
            st.sampled_from(_CHARACTERS.replace("-", "")),
            st.sampled_from(["a-b", "\\]", "\\-", "\\\\", "/", "-e"]).map(
                lambda item: item if item != "-e" else "a\\-e"
            ),
        ),
        min_size=1,
        max_size=3,
    ),
)
_TEXT = _CHARACTERS + "/*?[]\\^"


@st.composite
def _glob_and_name(draw: st.DrawFn) -> tuple[str, str]:
    """A pattern without ``**``, and a name made of a piece for each of its parts.

    Each piece is drawn to match its part often but not always (a `?` drawn as
    a `/`), so that the matches and the near misses are both common; and the
    name is sometimes any text instead.
    """
    pattern, name, star = "", "", False
    for _ in range(draw(st.integers(1, 6))):
        kind = draw(st.sampled_from(["char", "/", "?", "*", "escape", "class"]))
        if kind == "*" and not star:
            pattern += "*"
            name += draw(st.text(st.sampled_from(_TEXT), max_size=3))
        elif kind == "?":
            pattern += "?"
            name += draw(st.sampled_from(_TEXT))
        elif kind == "escape":
            escaped = draw(st.sampled_from("*?[.\\"))
            pattern += f"\\{escaped}"
            name += draw(st.sampled_from([escaped, escaped, "x"]))
        elif kind == "class":
            pattern += draw(_class)
            name += draw(st.sampled_from(_TEXT + "-"))
        else:
            piece = "/" if kind == "/" else draw(st.sampled_from(_CHARACTERS))
            pattern += piece
            name += draw(st.sampled_from([piece, piece, "/"]))
        # Whether this step wrote a wildcard: after an escaped backslash (`\\*`),
        # the text alone would not tell.
        star = kind == "*" and not star
    if draw(st.integers(0, 4)) == 0:
        name = draw(st.text(st.sampled_from(_TEXT), max_size=8))
    return pattern, name


@given(_glob_and_name())
def test_a_pattern_without_double_stars_matches_what_its_glob_matches(case: tuple[str, str]):
    pattern, name = case
    assert _compile(pattern, "/")(name) == _glob_match(pattern, name)


def test_a_class_that_matches_a_slash_is_tried_there_too():
    # Found by the property above: Go's filepath.Match would take `?` for the
    # class and fail on the `/` left; the regular expression tries `/`.
    assert _compile("aa*[^a]*", "/")("aa?/")
    assert _glob_match("aa*[^a]*", "aa?/")


@given(st.text(st.sampled_from("ab./"), max_size=12))
def test_a_path_is_cleaned_as_posixpath_normalises_it(path: str):
    # normpath keeps a leading `//`, which Go's Clean makes `/`.
    if path.startswith("//") and not path.startswith("///"):
        path = path[1:]
    assert _clean(path) == posixpath.normpath(path)
