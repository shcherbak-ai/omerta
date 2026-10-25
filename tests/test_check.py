"""``omerta check``: the index content of the staged changes, of PATHs, or of every entry."""

from __future__ import annotations

import base64
import json
import os
import urllib.parse

import pytest

from conftest import Repo

_KEY = "sk-test-0123456789abcdef"
_URL = "https://res.example.com/"
_PEM = "-----BEGIN KEY-----\nMIIEvQIBADANBg\n-----END KEY-----"
# Bytes whose base64 has `+` and `/` in the standard alphabet, `-` and `_` in the URL-safe one.
_TOKEN = ">>>???>>>???>>>???"


@pytest.fixture
def project(repo: Repo) -> Repo:
    """A repository with an ignored .env that sets a key, a URL, a multi-line value and a token."""
    repo.write(".gitignore", ".env\n.env.local\n")
    repo.write(
        ".env",
        f'API_KEY={_KEY}\nBASE_URL={_URL}\nPEM="{_PEM}"\nTOKEN="{_TOKEN}"\nDEBUG=true\n',
    )
    repo.add(".gitignore")
    return repo


def test_a_staged_value_is_found_at_its_line_and_never_shown(project: Repo, omerta):
    project.write("app.py", f"import os\n\nKEY = '{_KEY}'\n")
    project.add("app.py")
    result = omerta("check", cwd=project.path)
    assert result.code == 1
    assert result.lines == ["app.py:3: API_KEY (from .env)"]
    assert result.err == ""
    result.assert_silent_about(_KEY, _URL, _PEM)


def test_nothing_staged_with_a_value_passes(project: Repo, omerta):
    project.write("app.py", "print('hello')\n")
    project.add("app.py")
    result = omerta("check", cwd=project.path)
    assert (result.code, result.out, result.err) == (0, "", "")


def test_the_index_content_is_searched_not_the_working_tree(project: Repo, omerta):
    project.write("staged.py", f"KEY = '{_KEY}'\n")
    project.add("staged.py")
    project.write("staged.py", "KEY = os.environ['API_KEY']\n")
    project.write("unstaged.py", f"KEY = '{_KEY}'\n")
    result = omerta("check", cwd=project.path)
    assert result.lines == ["staged.py:1: API_KEY (from .env)"]


def test_only_the_staged_changes_are_searched_after_the_first_commit(project: Repo, omerta):
    project.write("old.py", f"KEY = '{_KEY}'\n")
    project.add("old.py")
    project.commit("old")
    project.write("new.py", "x = 1\n")
    project.add("new.py")
    assert omerta("check", cwd=project.path).code == 0
    assert omerta("check", "--all", cwd=project.path).lines == ["old.py:1: API_KEY (from .env)"]
    assert omerta("check", "old.py", cwd=project.path).lines == ["old.py:1: API_KEY (from .env)"]


def test_a_staged_deletion_is_not_searched(project: Repo, omerta):
    project.write("old.py", f"KEY = '{_KEY}'\n")
    project.add("old.py")
    project.commit("old")
    project.git("rm", "-q", "--cached", "old.py")
    assert omerta("check", cwd=project.path).code == 0


def test_paths_are_relative_to_the_folder_and_select_index_entries(project: Repo, omerta):
    for name in ("src/a.py", "src/b/c.py", "docs/d.md"):
        project.write(name, f"{_KEY}\n")
        project.add(name)
    project.write("src/untracked.py", f"{_KEY}\n")
    project.commit("files")
    result = omerta("check", "b", "untracked.py", "missing.py", cwd=project.path / "src")
    assert result.lines == ["src/b/c.py:1: API_KEY (from .env)"]
    result = omerta("check", ".", cwd=project.path / "src")
    assert result.lines == [
        "src/a.py:1: API_KEY (from .env)",
        "src/b/c.py:1: API_KEY (from .env)",
    ]
    # Literal pathspecs: a star is a character of a name.
    assert omerta("check", "src/*.py", cwd=project.path).code == 0
    assert omerta("-C", "src", "check", "a.py", cwd=project.path).lines == [
        "src/a.py:1: API_KEY (from .env)"
    ]


def test_a_path_outside_the_repository_is_an_error(project: Repo, omerta):
    result = omerta("check", "../elsewhere.py", cwd=project.path)
    assert result.code == 2
    assert result.err == "omerta: ../elsewhere.py is outside the repository\n"


