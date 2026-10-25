"""The changelog, the citation file and the version fields agree, and a bump dates them."""

from __future__ import annotations

import importlib.util
import itertools
import re
import sys
import tomllib
from collections.abc import Callable, Iterator
from pathlib import Path

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


@pytest.fixture
def date_release() -> Iterator[Callable[..., tuple[str, str]]]:
    """The dating function of scripts/date_release.py."""
    spec = importlib.util.spec_from_file_location(
        "date_release", _ROOT / "scripts" / "date_release.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["date_release"] = module
    try:
        spec.loader.exec_module(module)
        yield module.date_release
    finally:
        del sys.modules["date_release"]


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
