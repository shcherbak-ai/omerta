"""Prints the section of a version in the changelog, the notes of its GitHub release.

The publish workflow runs it in its build job, on the tagged commit and before
anything is published, with the version of the tag (``v0.2.0`` gives ``0.2.0``);
once PyPI has the release, its last job creates the GitHub release, named after
the tag, with what the script printed. The notes are the text under
``## [<version>] - <date>``, up to the next section or the links at the end of
the file. A version without a dated section, or with an empty one, stops the
release before it reaches PyPI.

Example:
    uv run python scripts/release_notes.py 0.1.1
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def release_notes(changelog: str, version: str) -> str:
    """Take the notes of a release from the text of the changelog.

    Args:
        changelog: The text of ``CHANGELOG.md``.
        version: The released version, e.g. ``0.2.0``.

    Returns:
        The text of the version's section, without its heading and the blank
        lines around it, ending in a newline.

    Raises:
        ValueError: The changelog has no dated section of the version, or the
            section is empty.
    """
    # Only whole lines are read, as scripts/date_release.py reads them: a section
    # heading at the start of a line, and the links at the end of the file. A
    # mention of a section in the text of an entry is neither.
    heading = re.search(
        rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}$", changelog, re.MULTILINE
    )
    if heading is None:
        raise ValueError(f"CHANGELOG.md has no dated section `## [{version}] - <date>`.")
    end = re.compile(r"^(?:## \[|\[[^\]]+\]: )", re.MULTILINE).search(changelog, heading.end())
    text = changelog[heading.end() : end.start() if end else len(changelog)]
    if not any(line.strip() and not line.startswith("#") for line in text.splitlines()):
        raise ValueError(f"The section [{version}] of CHANGELOG.md is empty.")
    return text.strip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    """Print the notes of the version that the one argument names.

    Args:
        argv: The arguments, by default those of the command line.

    Returns:
        0 when the notes are printed, else 1, which fails the workflow's step.
    """
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("Usage: release_notes.py VERSION, e.g. 0.2.0", file=sys.stderr)
        return 1
    try:
        notes = release_notes(_CHANGELOG.read_text(encoding="utf-8"), args[0])
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    # In UTF-8 with the changelog's line ends, whatever the console's encoding
    sys.stdout.buffer.write(notes.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