def test_gitlinks_are_skipped_and_links_searched_as_their_target_text(project: Repo, omerta):
    commit = project.commit("for the gitlink")
    target = project.blob(f"../secrets/{_KEY}")
    project.index_info(("160000", commit, 0, b"vendor/lib"), ("120000", target, 0, b"link"))
    result = omerta("check", "--all", cwd=project.path)
    assert result.lines == ["link:1: API_KEY (from .env)"]


def test_an_unmerged_entry_is_reported_once_at_the_lowest_stage_holding_the_value(
    project: Repo, omerta
):
    base = project.blob("x = 1\n")
    ours = project.blob(f"x = 1\ny = 2\nKEY = '{_KEY}'\n")
    theirs = project.blob(f"KEY = '{_KEY}'\n")
    project.index_info(
        ("100644", base, 1, b"conflict.py"),
        ("100644", ours, 2, b"conflict.py"),
        ("100644", theirs, 3, b"conflict.py"),
    )
    result = omerta("check", cwd=project.path)
    assert result.lines == ["conflict.py:3: API_KEY (from .env)"]


def test_a_tracked_env_file_is_a_leak_even_without_any_source(repo: Repo, omerta):
    repo.write(".env.production", "PUBLIC_URL=https://example.com\n")
    repo.write(".env.example", "API_KEY=\n")
    repo.write("config/.env", "X=1\n")
    repo.add(".env.production", ".env.example", "config/.env")
    result = omerta("check", cwd=repo.path)
    assert result.code == 1
    assert result.lines == [".env.production: tracked env file"]
    assert omerta("check", "--env-file", "config/.env", cwd=repo.path).lines == [
        "config/.env: tracked env file"
    ]


def test_a_tracked_env_file_is_no_source_of_values(repo: Repo, omerta):
    repo.write(".env", f"API_KEY={_KEY}\n")
    repo.write("app.py", f"{_KEY}\n")
    repo.add(".env", "app.py")
    assert omerta("check", "--json", cwd=repo.path).lines == [
        '{"path": ".env", "tracked_env_file": true}'
    ]


def _crlf(text: str) -> str:
    return text.replace("\n", "\r\n")


@pytest.mark.parametrize(
    ("content", "variable", "form"),
    [
        (f"a\nb\nKEY={_KEY}\n", "API_KEY", "plain"),
        ("a\nb\nhttps://res.example.com?api-version=1\n", "BASE_URL", "plain"),
        (f"a\nb\n{_crlf(_PEM)}\n", "PEM", "plain"),
        ("a\nb\nurl=https%3A%2F%2Fres.example.com%2F\n", "BASE_URL", "url"),
        ("a\nb\nq=https%3a%2f%2fres.example.com\n", "BASE_URL", "url"),
        ('a\nb\n{"url": "https:\\/\\/res.example.com\\/"}\n', "BASE_URL", "json"),
        (f"a\nb\n{json.dumps({'pem': _PEM})}\n", "PEM", "json"),
        (
            f"a\nb\nAuthorization: Basic {base64.b64encode(f'user:{_KEY}'.encode()).decode()}\n",
            "API_KEY",
            "base64",
        ),
        (
            f"a\nb\ntoken={base64.urlsafe_b64encode(f'?{_TOKEN}>>'.encode()).decode()}\n",
            "TOKEN",
            "base64",
        ),
        (f"a\nb\n{base64.b64encode(f'xy{_KEY}'.encode()).decode()}\n", "API_KEY", "base64"),
    ],
    ids=[
        "plain",
        "url without its slash",
        "multi-line with crlf",
        "url escaped",
        "url escaped lowercase",
        "json with escaped slashes",
        "json with escaped newlines",
        "base64",
        "base64 url-safe",
        "base64 after two bytes",
    ],
)
def test_each_form_is_found_at_its_line_with_its_kind(
    project: Repo, omerta, content: str, variable: str, form: str
):
    project.write("found.txt", content)
    project.add("found.txt")
    result = omerta("check", "--json", cwd=project.path)
    assert [json.loads(line) for line in result.lines] == [
        {"path": "found.txt", "line": 3, "variable": variable, "env_file": ".env", "form": form}
    ]
    result.assert_silent_about(_KEY, _URL, _PEM, _TOKEN, urllib.parse.quote(_URL, safe=""))


