"""git: found safely, run with settings that keep its output exact, and read."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import GIT_VERSION, Repo
from omerta import git as git_module
from omerta.errors import OmertaError
from omerta.git import (
    Git,
    GitError,
    environment,
    find_executable,
    find_repository,
    is_null,
)

_KEY = "sk-test-0123456789abcdef"


def _fake_git(folder: Path, name: str) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"")
    path.chmod(0o755)
    return str(path)


@pytest.mark.parametrize("windows", [True, False])
def test_git_is_taken_from_path_skipping_relative_current_and_repository_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: bool
):
    monkeypatch.setattr(git_module, "WINDOWS", windows)
    name = "git.exe" if windows else "git"
    repository = tmp_path / "repo"
    start = tmp_path / "start"
    folders = [
        "relative/bin",
        "",
        str(start),
        str(repository / ".venv" / "Scripts"),
        str(tmp_path / "empty"),
        str(tmp_path / "usr" / "bin"),
        str(tmp_path / "later"),
    ]
    for folder in ("relative/bin", start, repository / ".venv" / "Scripts", "usr/bin", "later"):
        _fake_git(tmp_path / folder, name)
    monkeypatch.chdir(tmp_path)
    found = find_executable(os.pathsep.join(folders), [str(start)], str(repository))
    assert found == str(tmp_path / "usr" / "bin" / name)


def test_a_file_that_is_not_executable_is_no_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(git_module, "WINDOWS", False)
    _fake_git(tmp_path / "a", "git")
    _fake_git(tmp_path / "b", "git")
    monkeypatch.setattr(os, "access", lambda path, mode: "b" in Path(path).parent.name)
    path = os.pathsep.join([str(tmp_path / "a"), str(tmp_path / "b")])
    assert find_executable(path, [], None) == str(tmp_path / "b" / "git")


def test_no_git_on_path_is_an_error(tmp_path: Path):
    with pytest.raises(OmertaError, match="git was not found on PATH"):
        find_executable(str(tmp_path), [], None)


def test_a_folder_on_another_drive_is_not_inside_the_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def commonpath(paths: list[str]) -> str:
        raise ValueError("Paths don't have the same drive")

    monkeypatch.setattr(os.path, "commonpath", commonpath)
    assert not git_module._inside(str(tmp_path / "a"), str(tmp_path))


def test_the_repository_is_found_upwards_by_its_git_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    (tmp_path / "top" / "a" / "b").mkdir(parents=True)
    (tmp_path / "top" / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    assert find_repository(str(tmp_path / "top" / "a" / "b")) == str(tmp_path / "top")
    monkeypatch.setattr(os.path, "lexists", lambda path: False)
    assert find_repository(str(tmp_path / "top" / "a")) is None


def test_git_runs_with_literal_pathspecs_no_lazy_fetch_and_the_index_it_is_given(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("GIT_INDEX_FILE", ".git/next-index")
    monkeypatch.setenv("GIT_GLOB_PATHSPECS", "1")
    monkeypatch.setenv("GIT_ICASE_PATHSPECS", "1")
    env = environment()
    # git reads a relative one from the root, where omerta runs it.
    assert env["GIT_INDEX_FILE"] == ".git/next-index"
    assert env["GIT_NO_LAZY_FETCH"] == "1"
    assert env["GIT_LITERAL_PATHSPECS"] == "1"
    assert env["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert "GIT_GLOB_PATHSPECS" not in env
    assert "GIT_ICASE_PATHSPECS" not in env


def test_an_index_file_that_is_not_there_is_an_error(repo: Repo, monkeypatch: pytest.MonkeyPatch):
    # git would read it as an empty index, and every check would pass.
    monkeypatch.setenv("GIT_INDEX_FILE", ".git/missing-index")
    with pytest.raises(OmertaError, match="GIT_INDEX_FILE names .git/missing-index, which is not"):
        _git(repo).open()


def test_a_git_error_shows_gits_output_with_each_value_replaced():
    error = GitError("diff", 128, f"fatal: bad path {_KEY}\n".encode())
    assert str(error) == "git diff failed with exit status 128"
    assert _KEY not in str(error)
    shown = error.describe(lambda data: data.replace(_KEY.encode(), b"<KEY>"))
    assert shown == "git diff failed with exit status 128:\nfatal: bad path <KEY>"
    assert (
        GitError("log", 1, b"  \n").describe(lambda data: data)
        == "git log failed with exit status 1"
    )


def _git(repo: Repo, folder: Path | None = None) -> Git:
    executable = shutil.which("git")
    assert executable is not None
    where = str(folder or repo.path)
    return Git(executable, where, environment())


def _with_version(monkeypatch: pytest.MonkeyPatch, output: bytes) -> None:
    run = Git.run

    def fake(self: Git, *args: str, **kwargs) -> bytes:
        return output if args == ("version",) else run(self, *args, **kwargs)

    monkeypatch.setattr(Git, "run", fake)


def test_open_reads_the_version_and_finds_the_root_from_a_subfolder(repo: Repo):
    (repo.path / "a" / "b").mkdir(parents=True)
    git = _git(repo, repo.path / "a" / "b")
    git.open()
    assert git.version >= (2, 31)
    assert os.path.samefile(git.top, repo.path)


@pytest.mark.parametrize(
    ("output", "problem"),
    [
        (b"git version 2.30.9\n", "git 2.30.9 is older than 2.31"),
        (b"git version 2\n", "the version of git cannot be read"),
        (b"", "the version of git cannot be read"),
    ],
)
def test_a_git_too_old_or_unknown_is_an_error(
    repo: Repo, monkeypatch: pytest.MonkeyPatch, output: bytes, problem: str
):
    _with_version(monkeypatch, output)
    with pytest.raises(OmertaError, match=problem):
        _git(repo).open()


def test_an_older_git_works_where_nothing_can_be_fetched(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
):
    _with_version(monkeypatch, b"git version 2.31.1 (Apple Git-130)\n")
    git = _git(repo)
    git.open()
    assert git.version == (2, 31, 1)


def test_outside_a_repository_git_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    git = Git(shutil.which("git") or "git", str(tmp_path), environment())
    with pytest.raises(GitError, match="git rev-parse failed") as error:
        git.open()
    assert b"not a git repository" in error.value.stderr


def test_a_git_inside_the_repository_is_refused(repo: Repo, monkeypatch: pytest.MonkeyPatch):
    # A git that runs only from its own installation stands in for the one in
    # the repository, which is never run.
    real, run = shutil.which("git") or "", Git.run

    def run_real(self: Git, *args: str, **kwargs) -> bytes:
        return run(Git(real, self.folder, self.env), *args, **kwargs)

    monkeypatch.setattr(Git, "run", run_real)
    planted = repo.path / "tools" / "git.exe"
    git = Git(str(planted), str(repo.path), environment())
    with pytest.raises(OmertaError, match="inside the repository"):
        git.open()


def test_promisor_remotes_make_a_partial_clone(repo: Repo):
    git = _git(repo)
    assert not git.is_partial_clone()
    repo.git("config", "remote.origin.promisor", "false")
    assert not git.is_partial_clone()
    repo.git("config", "remote.my remote.promisor", "yes")
    assert git.is_partial_clone()
    repo.git("config", "--unset", "remote.my remote.promisor")
    repo.git("config", "extensions.partialClone", "origin")
    assert git.is_partial_clone()


def _source(tmp_path: Path) -> Repo:
    source = Repo(tmp_path / "source")
    source.git("config", "uploadpack.allowFilter", "true")
    source.write("old.txt", "old\n")
    source.add("old.txt")
    source.commit("old")
    source.git("rm", "-q", "old.txt")
    source.write("new.txt", "new\n")
    source.add("new.txt")
    source.commit("new")
    return source


def test_an_older_git_is_refused_in_a_partial_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = _source(tmp_path)
    clone = tmp_path / "clone"
    source.git("clone", "-q", "--filter=blob:none", source.path.as_uri(), str(clone))
    _with_version(monkeypatch, b"git version 2.44.2\n")
    git = Git(shutil.which("git") or "", str(clone), environment())
    with pytest.raises(OmertaError, match="partial clone.*git 2.44.2 can fetch.*2.45"):
        git.open()


def test_a_missing_object_is_an_error_never_a_fetch(tmp_path: Path):
    source = _source(tmp_path)
    clone = tmp_path / "clone"
    source.git("clone", "-q", "--filter=blob:none", source.path.as_uri(), str(clone))
    git = Git(shutil.which("git") or "", str(clone), environment())
    if GIT_VERSION < (2, 45):
        # This git would fetch what it reads: omerta does not run it here.
        with pytest.raises(OmertaError, match="partial clone"):
            git.open()
        return
    git.open()
    old = source.oid("HEAD~1:old.txt")
    with pytest.raises(OmertaError, match=f"git object {old} cannot be read"):
        list(git.objects([old]))
    with pytest.raises(OmertaError, match=f"git object {old} cannot be read"):
        list(git.objects([old], check=True))
    # Nothing was fetched: the clone still lacks it.
    missing = subprocess.run(
        ["git", "cat-file", "-e", old],
        cwd=clone,
        env={**os.environ, "GIT_NO_LAZY_FETCH": "1"},
        capture_output=True,
        check=False,
    )
    assert missing.returncode != 0


def test_a_shallow_clone_is_known(tmp_path: Path):
    source = _source(tmp_path)
    clone = tmp_path / "clone"
    source.git("clone", "-q", "--depth", "1", source.path.as_uri(), str(clone))
    shallow = Git(shutil.which("git") or "", str(clone), environment())
    shallow.open()
    assert shallow.is_shallow()
    full = _git(source)
    full.open()
    assert not full.is_shallow()


def test_objects_are_read_in_order_with_their_types(repo: Repo):
    blob = repo.blob(b"line\n\x00binary")
    empty = repo.blob(b"")
    git = _git(repo)
    git.open()
    assert list(git.objects([blob, empty, blob])) == [
        (blob, "blob", b"line\n\x00binary"),
        (empty, "blob", b""),
        (blob, "blob", b"line\n\x00binary"),
    ]
    assert list(git.objects([blob], check=True)) == [(blob, "blob", b"")]


def test_a_reader_stopped_early_stops_git(repo: Repo):
    # More IDs than the pipes hold, so that git is stopped while they are written.
    blob = repo.blob(b"x" * 4096)
    git = _git(repo)
    git.open()
    reader = git.objects([blob] * 100_000)
    assert next(reader)[0] == blob
    reader.close()


@pytest.mark.parametrize(
    "output", [b"1234 blob 10\nshort\n", b"1234 blob 5\nshortX"], ids=["short", "no newline"]
)
def test_an_object_cut_short_is_an_error(
    repo: Repo, monkeypatch: pytest.MonkeyPatch, output: bytes
):
    git = _git(repo)
    git.open()

    class Process:
        """A git that stops in the middle of an object."""

        def __init__(self, *args, **kwargs) -> None:
            self.stdout = io.BytesIO(output)

        def kill(self) -> None:
            pass

        def wait(self) -> int:
            return -9

    monkeypatch.setattr(subprocess, "Popen", Process)
    with pytest.raises(OmertaError, match="git object 1234 cannot be read"):
        list(git.objects(["1234"]))


def test_null_object_ids():
    assert is_null("0" * 40)
    assert is_null("0" * 64)
    assert not is_null("0" * 39 + "1")
