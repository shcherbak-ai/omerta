"""``omerta check``: the index content of the staged changes, of given paths, or of every entry."""

from __future__ import annotations

import os

from omerta.errors import OmertaError
from omerta.git import GITLINK, Entry, Git
from omerta.output import Finding
from omerta.patterns import Pattern, is_env_file
from omerta.search import Target, line_of, search


def select(git: Git, index: list[Entry], paths: list[str], everything: bool) -> list[Entry]:
    """The index entries that ``check`` searches.

    A PATH selects the entry at that path, or with a folder the entries under
    it, matched exactly, as literal pathspecs are. The paths are matched here,
    not given to git, so that none goes on git's command line.

    Args:
        git: git, in the repository.
        index: Every index entry.
        paths: The PATH arguments, relative to the folder omerta runs in.
        everything: ``--all``: every entry.

    Raises:
        OmertaError: A path is outside the repository.
    """
    if everything:
        return index
    if paths:
        selected = [_from_root(git, path) for path in paths]
        return [
            entry
            for entry in index
            if any(
                not chosen or entry.path == chosen or entry.path.startswith(chosen + b"/")
                for chosen in selected
            )
        ]
    staged = git.staged_paths()
    return [entry for entry in index if entry.path in staged]


def _from_root(git: Git, path: str) -> bytes:
    """A PATH argument as an index path: from the root, with ``/``; empty for the root."""
    full = os.path.abspath(os.path.join(git.folder, path))
    try:
        relative = os.path.relpath(full, os.path.abspath(git.top))
    except ValueError:  # on another drive
        relative = os.pardir
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        raise OmertaError(f"{path} is outside the repository")
    return b"" if relative == os.curdir else os.fsencode(relative.replace(os.sep, "/"))


def run(
    git: Git,
    entries: list[Entry],
    targets: list[Target],
    patterns: tuple[Pattern, ...],
    findings: list[Finding],
) -> None:
    """Search the entries, adding what is found to ``findings``.

    An entry that matches the patterns is a tracked env file, and a path that
    holds a value is a finding of its own. A gitlink (a submodule) has no
    content to search. An unmerged entry is searched in each of its stages,
    and a value is reported at its first occurrence in the lowest stage that
    holds it.

    Raises:
        OmertaError: An entry's object cannot be read.
    """
    for path in sorted({entry.path for entry in entries}):
        for index, hit in search(path, targets).items():
            findings.append(_found(targets[index], hit.form, path=path, in_path=True))
    entries = sorted(
        (entry for entry in entries if entry.mode != GITLINK),
        key=lambda entry: (entry.path, entry.stage),
    )
    tracked: set[bytes] = set()
    for entry in entries:
        if entry.path not in tracked and is_env_file(entry.path, patterns):
            tracked.add(entry.path)
            findings.append(Finding(path=entry.path))
    if not targets:
        return
    by_oid: dict[str, list[Entry]] = {}
    for entry in entries:
        by_oid.setdefault(entry.oid, []).append(entry)
    found: dict[tuple[bytes, int], tuple[int, Finding]] = {}
    try:
        for oid, _, data in git.objects(list(by_oid)):
            hits = search(data, targets)
            for entry in by_oid[oid]:
                for index, hit in hits.items():
                    key = (entry.path, index)
                    if key in found and found[key][0] <= entry.stage:
                        continue
                    line = line_of(data, hit.position)
                    found[key] = (
                        entry.stage,
                        _found(targets[index], hit.form, path=entry.path, line=line),
                    )
    finally:
        # What was found before an error is reported with it.
        findings.extend(finding for _, finding in found.values())


def _found(
    target: Target, form: str, *, path: bytes, line: int | None = None, in_path: bool = False
) -> Finding:
    """A finding of a target in an entry: in its content at a line, or in its path."""
    return Finding(
        path=path,
        line=line,
        in_path=in_path,
        variable=target.variable,
        env_file=target.env_file,
        env_order=target.env_order,
        form=form,
    )
