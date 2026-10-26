"""``omerta history``: every blob reachable from a ref, and the messages of commits and tags."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from conftest import GIT_VERSION, Repo
from omerta import history
from omerta.errors import OmertaError
from omerta.history import Change, parse_log, parse_objects

_KEY = "sk-test-0123456789abcdef"


@pytest.fixture
def project(repo: Repo) -> Repo:
    """A repository with an ignored .env that sets a key, and a first commit."""
    repo.write(".gitignore", ".env\n")
    repo.write(".env", f"API_KEY={_KEY}\n")
    repo.add(".gitignore")
    repo.commit("start")
    return repo


def _commit_file(repo: Repo, name: str, content: str, message: str = "change") -> str:
    repo.write(name, content)
    repo.add(name)
    return repo.commit(message)


def test_a_value_is_reported_where_it_enters_a_path_not_where_it_stays(project: Repo, omerta):
    added = _commit_file(project, "app.py", f"x = 1\nKEY = '{_KEY}'\n")
    _commit_file(project, "app.py", f"x = 2\nKEY = '{_KEY}'\n")
    _commit_file(project, "app.py", "x = 2\n")
    again = _commit_file(project, "app.py", f"KEY = '{_KEY}'\n")
    result = omerta("history", cwd=project.path)
    assert result.code == 1
    assert result.lines == [
        f"{again} app.py: API_KEY (from .env)",
        f"{added} app.py: API_KEY (from .env)",
    ]
    result.assert_silent_about(_KEY)


def test_a_moved_file_is_reported_again_at_its_new_path(project: Repo, omerta):
    added = _commit_file(project, "a.py", f"{_KEY}\n")
    project.git("mv", "a.py", "b.py")
    moved = project.commit("move")
    assert omerta("history", cwd=project.path).lines == [
        f"{added} a.py: API_KEY (from .env)",
        f"{moved} b.py: API_KEY (from .env)",
    ]


def test_a_value_that_a_merge_brings_in_is_reported_at_the_branch_commit(project: Repo, omerta):
    project.git("switch", "-q", "-c", "feature")
    branch = _commit_file(project, "feature.py", f"{_KEY}\n")
    project.git("switch", "-q", "main")
    _commit_file(project, "main.py", "x = 1\n")
    project.git("merge", "-q", "--no-ff", "-m", "merge", "feature")
    assert omerta("history", cwd=project.path).lines == [
        f"{branch} feature.py: API_KEY (from .env)"
    ]


def test_a_value_that_a_merge_writes_itself_is_reported_at_the_merge(project: Repo, omerta):
    _commit_file(project, "conf.py", "x = 1\n")
    project.git("switch", "-q", "-c", "feature")
    _commit_file(project, "conf.py", "x = 2\n")
    project.git("switch", "-q", "main")
    _commit_file(project, "conf.py", "x = 3\n")
    with pytest.raises(subprocess.CalledProcessError):
        project.git("merge", "-q", "feature")
    merge = _commit_file(project, "conf.py", f"x = '{_KEY}'\n", "resolve")
    assert omerta("history", cwd=project.path).lines == [f"{merge} conf.py: API_KEY (from .env)"]


def test_messages_of_commits_and_annotated_tags_are_searched(project: Repo, omerta):
    commit = project.commit(f"use the key\n\nIt is {_KEY}.")
    project.git("tag", "-a", "v1", "-m", f"release with {_KEY}")
    project.git("tag", "light")
    tag = project.oid("refs/tags/v1")
    assert omerta("history", cwd=project.path).lines == sorted(
        [f"{commit} (message): API_KEY (from .env)", f"{tag} (message): API_KEY (from .env)"]
    )
    records = [json.loads(line) for line in omerta("history", "--json", cwd=project.path).lines]
    by_id = {record.get("commit") or record.get("tag"): record for record in records}
    assert by_id[commit] == {
        "commit": commit,
        "message": True,
        "variable": "API_KEY",
        "env_file": ".env",
        "form": "plain",
    }
    assert by_id[tag]["tag"] == tag


def test_the_author_lines_of_a_commit_are_not_searched(project: Repo, omerta, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", f"{_KEY}@example.com")
    project.commit("an author")
    assert omerta("history", cwd=project.path).code == 0


def test_a_blob_that_no_commit_writes_is_reported_by_its_id(project: Repo, omerta):
    alone = project.blob(f"alone {_KEY}\n")
    inner = project.blob(f"inner {_KEY}\n")
    env = project.blob("X=1\n")
    tree = project.git(
        "mktree", input=f"100644 blob {inner}\tinner.txt\n100644 blob {env}\t.env\n".encode()
    ).strip()
    project.git("tag", "blob-tag", alone)
    project.git("tag", "tree-tag", tree)
    lines = omerta("history", cwd=project.path).lines
    assert lines == sorted(
        [f"{env} .env: tracked env file", f"{inner} inner.txt: API_KEY (from .env)"],
        key=lambda line: line.split(" ", 1)[1],
    ) + [f"{alone}: API_KEY (from .env)"]
    record = json.loads(omerta("history", "--json", cwd=project.path).lines[-1])
    assert record == {
        "path": None,
        "blob": alone,
        "variable": "API_KEY",
        "env_file": ".env",
        "form": "plain",
    }


def test_a_tracked_env_file_is_reported_at_the_commits_that_add_it(repo: Repo, omerta):
    added = _commit_file(repo, ".env.production", "URL=https://example.com\n")
    _commit_file(repo, ".env.production", "URL=https://example.org\n")
    repo.git("rm", "-q", ".env.production")
    repo.commit("remove")
    again = _commit_file(repo, ".env.production", "URL=https://example.net\n")
    _commit_file(repo, ".env.example", "URL=\n")
    result = omerta("history", cwd=repo.path)
    assert result.lines == [
        f"{again} .env.production: tracked env file",
        f"{added} .env.production: tracked env file",
    ]


def test_gitlinks_in_the_history_have_no_content_and_are_no_env_files(project: Repo, omerta):
    commit = project.oid("HEAD")
    # A submodule at a path that the env file patterns match: a folder, no file.
    project.index_info(("160000", commit, 0, b"vendor/lib"), ("160000", commit, 0, b".env.d"))
    project.commit("submodules")
    assert omerta("history", cwd=project.path).code == 0


def test_each_value_of_a_key_given_twice_is_reported_where_it_enters(project: Repo, omerta):
    (project.path / ".env").write_text(f"API_KEY={_KEY}\nAPI_KEY=sk-second-9876543210\n")
    first = _commit_file(project, "app.py", f"a = '{_KEY}'\n")
    second = _commit_file(project, "app.py", f"a = '{_KEY}'\nb = 'sk-second-9876543210'\n")
    assert omerta("history", cwd=project.path).lines == [
        f"{second} app.py: API_KEY (from .env)",
        f"{first} app.py: API_KEY (from .env)",
    ]


def test_a_value_in_a_path_is_reported_where_the_path_enters(project: Repo, omerta):
    (project.path / ".env").write_text(f"API_KEY={_KEY}\nBUCKET=acme-prod-data-7731\n")
    added = _commit_file(project, "infra/acme-prod-data-7731.json", "{}\n")
    _commit_file(project, "infra/acme-prod-data-7731.json", '{"x": 1}\n')
    blob = project.blob("y")
    tree = project.git("mktree", input=f"100644 blob {blob}\tacme-prod-data-7731.txt\n".encode())
    project.git("tag", "tree-tag", tree.strip())
    assert omerta("history", cwd=project.path).lines == [
        f"{blob} <BUCKET>.txt (path): BUCKET (from .env)",
        f"{added} infra/<BUCKET>.json (path): BUCKET (from .env)",
    ]


def test_an_older_git_gives_paths_without_nul_separators(
    project: Repo, omerta, monkeypatch: pytest.MonkeyPatch
):
    tree = project.git(
        "mktree", input=f"100644 blob {project.blob(_KEY)}\tdeep name.txt\n".encode()
    ).strip()
    project.git("tag", "tree-tag", tree)
    monkeypatch.setattr(history, "REV_LIST_Z_VERSION", (999,))
    assert (
        omerta("history", cwd=project.path).lines[0].endswith(" deep name.txt: API_KEY (from .env)")
    )


def test_a_shallow_clone_is_an_error(project: Repo, omerta, tmp_path: Path):
    _commit_file(project, "a.py", "x = 1\n")
    clone = tmp_path / "shallow"
    project.git("clone", "-q", "--depth", "1", project.path.as_uri(), str(clone))
    result = omerta("history", cwd=clone)
    assert result.code == 2
    assert "shallow clone" in result.err
    assert "fetch-depth: 0" in result.err


def test_a_partial_clone_fails_at_the_first_blob_never_fetched(
    project: Repo, omerta, tmp_path: Path
):
    project.git("config", "uploadpack.allowFilter", "true")
    _commit_file(project, "a.py", f"{_KEY}\n")
    project.git("rm", "-q", "a.py")
    project.commit("remove")
    clone = tmp_path / "partial"
    project.git("clone", "-q", "--filter=blob:none", project.path.as_uri(), str(clone))
    (clone / ".env").write_text(f"API_KEY={_KEY}\n", encoding="utf-8")
    result = omerta("history", cwd=clone)
    assert result.code == 2
    # Before 2.45, git would fetch what it reads: omerta does not run it here.
    assert ("partial clone" if GIT_VERSION < (2, 45) else "missing blob object") in result.err
    result.assert_silent_about(_KEY)


def test_without_sources_only_tracked_env_files_are_found(repo: Repo, omerta):
    added = _commit_file(repo, ".env", f"API_KEY={_KEY}\n")
    _commit_file(repo, "app.py", f"{_KEY}\n")
    result = omerta("history", "--json", cwd=repo.path)
    assert [json.loads(line) for line in result.lines] == [
        {"path": ".env", "commit": added, "tracked_env_file": True}
    ]


def test_the_log_is_read_with_merges_in_the_combined_format():
    commit, merge = "a" * 40, "b" * 40
    one, two, new, null = "1" * 40, "2" * 40, "3" * 40, "0" * 40
    output = (
        f"{merge}\0\0::100644 100644 100644 {one} {two} {new} MM\0conf.py\0"
        f"{commit}\0\n:000000 100644 {null} {one} A\0new\nline.py\0"
        f":100644 000000 {one} {null} D\0gone.py\0"
    ).encode()
    assert parse_log(output) == [
        Change(0, merge, b"conf.py", "100644", new, (one, two)),
        Change(1, commit, b"new\nline.py", "100644", one, (null,)),
        Change(1, commit, b"gone.py", "000000", null, (one,)),
    ]
    assert parse_log(b"") == []


@pytest.mark.parametrize(
    "output",
    [
        b"not-a-commit\0",
        b":100644 100644 " + b"1" * 40 + b" " + b"2" * 40 + b" M\0a.py\0",
        b"a" * 40 + b"\0:100644 100644 " + b"1" * 40 + b" M\0a.py\0",
        b"a" * 40 + b"\0:100644 100644 " + b"1" * 40 + b" " + b"2" * 40 + b" M",
    ],
    ids=["not a commit", "a change before any commit", "a field missing", "no path"],
)
def test_log_output_in_another_format_is_an_error(output: bytes):
    with pytest.raises(OmertaError, match="the output of git log cannot be read"):
        parse_log(output)


def test_objects_are_read_with_their_paths_in_both_formats():
    blob, tree, tag = "1" * 40, "2" * 40, "3" * 40
    assert parse_objects(f"{tag}\0path=v1\0{tree}\0{blob}\0path=a b\nc\0".encode(), True) == [
        (tag, b"v1"),
        (tree, None),
        (blob, b"a b\nc"),
    ]
    assert parse_objects(f"{tag} v1\n{tree} \n{blob} a b\n".encode(), False) == [
        (tag, b"v1"),
        (tree, None),
        (blob, b"a b"),
    ]


# A model of `history`, written from its rules rather than from its code, and
# random histories to hold it against: merges of two and three parents, new
# roots, a version taken from a parent or written anew, a mode or a type that
# changes alone, a tracked env file, and a path that holds a value. KEY's
# second value holds its first, so a version can hold the first alone or both.
_ALPHA = "sk-alpha-0123456789"
_MODEL_VALUES = {"KEY": (_ALPHA, f"{_ALPHA}-rotated"), "ORG": ("org-beta-9876543210",)}
_CONTENTS = (
    b"no value here\n",
    b"nor here\n",
    f"key = {_ALPHA}\n".encode(),
    f"key = {_ALPHA}-rotated\n".encode(),
    f"org-beta-9876543210 {_ALPHA}\n".encode(),
    f"org-beta-9876543210 {_ALPHA}-rotated\n".encode(),
    b"org = org-beta-9876543210\n",
)
_MODES = ("100644", "100755", "120000")
_MODEL_PATHS = (".env.prod", "a.txt", "b.txt", "d/c.txt", "d/e/f.txt", f"keys/{_ALPHA}.txt")

Version = tuple[str, bytes] | None
"""A path's mode and content in a commit, or ``None`` where the commit has none."""

