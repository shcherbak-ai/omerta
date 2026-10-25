"""The settings of a run: the defaults, the configuration file, and the options."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omerta.errors import OmertaError
from omerta.patterns import DEFAULT_PATTERNS, Pattern

DEFAULT_MIN_CHARS = 8

_KEYS = ("env-files", "min-chars", "ignore", "encoded")


@dataclass(frozen=True)
class Settings:
    """What a run searches for: the env files, and which of their values."""

    patterns: tuple[Pattern, ...]
    min_chars: int
    ignore: frozenset[str]
    encoded: bool


def load(
    top: Path,
    *,
    env_files: list[str] | None = None,
    min_chars: int | None = None,
    ignore: list[str] | None = None,
    encoded: bool | None = None,
) -> Settings:
    """The settings, from the options given, the configuration, and the defaults.

    An option overrides the setting of the same name in the configuration,
    which overrides the default, except ``ignore``, whose names are added to
    the configured ones.

    Args:
        top: The root of the repository, where the configuration file is.
        env_files: The env file patterns of the command line, if any.
        min_chars: The ``--min-chars`` of the command line, if given.
        ignore: The variable names of the command line.
        encoded: ``--encoded`` or ``--no-encoded``, if given.

    Raises:
        OmertaError: The configuration cannot be read or has a wrong setting.
    """
    configured = _read(top)
    patterns = env_files or configured.get("env-files") or list(DEFAULT_PATTERNS)
    return Settings(
        patterns=tuple(Pattern(text) for text in patterns),
        min_chars=min_chars or configured.get("min-chars", DEFAULT_MIN_CHARS),
        ignore=frozenset([*configured.get("ignore", []), *(ignore or [])]),
        encoded=configured.get("encoded", True) if encoded is None else encoded,
    )


def _read(top: Path) -> dict[str, Any]:
    """The settings of ``[tool.omerta]`` in ``pyproject.toml`` or of ``.omerta.toml``."""
    pyproject = _toml(top / "pyproject.toml")
    tool = pyproject.get("tool") if pyproject is not None else None
    in_pyproject = tool.get("omerta") if isinstance(tool, dict) else None
    standalone = _toml(top / ".omerta.toml")
    if in_pyproject is not None and standalone is not None:
        raise OmertaError(
            "settings are in both pyproject.toml ([tool.omerta]) and .omerta.toml: keep one"
        )
    if in_pyproject is not None:
        if not isinstance(in_pyproject, dict):
            raise OmertaError("pyproject.toml: tool.omerta must be a table")
        return _validate(in_pyproject, "pyproject.toml [tool.omerta]")
    if standalone is not None:
        return _validate(standalone, ".omerta.toml")
    return {}


def _toml(path: Path) -> dict[str, Any] | None:
    """The document of a TOML file, or ``None`` where there is no such file."""
    if not path.is_file():
        return None
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise OmertaError(f"{path.name} is not UTF-8") from None
    except OSError as error:
        raise OmertaError(f"{path.name} cannot be read ({error.strerror})") from None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise OmertaError(f"{path.name}: {error}") from None


def _validate(table: dict[str, Any], source: str) -> dict[str, Any]:
    """The settings of a configuration table, checked key by key."""
    for key, value in table.items():
        if key not in _KEYS:
            raise OmertaError(f"{source}: unknown setting {key!r}")
        if key == "env-files" and not (_strings(value) and value):
            raise OmertaError(f"{source}: env-files must be a non-empty list of patterns")
        if key == "min-chars" and (type(value) is not int or value < 1):
            raise OmertaError(f"{source}: min-chars must be an integer of at least 1")
        if key == "ignore" and not _strings(value):
            raise OmertaError(f"{source}: ignore must be a list of variable names")
        if key == "encoded" and type(value) is not bool:
            raise OmertaError(f"{source}: encoded must be true or false")
    for pattern in table.get("env-files", []):
        try:
            Pattern(pattern)
        except OmertaError as error:
            raise OmertaError(f"{source}: {error}") from None
    return table


def _strings(value: object) -> bool:
    """Whether a value is a list of strings."""
    return isinstance(value, list) and all(isinstance(item, str) for item in value)
