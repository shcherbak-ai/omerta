"""``omerta history``: every blob reachable from a ref, and the messages of the commits and tags."""

from __future__ import annotations

import re
from dataclasses import dataclass

from omerta.errors import OmertaError
from omerta.git import GITLINK, REV_LIST_Z_VERSION, Git, is_null
from omerta.output import Finding
from omerta.patterns import Pattern, is_env_file
from omerta.search import Matcher, Occurrence, Target, first

_HASH = re.compile(rb"[0-9a-f]{40}|[0-9a-f]{64}")


@dataclass(frozen=True)
class Change:
    """A path that a commit writes, from ``git log --raw``."""

    order: int
    """The commit's place in the order of ``git log``, newest first."""

    commit: str
    path: bytes
    mode: str
    oid: str
    replaced: tuple[str, ...]
    """The version the path had in each parent: the null ID where it had none."""


def parse_log(output: bytes) -> list[Change]:
    """The changes of ``git log --raw -z --format=%H``, merges in the combined format.

    Raises:
        OmertaError: The output is not in that format.
    """
    tokens = output.split(b"\0")
    commits: list[str] = []
    changes = []
    position = 0
    while position < len(tokens):
        token = tokens[position].lstrip(b"\n")
        position += 1
        if not token:
            continue
        if not token.startswith(b":"):
            if not _HASH.fullmatch(token):
                raise OmertaError("the output of git log cannot be read")
            commits.append(token.decode("ascii"))
            continue
        # `:mode mode oid oid status` for a commit with one parent; a merge has
        # a colon, a mode and an ID for each parent.
        parents = len(token) - len(token.lstrip(b":"))
        fields = token[parents:].decode("ascii").split(" ")
        if not commits or position >= len(tokens) or len(fields) != 2 * parents + 3:
            raise OmertaError("the output of git log cannot be read")
        path = tokens[position]
        position += 1
        oids = fields[parents + 1 : 2 * parents + 2]
        changes.append(
            Change(
                order=len(commits) - 1,
                commit=commits[-1],
                path=path,
                mode=fields[parents],
                oid=oids[-1],
                replaced=tuple(oids[:-1]),
            )
        )
    return changes


def parse_objects(output: bytes, nul: bool) -> list[tuple[str, bytes | None]]:
    """The objects of ``git rev-list --objects``, each with its path, if it has one."""
    objects: list[tuple[str, bytes | None]] = []
    if nul:
        for token in output.split(b"\0"):
            if token.startswith(b"path="):
                objects[-1] = (objects[-1][0], token[len(b"path=") :])
            elif token:
                objects.append((token.decode("ascii"), None))
        return objects
    for line in output.split(b"\n"):
        if line:
            oid, _, path = line.partition(b" ")
            objects.append((oid.decode("ascii"), path or None))
    return objects


def run(git: Git, targets: list[Target], patterns: tuple[Pattern, ...], findings: list[Finding]):
    """Search the history, adding what is found to ``findings``.

    A value is reported where it enters a path: at a commit whose new version
    of the file holds it while the version it replaces does not. A blob that no
    commit writes is reported with its ID, and a tracked env file at the
    commits that add its path.

    Raises:
        OmertaError: An object cannot be read, or git's output is not as expected.
    """
    nul = git.version >= REV_LIST_Z_VERSION
    listing = git.run("rev-list", "--objects", "--all", *(["-z"] if nul else []))
    objects = parse_objects(listing, nul)
    kinds = {oid: kind for oid, kind, _ in git.objects([oid for oid, _ in objects], check=True)}
    changes = parse_log(
        git.run(
            "-c",
            "log.showSignature=false",
            "log",
            "--all",
            "--root",
            "--no-abbrev",
            "--raw",
            "-z",
            "--no-renames",
            "--no-color",
            "--diff-merges=combined",
            "--format=%H",
        )
    )
    matcher = Matcher(targets)
    # For each blob with a value: for each target, where each of its values is.
    hits: dict[str, dict[int, dict[int, Occurrence]]] = {}
    if targets:
        wanted = [oid for oid, _ in objects if kinds[oid] in ("blob", "commit", "tag")]
        for oid, kind, data in git.objects(wanted):
            if kind == "blob":
                if found := matcher.occurrences(data):
                    hits[oid] = found
                continue
            message = data.partition(b"\n\n")[2]
            for index, hit in matcher.search(message).items():
                findings.append(
                    _found(
                        targets[index],
                        hit.form,
                        commit=oid if kind == "commit" else None,
                        tag=oid if kind == "tag" else None,
                        message=True,
                    )
                )
    written = set()
    for change in changes:
        written.add(change.oid)
        if is_null(change.oid):
            continue
        added = all(is_null(oid) for oid in change.replaced)
        if added:
            _search_path(
                matcher, targets, findings, change.path, commit=change.commit, order=change.order
            )
        if change.mode == GITLINK:
            continue
        if added and is_env_file(change.path, patterns):
            findings.append(Finding(path=change.path, commit=change.commit, order=change.order))
        for index, values in hits.get(change.oid, {}).items():
            before = [hits.get(oid, {}).get(index, {}) for oid in change.replaced]
            # A value enters where no version that this one replaces holds it.
            entering = [
                found
                for value, found in values.items()
                if not any(value in replaced for replaced in before)
            ]
            if entering:
                findings.append(
                    _found(
                        targets[index],
                        min(entering)[2],
                        path=change.path,
                        commit=change.commit,
                        order=change.order,
                    )
                )
    for oid, path in objects:
        if kinds[oid] != "blob" or oid in written:
            continue
        if path is not None:
            _search_path(matcher, targets, findings, path, blob=oid)
            if is_env_file(path, patterns):
                findings.append(Finding(path=path, blob=oid))
        for index, values in hits.get(oid, {}).items():
            findings.append(_found(targets[index], first(values).form, path=path, blob=oid))


def _search_path(
    matcher: Matcher,
    targets: list[Target],
    findings: list[Finding],
    path: bytes,
    *,
    commit: str | None = None,
    blob: str | None = None,
    order: int = 0,
) -> None:
    """Add a finding for each target whose value is in a path."""
    for index, hit in matcher.search(path).items():
        findings.append(
            _found(
                targets[index],
                hit.form,
                path=path,
                commit=commit,
                blob=blob,
                in_path=True,
                order=order,
            )
        )


def _found(
    target: Target,
    form: str,
    *,
    path: bytes | None = None,
    commit: str | None = None,
    blob: str | None = None,
    tag: str | None = None,
    message: bool = False,
    in_path: bool = False,
    order: int = 0,
) -> Finding:
    """A finding of a target, at a place."""
    return Finding(
        path=path,
        commit=commit,
        blob=blob,
        tag=tag,
        message=message,
        in_path=in_path,
        variable=target.variable,
        env_file=target.env_file,
        env_order=target.env_order,
        form=form,
        order=order,
    )
