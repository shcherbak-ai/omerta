"""Pre-commit check: new releases stay out of the lockfiles for a week, and no
locked version, nor uv, has an advisory at osv.dev.

A release enters the lockfiles only ``COOLDOWN_DAYS`` after its upload, when a
compromised one has been found and removed. uv takes the cutoff from
``[tool.uv] exclude-newer`` of ``pyproject.toml``, npm from ``min-release-age``
of ``.npmrc``, and the check fails when either is missing or is another span.
While the newest version of ``uv.lock`` is younger than the cooldown,
``exclude-newer`` can be a fixed date, past which nothing enters ``uv.lock``:
the check warns of it until every version of ``uv.lock`` is older than the
cooldown, and then fails until it is set to the span. A security fix taken
inside the cooldown is an ``exclude-newer-package`` entry with the date of its
upload: the check warns of it while the cooldown would hold the fix out, and
then fails until it is removed.

It asks osv.dev for the advisories of each version of ``uv.lock`` and
``package-lock.json``, of the uv that the workflows pin (``UV_VERSION``), and
of the uv that runs here. A known vulnerability or a report of a malicious
package fails the check, but for the advisories of ``ACCEPTED_ADVISORIES``,
with their reasons. A report of a malicious package (``MAL-``) is never
accepted, and an accepted advisory of no checked version fails the check. An
advisory accepted while its fix waits out the cooldown carries the fix's
upload: the check warns of it until the cooldown admits the fix, and then
fails until the entry is removed.

It runs at each commit, as an advisory can come out between two commits. CI
skips it (``SKIP``), where its result would depend on the day rather than on
the commit. Offline: ``SKIP=advisories git commit``.

Run directly::

    uv run python scripts/check_advisories.py
"""

from __future__ import annotations

import datetime
import json
import re
import shutil

# uv runs by the absolute path found, with an argument of its own and no shell.
import subprocess  # nosec B404
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# How many days a release stays out of the lockfiles after its upload:
# `[tool.uv] exclude-newer` of pyproject.toml and `min-release-age` of .npmrc.
COOLDOWN_DAYS = 7

# The advisories of checked versions that the check accepts, by their IDs at
# osv.dev, each with its reason: a vulnerability that no version the ranges
# admit fixes. Where the fix waits out the cooldown, the entry also carries its
# upload, in UTC: the check fails once the cooldown admits it, so that the fix
# is taken and the entry removed. A report of a malicious package (MAL-) is
# never accepted.
_MARKDOWNLINT_0_24 = (
    "a slow parse of the repository's own Markdown or TOML in markdownlint-cli2, fixed in "
    "0.24.0: raise its range in package.json",
    "2026-10-21T06:59:51Z",
)
ACCEPTED_ADVISORIES: dict[str, tuple[str, str | None]] = {
    "GHSA-238p-pmpm-9mq7": (
        "katex, through markdownlint: low severity, fixed in 0.18.2 only, which "
        "micromark-extension-math (^0.16.0) does not admit",
        None,
    ),
    "GHSA-gfr2-rv44-r23c": _MARKDOWNLINT_0_24,
    "GHSA-r7fv-28h4-cvq7": _MARKDOWNLINT_0_24,
    "GHSA-vfgh-jcmw-g37q": _MARKDOWNLINT_0_24,
    "GHSA-r4xh-jqrq-34v2": _MARKDOWNLINT_0_24,
}

# The batch query of osv.dev, for the advisories of many versions at once.
OSV_QUERY_BATCH = "https://api.osv.dev/v1/querybatch"

# The uv a workflow pins: `UV_VERSION: "0.13.4"`.
_UV_PIN = re.compile(r"""^\s*UV_VERSION:\s*["']?([0-9][^"'\s]*)["']?\s*$""", re.MULTILINE)


class _CheckError(Exception):
    """The check cannot run: a file or the answer of osv.dev cannot be read."""


def _is_date(value: object) -> bool:
    """Whether an ``exclude-newer`` value is a fixed date, not a span."""
    return isinstance(value, str) and re.match(r"\d{4}-\d{2}-\d{2}", value) is not None


def _newest_upload(lock: dict, exempt: dict) -> datetime.datetime:
    """When the newest file of ``uv.lock`` was uploaded, in UTC, but for the
    packages that ``exclude-newer-package`` exempts from the cooldown."""
    times = [
        file["upload-time"]
        for package in lock["package"]
        if package["name"] not in exempt
        for file in [*package.get("wheels", []), package.get("sdist", {})]
        if "upload-time" in file
    ]
    if not times:
        raise _CheckError("uv.lock holds no upload times.")
    return max(datetime.datetime.fromisoformat(time) for time in times)