def test_a_url_safe_base64_form_is_found_where_no_standard_one_is(project: Repo, omerta):
    encoded = base64.urlsafe_b64encode(f"?{_TOKEN}>>".encode()).decode()
    assert "+" not in encoded and "/" not in encoded and ("-" in encoded or "_" in encoded)
    project.write("token.txt", encoded)
    project.add("token.txt")
    assert omerta("check", cwd=project.path).lines == ["token.txt:1: TOKEN (from .env)"]
    assert omerta("check", "--no-encoded", cwd=project.path).code == 0


def test_no_encoded_searches_the_plain_forms_only(project: Repo, omerta):
    project.write("b64.txt", base64.b64encode(f"user:{_KEY}".encode()).decode())
    project.add("b64.txt")
    assert omerta("check", cwd=project.path).code == 1
    assert omerta("check", "--no-encoded", cwd=project.path).code == 0


def test_binary_content_is_searched(project: Repo, omerta):
    project.write("data.bin", b"\x00\xff\n\x00" + _KEY.encode() + b"\x00")
    project.add("data.bin")
    assert omerta("check", cwd=project.path).lines == ["data.bin:2: API_KEY (from .env)"]


def test_options_choose_the_env_files_the_values_and_the_output(project: Repo, omerta):
    project.write(".env.local", "SHORT=abc12\nAPI_KEY=sk-local-0123456789\n")
    project.write("app.py", f"{_KEY}\nsk-local-0123456789\nabc12\nhttps://res.example.com\n")
    project.add("app.py")
    assert omerta("check", cwd=project.path).lines == [
        "app.py:1: API_KEY (from .env)",
        "app.py:2: API_KEY (from .env.local)",
        "app.py:4: BASE_URL (from .env)",
    ]
    assert omerta("check", "--min-chars", "5", "--ignore", "BASE_URL", cwd=project.path).lines == [
        "app.py:1: API_KEY (from .env)",
        "app.py:2: API_KEY (from .env.local)",
        "app.py:3: SHORT (from .env.local)",
    ]
    assert omerta("check", "--env-file", ".env.local", cwd=project.path).lines == [
        "app.py:2: API_KEY (from .env.local)"
    ]
    quiet = omerta("check", "-q", cwd=project.path)
    assert (quiet.code, quiet.out) == (1, "")


def test_the_configuration_file_sets_what_options_do(project: Repo, omerta):
    project.write("app.py", f"{_KEY}\nhttps://res.example.com\n")
    project.add("app.py")
    project.write(".omerta.toml", 'ignore = ["API_KEY"]\n')
    assert omerta("check", cwd=project.path).lines == ["app.py:2: BASE_URL (from .env)"]
    project.write(".omerta.toml", "min-chars = 0\n")
    result = omerta("check", cwd=project.path)
    assert result.code == 2
    assert result.err == "omerta: .omerta.toml: min-chars must be an integer of at least 1\n"


def test_the_index_of_git_index_file_is_searched(
    project: Repo, omerta, monkeypatch: pytest.MonkeyPatch
):
    project.write("app.py", f"{_KEY}\n")
    alternate = project.path / ".git" / "alternate-index"
    env = {**os.environ, "GIT_INDEX_FILE": str(alternate)}
    import subprocess

    subprocess.run(["git", "add", "app.py"], cwd=project.path, env=env, check=True)
    (project.path / "sub").mkdir()
    assert omerta("check", cwd=project.path).code == 0
    # git reads a relative one from the root, wherever it is run.
    monkeypatch.setenv("GIT_INDEX_FILE", ".git/alternate-index")
    for folder in (project.path, project.path / "sub"):
        assert omerta("check", cwd=folder).lines == ["app.py:1: API_KEY (from .env)"]
        assert omerta("check", "--all", cwd=folder).lines == ["app.py:1: API_KEY (from .env)"]
    monkeypatch.setenv("GIT_INDEX_FILE", ".git/missing-index")
    result = omerta("check", cwd=project.path / "sub")
    assert result.code == 2
    assert "GIT_INDEX_FILE names .git/missing-index" in result.err


def test_a_value_in_a_path_is_found_and_never_shown(project: Repo, omerta):
    project.write(".env.local", "BUCKET=acme-prod-data-7731\n")
    project.write("infra/acme-prod-data-7731.json", f'{{"key": "{_KEY}"}}\n')
    project.write("infra/acme-prod-data-7731/notes.md", "nothing\n")
    project.add("infra")
    result = omerta("check", cwd=project.path)
    assert result.lines == [
        "infra/<BUCKET>.json (path): BUCKET (from .env.local)",
        "infra/<BUCKET>.json:1: API_KEY (from .env)",
        "infra/<BUCKET>/notes.md (path): BUCKET (from .env.local)",
    ]
    assert json.loads(omerta("check", "--json", cwd=project.path).lines[0]) == {
        "path": "infra/<BUCKET>.json",
        "in_path": True,
        "variable": "BUCKET",
        "env_file": ".env.local",
        "form": "plain",
    }
    # A PATH that holds a value never reaches git's command line, and the
    # error that names it shows the variable instead.
    outside = omerta("check", "../acme-prod-data-7731.json", cwd=project.path)
    assert outside.err == "omerta: ../<BUCKET>.json is outside the repository\n"