Commit = tuple[list[int], dict[str, Version]]
"""A commit's parents, by their places in the history, and its tree."""


@st.composite
def _histories(draw: st.DrawFn) -> list[Commit]:
    """Commits, oldest first, each with parents among the commits before it."""
    commits: list[Commit] = []
    for number in range(draw(st.integers(1, 8))):
        parents = []
        # After the first, one commit in six starts a new root; of the others,
        # half are merges, of two parents or of three.
        if number and draw(st.integers(0, 5)):
            count = min(number, draw(st.sampled_from([1, 1, 2, 3])))
            parents = draw(st.permutations(range(number)))[:count]
        written = st.tuples(st.sampled_from(_MODES), st.sampled_from(_CONTENTS))
        tree = {}
        for path in _MODEL_PATHS:
            versions = [commits[parent][1].get(path) for parent in parents]
            kept = st.sampled_from(versions or [None])
            # A parent's content in any mode: at a merge, a version that
            # differs from every parent and holds what one of them holds.
            contents = [version[1] for version in versions if version is not None]
            retyped = st.tuples(st.sampled_from(_MODES), st.sampled_from(contents or _CONTENTS))
            if version := draw(st.one_of(kept, kept, written, retyped, st.none())):
                tree[path] = version
        commits.append((parents, tree))
    return commits