def _cooldown(pyproject: dict, lock: dict, now: datetime.datetime) -> tuple[list[str], list[str]]:
    """Check that new releases stay out of the lockfiles for ``COOLDOWN_DAYS``.

    Returns what fails the check, and what it warns of.
    """
    span = f"{COOLDOWN_DAYS} days"
    cooldown = datetime.timedelta(days=COOLDOWN_DAYS)
    findings = []
    warnings = []
    settings = pyproject.get("tool", {}).get("uv", {})
    cutoff = settings.get("exclude-newer")
    exempt = settings.get("exclude-newer-package", {})
    for name, until in exempt.items():
        if not _is_date(until):
            findings.append(
                f"pyproject.toml: [tool.uv.exclude-newer-package] {name} = {until!r}: "
                "give it the date of the upload of the fix"
            )
            continue
        moment = datetime.datetime.fromisoformat(until)
        # uv reads a date without an offset as UTC.
        moment = moment.replace(tzinfo=moment.tzinfo or datetime.UTC).astimezone(datetime.UTC)
        due = moment + cooldown
        if now >= due:
            findings.append(
                f"pyproject.toml: [tool.uv.exclude-newer-package] {name}: the cooldown "
                "admits the fix now: remove the entry"
            )
        else:
            warnings.append(
                f"pyproject.toml: [tool.uv.exclude-newer-package] exempts {name} from the "
                f"cooldown up to {until}: remove the entry from {due:%Y-%m-%d %H:%M} UTC"
            )
    if cutoff is None:
        findings.append(f'pyproject.toml: set [tool.uv] exclude-newer = "{span}"')
    elif _is_date(cutoff):
        # A fixed date, until the newest version of uv.lock is as old as the
        # cooldown, from when the span changes nothing in uv.lock.
        due = _newest_upload(lock, exempt) + cooldown
        if now >= due:
            findings.append(
                f"pyproject.toml: [tool.uv] exclude-newer is the fixed date {cutoff}, "
                f'and every version of uv.lock is older than {span}: set it to "{span}"'
            )
        else:
            warnings.append(
                f"pyproject.toml: [tool.uv] exclude-newer is the fixed date {cutoff}, "
                f'past which nothing enters uv.lock: set it to "{span}" from '
                f"{due:%Y-%m-%d %H:%M} UTC"
            )
    elif cutoff != span:
        findings.append(
            f'pyproject.toml: [tool.uv] exclude-newer is "{cutoff}": set it to "{span}"'
        )
    npmrc = ROOT / ".npmrc"
    text = npmrc.read_text(encoding="utf-8") if npmrc.is_file() else ""
    match = re.search(r"^\s*min-release-age\s*=\s*(\d+)\s*$", text, re.MULTILINE)
    if match is None or int(match[1]) != COOLDOWN_DAYS:
        findings.append(f".npmrc: set min-release-age={COOLDOWN_DAYS}")
    return findings, warnings


def _waiting_fixes(now: datetime.datetime) -> tuple[list[str], list[str]]:
    """Check the accepted advisories whose fixes wait out the cooldown.

    Returns what fails the check, a fix that the cooldown admits now, and what
    it warns of, a fix that it does not admit yet.
    """
    cooldown = datetime.timedelta(days=COOLDOWN_DAYS)
    waiting: dict[tuple[str, str], list[str]] = {}
    for advisory, (reason, upload) in ACCEPTED_ADVISORIES.items():
        if upload is not None:
            waiting.setdefault((upload, reason), []).append(advisory)
    findings = []
    warnings = []
    for (upload, reason), advisories in waiting.items():
        moment = datetime.datetime.fromisoformat(upload)
        due = moment.replace(tzinfo=moment.tzinfo or datetime.UTC) + cooldown
        listed = ", ".join(advisories)
        if now >= due:
            findings.append(f"{listed}: the cooldown admits the fix now ({reason}): remove them")
        else:
            warnings.append(
                f"ACCEPTED_ADVISORIES accepts {listed} until the cooldown admits the fix, from "
                f"{due:%Y-%m-%d %H:%M} UTC: {reason}"
            )
    return findings, warnings


