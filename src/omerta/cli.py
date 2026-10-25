"""The command line: ``omerta check`` and ``omerta history``."""

from __future__ import annotations

import argparse
import errno
import os
import sys
import traceback
from pathlib import Path
from typing import TextIO

from omerta import __version__, check, config, envfiles, history
from omerta.errors import OmertaError
from omerta.git import WINDOWS, Git, GitError, environment, find_executable, find_repository
from omerta.output import Finding, json_line, sort_key, text_line
from omerta.search import Target, redact, targets

_ISSUES = "https://github.com/shcherbak-ai/omerta/issues"


def main(argv: list[str] | None = None) -> int:
    """Run omerta with the arguments of the command line, or ``argv``.

    Returns:
        The exit code: 0 when nothing is found, 1 when a value or a tracked env
        file is found, and 2 on an error, which wins over findings.
    """
    parser, check_parser = _parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "check" and args.all and args.paths:
            check_parser.error("PATH arguments cannot be given with --all")
    except SystemExit as exit_:
        return exit_.code if isinstance(exit_.code, int) else 0 if exit_.code is None else 2
    findings: list[Finding] = []
    searched: list[Target] = []

    def hide(data: bytes) -> bytes:
        """``data`` with each value of the env files replaced by its variable's name."""
        return redact(data, searched)

    try:
        _run(args, findings, searched)
        error = None
    except GitError as failure:
        error = failure.describe(hide)
    except OmertaError as failure:
        error = str(failure)
    except Exception as failure:  # noqa: BLE001 - reported without its text, which may hold a value
        frame = traceback.extract_tb(failure.__traceback__)[-1]
        error = (
            f"unexpected {type(failure).__name__} at {Path(frame.filename).name}:{frame.lineno}; "
            f"please report it at {_ISSUES}"
        )
    if not args.quiet:
        line = json_line if args.json else text_line
        ordered = sorted(findings, key=sort_key)
        _write(sys.stdout, "".join(line(each, hide) + "\n" for each in ordered))
    if error is not None:
        # A path or an env file's name in the message may hold a value too.
        message = hide(f"omerta: {error}\n".encode("utf-8", "surrogateescape"))
        _write(sys.stderr, message.decode("utf-8", "backslashreplace"))
        return 2
    return 1 if findings else 0


def _run(args: argparse.Namespace, findings: list[Finding], searched: list[Target]) -> None:
    """Run a command, adding what it finds to ``findings``, and its targets to ``searched``."""
    start = os.getcwd()
    folder = start
    for given in args.folders:
        folder = os.path.join(folder, given)
        if not os.path.isdir(folder):
            raise OmertaError(f"cannot change to {given}: no such folder")
    # Where its links lead, as git sees it: git's `--show-cdup` climbs from there.
    folder = os.path.realpath(folder)
    executable = find_executable(
        os.environ.get("PATH", ""), [start, folder], find_repository(folder)
    )
    git = Git(executable, folder, environment())
    git.open()
    if args.command == "history" and git.is_shallow():
        raise OmertaError(
            "this repository is a shallow clone, whose history stops at a cut: fetch all of it "
            "(in CI, check out with fetch-depth: 0)"
        )
    top = Path(git.top)
    settings = config.load(
        top,
        env_files=args.env_files,
        min_chars=args.min_chars,
        ignore=args.ignore,
        encoded=args.encoded,
    )
    index = git.index()
    sources = envfiles.find(top, settings.patterns, {entry.path for entry in index})
    searched.extend(targets(sources, settings))
    if args.command == "check":
        entries = check.select(git, index, args.paths, args.all)
        check.run(git, entries, searched, settings.patterns, findings)
    else:
        history.run(git, searched, settings.patterns, findings)


def _parser() -> tuple[argparse.ArgumentParser, argparse.ArgumentParser]:
    """The parser of the command line, and that of ``check``."""
    parser = argparse.ArgumentParser(
        prog="omerta",
        description="Fail when a value of your env files, e.g. an API key in .env, is in git.",
    )
    parser.add_argument(
        "-C",
        dest="folders",
        action="append",
        default=[],
        metavar="PATH",
        help="run as if started in PATH, as git -C does",
    )
    parser.add_argument("--version", action="version", version=f"omerta {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)
    options = argparse.ArgumentParser(add_help=False)
    options.add_argument(
        "--min-chars",
        type=_positive,
        metavar="N",
        help="the shortest value searched for, in characters (default: 8)",
    )
    options.add_argument(
        "--env-file",
        dest="env_files",
        action="append",
        metavar="PATTERN",
        help="an env file pattern, repeatable (default: .env and .env.*)",
    )
    options.add_argument(
        "--ignore",
        action="append",
        default=[],
        metavar="NAME",
        help="a variable not searched for, repeatable",
    )
    options.add_argument(
        "--encoded",
        action=argparse.BooleanOptionalAction,
        help="search the URL, JSON and base64 forms too (default), or the plain forms only",
    )
    options.add_argument("--json", action="store_true", help="findings as JSON Lines")
    options.add_argument(
        "-q", "--quiet", action="store_true", help="print no findings: the exit code says it"
    )
    found = commands.add_parser(
        "check",
        parents=[options],
        help="search the staged changes, PATHs, or every index entry",
        description=(
            "Search the index content of the staged changes, of the PATHs given, or with --all "
            "of every entry."
        ),
    )
    found.add_argument("paths", nargs="*", metavar="PATH", help="paths of index entries")
    found.add_argument("--all", action="store_true", help="search every index entry")
    commands.add_parser(
        "history",
        parents=[options],
        help="search every blob and message reachable from a ref",
        description=(
            "Search every blob reachable from a ref, and the messages of the commits and tags."
        ),
    )
    return parser, found


def _positive(text: str) -> int:
    """An integer of at least 1, from the command line."""
    try:
        number = int(text)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer of at least 1")
    return number


def _write(stream: TextIO, text: str) -> None:
    """Write text to a stream in UTF-8, with ``\\n`` line ends, whatever its encoding.

    A stream whose reader has gone (``omerta history | head``) takes nothing
    more: the rest is dropped, and so is what Python would flush into it at exit.
    """
    buffer = getattr(stream, "buffer", None)
    try:
        if buffer is None:
            stream.write(text)
            return
        stream.flush()
        buffer.write(text.encode("utf-8"))
        buffer.flush()
    except OSError as error:
        if not _reader_gone(error):
            raise
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, stream.fileno())
        os.close(devnull)


def _reader_gone(error: OSError) -> bool:
    """Whether a write failed because the reader of its pipe has gone.

    Other platforms raise :class:`BrokenPipeError`. Windows raises it too
    for a reader that goes while omerta writes, but ``EINVAL`` for one that
    went before the write.
    """
    return isinstance(error, BrokenPipeError) or (WINDOWS and error.errno == errno.EINVAL)
