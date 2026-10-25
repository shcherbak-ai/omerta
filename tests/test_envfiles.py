"""Env files: which files are sources, and how they are read."""

from __future__ import annotations

import codecs
import os
from pathlib import Path

import pytest

from omerta import envfiles
from omerta.envfiles import EnvFile
from omerta.errors import OmertaError
from omerta.patterns import DEFAULT_PATTERNS, Pattern

_SECRET = "sk-test-0123456789abcdef"


def _patterns(*texts: str) -> tuple[Pattern, ...]:
    return tuple(Pattern(text) for text in texts or DEFAULT_PATTERNS)


def _write(path: Path, content: str | bytes = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    return path


def _found(top: Path, *patterns: str, tracked: set[bytes] | None = None) -> list[str]:
    return [env.path for env in envfiles.find(top, _patterns(*patterns), tracked or set())]


def test_the_default_patterns_find_the_env_files_at_the_root(tmp_path: Path):
    for name in (".env", ".env.local", ".env.production", ".envrc", "app/.env", "env"):
        _write(tmp_path / name)
    assert _found(tmp_path) == [".env", ".env.local", ".env.production"]


def test_templates_tracked_files_and_folders_are_no_sources(tmp_path: Path):
    for name in (".env", ".env.example", ".env.local.sample", ".env.template", ".env.ci"):
        _write(tmp_path / name)
    # A virtual environment named .env.venv is a folder, never a source.
    _write(tmp_path / ".env.venv" / "pyvenv.cfg")
    assert _found(tmp_path, tracked={b".env.ci"}) == [".env"]


def test_sources_come_in_the_order_of_the_patterns_then_of_the_paths(tmp_path: Path):
    for name in ("b/.env", "a/.env", ".env", "a/.env.local"):
        _write(tmp_path / name)
    assert _found(tmp_path, "*/.env.*", "*/.env", ".env", "a/.env") == [
        "a/.env.local",
        "a/.env",
        "b/.env",
        ".env",
    ]


def test_a_double_star_walks_every_folder_but_git_and_nested_repositories(tmp_path: Path):
    for name in (
        ".env",
        "a/.env",
        "a/b/c/.env",
        ".git/.env",
        "vendor/lib/.git/HEAD",
        "vendor/lib/.env",
        "module/.git",
        "module/.env",
    ):
        _write(tmp_path / name)
    assert _found(tmp_path, "**/.env") == [".env", "a/.env", "a/b/c/.env"]
    assert _found(tmp_path, "a/**") == ["a/.env", "a/b/c/.env"]


def test_a_missing_folder_has_no_sources(tmp_path: Path):
    assert _found(tmp_path, "config/.env") == []
    _write(tmp_path / "config")  # a file where the folder would be
    assert _found(tmp_path, "config/.env") == []


class _Entry:
    """An entry of :func:`os.scandir` that is neither a folder nor a file, as a pipe is."""

    def __init__(self, folder: Path, name: str) -> None:
        self.name = name
        self.path = str(folder / name)

    def is_dir(self, follow_symlinks: bool = True) -> bool:
        return False

    def is_file(self) -> bool:
        return False


def test_what_is_neither_a_folder_nor_a_file_is_no_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    class Listing(list):
        def __enter__(self) -> Listing:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(os, "scandir", lambda path: Listing([_Entry(tmp_path, ".env")]))
    assert _found(tmp_path) == []


def test_a_folder_that_cannot_be_listed_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    (tmp_path / "config").mkdir()
    scandir = os.scandir
    refused: list[str] = []

    def refuse(path: Path):
        if Path(path).name in refused:
            raise PermissionError(13, "Permission denied")
        return scandir(path)

    monkeypatch.setattr(os, "scandir", refuse)
    refused[:] = [tmp_path.name]
    with pytest.raises(OmertaError, match=r"folder \. cannot be listed \(Permission denied\)"):
        envfiles.find(tmp_path, _patterns(), set())
    refused[:] = ["config"]
    with pytest.raises(OmertaError, match="folder config cannot be listed"):
        envfiles.find(tmp_path, _patterns("config/.env"), set())


@pytest.mark.usefixtures("symlinks")
def test_a_link_to_a_file_is_read_through_and_a_link_to_a_folder_is_not_followed(
    tmp_path: Path,
):
    target = _write(tmp_path / "outside" / "secrets.txt", f"KEY={_SECRET}\n")
    top = tmp_path / "top"
    _write(top / "elsewhere" / ".env", f"KEY={_SECRET}\n")
    (top / ".env").symlink_to(target)
    (top / "linked").symlink_to(top / "elsewhere", target_is_directory=True)
    (top / ".env.broken").symlink_to(top / "missing")
    found = envfiles.find(top, _patterns(".env*", "**/.env"), set())
    assert [env.path for env in found] == [".env", "elsewhere/.env"]
    assert envfiles.read(found[0]) == [("KEY", _SECRET)]


def _read(tmp_path: Path, content: str | bytes) -> list[tuple[str, str | None]]:
    path = _write(tmp_path / ".env", content)
    return envfiles.read(EnvFile(".env", path))


def test_python_dotenv_parses_the_statements(tmp_path: Path):
    assert _read(
        tmp_path,
        "# a comment\n"
        "PLAIN=value one  # inline comment\n"
        "export EXPORTED=exported\n"
        "SINGLE='single ${PLAIN}'\n"
        'DOUBLE="tab\\tnew\\nline"\n'
        'MULTI="first\nsecond"\n'
        "EMPTY=\n"
        "COMMENT_ONLY= # nothing\n"
        "NO_VALUE\n"
        "PLAIN=again\n",
    ) == [
        ("PLAIN", "value one"),
        ("EXPORTED", "exported"),
        ("SINGLE", "single ${PLAIN}"),
        ("DOUBLE", "tab\tnew\nline"),
        ("MULTI", "first\nsecond"),
        ("EMPTY", ""),
        ("COMMENT_ONLY", ""),
        ("NO_VALUE", None),
        ("PLAIN", "again"),
    ]


@pytest.mark.parametrize(
    "data",
    [
        b'KEY="a\nb"\n',
        codecs.BOM_UTF8 + b'KEY="a\nb"\n',
        b'KEY="a\r\nb"\r\n',
        b'KEY="a\rb"\r',
        codecs.BOM_UTF16_LE + 'KEY="a\r\nb"\r\n'.encode("utf-16-le"),
        codecs.BOM_UTF16_BE + 'KEY="a\nb"\n'.encode("utf-16-be"),
    ],
    ids=["utf-8", "utf-8 with bom", "crlf", "cr", "utf-16-le crlf", "utf-16-be"],
)
def test_encodings_and_line_ends_give_the_same_values(tmp_path: Path, data: bytes):
    assert _read(tmp_path, data) == [("KEY", "a\nb")]


def test_bytes_that_do_not_decode_are_an_error_without_them(tmp_path: Path):
    for data in (b"KEY=sk-\xff\xfe-secret\n", codecs.BOM_UTF16_LE + b"K\x00="):
        with pytest.raises(OmertaError) as error:
            _read(tmp_path, data)
        assert str(error.value) == ".env is neither UTF-8 nor UTF-16 text"
        assert error.value.__context__ is None
        assert error.value.__cause__ is None


def test_a_statement_that_cannot_be_parsed_is_an_error_at_its_line_without_its_text(
    tmp_path: Path,
):
    with pytest.raises(OmertaError) as error:
        _read(tmp_path, f'GOOD=1\n\n\n  \nBAD="{_SECRET}\nOTHER=2\n')
    assert str(error.value) == ".env, line 5: python-dotenv cannot parse this statement"
    assert _SECRET not in str(error.value)


def test_a_file_that_cannot_be_read_is_an_error(tmp_path: Path):
    with pytest.raises(OmertaError, match=r"^\.env cannot be read \(.+\)$"):
        envfiles.read(EnvFile(".env", tmp_path / "missing" / ".env"))


def test_an_env_file_path_is_quoted_in_errors(tmp_path: Path):
    path = _write(tmp_path / "x", b"\xff")
    with pytest.raises(OmertaError, match=r'^"\.env\\t" is neither'):
        envfiles.read(EnvFile(".env\t", path))