def _fast_import(commits: list[Commit]) -> bytes:
    """The stream that ``git fast-import`` builds the history from, a branch for each commit."""

    def data(content: bytes) -> bytes:
        return b"data %d\n%s\n" % (len(content), content)

    stream = b""
    for number, (parents, tree) in enumerate(commits):
        stream += f"commit refs/heads/c{number}\nmark :{number + 1}\n".encode()
        stream += f"committer Test <test@example.com> {1_700_000_000 + number} +0000\n".encode()
        stream += data(f"commit {number}".encode())
        if parents:
            stream += f"from :{parents[0] + 1}\n".encode()
        stream += "".join(f"merge :{parent + 1}\n" for parent in parents[1:]).encode()
        stream += b"deleteall\n"
        for path, (mode, content) in tree.items():
            stream += f"M {mode} inline {path}\n".encode() + data(content)
    return stream


def _held(version: Version) -> set[tuple[str, str]]:
    """The values that a version holds, each with its variable."""
    if version is None:
        return set()
    return {
        (variable, value)
        for variable, values in _MODEL_VALUES.items()
        for value in values
        if value.encode() in version[1]
    }


def _model(commits: list[Commit], ids: list[str]) -> list[dict]:
    """The findings of ``history --json``, by its rules, in their order."""
    found = []
    for number, (parents, tree) in enumerate(commits):
        for path, version in tree.items():
            replaced = [commits[parent][1].get(path) for parent in parents]
            # git log lists a path where the commit differs from every parent.
            if version in replaced:
                continue
            place = {"path": path.replace(_ALPHA, "<KEY>"), "commit": ids[number]}
            source = {"env_file": ".env", "form": "plain"}
            if all(other is None for other in replaced):
                if path == ".env.prod":
                    found.append(((path, 0, "", -number), {**place, "tracked_env_file": True}))
                if _ALPHA in path:
                    record = {**place, "in_path": True, "variable": "KEY", **source}
                    found.append(((path, 1, "KEY", -number), record))
            before = set().union(*map(_held, replaced))
            for variable in sorted({variable for variable, _ in _held(version) - before}):
                record = {**place, "variable": variable, **source}
                found.append(((path, 2, variable, -number), record))
    return [record for _, record in sorted(found, key=lambda each: each[0])]


