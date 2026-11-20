"""``omerta docker``: the env files that a Dockerfile's build context holds.

Docker reads ``.dockerignore``, not ``.gitignore``: a build context is the
folder it is built from, less what its ignore file leaves out, and ``COPY . .``
copies the rest into the image, an env file that git ignores too.
"""

from __future__ import annotations

import codecs
import os
import re
from pathlib import Path

from omerta import envfiles
from omerta.config import Settings
from omerta.dockerignore import GO_SPACES, LONGEST_LINE, Ignore
from omerta.errors import OmertaError
from omerta.git import WINDOWS
from omerta.output import Finding, quote
from omerta.patterns import is_template


def is_dockerfile(name: str) -> bool:
    """Whether a file's name is that of a Dockerfile.

    ``Dockerfile``, ``Dockerfile.<name>`` and ``<name>.Dockerfile``, in any
    case, but for a Dockerfile's own ignore file (``Dockerfile.dockerignore``).
    """
    lower = name.lower()
    if lower.endswith(".dockerignore"):
        return False
    return lower == "dockerfile" or lower.startswith("dockerfile.") or lower.endswith(".dockerfile")


def builds(
    top: Path, env_files: list[str], configured: tuple[tuple[str, str], ...]
) -> list[tuple[str, str]]:
    """Each Dockerfile whose build context can hold an env file, with the folder it is built from.

    A Dockerfile is built from its own folder, as ``docker build .`` beside it
    builds it, unless the configuration names another, so that it can take only
    the env files in its folder or below it. Only the folders of the env files,
    and those above them, are listed for Dockerfiles: not the whole working
    tree, whose ignored folders, such as ``node_modules``, may hold many
    thousands of files and other projects' Dockerfiles. What git makes of a
    Dockerfile does not count, as Docker reads no ``.gitignore``. A file named
    like one is one where Docker could build it (:func:`_starts_a_build`); a
    file that the configuration names is one, whatever its name, its folder
    and its text.

    Args:
        top: The root of the repository.
        env_files: The paths of the env files, from the root with ``/``.
        configured: The ``docker-contexts`` setting: Dockerfiles, each with
            the folder it is built from, ``.`` for the root.

    Returns:
        The Dockerfiles and their folders, as paths from the root with ``/``,
        the root as an empty folder, in the order of the Dockerfiles.

    Raises:
        OmertaError: A folder cannot be listed, or the configuration names a
            file that is not in the working tree, or a file or a folder that
            is, but spelled otherwise.
    """
    folders = {
        "/".join(parts[:depth])
        for parts in (path.split("/") for path in env_files)
        for depth in range(len(parts))
    }
    found: dict[str, str] = {}
    for folder in sorted(folders):
        for name in envfiles.files(top, folder):
            path = f"{folder}/{name}" if folder else name
            if is_dockerfile(name) and _starts_a_build(_resolved(top, folder, name)):
                found[path] = folder
    for dockerfile, folder in configured:
        if not top.joinpath(*dockerfile.split("/")).is_file():
            name = quote(os.fsencode(dockerfile))
            raise OmertaError(f"docker-contexts names {name}, which is no file of the working tree")
        # A folder that is not there yet, which a build step makes, holds no env file.
        for path in (dockerfile, folder):
            if path != "." and top.joinpath(*path.split("/")).exists() and not _spelled(top, path):
                name = quote(os.fsencode(path))
                raise OmertaError(
                    f"docker-contexts names {name}, which the working tree spells otherwise "
                    "(in another case or Unicode form)"
                )
        found[dockerfile] = "" if folder == "." else folder
    return sorted(found.items())


# What BuildKit's parser splits an instruction's word off at: ASCII spaces only.
_WORD_END = re.compile(r"[\t\v\f\r ]+")


def _starts_a_build(path: Path | None) -> bool:
    """Whether a file named like a Dockerfile can be one: its first instruction is FROM or ARG.

    ``None``, a name that leads to no file where BuildKit reads it
    (:func:`_resolved`), is none. Docker refuses any other first instruction
    (``no build stage in current context``), an unknown word, or an empty
    file, so a note (``Dockerfile.md``) or an empty file is no build. Its
    lines are read as BuildKit's parser reads them: a line of 64 KiB or more
    ends the reading (``file with no instructions``), a byte order mark is
    taken off the first, Go's spaces (a no-break space too) are trimmed off
    each end, blank lines and comments, parser directives among them, come
    first, and the word ends at an ASCII space only (Docker refuses ``FROM\\``).
    A file that cannot be read is taken to be one, so that its build is still
    checked.
    """
    if path is None:
        return False
    try:
        with path.open("rb") as file:
            first = True
            while line := file.readline(LONGEST_LINE + 1):
                if len(line) > LONGEST_LINE and not line.endswith(b"\n"):
                    return False
                if first:
                    line, first = line.removeprefix(codecs.BOM_UTF8), False
                text = line.decode("utf-8", "surrogateescape").strip(GO_SPACES)
                if text and not text.startswith("#"):
                    return _WORD_END.split(text, maxsplit=1)[0].lower() in ("from", "arg")
    except OSError:
        return True
    return False


