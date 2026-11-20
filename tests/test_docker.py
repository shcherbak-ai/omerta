"""``omerta docker``: the env files in the build context of each Dockerfile of the working tree."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import Repo, link_folder
from omerta import docker
from omerta.errors import OmertaError

_KEY = "sk-test-0123456789abcdef"


@pytest.fixture
def project(repo: Repo) -> Repo:
    """A repository with a Dockerfile at the root and a .env that git ignores."""
    repo.write(".gitignore", ".env*\n!.env.example\n")
    repo.write(".env", f"API_KEY={_KEY}\n")
    repo.write("Dockerfile", "FROM scratch\nCOPY . /app\n")
    repo.add(".gitignore", "Dockerfile")
    return repo


def test_an_env_file_git_ignores_is_in_the_build_context_without_a_dockerignore(
    project: Repo, omerta
):
    result = omerta("docker", cwd=project.path)
    assert result.code == 1
    assert result.lines == [".env: in the build context of Dockerfile"]
    assert result.err == ""
    result.assert_silent_about(_KEY)


def test_a_dockerignore_that_leaves_the_env_files_out_passes(project: Repo, omerta):
    project.write(".dockerignore", ".git\n.env*\n")
    project.write(".env.local", "TOKEN=local-token-0123\n")
    result = omerta("docker", cwd=project.path)
    assert (result.code, result.out, result.err) == (0, "", "")


def test_each_env_file_the_dockerignore_misses_is_reported(project: Repo, omerta):
    project.write(".dockerignore", ".env\n")
    project.write(".env.local", "TOKEN=local-token-0123\n")
    # A template documents the variables; a swap file holds the text of a file.
    project.write(".env.example", "API_KEY=\n")
    project.write(".env.swp", b"b0VIM 9.1\x00\xff\xfe")
    result = omerta("docker", cwd=project.path)
    assert result.lines == [
        ".env.local: in the build context of Dockerfile",
        ".env.swp: in the build context of Dockerfile",
    ]
    result.assert_silent_about(_KEY, "local-token-0123")


def test_a_tracked_env_file_is_in_the_build_context_too(project: Repo, omerta):
    project.write(".dockerignore", ".env\n")
    project.write(".env.production", "MODE=production-mode\n")
    project.git("add", "-f", ".env.production")
    result = omerta("docker", cwd=project.path)
    assert result.lines == [".env.production: in the build context of Dockerfile"]


def test_the_dockerfile_s_own_ignore_file_takes_precedence_even_empty(project: Repo, omerta):
    project.write(".dockerignore", ".env\n")
    project.write("Dockerfile.dockerignore", "")
    assert omerta("docker", cwd=project.path).lines == [".env: in the build context of Dockerfile"]
    project.write("Dockerfile.dockerignore", "**/.env\n")
    project.write(".dockerignore", "")
    assert omerta("docker", cwd=project.path).code == 0


def _fake_links(monkeypatch: pytest.MonkeyPatch, root: Path, links: dict[str, str]) -> None:
    """Have ``_link`` read the paths of ``links``, from ``root`` with ``/``, as links.

    For the cases that a machine without the right to make links (Windows) runs
    too. Each link is a file of its own on disk, so that its folder lists it.
    """
    for name in links:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).touch()

    def link(path: Path) -> str | None:
        inside = Path(path).is_relative_to(root)
        return links.get(Path(path).relative_to(root).as_posix()) if inside else None

    monkeypatch.setattr(docker, "_link", link)


# Each as Docker 29.9 builds it, from api/ with a Linux client: the file the
# ignore file leads to there, or None where it leads to none.
@pytest.mark.parametrize(
    ("links", "expected"),
    [
        ({}, ".dockerignore"),
        ({".dockerignore": "docker/common.dockerignore"}, "docker/common.dockerignore"),
        ({".dockerignore": "./docker//common.dockerignore"}, "docker/common.dockerignore"),
        # Out of the folder, which BuildKit's copy of it does not hold.
        ({".dockerignore": "../shared.dockerignore"}, None),
        ({".dockerignore": "../api/docker/common.dockerignore"}, None),
        # `..` stops at the folder, and an absolute target starts from it.
        ({".dockerignore": "../docker/common.dockerignore"}, "docker/common.dockerignore"),
        ({".dockerignore": "/docker/common.dockerignore"}, "docker/common.dockerignore"),
        # Through a link to a folder, inside and outside.
        (
            {".dockerignore": "conf/common.dockerignore", "conf": "docker"},
            "docker/common.dockerignore",
        ),
        ({".dockerignore": "conf/common.dockerignore", "conf": "../shared"}, None),
        ({".dockerignore": "docker"}, None),
        ({".dockerignore": "missing"}, None),
        ({".dockerignore": ".dockerignore"}, None),
    ],
)
def test_an_ignore_file_s_links_are_followed_inside_its_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, links: dict[str, str], expected: str | None
):
    root = tmp_path / "api"
    for path in (
        "api/.dockerignore",
        "api/docker/common.dockerignore",
        "shared.dockerignore",
        "shared/common.dockerignore",
    ):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(".env\n", encoding="utf-8")
    _fake_links(monkeypatch, root, links)
    monkeypatch.setattr(docker, "WINDOWS", False)
    found = docker._inside(root, ".dockerignore")
    assert found == (None if expected is None else root.joinpath(*expected.split("/")))


def test_a_long_chain_of_links_leads_to_no_ignore_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    (tmp_path / "end").write_text(".env\n", encoding="utf-8")
    chain = {f"{step}": f"{step + 1}" for step in range(docker._LINKS - 1)}
    chain[f"{docker._LINKS - 1}"] = "end"
    _fake_links(monkeypatch, tmp_path, chain)
    assert docker._inside(tmp_path, "0") == tmp_path / "end"
    _fake_links(monkeypatch, tmp_path, {"start": "0", **chain})
    assert docker._inside(tmp_path, "start") is None


# As Docker 29.9's Windows client builds them, from api/: it sends a target
# with / for \ and without \\?\, so that a drive (C:/...) names nothing there.
@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("docker\\common.dockerignore", "docker/common.dockerignore"),
        ("..\\shared.dockerignore", None),
        ("\\\\?\\C:\\elsewhere\\api\\docker\\common.dockerignore", None),
        ("C:/api/docker/common.dockerignore", None),
    ],
)
def test_on_windows_a_link_s_target_is_read_as_docker_s_client_sends_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str, expected: str | None
):
    root = tmp_path / "api"
    for path in ("api/docker/common.dockerignore", "shared.dockerignore"):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(".env\n", encoding="utf-8")
    _fake_links(monkeypatch, root, {".dockerignore": target})
    monkeypatch.setattr(docker, "WINDOWS", True)
    found = docker._inside(root, ".dockerignore")
    assert found == (None if expected is None else root.joinpath(*expected.split("/")))


@pytest.mark.parametrize(
    ("tag", "code", "err"),
    [
        # A link made in WSL: Docker's Windows client fails the build on it.
        (
            docker._WSL_LINK,
            2,
            "omerta: .dockerignore leads through a link made in WSL, which Docker's Windows "
            "client refuses to read: check it from WSL, or replace the link with the file\n",
        ),
        # A file that OneDrive keeps (IO_REPARSE_TAG_CLOUD_6) is a file.
        (0x9000601A, 0, ""),
    ],
)
def test_on_windows_a_reparse_point_that_is_no_link_is_read_by_its_tag(
    project: Repo, omerta, monkeypatch: pytest.MonkeyPatch, tag: int, code: int, err: str
):
    # As Windows reports it: os.readlink refuses it with a ValueError.
    project.write(".dockerignore", ".env\n")
    readlink, lstat = os.readlink, os.lstat

    def refuse(path: Path) -> str:
        if Path(path).name == ".dockerignore":
            raise ValueError("not a symbolic link")
        return readlink(path)

    def tagged(path: Path, **options: object) -> object:
        if Path(path).name == ".dockerignore":
            return SimpleNamespace(st_reparse_tag=tag)
        return lstat(path, **options)

    monkeypatch.setattr(os, "readlink", refuse)
    monkeypatch.setattr(os, "lstat", tagged)
    result = omerta("docker", cwd=project.path)
    assert (result.code, result.err) == (code, err)


def test_the_dockerfile_s_own_ignore_file_counts_where_it_leads_to_a_file(
    project: Repo, omerta, monkeypatch: pytest.MonkeyPatch
):
    project.write(".dockerignore", ".env\n")
    project.write("docker/empty", "")
    # Leading nowhere, it is none, and the folder's counts.
    _fake_links(monkeypatch, project.path, {"Dockerfile.dockerignore": "missing"})
    assert omerta("docker", cwd=project.path).code == 0
    # Leading to an empty file, it takes precedence.
    _fake_links(monkeypatch, project.path, {"Dockerfile.dockerignore": "docker/empty"})
    assert omerta("docker", cwd=project.path).lines == [".env: in the build context of Dockerfile"]


def test_a_linked_env_file_is_judged_by_the_file_it_leads_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Faked, for a machine without the right to make links: what each leads to.
    top = tmp_path / "repo"
    links = {
        top / ".env": top / "config" / "app.secrets",
        top / ".env.shared": tmp_path / "outside" / "secrets.env",
        top / ".env.dev": top / ".env.example",
        top / "api" / ".env": top / ".env.local",
    }
    realpath = os.path.realpath
    monkeypatch.setattr(Path, "is_symlink", lambda self: self in links)
    monkeypatch.setattr(
        os.path, "realpath", lambda path, **options: str(links.get(Path(path), realpath(path)))
    )
    env_files = [".env", ".env.dev", ".env.local", ".env.shared", "api/.env"]
    # Out of the working tree, or to a template, nothing; to another env file, it once.
    assert docker._texts(top, env_files) == ["config/app.secrets", ".env.local"]


@pytest.mark.usefixtures("symlinks")
def test_a_build_takes_the_file_a_linked_env_file_leads_to(repo: Repo, omerta):
    # Docker sends the link as a link, and the file it leads to in its own right.
    repo.write("Dockerfile", "FROM scratch\n")
    repo.write("config/Dockerfile", "FROM scratch\n")
    repo.write("config/app.secrets", "DB_PASSWORD=api-password-0123\n")
    repo.write(".dockerignore", ".env\n")
    # With the platform's separator, as Windows' own tools write a link: a
    # target written with `/` there is one that Windows cannot follow.
    (repo.path / ".env").symlink_to(Path("config", "app.secrets"))
    result = omerta("docker", cwd=repo.path)
    assert result.lines == [
        "config/app.secrets: in the build context of Dockerfile",
        "config/app.secrets: in the build context of config/Dockerfile",
    ]
    # Leading out of the context, it reaches the image leading nowhere.
    (repo.path / ".env").unlink()
    repo.write("../outside/secrets.env", "DB_PASSWORD=api-password-0123\n")
    (repo.path / ".env").symlink_to(Path("..", "outside", "secrets.env"))
    repo.write(".dockerignore", "")
    assert omerta("docker", cwd=repo.path).code == 0


@pytest.mark.usefixtures("symlinks")
def test_a_dockerignore_that_links_out_of_its_folder_leaves_nothing_out(repo: Repo, omerta):
    repo.write(".omerta.toml", 'env-files = ["**/.env"]\n')
    repo.write("shared.dockerignore", ".env\n")
    repo.write("api/Dockerfile", "FROM scratch\n")
    repo.write("api/.env", "DB_PASSWORD=api-password-0123\n")
    repo.write("api/docker/common.dockerignore", ".env\n")
    link = repo.path / "api" / ".dockerignore"
    link.symlink_to("../shared.dockerignore")
    result = omerta("docker", cwd=repo.path)
    assert result.lines == ["api/.env: in the build context of api/Dockerfile"]
    link.unlink()
    link.symlink_to("docker/common.dockerignore")
    assert omerta("docker", cwd=repo.path).code == 0


def test_a_dockerfile_is_built_from_its_own_folder(repo: Repo, omerta):
    repo.write(".omerta.toml", 'env-files = ["**/.env"]\n')
    repo.write(".env", f"API_KEY={_KEY}\n")
    repo.write("api/.env", "DB_PASSWORD=api-password-0123\n")
    repo.write("api/Dockerfile", "FROM scratch\n")
    repo.write("web/Dockerfile", "FROM scratch\n")
    repo.add("api/Dockerfile", "web/Dockerfile")
    result = omerta("docker", cwd=repo.path)
    assert result.lines == ["api/.env: in the build context of api/Dockerfile"]
    # The folder's own .dockerignore, not the root's.
    repo.write(".dockerignore", ".env\n")
    assert omerta("docker", cwd=repo.path).code == 1
    repo.write("api/.dockerignore", ".env\n")
    assert omerta("docker", cwd=repo.path).code == 0


def test_the_configuration_names_the_folder_a_dockerfile_is_built_from(repo: Repo, omerta):
    repo.write(".env", f"API_KEY={_KEY}\n")
    repo.write("docker/app.Dockerfile", "FROM scratch\n")
    repo.add("docker/app.Dockerfile")
    assert omerta("docker", cwd=repo.path).code == 0
    repo.write(".omerta.toml", 'docker-contexts = { "docker/app.Dockerfile" = "." }\n')
    result = omerta("docker", "--json", cwd=repo.path)
    assert [json.loads(line) for line in result.lines] == [
        {"path": ".env", "dockerfile": "docker/app.Dockerfile", "context": "."}
    ]
    # Its own ignore file is beside it, whatever folder it is built from.
    repo.write("docker/app.Dockerfile.dockerignore", ".env\n")
    assert omerta("docker", cwd=repo.path).code == 0


def test_a_file_the_configuration_names_is_a_dockerfile_whatever_its_name(repo: Repo, omerta):
    repo.write(".env", f"API_KEY={_KEY}\n")
    repo.write("build/image.def", "FROM scratch\n")
    repo.add("build/image.def")
    repo.write(".omerta.toml", 'docker-contexts = { "build/image.def" = "." }\n')
    result = omerta("docker", cwd=repo.path)
    assert result.lines == [".env: in the build context of build/image.def"]


def test_every_dockerfile_is_built_whatever_git_makes_of_it(repo: Repo, omerta):
    # Docker reads no .gitignore: a Dockerfile that git ignores, or that it has
    # never been given, is built as a tracked one is.
    repo.write(".gitignore", ".env\nnode_modules/\nDockerfile.local\n")
    repo.write(".env", f"API_KEY={_KEY}\n")
    repo.add(".gitignore")
    repo.write(".omerta.toml", 'env-files = ["**/.env", "**/.env.*"]\n')
    repo.write("Dockerfile.local", "FROM scratch\n")
    repo.write("web/Dockerfile", "FROM scratch\n")
    repo.write("web/.env.local", "TOKEN=web-token-012345\n")
    repo.write("node_modules/pkg/Dockerfile", "FROM scratch\n")
    repo.write("node_modules/pkg/.env", "PKG=package-value-0123\n")
    # Another repository's Dockerfile, which git does not list.
    repo.write("tools/sub/Dockerfile", "FROM scratch\n")
    repo.git("init", "-q", cwd=repo.path / "tools" / "sub")
    result = omerta("docker", cwd=repo.path)
    assert result.lines == [
        ".env: in the build context of Dockerfile.local",
        "node_modules/pkg/.env: in the build context of Dockerfile.local",
        "node_modules/pkg/.env: in the build context of node_modules/pkg/Dockerfile",
        "web/.env.local: in the build context of Dockerfile.local",
        "web/.env.local: in the build context of web/Dockerfile",
    ]


def test_the_env_files_of_a_nested_repository_are_in_the_build_context(repo: Repo, omerta):
    # Docker knows nothing of git: a submodule is copied as any other folder,
    # its Dockerfile built as any other.
    repo.write(".omerta.toml", 'env-files = ["**/.env"]\n')
    repo.write("Dockerfile", "FROM scratch\n")
    repo.write("vendor/lib/Dockerfile", "FROM scratch\n")
    repo.write("vendor/lib/.env", "LIB_TOKEN=library-token-0123\n")
    repo.git("init", "-q", cwd=repo.path / "vendor" / "lib")
    result = omerta("docker", cwd=repo.path)
    assert result.lines == [
        "vendor/lib/.env: in the build context of Dockerfile",
        "vendor/lib/.env: in the build context of vendor/lib/Dockerfile",
    ]


def test_an_ignore_file_named_in_another_case_is_none(repo: Repo, omerta, tmp_path: Path):
    # As Docker 29.9 builds it: Windows and macOS open `.DOCKERIGNORE` as
    # `.dockerignore`, but Docker's client sends it under its own name, which
    # BuildKit does not ask for.
    (tmp_path / "probe").mkdir()
    if not (tmp_path / "PROBE").exists():
        pytest.skip("this file system tells names apart by case")
    repo.write(".env", f"API_KEY={_KEY}\n")
    repo.write("Dockerfile", "FROM scratch\n")
    repo.write(".DOCKERIGNORE", ".env\n")
    assert omerta("docker", cwd=repo.path).lines == [".env: in the build context of Dockerfile"]


def test_a_name_is_one_that_its_folder_lists_as_spelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    (tmp_path / ".dockerignore").write_text(".env\n", encoding="utf-8")
    assert docker._inside(tmp_path, ".dockerignore") == tmp_path / ".dockerignore"
    # As Windows lists a folder whose file opens under another case too.
    listdir = os.listdir
    monkeypatch.setattr(os, "listdir", lambda path: [name.upper() for name in listdir(path)])
    assert docker._inside(tmp_path, ".dockerignore") is None

    def refuse(path: Path) -> list[str]:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "listdir", refuse)
    assert docker._inside(tmp_path, ".dockerignore") is None


def test_a_configured_folder_is_taken_where_its_links_lead(repo: Repo, omerta, tmp_path: Path):
    # As Docker 29.9 builds `-f build/image.def alias`: it opens the folder
    # where the link leads, while the env files are found where they are.
    repo.write(".omerta.toml", 'env-files = ["**/.env"]\n' + _CONTEXT.format("alias"))
    repo.write("build/image.def", "FROM scratch\n")
    repo.write("real/.env", "TOKEN=real-token-012345\n")
    link_folder(repo.path / "alias", repo.path / "real")
    result = omerta("docker", cwd=repo.path)
    assert result.lines == ["real/.env: in the build context of build/image.def"]
    # Leading out of the working tree, it holds none of its env files.
    (tmp_path / "outside").mkdir()
    link_folder(repo.path / "away", tmp_path / "outside")
    repo.write(".omerta.toml", 'env-files = ["**/.env"]\n' + _CONTEXT.format("away"))
    assert omerta("docker", cwd=repo.path).code == 0


_CONTEXT = 'docker-contexts = {{ "build/image.def" = "{}" }}\n'


def test_a_linked_dockerfile_is_read_where_buildkit_reads_it(
    repo: Repo, omerta, monkeypatch: pytest.MonkeyPatch
):
    # As Docker 29.9 builds `api`: BuildKit reads the Dockerfile from a copy of
    # `api`, where `..` stops, so `../recipe` is `api/recipe`, not the note.
    repo.write(".omerta.toml", 'env-files = ["**/.env"]\n')
    repo.write("recipe", "Notes on how we build.\n")
    repo.write("api/recipe", "FROM scratch\n")
    repo.write("api/.env", "DB_PASSWORD=api-password-0123\n")
    _fake_links(monkeypatch, repo.path / "api", {"Dockerfile": "../recipe"})
    result = omerta("docker", cwd=repo.path)
    assert result.lines == ["api/.env: in the build context of api/Dockerfile"]
    # Leading to no file there, it is no build.
    _fake_links(monkeypatch, repo.path / "api", {"Dockerfile": "../missing"})
    assert omerta("docker", cwd=repo.path).code == 0


@pytest.mark.usefixtures("symlinks")
def test_a_dockerfile_that_links_up_is_read_inside_its_folder(repo: Repo, omerta):
    repo.write(".omerta.toml", 'env-files = ["**/.env"]\n')
    repo.write("recipe", "Notes on how we build.\n")
    repo.write("api/recipe", "FROM scratch\n")
    repo.write("api/.env", "DB_PASSWORD=api-password-0123\n")
    (repo.path / "api" / "Dockerfile").symlink_to(Path("..", "recipe"))
    result = omerta("docker", cwd=repo.path)
    assert result.lines == ["api/.env: in the build context of api/Dockerfile"]


def test_the_values_of_tracked_and_nested_env_files_are_hidden_too(repo: Repo, omerta):
    # It names them, so a path that holds one of their values hides it, as
    # it hides the values of the untracked env files at the root.
    repo.write(".omerta.toml", 'env-files = ["**/.env", "**/.env.*"]\n')
    repo.write(".env", f"API_KEY={_KEY}\n")
    repo.write("tracked/.env", "TOKEN=tracked-token-0123\n")
    repo.git("add", "-f", "tracked/.env")
    repo.write("tracked/Dockerfile.tracked-token-0123", "FROM scratch\n")
    repo.write("vendor/lib/.env", "LIB_TOKEN=library-token-0123\n")
    repo.write("vendor/lib/Dockerfile.library-token-0123", "FROM scratch\n")
    repo.git("init", "-q", cwd=repo.path / "vendor" / "lib")
    # A swap file, binary, has no values, and fails nothing.
    repo.write("tracked/.env.swp", b"b0VIM 9.1\x00\xff\xfe")
    result = omerta("docker", cwd=repo.path)
    assert "tracked/.env: in the build context of tracked/Dockerfile.<TOKEN>" in result.lines
    assert (
        "vendor/lib/.env: in the build context of vendor/lib/Dockerfile.<LIB_TOKEN>" in result.lines
    )
    result.assert_silent_about("tracked-token-0123", "library-token-0123")


def test_the_configuration_names_a_dockerfile_that_git_does_not_list(repo: Repo, omerta):
    repo.write(".env", f"API_KEY={_KEY}\n")
    repo.write("tools/sub/build.def", "FROM scratch\n")
    repo.git("init", "-q", cwd=repo.path / "tools" / "sub")
    repo.write(".omerta.toml", 'docker-contexts = { "tools/sub/build.def" = "." }\n')
    result = omerta("docker", cwd=repo.path)
    assert result.lines == [".env: in the build context of tools/sub/build.def"]


def test_a_configured_dockerfile_not_in_the_working_tree_is_an_error(repo: Repo, omerta):
    # Tracked or not: Docker builds from the files on disk.
    repo.write("build/image.def", "FROM scratch\n")
    repo.add("build/image.def")
    (repo.path / "build" / "image.def").unlink()
    repo.write(".omerta.toml", 'docker-contexts = { "build/image.def" = "." }\n')
    result = omerta("docker", cwd=repo.path)
    assert (result.code, result.out) == (2, "")
    assert result.err == (
        "omerta: docker-contexts names build/image.def, which is no file of the working tree\n"
    )


def test_a_configured_folder_spelled_otherwise_than_on_disk_is_an_error(
    repo: Repo, omerta, tmp_path: Path
):
    # Windows and macOS open API for api, while the env files are found as
    # spelled there: none would be under API.
    (tmp_path / "probe").mkdir()
    if not (tmp_path / "PROBE").exists():
        pytest.skip("this file system tells names apart by case")
    repo.write(
        ".omerta.toml", 'env-files = ["**/.env"]\ndocker-contexts = { "api/Dockerfile" = "API" }\n'
    )
    repo.write("api/Dockerfile", "FROM scratch\n")
    repo.write("api/.env", "DB_PASSWORD=api-password-0123\n")
    result = omerta("docker", cwd=repo.path)
    assert (result.code, result.out) == (2, "")
    assert result.err == (
        "omerta: docker-contexts names API, which the working tree spells otherwise "
        "(in another case or Unicode form)\n"
    )
    repo.write(
        ".omerta.toml", 'env-files = ["**/.env"]\ndocker-contexts = { "API/Dockerfile" = "api" }\n'
    )
    assert (
        "names API/Dockerfile, which the working tree spells otherwise"
        in omerta("docker", cwd=repo.path).err
    )


def test_the_spelling_of_a_configured_path_is_read_from_its_folders_listings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    (tmp_path / "API").mkdir()
    (tmp_path / "API" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    # A folder that is not there yet holds no env file, and is no error.
    assert docker.builds(tmp_path, [], (("API/Dockerfile", "dist"),)) == [
        ("API/Dockerfile", "dist")
    ]
    # As Windows lists a folder it opens under another case.
    listdir = os.listdir
    monkeypatch.setattr(os, "listdir", lambda path: [name.lower() for name in listdir(path)])
    with pytest.raises(OmertaError, match="^docker-contexts names API/Dockerfile, which the"):
        docker.builds(tmp_path, [], (("API/Dockerfile", "."),))

    def refuse(path: Path) -> list[str]:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "listdir", refuse)
    with pytest.raises(OmertaError, match=r"^folder \. cannot be listed \(Permission denied\)$"):
        docker.builds(tmp_path, [], (("API/Dockerfile", "."),))


def test_a_build_with_no_env_file_in_its_folder_is_left_alone(repo: Repo, omerta):
    # A package's Dockerfile and its broken ignore file: no env file is there,
    # so neither is read, and nothing fails over a file that is not yours.
    repo.write(".env", f"API_KEY={_KEY}\n")
    repo.write("node_modules/pkg/Dockerfile", "FROM scratch\n")
    repo.write("node_modules/pkg/.dockerignore", "[\n")
    repo.write(".omerta.toml", 'docker-contexts = { "node_modules/pkg/Dockerfile" = "web" }\n')
    result = omerta("docker", cwd=repo.path)
    assert (result.code, result.out, result.err) == (0, "", "")


def test_without_a_dockerfile_nothing_is_found(repo: Repo, omerta):
    repo.write(".env", f"API_KEY={_KEY}\n")
    result = omerta("docker", cwd=repo.path)
    assert (result.code, result.out, result.err) == (0, "", "")


def test_env_file_patterns_and_quiet_apply(project: Repo, omerta):
    project.write("secrets.txt", "TOKEN=from-secrets-0123\n")
    result = omerta("docker", "--env-file", "secrets.txt", cwd=project.path)
    assert result.lines == ["secrets.txt: in the build context of Dockerfile"]
    quiet = omerta("docker", "-q", cwd=project.path)
    assert (quiet.code, quiet.out) == (1, "")


def test_a_value_in_an_env_file_s_name_is_hidden(project: Repo, omerta):
    project.write(".env", "BUCKET=acme-prod-data-7731\n")
    project.write(".env.acme-prod-data-7731", "REGION=eu-north-1a\n")
    result = omerta("docker", cwd=project.path)
    assert result.lines == [
        ".env: in the build context of Dockerfile",
        ".env.<BUCKET>: in the build context of Dockerfile",
    ]


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS refuses a name that is not UTF-8")
def test_a_dockerfile_whose_name_is_not_text_is_quoted(project: Repo, omerta):
    # Windows has a lone UTF-16 surrogate in a name, which Python keeps as it is;
    # Linux a byte that is not UTF-8, which Python escapes. Each as git quotes it.
    if sys.platform == "win32":
        name, shown = "Dockerfile.\ud800", '"Dockerfile.\\355\\240\\200"'
    else:
        name, shown = os.fsdecode(b"Dockerfile.\xff"), '"Dockerfile.\\377"'
    (project.path / name).write_text("FROM scratch\n", encoding="utf-8")
    result = omerta("docker", cwd=project.path)
    assert result.lines == [
        ".env: in the build context of Dockerfile",
        f".env: in the build context of {shown}",
    ]


def test_a_pattern_docker_refuses_is_an_error_at_its_line(project: Repo, omerta):
    project.write(".dockerignore", "# secrets\n.env\n[\n")
    result = omerta("docker", cwd=project.path)
    assert (result.code, result.out) == (2, "")
    assert result.err == "omerta: .dockerignore, line 3: Docker refuses this pattern\n"


def test_an_ignore_file_that_cannot_be_read_is_an_error(
    project: Repo, omerta, monkeypatch: pytest.MonkeyPatch
):
    project.write(".dockerignore", ".env\n")
    read = Path.read_bytes

    def refuse(self: Path) -> bytes:
        if self.name == ".dockerignore":
            raise PermissionError(13, "Permission denied")
        return read(self)

    monkeypatch.setattr(Path, "read_bytes", refuse)
    result = omerta("docker", cwd=project.path)
    assert (result.code, result.err) == (
        2,
        "omerta: .dockerignore cannot be read (Permission denied)\n",
    )


@pytest.mark.parametrize(
    "name",
    [
        "Dockerfile",
        "dockerfile",
        "DOCKERFILE",
        "Dockerfile.dev",
        "api.Dockerfile",
        "web.dockerfile",
    ],
)
def test_a_dockerfile_is_named_dockerfile_dockerfile_dot_any_or_any_dot_dockerfile(name: str):
    assert docker.is_dockerfile(name)


@pytest.mark.parametrize(
    "name",
    ["Dockerfile.dockerignore", "app.Dockerfile.dockerignore", "Dockerfiles", "Containerfile"],
)
def test_other_files_are_no_dockerfiles(name: str):
    assert not docker.is_dockerfile(name)


# Each as Docker 29.9 builds it with `-f`: whether it builds, or is refused before
# any stage (an empty file, another first instruction, an unknown word).
@pytest.mark.parametrize(
    ("text", "builds"),
    [
        (b"", False),
        (b"RUN echo hi\n", False),
        (b"LABEL a=b\nFROM scratch\n", False),
        (b"ONBUILD RUN x\nFROM scratch\n", False),
        (b"# Notes on the Dockerfile\n\nBuild it with docker build.\n", False),
        (b"FROMX scratch\n", False),
        (b"FROM\\\n  scratch\n", False),
        (b"# only a comment\n\n", False),
        (b"ARG X=1\nFROM scratch\n", True),
        (b"from scratch\n", True),
        (b"   FROM scratch\n", True),
        (b"\n\n\nFROM scratch\n", True),
        (b"# a note\n\n   # indented note\nFROM scratch\n", True),
        (b"# escape=`\nFROM scratch\n", True),
        (b"\xef\xbb\xbfFROM scratch\n", True),
        # Go's spaces, a no-break space among them, are trimmed as ASCII ones are.
        (b"\xc2\xa0FROM scratch\n", True),
        # A byte order mark counts on the first line only.
        (b"# note\n\xef\xbb\xbfFROM scratch\n", False),
    ],
)
def test_a_file_named_like_a_dockerfile_is_one_where_docker_could_build_it(
    tmp_path: Path, text: bytes, builds: bool
):
    (tmp_path / "Dockerfile").write_bytes(text)
    assert docker._starts_a_build(tmp_path / "Dockerfile") is builds


def test_a_line_of_64_kib_or_more_ends_what_docker_reads(tmp_path: Path):
    # As Docker 29.9 builds them: 60,000 spaces before FROM build, 65,536 are
    # "file with no instructions". A binary file is not read whole to decide.
    dockerfile = tmp_path / "Dockerfile.bin"
    dockerfile.write_bytes(b" " * 60_000 + b"FROM scratch\n")
    assert docker._starts_a_build(dockerfile)
    dockerfile.write_bytes(b" " * 65_536 + b"FROM scratch\n")
    assert not docker._starts_a_build(dockerfile)
    # The longest that bufio's buffer holds, its line end included, builds;
    # one byte more does not, as Docker 29.9 builds show.
    dockerfile.write_bytes(b" " * (65_535 - len(b"FROM scratch")) + b"FROM scratch\n")
    assert docker._starts_a_build(dockerfile)
    dockerfile.write_bytes(b" " * (65_536 - len(b"FROM scratch")) + b"FROM scratch\n")
    assert not docker._starts_a_build(dockerfile)
    dockerfile.write_bytes(b"\x00" * (3 * 65_536))
    assert not docker._starts_a_build(dockerfile)


def test_a_note_named_like_a_dockerfile_is_no_build(project: Repo, omerta):
    # Docker cannot build it, so that its lack of an ignore file of its own leaks nothing.
    project.write("Dockerfile.dockerignore", ".env\n")
    project.write("Dockerfile.md", "# Notes on the Dockerfile\n\nBuild it with docker build.\n")
    project.write("Dockerfile.empty", "")
    assert omerta("docker", cwd=project.path).code == 0
    # Named in the configuration, any file is one.
    project.write(".omerta.toml", 'docker-contexts = { "Dockerfile.md" = "." }\n')
    assert omerta("docker", cwd=project.path).lines == [
        ".env: in the build context of Dockerfile.md"
    ]


def test_a_dockerfile_that_cannot_be_read_is_still_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    (tmp_path / "Dockerfile").write_bytes(b"FROM scratch\n")

    def refuse(self: Path, *args: object, **kwargs: object) -> object:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "open", refuse)
    assert docker._starts_a_build(tmp_path / "Dockerfile")


def test_dockerfiles_are_looked_for_only_where_an_env_file_could_be_in_their_build(
    tmp_path: Path,
):
    for path in (
        "Dockerfile",
        "api/app.Dockerfile",
        "api/v1/Dockerfile.dev",
        "web/Dockerfile",
        "node_modules/pkg/Dockerfile",
        "api/Dockerfile/README",
    ):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("FROM scratch\n", encoding="utf-8")
    # The folders of the env files and those above them: the root, api and
    # api/v1; not web, nor node_modules. A folder named like a Dockerfile is none.
    assert docker.builds(tmp_path, [".env", "api/v1/.env"], ()) == [
        ("Dockerfile", ""),
        ("api/app.Dockerfile", "api"),
        ("api/v1/Dockerfile.dev", "api/v1"),
    ]
    assert docker.builds(tmp_path, [], ()) == []