def test_an_env_file_named_after_a_value_is_shown_with_its_variable(repo: Repo, omerta):
    repo.write(".env.acme-prod-data-7731", "BUCKET=acme-prod-data-7731\nKEY=sk-test-abcdef0123\n")
    repo.write("app.py", "sk-test-abcdef0123\n")
    repo.add("app.py")
    assert omerta("check", cwd=repo.path).lines == ["app.py:1: KEY (from .env.<BUCKET>)"]


def test_a_path_on_another_drive_is_outside_the_repository(
    project: Repo, omerta, monkeypatch: pytest.MonkeyPatch
):
    def relpath(path: str, start: str | None = None) -> str:
        raise ValueError("path is on mount 'D:', start on mount 'C:'")

    monkeypatch.setattr(os.path, "relpath", relpath)
    result = omerta("check", "D:/elsewhere.py", cwd=project.path)
    assert (result.code, result.err) == (2, "omerta: D:/elsewhere.py is outside the repository\n")


def test_a_path_may_name_the_root_or_an_absolute_path(project: Repo, omerta):
    project.write("src/a.py", f"{_KEY}\n")
    project.write("b.py", f"{_KEY}\n")
    project.add("src/a.py", "b.py")
    expected = ["b.py:1: API_KEY (from .env)", "src/a.py:1: API_KEY (from .env)"]
    assert omerta("check", "..", cwd=project.path / "src").lines == expected
    assert omerta("check", str(project.path / "b.py"), cwd=project.path / "src").lines == [
        expected[0]
    ]


def test_paths_that_need_it_are_quoted(project: Repo, omerta):
    # Git for Windows refuses a control character in a path unless told not to.
    project.git("config", "core.protectNTFS", "false")
    blob = project.blob(f"{_KEY}\n")
    project.index_info(
        ("100644", blob, 0, b"tab\there.txt"),
        ("100644", blob, 0, b"caf\xc3\xa9.txt"),
        ("100644", blob, 0, b"latin\xe9.txt"),
    )
    assert omerta("check", "--all", cwd=project.path).lines == [
        "café.txt:1: API_KEY (from .env)",
        '"latin\\351.txt":1: API_KEY (from .env)',
        '"tab\\there.txt":1: API_KEY (from .env)',
    ]


def test_an_entry_that_cannot_be_read_is_an_error_after_the_findings_before_it(
    project: Repo, omerta
):
    found = project.blob(f"{_KEY}\n")
    missing = "1234567890" * 4
    project.index_info(("100644", found, 0, b"a.py"), ("100644", missing, 0, b"b.py"))
    result = omerta("check", "--all", cwd=project.path)
    assert result.code == 2
    assert result.lines == ["a.py:1: API_KEY (from .env)"]
    assert result.err == f"omerta: git object {missing} cannot be read\n"


def test_an_env_file_that_cannot_be_parsed_is_an_error_without_its_text(project: Repo, omerta):
    project.write(".env.local", f'OK=1\nBROKEN="{_KEY}\n')
    project.write("app.py", f"{_KEY}\n")
    project.add("app.py")
    result = omerta("check", cwd=project.path)
    assert result.code == 2
    assert result.out == ""
    assert result.err == "omerta: .env.local, line 2: python-dotenv cannot parse this statement\n"
    result.assert_silent_about(_KEY)


def test_gits_error_output_is_shown_with_each_value_replaced(
    project: Repo, omerta, monkeypatch: pytest.MonkeyPatch
):
    from omerta.git import Git, GitError

    def fail(self: Git) -> set[bytes]:
        raise GitError("diff", 128, f"fatal: cannot read {_KEY}\n".encode())

    monkeypatch.setattr(Git, "staged_paths", fail)
    result = omerta("check", cwd=project.path)
    assert result.code == 2
    assert result.err == (
        "omerta: git diff failed with exit status 128:\nfatal: cannot read <API_KEY>\n"
    )
