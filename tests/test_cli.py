"""The command line: usage, errors, and what reaches the terminal."""

from __future__ import annotations

import io
import socket
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
