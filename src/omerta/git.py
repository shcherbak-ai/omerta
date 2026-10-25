"""git: found on ``PATH``, run with settings that keep its output exact, and read."""

from __future__ import annotations

import os
import re

# git runs by its absolute path, with arguments of its own and no shell.
import subprocess  # nosec B404
import sys
import tempfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import IO

from omerta.errors import OmertaError

WINDOWS = sys.platform == "win32"

# git 2.31 has every option omerta gives it (`--diff-merges`); 2.45 is the first
# to honour GIT_NO_LAZY_FETCH, which only a partial clone needs.
MINIMUM_VERSION = (2, 31)
NO_LAZY_FETCH_VERSION = (2, 45)
# From git 2.50, `rev-list -z` gives a path whole, where an older git cuts it
# at a newline.
REV_LIST_Z_VERSION = (2, 50)

GITLINK = "160000"

_VERSION = re.compile(rb"git version (\d+)\.(\d+)(?:\.(\d+))?")
_NULL = re.compile(r"0+")


class GitError(OmertaError):
    """A git command that failed. Its message names the command and its exit status.

    git's own error output is kept apart, in :attr:`stderr`, to be shown with
    every searched form replaced by the name of its variable.
    """

    def __init__(self, command: str, status: int, stderr: bytes) -> None:
        super().__init__(f"git {command} failed with exit status {status}")
        self.stderr = stderr

    def describe(self, redact: Callable[[bytes], bytes]) -> str:
        """The message, followed by git's error output after ``redact``."""
        detail = redact(self.stderr).decode("utf-8", "backslashreplace").strip()
        return f"{self}:\n{detail}" if detail else str(self)


@dataclass(frozen=True)
class Entry:
    """An index entry."""

    mode: str
    oid: str
    stage: int
    path: bytes


def is_null(oid: str) -> bool:
    """Whether an object ID is the null one, which stands for no object."""
    return _NULL.fullmatch(oid) is not None


def find_executable(path_variable: str, skipped: Iterable[str], repository: str | None) -> str:
    """The absolute path of git, from the folders of ``PATH`` in order.

    Skipped are relative entries, the folders of ``skipped`` (where omerta
    starts, and its ``-C`` folder), and any folder inside ``repository``: on
    Windows, a program's own folder and the current folder are searched before
    ``PATH``, so a ``git.exe`` in the checked repository could otherwise run.

    Raises:
        OmertaError: No git is found.
    """
    skip = {_real(folder) for folder in skipped}
    for folder in path_variable.split(os.pathsep):
        if not folder or not os.path.isabs(folder):
            continue
        if _real(folder) in skip or (repository is not None and _inside(folder, repository)):
            continue
        candidate = os.path.join(folder, "git.exe" if WINDOWS else "git")
        if os.path.isfile(candidate) and (WINDOWS or os.access(candidate, os.X_OK)):
            return candidate
    raise OmertaError("git was not found on PATH")


def find_repository(folder: str) -> str | None:
    """The nearest folder, from ``folder`` upwards, that holds a ``.git``."""
    current = os.path.abspath(folder)
    while True:
        if os.path.lexists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _real(path: str) -> str:
    """A path as compared: resolved, and in the case the platform compares it in."""
    return os.path.normcase(os.path.realpath(path))


def _inside(path: str, folder: str) -> bool:
    """Whether ``path`` is ``folder`` or inside it."""
    path, folder = _real(path), _real(folder)
    try:
        return os.path.commonpath([path, folder]) == folder
    except ValueError:  # on different drives
        return False


def environment() -> dict[str, str]:
    """The environment git runs in.

    ``GIT_INDEX_FILE``, which ``git commit -a`` sets, is kept as it is: git reads
    a relative one from the root, where omerta runs it. A missing object is an
    error, never a fetch (``GIT_NO_LAZY_FETCH``); pathspecs are literal; a
    replace ref would hide the original object; and the pathspec settings that
    git refuses beside ``GIT_LITERAL_PATHSPECS`` are removed.
    """
    env = dict(os.environ)
    env.update(GIT_NO_LAZY_FETCH="1", GIT_LITERAL_PATHSPECS="1", GIT_NO_REPLACE_OBJECTS="1")
    for name in ("GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS"):
        env.pop(name, None)
    return env