def _spelled(top: Path, path: str) -> bool:
    """Whether each name of a path from the root is spelled as its folder lists it.

    Windows and macOS open a path in another case too (``API`` for ``api``),
    and macOS in another Unicode form, while the env files are listed as they
    are spelled there: none would be found under the folder as configured.

    Raises:
        OmertaError: A folder on the way cannot be listed.
    """
    folder = top
    for part in path.split("/"):
        try:
            names = os.listdir(folder)
        except OSError as error:
            name = quote(os.fsencode(folder.relative_to(top).as_posix()))
            raise OmertaError(f"folder {name} cannot be listed ({error.strerror})") from None
        if part not in names:
            return False
        folder = folder / part
    return True


def run(top: Path, settings: Settings, findings: list[Finding]) -> None:
    """Add each env file of the working tree that a build context holds to ``findings``.

    Each env file, tracked or not, under the folder that a Dockerfile is built
    from, is a finding unless the build's ignore file leaves it out: the
    Dockerfile's own (``<Dockerfile>.dockerignore``, beside it) where there is
    one, otherwise the folder's ``.dockerignore``. A linked env file is judged
    by the file it leads to (:func:`_texts`), and a build's folder is taken
    where its links lead (:func:`_opened`). A build with no env file under its
    folder is left alone: its ignore file is not even read.

    Raises:
        OmertaError: A folder on the way to the env files or the Dockerfiles
            cannot be listed, the configuration names a file that is not in
            the working tree, or an ignore file cannot be read or matched as
            Docker matches it.
    """
    env_files = _texts(top, envfiles.every(top, settings.patterns))
    for dockerfile, folder in builds(top, env_files, settings.docker_contexts):
        context = _opened(top, folder)
        if context is None:
            continue
        inside = [(path, _under(path, context)) for path in env_files]
        held = [(path, relative) for path, relative in inside if relative is not None]
        if not held:
            continue
        ignore = _ignore(top, dockerfile, context)
        for path, relative in held:
            if not ignore.leaves_out(relative):
                findings.append(
                    Finding(
                        path=os.fsencode(path),
                        dockerfile=os.fsencode(dockerfile),
                        context=os.fsencode(folder or "."),
                    )
                )


def _texts(top: Path, env_files: list[str]) -> list[str]:
    """Where a build can take the text of each env file: the file, or the one its link leads to.

    Docker sends a symbolic link as a link, which an image then holds leading
    nowhere, and the file it leads to in its own right, under that file's path,
    where that is in the build context. So a linked env file is judged by that
    file, and reported at its path; one that leads out of the working tree, or
    to a template, holds nothing a build could take.

    Returns:
        The paths from the root with ``/``, each once, in the order of the env files.
    """
    real = Path(os.path.realpath(top))
    texts: dict[str, None] = {}
    for path in env_files:
        file = top.joinpath(*path.split("/"))
        if not file.is_symlink():
            texts[path] = None
            continue
        target = Path(os.path.realpath(file))
        if target.is_relative_to(real) and not is_template(target.name):
            texts[target.relative_to(real).as_posix()] = None
    return list(texts)


def _opened(top: Path, folder: str) -> str | None:
    """Where a build's folder is in the working tree, its links followed, as Docker opens it.

    The env files are found as their folders are on disk, links not followed,
    so a configured folder that is a link (``alias`` for ``real``) holds them
    at the path the link leads to.

    Returns:
        The path from the root with ``/``, empty for the root, or ``None``
        where the folder leads out of the working tree, where no env file of
        it is.
    """
    real = Path(os.path.realpath(top))
    opened = Path(os.path.realpath(top.joinpath(*folder.split("/")) if folder else top))
    if not opened.is_relative_to(real):
        return None
    relative = opened.relative_to(real).as_posix()
    return "" if relative == "." else relative


def _under(path: str, folder: str) -> str | None:
    """A path relative to a folder (empty for the root), or ``None`` where it is outside it."""
    if not folder:
        return path
    prefix = f"{folder}/"
    return path[len(prefix) :] if path.startswith(prefix) else None