def _note_cases(commits: list[Commit]) -> None:
    """Count the rare cases of an example, which ``--hypothesis-show-statistics`` shows."""
    for parents, tree in commits:
        event(f"{len(parents)} parents")
        for path, version in tree.items():
            replaced = [commits[parent][1].get(path) for parent in parents]
            if version in replaced:
                continue
            if any(other is not None and other[1] == version[1] for other in replaced):
                event("a mode or a type changes alone")
            if len(parents) > 1:
                if _held(version) - set().union(*map(_held, replaced)):
                    event("a value enters at a merge")
                if _held(version) & set().union(*map(_held, replaced[1:])):
                    event("a merge writes a value that a parent other than the first holds")


@settings(
    max_examples=40,
    # The omerta fixture changes to the folder and empties its capture at each
    # run, so the examples share nothing through it.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(commits=_histories())
def test_history_reports_what_a_model_of_its_rules_reports(
    tmp_path_factory: pytest.TempPathFactory, omerta, commits: list[Commit]
):
    folder = tmp_path_factory.mktemp("history")
    subprocess.run(["git", "init", "-q", str(folder)], check=True, capture_output=True)
    (folder / ".env").write_text(
        "".join(
            f"{variable}={value}\n"
            for variable, values in _MODEL_VALUES.items()
            for value in values
        ),
        encoding="utf-8",
    )
    marks = folder / ".git" / "marks"
    subprocess.run(
        ["git", "fast-import", "--quiet", f"--export-marks={marks}"],
        cwd=folder,
        input=_fast_import(commits),
        check=True,
        capture_output=True,
    )
    oids = dict(line.split() for line in marks.read_text().splitlines())
    expected = _model(commits, [oids[f":{number + 1}"] for number in range(len(commits))])
    _note_cases(commits)
    result = omerta("history", "--json", cwd=folder)
    assert [json.loads(line) for line in result.lines] == expected
    assert result.code == (1 if expected else 0)
