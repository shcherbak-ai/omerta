"""Test configuration: no network, git cut off from the user's configuration, and Hypothesis."""

from __future__ import annotations

import tethered

# Before any other import: no Python socket leaves the process. git runs as a
# subprocess, which is what omerta does, and reaches no network either: every
# remote of the tests is a folder (file://).
tethered.activate(allow=[], external_subprocess_policy="allow")

import os  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402
from collections.abc import Iterator  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from dotenv import dotenv_values  # noqa: E402
from hypothesis import settings  # noqa: E402

from omerta.cli import main  # noqa: E402

# Property tests try new examples at each local run, and keep those that failed
# in .hypothesis/ to try first the next time. In CI (GitHub sets CI) they try
# the same examples at each run, so that a commit passes or fails whatever the
# chance, and a failure prints the line that replays it. No deadline: a busy
# runner is slow, not wrong.
settings.register_profile("local", deadline=None)
settings.register_profile(
    "ci", settings.get_profile("local"), derandomize=True, database=None, print_blob=True
)
settings.load_profile("ci" if os.environ.get("CI") else "local")

# Variables of git's that would point a test's git, or omerta's, at another
# repository, index or configuration.
_GIT_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_GLOB_PATHSPECS",
    "GIT_NOGLOB_PATHSPECS",
    "GIT_ICASE_PATHSPECS",
    "GIT_LITERAL_PATHSPECS",
)

# The git of this machine: before 2.45, omerta refuses a partial clone.
GIT_VERSION = tuple(
    int(part)
    for part in re.findall(
        r"\d+", subprocess.run(["git", "version"], capture_output=True, text=True).stdout
    )[:3]
)


@pytest.fixture(autouse=True)
def _isolated_git(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """Run git as on a fresh machine: no system or user configuration, a fixed author."""
    config = tmp_path_factory.mktemp("home") / "gitconfig"
    config.write_text("", encoding="utf-8")
    for name in _GIT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")


@dataclass
class Result:
    """What a run of omerta returned and wrote."""

    code: int
    out: str
    err: str

    @property
    def lines(self) -> list[str]:
        """The lines of standard output."""
        return self.out.splitlines()

    def assert_silent_about(self, *values: str) -> None:
        """Assert that no value is in the output, standard or error."""
        for value in values:
            assert value not in self.out
            assert value not in self.err


class Repo:
    """A git repository under the test's temporary folder."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._clock = 1_700_000_000
        path.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "core.autocrlf", "false")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "tag.gpgsign", "false")

    def git(self, *args: str, cwd: Path | None = None, input: bytes | None = None) -> str:
        """Run git in the repository; its output, as text."""
        result = subprocess.run(
            ["git", *args],
            cwd=cwd or self.path,
            input=input,
            capture_output=True,
            check=True,
            env={**os.environ, "GIT_AUTHOR_DATE": self._date(), "GIT_COMMITTER_DATE": self._date()},
        )
        return result.stdout.decode("utf-8", "surrogateescape")

    def _date(self) -> str:
        """A commit date a second after the last, so that git log's order is known."""
        self._clock += 1
        return f"{self._clock} +0000"

    def write(self, name: str, content: str | bytes) -> Path:
        """Write a file of the working tree, creating its folders."""
        path = self.path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            content = content.encode("utf-8")
        path.write_bytes(content)
        return path

    def add(self, *names: str) -> None:
        """Stage files."""
        self.git("add", "--", *names)

    def commit(self, message: str = "change", *extra: str) -> str:
        """Commit what is staged; the commit's ID."""
        self.git("commit", "-q", "--allow-empty", "-m", message, *extra)
        return self.git("rev-parse", "HEAD").strip()

    def oid(self, revision: str) -> str:
        """The full ID of an object."""
        return self.git("rev-parse", revision).strip()

    def blob(self, content: str | bytes) -> str:
        """Write a blob to the object store; its ID."""
        data = content.encode("utf-8") if isinstance(content, str) else content
        return self.git("hash-object", "-w", "--stdin", input=data).strip()

    def index_info(self, *records: tuple[str, str, int, bytes]) -> None:
        """Put entries into the index as they are: mode, object ID, stage and path."""
        data = b"".join(
            f"{mode} {oid} {stage}\t".encode() + path + b"\0" for mode, oid, stage, path in records
        )
        self.git("update-index", "-z", "--index-info", input=data)


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    """An empty repository, on an unborn ``main``."""
    return Repo(tmp_path / "repo")


def _values_near(folder: Path) -> list[str]:
    """The values of the env files at the root of the repository that holds ``folder``."""
    root = next((path for path in (folder, *folder.parents) if (path / ".git").exists()), folder)
    values = []
    for env_file in root.glob(".env*"):
        if env_file.is_file():
            try:
                parsed = dotenv_values(env_file, interpolate=False)
            except UnicodeDecodeError:
                continue
            values += [value for value in parsed.values() if value and len(value) >= 8]
    return values


@pytest.fixture
def omerta(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch):
    """Run omerta's command line in a folder; what it returned and wrote.

    Every run is checked to write no value of the env files at the root of
    the repository, whatever it found.
    """

    def run(*args: str, cwd: Path) -> Result:
        monkeypatch.chdir(cwd)
        capsys.readouterr()
        code = main(list(args))
        out, err = capsys.readouterr()
        result = Result(code, out, err)
        result.assert_silent_about(*_values_near(cwd))
        return result

    return run


@pytest.fixture
def symlinks(tmp_path: Path) -> Iterator[None]:
    """Skip a test where this machine cannot make symbolic links (Windows without the right)."""
    try:
        (tmp_path / "probe-link").symlink_to(tmp_path)
    except OSError:
        pytest.skip("symbolic links cannot be made here")
    yield