def _uv_versions() -> set[str]:
    """The uv versions in use: those the workflows pin, and the one that runs here."""
    versions = set()
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        versions.update(_UV_PIN.findall(workflow.read_text(encoding="utf-8")))
    uv = shutil.which("uv")
    if uv is not None:
        # uv by the absolute path found, with an argument of its own
        output = subprocess.run(  # noqa: S603  # nosec B603
            [uv, "--version"], capture_output=True, text=True, check=False
        ).stdout
        if match := re.match(r"uv (\S+)", output):
            versions.add(match[1])
    return versions


def _checked_packages(lock: dict) -> list[tuple[str, str, str]]:
    """The ``(ecosystem, name, version)`` of each checked version: the registry
    packages of ``uv.lock``, uv, and the packages of ``package-lock.json``."""
    python = {
        (package["name"], package["version"])
        for package in lock["package"]
        if "registry" in package.get("source", {})
    }
    python.update(("uv", version) for version in _uv_versions())
    entries = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))["packages"]
    npm = {
        (path.rsplit("node_modules/", 1)[-1], entry["version"])
        for path, entry in entries.items()
        if path and "version" in entry and not entry.get("link")
    }
    return [("PyPI", *package) for package in sorted(python)] + [
        ("npm", *package) for package in sorted(npm)
    ]


def _advisories(packages: list[tuple[str, str, str]]) -> tuple[list[str], set[str]]:
    """Ask osv.dev for the advisories of the checked versions: known
    vulnerabilities, and reports of malicious packages.

    Returns each version with advisories that ``ACCEPTED_ADVISORIES`` does not
    accept, and the IDs of all the advisories found.
    """
    queries = [
        {"package": {"ecosystem": ecosystem, "name": name}, "version": version}
        for ecosystem, name, version in packages
    ]
    request = urllib.request.Request(  # noqa: S310 - the fixed https address of osv.dev
        OSV_QUERY_BATCH,
        data=json.dumps({"queries": queries}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        # The fixed https address of osv.dev
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310  # nosec B310
            results = json.load(response)["results"]
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as e:
        raise _CheckError(
            f"Cannot ask osv.dev for advisories: {e}. Offline: SKIP=advisories git commit"
        ) from None
    if len(results) != len(packages):
        raise _CheckError("osv.dev did not answer for each checked version.")
    found = []
    seen: set[str] = set()
    for (ecosystem, name, version), result in zip(packages, results, strict=True):
        ids = [vuln["id"] for vuln in result.get("vulns", [])]
        seen.update(ids)
        listed = [
            advisory
            for advisory in ids
            if advisory.startswith("MAL-") or advisory not in ACCEPTED_ADVISORIES
        ]
        # osv.dev sends the advisories of a version in pages.
        if result.get("next_page_token"):
            listed.append("and more, at osv.dev")
        if listed:
            found.append(f"{name} {version} ({ecosystem}): {', '.join(listed)}")
    return found, seen


def main() -> int:
    """Run the check; 1 when it fails, else 0."""
    try:
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
        now = datetime.datetime.now(datetime.UTC)
        cooldown_findings, warnings = _cooldown(pyproject, lock, now)
        fix_findings, fix_warnings = _waiting_fixes(now)
        warnings += fix_warnings
        advisories, seen = _advisories(_checked_packages(lock))
    except _CheckError as e:
        print(e)
        return 1

    for line in warnings:
        print(f"Warning: {line}")
        print()
    findings = {
        f"The cooldown: a release enters the lockfiles {COOLDOWN_DAYS} days after its upload": (
            cooldown_findings
        ),
        "Advisories of checked versions (osv.dev)": advisories,
        "ACCEPTED_ADVISORIES: advisories of no checked version, to remove": sorted(
            ACCEPTED_ADVISORIES.keys() - seen
        ),
        "ACCEPTED_ADVISORIES: fixes the cooldown admits, to take": fix_findings,
    }
    for title, lines in findings.items():
        if lines:
            print(f"{title}:")
            for line in lines:
                print(f"  {line}")
            print()
    if advisories:
        print(
            "An advisory: upgrade past it, or accept it in ACCEPTED_ADVISORIES with the "
            "reason, where no version that the ranges admit fixes it. A malicious package "
            "(MAL-): remove it, and treat each machine that installed it as compromised: "
            "rotate every credential it could reach."
        )
        print()
    if any(findings.values()):
        return 1
    print(
        f"New releases stay out of the lockfiles for {COOLDOWN_DAYS} days, and no checked "
        "version has an advisory" + (", but for those accepted." if ACCEPTED_ADVISORIES else ".")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
