"""The command line: usage, errors, and what reaches the terminal."""

from __future__ import annotations

import errno
import io
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import tethered

from conftest import Repo
from omerta import __version__, cli, config
from omerta.output import Finding

_KEY = "sk-test-0123456789abcdef"


def test_version_and_help_exit_with_0(omerta, tmp_path: Path):
    version = omerta("--version", cwd=tmp_path)
    assert (version.code, version.out) == (0, f"omerta {__version__}\n")
    help_ = omerta("check", "-h", cwd=tmp_path)
    assert help_.code == 0
    assert "--min-chars N" in help_.out


@pytest.mark.parametrize(
    ("args", "problem"),
    [
        ([], "the following arguments are required: COMMAND"),
        (["scan"], "invalid choice: 'scan'"),
        (["check", "--min-chars", "0"], "'0' is not an integer of at least 1"),
        (["check", "--min-chars", "eight"], "'eight' is not an integer of at least 1"),
        (["check", "--all", "a.py"], "PATH arguments cannot be given with --all"),
        (["history", "a.py"], "unrecognized arguments: a.py"),
        (["check", "-C", "x"], "unrecognized arguments: -C"),
    ],
)
def test_a_usage_error_exits_with_2(omerta, tmp_path: Path, args: list[str], problem: str):
    result = omerta(*args, cwd=tmp_path)
    assert result.code == 2
    assert result.out == ""
    assert problem in result.err