def _ignore(top: Path, dockerfile: str, folder: str) -> Ignore:
    """The ignore file of a build: the Dockerfile's own where there is one, else its context's.

    The Dockerfile's own takes precedence even where it is empty, as Docker
    documents it. BuildKit reads each from the folder it is in, the
    Dockerfile's and the context's (:func:`_resolved`).
    """
    beside, _, name = dockerfile.rpartition("/")
    for root, file in ((beside, f"{name}.dockerignore"), (folder, ".dockerignore")):
        found = _resolved(top, root, file)
        if found is not None:
            quoted = quote(os.fsencode(f"{root}/{file}" if root else file))
            try:
                data = found.read_bytes()
            except OSError as error:
                raise OmertaError(f"{quoted} cannot be read ({error.strerror})") from None
            return Ignore(data, quoted)
    return Ignore(b"", ".dockerignore")


def _resolved(top: Path, folder: str, name: str) -> Path | None:
    """The file that a name in a folder of the working tree leads to, as BuildKit reads it there.

    BuildKit reads a Dockerfile and an ignore file from a copy of the folder
    they are in (:func:`_inside`).

    Raises:
        OmertaError: The name leads through a link made in WSL.
    """
    try:
        return _inside(top.joinpath(*folder.split("/")) if folder else top, name)
    except _WslLinkError:
        path = quote(os.fsencode(f"{folder}/{name}" if folder else name))
        raise OmertaError(
            f"{path} leads through a link made in WSL, which Docker's Windows client "
            "refuses to read: check it from WSL, or replace the link with the file"
        ) from None


# BuildKit gives up on a path after this many symbolic links (continuity's RootPath).
_LINKS = 255


def _inside(root: Path, name: str) -> Path | None:
    """The file that a name in a folder leads to, as BuildKit reads it there, or ``None``.

    BuildKit reads a Dockerfile or an ignore file from a copy of its folder,
    out of which no link leads: ``..`` stops at the folder, and an absolute
    target starts from it. So a link that leads out of the folder, or through
    more than 255 links, or to what is no file, leads to none, and for an
    ignore file the next one counts. Each name is one that its folder lists,
    spelled as there (:func:`_listed`). Docker's Windows client sends a target
    with ``/`` for ``\\`` and without ``\\\\?\\``, so that ``C:\\x`` arrives as
    ``C:/x``, whose ``C:`` is in no folder.
    """
    parts: list[str] = []
    pending = [name]
    links = 0
    while pending:
        part = pending.pop(0)
        if part == "..":
            parts = parts[:-1]
            continue
        if part in ("", "."):
            continue
        target = _link(root.joinpath(*parts, part)) if _listed(root, [*parts, part]) else None
        if target is None:
            parts.append(part)
            continue
        links += 1
        if links > _LINKS:
            return None
        if WINDOWS:
            target = target.removeprefix("\\\\?\\").replace("\\", "/")
        if target.startswith("/"):
            parts = []
        pending[:0] = target.split("/")
    if not _listed(root, parts):
        return None
    found = root.joinpath(*parts)
    return found if found.is_file() else None


def _listed(root: Path, parts: list[str]) -> bool:
    """Whether each name of a path from a folder is one that its folder lists, spelled as there.

    Docker's client sends each file under its name on disk, and BuildKit
    asks for the name it reads, ``.dockerignore`` or a link's target, byte for
    byte: on Windows and macOS, where ``.DOCKERIGNORE`` opens as
    ``.dockerignore``, it is no ignore file, as Docker 29.9 builds show. A name
    with a ``:``, which no Windows file has, is in no folder there, and is
    never joined, which would read it as a drive.
    """
    folder = root
    for part in parts:
        try:
            if part not in os.listdir(folder):
                return False
        except OSError:
            return False
        folder = folder / part
    return True


# The reparse tag of a link made in WSL on a Windows drive (IO_REPARSE_TAG_LX_SYMLINK).
_WSL_LINK = 0xA000001D


class _WslLinkError(Exception):
    """A link made in WSL, which Windows reads through but Docker's Windows client refuses."""


def _link(path: Path) -> str | None:
    """The target of a symbolic link, or ``None`` where the path is no link, or nothing.

    Raises:
        _WslLinkError: The path is a link made in WSL, whose target Python
            cannot read on Windows, while Windows opens the file it leads
            to, out of the folder too. Docker's Windows client fails the
            build on one ("invalid file request").
    """
    try:
        return os.readlink(path)
    except OSError:
        return None
    except ValueError:
        # Windows raises it for a reparse point that is neither a link nor a
        # junction: one made in WSL, or a file that OneDrive keeps, a file.
        if getattr(os.lstat(path), "st_reparse_tag", 0) == _WSL_LINK:
            raise _WslLinkError from None
        return None
