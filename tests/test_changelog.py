"""The changelog, the citation file and the version fields agree, and a bump dates them."""

from __future__ import annotations

import contextlib
import importlib.util
import itertools
import re
import sys
import tomllib
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

import pytest

import omerta

_ROOT = Path(__file__).resolve().parent.parent
_CHANGELOG = (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
_CITATION = (_ROOT / "CITATION.cff").read_text(encoding="utf-8")
_PYPROJECT = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
_REPOSITORY = "https://github.com/shcherbak-ai/omerta"
_COMPARE = f"{_REPOSITORY}/compare/"


def _ref(version: str) -> str:
    """What a comparison ends at: the version's tag, or ``HEAD`` for unreleased work."""
    return "HEAD" if version == "Unreleased" else f"v{version}"


def test_each_version_links_to_its_changes():
    versions = re.findall(r"^## \[(.+?)\]", _CHANGELOG, re.MULTILINE)
    links = dict(re.findall(r"^\[(.+?)\]: (\S+)$", _CHANGELOG, re.MULTILINE))
    # The links stand at the end of the file, newest first like the sections:
    # a new version's link goes on top.
    assert list(links) == versions
    for version, previous in itertools.pairwise(versions):
        assert links[version] == f"{_COMPARE}v{previous}...{_ref(version)}"
    # Before the first release, the unreleased changes are the development
    # branch's commits; the first release links to its tag.
    oldest = versions[-1]
    if oldest == "Unreleased":
        assert links[oldest] == f"{_REPOSITORY}/commits/dev"
    else:
        assert links[oldest] == f"{_REPOSITORY}/releases/tag/v{oldest}"


def test_the_version_fields_agree_and_the_citation_file_has_the_release_date():
    released = dict(re.findall(r"^## \[(.+?)\] - (\d{4}-\d{2}-\d{2})$", _CHANGELOG, re.MULTILINE))
    (version,) = re.findall(r"^version: (\S+)$", _CITATION, re.MULTILINE)
    dates = re.findall(r"^date-released: (\S+)$", _CITATION, re.MULTILINE)
    assert version == omerta.__version__
    assert version == _PYPROJECT["project"]["version"]
    assert version == _PYPROJECT["tool"]["commitizen"]["version"]
    if version == "0.0.0":
        # Before the first release: nothing released, nothing dated.
        assert released == {}
        assert dates == []
    else:
        # `cz bump` rewrites the version (`version_files`), and
        # scripts/date_release.py, one of its `pre_bump_hooks`, writes the day
        # into the changelog heading and here. Run as the last of the hooks,
        # this test stops a bump whose two dates differ.
        assert dates == [released[version]]
    # The version of the CFF format, which `cz bump` must leave alone.
    assert re.findall(r"^cff-version: (\S+)$", _CITATION, re.MULTILINE) == ["1.2.0"]


def test_the_examples_pin_the_current_version():
    for name in ("README.md", "PYPI.md"):
        text = (_ROOT / name).read_text(encoding="utf-8")
        pins = re.findall(r"omerta@(\S+?)[\s`]|rev: v(\S+)", text)
        assert pins
        assert {pin for pair in pins for pin in pair if pin} == {omerta.__version__}


@contextlib.contextmanager
def _script(name: str) -> Iterator[ModuleType]:
    """A module of scripts/, imported for the length of a test."""
    spec = importlib.util.spec_from_file_location(name, _ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        del sys.modules[name]


@pytest.fixture
def date_release() -> Iterator[Callable[..., tuple[str, str]]]:
    """The dating function of scripts/date_release.py."""
    with _script("date_release") as module:
        yield module.date_release


@pytest.fixture
def release_notes() -> Iterator[ModuleType]:
    """scripts/release_notes.py, which the publish workflow runs."""
    with _script("release_notes") as module:
        yield module


_MENTIONS = (
    "Changes go under [Unreleased], see `## [Unreleased]`.\n"
    "[Unreleased] entries are dated by `cz bump`.\n\n"
)


def test_the_first_release_links_to_its_tag_and_adds_its_date(date_release):
    changelog = (
        f"# Changelog\n\n{_MENTIONS}"
        "## [Unreleased]\n\n### Added\n\n- A change, once in [Unreleased]\n\n"
        f"[Unreleased]: {_REPOSITORY}/commits/dev\n"
    )
    citation = "cff-version: 1.2.0\nversion: 0.1.0\nlicense: MIT\n"
    dated, cited = date_release(changelog, citation, "0.1.0", "v0.1.0", "2026-11-02", "0.0.0")
    assert dated == (
        f"# Changelog\n\n{_MENTIONS}"
        "## [0.1.0] - 2026-11-02\n\n### Added\n\n- A change, once in [Unreleased]\n\n"
        f"[0.1.0]: {_REPOSITORY}/releases/tag/v0.1.0\n"
    )
    assert cited == "cff-version: 1.2.0\nversion: 0.1.0\ndate-released: 2026-11-02\nlicense: MIT\n"
    # Run again after a stopped bump, it names and dates the section again.
    again, cited_again = date_release(dated, cited, "0.1.1", "v0.1.1", "2026-11-03", "0.0.0")
    assert again == dated.replace("0.1.0", "0.1.1").replace("2026-11-02", "2026-11-03")
    assert cited_again == cited.replace("2026-11-02", "2026-11-03")


def test_a_later_release_points_its_compare_link_at_the_new_tag(date_release):
    changelog = (
        "# Changelog\n\n"
        "## [Unreleased]\n\n- A change\n\n## [0.1.0] - 2026-11-02\n\n- B\n\n"
        f"[Unreleased]: {_COMPARE}v0.1.0...HEAD\n"
        f"[0.1.0]: {_REPOSITORY}/releases/tag/v0.1.0\n"
    )
    citation = "version: 0.2.0\ndate-released: 2026-11-02\n"
    dated, cited = date_release(changelog, citation, "0.2.0", "v0.2.0", "2026-11-09", "0.1.0")
    assert dated == changelog.replace("## [Unreleased]", "## [0.2.0] - 2026-11-09").replace(
        f"[Unreleased]: {_COMPARE}v0.1.0...HEAD", f"[0.2.0]: {_COMPARE}v0.1.0...v0.2.0"
    )
    assert cited == "version: 0.2.0\ndate-released: 2026-11-09\n"


@pytest.mark.parametrize(
    ("sections", "links", "citation", "error"),
    [
        # Nothing above the released version
        ("", "", "version: 0.2.0\n", r"## \[Unreleased\]"),
        # Headings only: nothing to release
        ("## [Unreleased]\n\n### Added\n\n", "", "version: 0.2.0\n", "is empty"),
        # A section twice, e.g. after a merge
        ("## [Unreleased]\n\n- A\n\n## [Unreleased]\n\n- B\n\n", "", "version: 0.2.0\n", "more"),
        # A line that only looks like the compare link
        ("## [Unreleased]\n\n- A\n\n", "[Unreleased]: notes...below\n", "version: 0.2.0\n", "no"),
        # A citation file without its version
        ("## [Unreleased]\n\n- A\n\n", f"[Unreleased]: {_COMPARE}v0.1.0...HEAD\n", "x: 1\n", "no"),
    ],
)
def test_a_bump_stops_on_a_changelog_or_citation_it_cannot_date(
    date_release, sections: str, links: str, citation: str, error: str
):
    changelog = (
        f"# Changelog\n\n{sections}## [0.1.0] - 2026-11-02\n\n- B\n\n{links}"
        f"[0.1.0]: {_REPOSITORY}/releases/tag/v0.1.0\n"
    )
    with pytest.raises(ValueError, match=error):
        date_release(changelog, citation, "0.2.0", "v0.2.0", "2026-11-09", "0.1.0")


def test_each_release_has_the_notes_of_its_github_release(release_notes):
    # The publish workflow takes them before it uploads anything, so a version
    # without notes would stop its release.
    versions = re.findall(r"^## \[(.+?)\] - \d{4}-\d{2}-\d{2}$", _CHANGELOG, re.MULTILINE)
    for version in versions:
        notes = release_notes.release_notes(_CHANGELOG, version)
        assert notes in _CHANGELOG
        assert notes == notes.strip() + "\n"
        assert not re.search(r"^## \[|^\[[^\]]+\]: ", notes, re.MULTILINE)


_RELEASES = (
    f"# Changelog\n\n{_MENTIONS}"
    "## [Unreleased]\n\n- C\n\n"
    "## [0.2.0] - 2026-11-09\n\n### Fixed\n\n- B — not in [0.1.0]\n\n"
    "## [0.1.0] - 2026-11-02\n\n### Added\n\n- A\n\n"
    f"[Unreleased]: {_COMPARE}v0.2.0...HEAD\n"
    f"[0.2.0]: {_COMPARE}v0.1.0...v0.2.0\n"
    f"[0.1.0]: {_REPOSITORY}/releases/tag/v0.1.0\n"
)


def test_the_notes_of_a_release_are_its_section(release_notes):
    # Up to the next section, or for the oldest, up to the links
    assert release_notes.release_notes(_RELEASES, "0.2.0") == "### Fixed\n\n- B — not in [0.1.0]\n"
    assert release_notes.release_notes(_RELEASES, "0.1.0") == "### Added\n\n- A\n"


@pytest.mark.parametrize(
    ("changelog", "version", "error"),
    [
        # Not released
        (_RELEASES, "0.3.0", "no dated section"),
        # Not yet named and dated by `cz bump`
        (_RELEASES, "Unreleased", "no dated section"),
        # Only the start of a version
        (_RELEASES, "0.2", "no dated section"),
        # The dots are dots
        (_RELEASES, "0x2x0", "no dated section"),
        # Headings only
        (
            _RELEASES.replace("### Fixed\n\n- B — not in [0.1.0]\n\n", "### Fixed\n\n"),
            "0.2.0",
            "empty",
        ),
    ],
)
def test_a_release_without_notes_stops(release_notes, changelog: str, version: str, error: str):
    with pytest.raises(ValueError, match=error):
        release_notes.release_notes(changelog, version)


def test_the_script_prints_the_notes_in_utf8_or_says_why_not(
    release_notes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsysbinary
):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_bytes(_RELEASES.encode("utf-8"))
    monkeypatch.setattr(release_notes, "_CHANGELOG", changelog)
    assert release_notes.main(["0.2.0"]) == 0
    assert capsysbinary.readouterr() == ("### Fixed\n\n- B — not in [0.1.0]\n".encode(), b"")
    assert release_notes.main(["0.3.0"]) == 1
    out, err = capsysbinary.readouterr()
    assert out == b""
    assert b"no dated section" in err
    assert release_notes.main([]) == 1
    assert b"Usage" in capsysbinary.readouterr().err