def test_outside_a_repository_omerta_exits_with_2(
    omerta, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    result = omerta("check", cwd=tmp_path)
    assert result.code == 2
    assert result.err.startswith("omerta: git rev-parse failed with exit status 128:\n")
    assert "not a git repository" in result.err


def test_a_folder_given_with_c_must_exist(omerta, repo: Repo):
    result = omerta("-C", "missing", "check", cwd=repo.path)
    assert (result.code, result.err) == (2, "omerta: cannot change to missing: no such folder\n")


def _link_folder(link: Path, target: Path) -> None:
    """Make ``link`` lead to the folder ``target``: on Windows a junction, which needs no right."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def test_a_folder_reached_through_a_link_is_where_the_link_leads(
    omerta, repo: Repo, tmp_path: Path
):
    repo.write(".env", f"API_KEY={_KEY}\n")
    repo.write("sub/app.py", f"{_KEY}\n")
    repo.add("sub/app.py")
    link = tmp_path / "link"
    _link_folder(link, repo.path / "sub")
    found = ["sub/app.py:1: API_KEY (from .env)"]
    # git climbs from where the link leads, and so does omerta: not from its parent.
    assert omerta("-C", str(link), "check", "--all", cwd=repo.path).lines == found
    assert omerta("-C", str(link), "check", "app.py", cwd=repo.path).lines == found
    within = omerta("check", "--all", cwd=link)
    assert within.lines == found
    within.assert_silent_about(_KEY)


def test_git_must_be_on_path(omerta, repo: Repo, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PATH", "")
    result = omerta("check", cwd=repo.path)
    assert (result.code, result.err) == (2, "omerta: git was not found on PATH\n")


def test_an_unexpected_error_is_reported_without_its_text(
    omerta, repo: Repo, monkeypatch: pytest.MonkeyPatch
):
    def fail(*args, **kwargs):
        raise ValueError(f"a bug that shows {_KEY}")

    monkeypatch.setattr(config, "load", fail)
    result = omerta("check", cwd=repo.path)
    assert result.code == 2
    assert result.err.startswith("omerta: unexpected ValueError at test_cli.py:")
    assert "please report it at https://github.com/shcherbak-ai/omerta/issues" in result.err
    result.assert_silent_about(_KEY)


def test_output_is_utf8_with_newlines_whatever_the_stream(monkeypatch: pytest.MonkeyPatch):
    stream = io.StringIO()
    cli._write(stream, "café\n")
    assert stream.getvalue() == "café\n"
    raw = io.BytesIO()
    wrapper = io.TextIOWrapper(raw, encoding="cp1252", newline="\r\n")
    cli._write(wrapper, "café\n")
    assert raw.getvalue() == "café\n".encode()


class _GonePipe:
    """Standard output into a pipe whose reader has gone, failing with ``error``."""

    def __init__(self, fd: int, error: OSError) -> None:
        self.fd = fd
        self.error = error
        self.buffer = self

    def flush(self) -> None:
        pass

    def write(self, data: bytes) -> None:
        raise self.error

    def fileno(self) -> int:
        return self.fd


@pytest.mark.parametrize(
    ("error", "windows"),
    [
        (BrokenPipeError(errno.EPIPE, "Broken pipe"), False),
        (BrokenPipeError(errno.EPIPE, "Broken pipe"), True),
        # Windows, for a reader that went before the write.
        (OSError(errno.EINVAL, "Invalid argument"), True),
    ],
)
def test_a_stream_whose_reader_has_gone_takes_nothing_more(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError, windows: bool
):
    monkeypatch.setattr(cli, "WINDOWS", windows)
    target = tmp_path / "out.txt"
    with target.open("wb") as file:
        cli._write(_GonePipe(file.fileno(), error), "a.py:1: K (from .env)\n")
        # What Python would flush into the stream at exit goes nowhere.
        os.write(file.fileno(), b"a.py:1: K (from .env)\n")
    assert target.read_bytes() == b""


@pytest.mark.parametrize(
    ("error", "windows"),
    [(OSError(errno.EINVAL, "Invalid argument"), False), (OSError(errno.EIO, "I/O error"), True)],
)
def test_another_failure_to_write_is_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError, windows: bool
):
    monkeypatch.setattr(cli, "WINDOWS", windows)
    with (tmp_path / "out.txt").open("wb") as file, pytest.raises(OSError) as raised:
        cli._write(_GonePipe(file.fileno(), error), "a.py:1: K (from .env)\n")
    assert raised.value is error


def test_findings_to_a_reader_that_stops_early_end_the_run_quietly(repo: Repo):
    repo.write(".env", f"API_KEY={_KEY}\n")
    blob = repo.blob(f"{_KEY}\n")
    # More findings than a pipe holds, so that omerta writes while its reader has gone.
    repo.index_info(*(("100644", blob, 0, f"files/{n:05}.txt".encode()) for n in range(5000)))
    run = "import sys; from omerta.cli import main; sys.exit(main(['check', '--all']))"
    with subprocess.Popen(
        [sys.executable, "-c", run], cwd=repo.path, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as process:
        assert process.stdout is not None and process.stderr is not None
        assert process.stdout.readline() == b"files/00000.txt:1: API_KEY (from .env)\n"
        process.stdout.close()
        assert process.stderr.read() == b""
    assert process.returncode == 1


def test_findings_go_to_stdout_and_errors_to_stderr(
    omerta, repo: Repo, monkeypatch: pytest.MonkeyPatch
):
    from omerta import check
    from omerta.errors import OmertaError

    def run(git, entries, targets, patterns, findings: list[Finding]) -> None:
        findings.append(Finding(path=b"b.py", line=1, variable="K", env_file=".env"))
        findings.append(Finding(path=b"a.py", line=2, variable="K", env_file=".env"))
        raise OmertaError("stopped")

    monkeypatch.setattr(check, "run", run)
    result = omerta("check", cwd=repo.path)
    assert result.code == 2
    assert result.lines == ["a.py:2: K (from .env)", "b.py:1: K (from .env)"]
    assert result.err == "omerta: stopped\n"
    quiet = omerta("check", "--quiet", cwd=repo.path)
    assert (quiet.code, quiet.out, quiet.err) == (2, "", "omerta: stopped\n")


def test_the_entry_point_exits_with_the_code_of_main(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "argv", ["omerta", "--version"])
    assert cli.main() == 0


def test_no_test_reaches_the_network():
    with socket.socket() as connection, pytest.raises(tethered.EgressBlocked):
        connection.connect(("192.0.2.1", 443))
