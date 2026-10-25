"""The settings: defaults, the configuration files, and the options over them."""

from __future__ import annotations

from pathlib import Path

import pytest

from omerta import config
from omerta.errors import OmertaError


def _patterns(settings: config.Settings) -> list[str]:
    return [pattern.text for pattern in settings.patterns]


def test_the_defaults_hold_without_configuration(tmp_path: Path):
    settings = config.load(tmp_path)
    assert _patterns(settings) == [".env", ".env.*"]
    assert settings.min_chars == 8
    assert settings.ignore == frozenset()
    assert settings.encoded is True


def test_a_pyproject_without_tool_omerta_changes_nothing(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    assert config.load(tmp_path).min_chars == 8
    (tmp_path / "pyproject.toml").write_text("tool = 1\n", encoding="utf-8")
    assert config.load(tmp_path).min_chars == 8


@pytest.mark.parametrize(
    ("name", "header"), [("pyproject.toml", "[tool.omerta]\n"), (".omerta.toml", "")]
)
def test_settings_are_read_from_either_file(tmp_path: Path, name: str, header: str):
    (tmp_path / name).write_text(
        header + 'env-files = ["config/.env"]\nmin-chars = 12\nignore = ["HOST"]\n'
        "encoded = false\n",
        encoding="utf-8",
    )
    settings = config.load(tmp_path)
    assert _patterns(settings) == ["config/.env"]
    assert settings.min_chars == 12
    assert settings.ignore == {"HOST"}
    assert settings.encoded is False


def test_options_override_the_configuration_and_add_ignored_names(tmp_path: Path):
    (tmp_path / ".omerta.toml").write_text(
        'env-files = ["a/.env"]\nmin-chars = 12\nignore = ["HOST"]\nencoded = false\n',
        encoding="utf-8",
    )
    settings = config.load(
        tmp_path, env_files=["b/.env"], min_chars=4, ignore=["PORT"], encoded=True
    )
    assert _patterns(settings) == ["b/.env"]
    assert settings.min_chars == 4
    assert settings.ignore == {"HOST", "PORT"}
    assert settings.encoded is True


def test_settings_in_both_files_are_an_error(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[tool.omerta]\nmin-chars = 9\n", encoding="utf-8")
    (tmp_path / ".omerta.toml").write_text("min-chars = 10\n", encoding="utf-8")
    with pytest.raises(OmertaError, match="both pyproject.toml"):
        config.load(tmp_path)


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("colour = 1", "unknown setting 'colour'"),
        ("env-files = []", "env-files must be a non-empty list"),
        ('env-files = ".env"', "env-files must be a non-empty list"),
        ("env-files = [1]", "env-files must be a non-empty list"),
        ('env-files = ["/abs/.env"]', "is absolute"),
        ("min-chars = 0", "min-chars must be an integer"),
        ("min-chars = true", "min-chars must be an integer"),
        ('min-chars = "8"', "min-chars must be an integer"),
        ('ignore = "HOST"', "ignore must be a list"),
        ("ignore = [1]", "ignore must be a list"),
        ('encoded = "yes"', "encoded must be true or false"),
    ],
)
def test_a_wrong_setting_is_an_error_naming_the_file(tmp_path: Path, text: str, problem: str):
    (tmp_path / ".omerta.toml").write_text(text + "\n", encoding="utf-8")
    with pytest.raises(OmertaError, match=problem) as error:
        config.load(tmp_path)
    assert str(error.value).startswith(".omerta.toml: ")


def test_tool_omerta_must_be_a_table(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[tool]\nomerta = 1\n", encoding="utf-8")
    with pytest.raises(OmertaError, match="tool.omerta must be a table"):
        config.load(tmp_path)


def test_a_file_that_is_not_toml_or_not_utf8_is_an_error(tmp_path: Path):
    path = tmp_path / ".omerta.toml"
    path.write_text("min-chars = \n", encoding="utf-8")
    with pytest.raises(OmertaError, match=r"\.omerta\.toml: .*line 1"):
        config.load(tmp_path)
    path.write_bytes(b"ignore = ['\xff']\n")
    with pytest.raises(OmertaError, match="is not UTF-8"):
        config.load(tmp_path)


def test_a_file_that_cannot_be_read_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / ".omerta.toml").write_text("min-chars = 9\n", encoding="utf-8")

    def refuse(self: Path) -> bytes:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_bytes", refuse)
    with pytest.raises(OmertaError, match=r"\.omerta\.toml cannot be read \(Permission denied\)"):
        config.load(tmp_path)