class Git:
    """git, run in a repository."""

    def __init__(self, executable: str, folder: str, env: dict[str, str]) -> None:
        self.executable = executable
        self.folder = folder
        self.env = env
        self.top = folder
        self.version: tuple[int, ...] = ()

    def run(self, *args: str, cwd: str | None = None, ok: tuple[int, ...] = (0,)) -> bytes:
        """The output of a git command, in ``cwd`` (by default the root).

        Raises:
            GitError: The command exits with a status not in ``ok``.
        """
        # git by its absolute path, with arguments of its own and no shell
        result = subprocess.run(  # noqa: S603  # nosec B603
            [self.executable, *args],
            cwd=cwd or self.top,
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
        if result.returncode not in ok:
            command = next(arg for arg in args if not arg.startswith("-") and "=" not in arg)
            raise GitError(command, result.returncode, result.stderr)
        return result.stdout

    def open(self) -> None:
        """Read git's version, find the root, and check that git can read it as it is.

        Raises:
            OmertaError: git is too old, for any repository or for a partial
                clone, the folder is in no repository, or ``GIT_INDEX_FILE``
                names no file, which git would read as an empty index.
        """
        match = _VERSION.search(self.run("version", cwd=self.folder))
        if match is None:
            raise OmertaError("the version of git cannot be read")
        self.version = tuple(int(part or 0) for part in match.groups())
        if self.version < MINIMUM_VERSION:
            raise OmertaError(f"git {_dotted(self.version)} is older than 2.31, which omerta needs")
        cdup = self.run("rev-parse", "--show-cdup", cwd=self.folder)
        cdup = cdup.decode("utf-8", "surrogateescape").strip()
        self.top = os.path.normpath(os.path.join(self.folder, cdup))
        if _inside(self.executable, self.top):
            raise OmertaError("the git found on PATH is inside the repository, which omerta checks")
        index = self.env.get("GIT_INDEX_FILE")
        if index and not os.path.isfile(os.path.join(self.top, index)):
            raise OmertaError(f"GIT_INDEX_FILE names {index}, which is not a file")
        if self.version < NO_LAZY_FETCH_VERSION and self.is_partial_clone():
            raise OmertaError(
                f"this repository is a partial clone, where git {_dotted(self.version)} can fetch "
                "objects it reads: omerta needs git 2.45 or later here"
            )

    def is_partial_clone(self) -> bool:
        """Whether the repository has a promisor remote, from which git fetches lazily."""
        if self.run("config", "--get", "extensions.partialClone", ok=(0, 1)):
            return True
        output = self.run(
            "config", "--type=bool", "-z", "--get-regexp", r"^remote\..*\.promisor$", ok=(0, 1)
        )
        return any(item.endswith(b"\ntrue") for item in output.split(b"\0"))

    def is_shallow(self) -> bool:
        """Whether the repository is a shallow clone, whose history stops at a cut."""
        return self.run("rev-parse", "--is-shallow-repository").strip() == b"true"

    def index(self) -> list[Entry]:
        """The index entries."""
        output = self.run("ls-files", "-s", "-z", "--full-name")
        entries = []
        for record in output.split(b"\0"):
            if record:
                info, path = record.split(b"\t", 1)
                mode, oid, stage = info.decode("ascii").split(" ")
                entries.append(Entry(mode, oid, int(stage), path))
        return entries

    def staged_paths(self) -> set[bytes]:
        """The paths of the staged changes, but deletions."""
        output = self.run(
            "diff",
            "--cached",
            "--name-only",
            "-z",
            "--no-renames",
            "--no-relative",
            "--no-color",
            "--diff-filter=ACMRTUXB",
        )
        return {path for path in output.split(b"\0") if path}

    def objects(self, oids: list[str], check: bool = False) -> Iterator[tuple[str, str, bytes]]:
        """Each object's ID, type and content, read once with ``git cat-file``.

        With ``check``, the content is left empty and only the types are read
        (``--batch-check``).

        Raises:
            GitError: git stops before it has given an object whole, with an
                exit status other than 0, such as for a corrupt object.
            OmertaError: An object cannot be read otherwise, such as one that
                is missing.
        """
        # The IDs come from a file, which git reads at its pace: a pipe would need
        # a thread writing to it, which git can block for good.
        with tempfile.TemporaryFile() as ids, tempfile.TemporaryFile() as errors:
            ids.write("".join(f"{oid}\n" for oid in oids).encode("ascii"))
            ids.seek(0)
            # git by its absolute path, with arguments of its own and no shell
            process = subprocess.Popen(  # noqa: S603  # nosec B603
                [self.executable, "cat-file", "--batch-check" if check else "--batch"],
                cwd=self.top,
                env=self.env,
                stdin=ids,
                stdout=subprocess.PIPE,
                stderr=errors,
            )
            stdout = _output(process)
            try:
                for oid in oids:
                    line = stdout.readline()
                    header = line.split()
                    if len(header) != 3:
                        # `<oid> missing`, or nothing from a git that stopped
                        raise _unread(oid, process, errors, stopped=not line)
                    kind, size = header[1].decode("ascii"), int(header[2])
                    data = b"" if check else stdout.read(size + 1)
                    if not check and (len(data) != size + 1 or not data.endswith(b"\n")):
                        raise _unread(oid, process, errors, stopped=len(data) <= size)
                    yield oid, kind, data[:-1]
            finally:
                # Closed first: the git that a launcher starts (Git for Windows'
                # cmd\git.exe) outlives the launcher's kill, until it writes to
                # a pipe that nobody reads.
                stdout.close()
                process.kill()
                process.wait()


def _unread(
    oid: str, process: subprocess.Popen[bytes], errors: IO[bytes], *, stopped: bool
) -> OmertaError:
    """The error for an object that git did not give whole.

    Where git ``stopped`` (its output ended), it has exited or is exiting, and
    a failure is reported as git's own, with its error output.
    """
    if stopped:
        status = process.wait()
        if status:
            errors.seek(0)
            return GitError("cat-file", status, errors.read())
    return OmertaError(f"git object {oid} cannot be read")


def _output(process: subprocess.Popen[bytes]) -> IO[bytes]:
    """The output pipe of a process started with one."""
    assert process.stdout is not None  # noqa: S101 - started with stdout=PIPE
    return process.stdout


def _dotted(version: tuple[int, ...]) -> str:
    """A version as git writes it."""
    return ".".join(str(part) for part in version)
