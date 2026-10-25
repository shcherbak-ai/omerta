"""Check that omerta installs from wheels alone on each platform and Python it supports.

A dependency without a wheel for a platform is built from source there, which
needs a compiler and the library's own build tools (hyperscan: a C toolchain
and CMake), and fails where they are missing: the user cannot install omerta.
The check resolves the dependencies of ``pyproject.toml`` with wheels only, for
each platform of ``PLATFORMS`` and each Python of the classifiers, under the
cooldown of ``[tool.uv] exclude-newer``. It fails on a gap that
``KNOWN_GAPS`` does not list, and on a listed gap that has closed, so that the
README's note on it goes too.

The dependencies are pinned exactly, so it checks the versions users install.
It runs as a pre-commit hook where a commit changes ``pyproject.toml`` or
``uv.lock``, and in CI at every push. Run directly::

    uv run python scripts/check_wheels.py
"""

from __future__ import annotations

import os
import re
import shutil

# uv by the absolute path found, with arguments of its own and no shell.
import subprocess  # nosec B404
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The platforms whose users omerta has: glibc and musl Linux, macOS and Windows,
# on x86-64 and ARM.
PLATFORMS = (
    "x86_64-unknown-linux-gnu",
    "aarch64-unknown-linux-gnu",
    "x86_64-unknown-linux-musl",
    "aarch64-unknown-linux-musl",
    "x86_64-apple-darwin",
    "aarch64-apple-darwin",
    "x86_64-pc-windows-msvc",
    "aarch64-pc-windows-msvc",
)

# The gaps known, by platform, on every Python there, with what the README tells
# their users.
KNOWN_GAPS = {
    "aarch64-pc-windows-msvc": (
        "hyperscan has no wheels for Windows on ARM: the README says to use an x64 Python there"
    ),
}

# A current macOS: for an older one, uv takes the wheels built for current
# releases as unusable, and reports gaps that are none.
MACOS = "15.0"


def _pythons() -> list[str]:
    """The Python versions of the classifiers of ``pyproject.toml``."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    found = [
        match[1]
        for classifier in project["classifiers"]
        if (match := re.fullmatch(r"Programming Language :: Python :: (3\.\d+)", classifier))
    ]
    return sorted(found, key=lambda version: int(version.split(".")[1]))


def _installs(uv: str, platform: str, python: str) -> bool:
    """Whether the dependencies resolve from wheels alone for a platform and a Python."""
    # uv by the absolute path found, with arguments of its own
    result = subprocess.run(  # noqa: S603  # nosec B603
        [
            uv,
            "pip",
            "compile",
            "pyproject.toml",
            "--only-binary",
            ":all:",
            "--python-platform",
            platform,
            "--python-version",
            python,
            "--quiet",
            "--no-header",
        ],
        cwd=ROOT,
        env={**os.environ, "MACOSX_DEPLOYMENT_TARGET": MACOS},
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def main() -> int:
    """Run the check; 1 when it fails, else 0."""
    uv = shutil.which("uv")
    if uv is None:
        print("uv is not on PATH.")
        return 1
    pythons = _pythons()
    gaps = []
    closed = []
    for platform in PLATFORMS:
        missing = [python for python in pythons if not _installs(uv, platform, python)]
        if platform in KNOWN_GAPS:
            if len(missing) < len(pythons):
                closed.append(f"{platform}: installs on {len(pythons) - len(missing)} Pythons now")
        elif missing:
            gaps.append(f"{platform}: Python {', '.join(missing)}")
    for title, lines in {
        "No wheels, so no install without a compiler": gaps,
        "KNOWN_GAPS that have closed: remove them, and the README's note": closed,
    }.items():
        if lines:
            print(f"{title}:")
            for line in lines:
                print(f"  {line}")
    if gaps or closed:
        return 1
    print(
        f"omerta installs from wheels on {len(PLATFORMS) - len(KNOWN_GAPS)} platforms and "
        f"Python {', '.join(pythons)}, as KNOWN_GAPS has it."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
