"""Env file patterns: their checks, and how they match paths."""

from __future__ import annotations

import pytest

from omerta.errors import OmertaError
from omerta.patterns import DEFAULT_PATTERNS, Pattern, is_env_file, is_template


@pytest.mark.parametrize(
    ("pattern", "path", "matches"),
    [
        (".env", ".env", True),
        (".env", "app/.env", False),
        (".env", ".ENV", False),
        (".env.*", ".env.local", True),
        (".env.*", ".env", False),
        (".env.*", "app/.env.local", False),
        ("app/.env", "app/.env", True),
        ("*/.env", "app/.env", True),
        ("*/.env", "app/sub/.env", False),
        ("?/.env", "a/.env", True),
        ("?/.env", "ab/.env", False),
        ("**/.env", ".env", True),
        ("**/.env", "a/b/c/.env", True),
        ("**/.env", "a/b/c/.envrc", False),
        ("apps/**/.env.*", "apps/web/.env.local", True),
        ("apps/**/.env.*", "apps/.env.local", True),
        ("apps/**/.env.*", "lib/.env.local", False),
        ("secrets/**", "secrets/a/b.txt", True),
        ("secrets/**", "secrets", True),
        ("[.]env", ".env", True),
    ],
)
def test_a_pattern_is_matched_part_by_part(pattern: str, path: str, matches: bool):
    assert Pattern(pattern).matches(path.split("/")) is matches


@pytest.mark.parametrize(
    ("pattern", "problem"),
    [
        ("", "empty"),
        ("/.env", "absolute"),
        ("C:/repo/.env", "absolute"),
        ("config\\.env", "backslash"),
        ("config//.env", "empty or '.'"),
        ("./.env", "empty or '.'"),
        ("config/", "empty or '.'"),
        ("../.env", "'..'"),
        (".env**", "'\\*\\*' within a part"),
        ("a**/.env", "'\\*\\*' within a part"),
    ],
)
def test_a_pattern_that_cannot_be_matched_from_the_root_is_an_error(pattern: str, problem: str):
    with pytest.raises(OmertaError, match=problem):
        Pattern(pattern)


@pytest.mark.parametrize(
    ("name", "template"),
    [
        (".env.example", True),
        (".env.local.example", True),
        (".env.sample", True),
        (".env.template", True),
        (".env.EXAMPLE", False),
        (".env", False),
        (".env.local", False),
    ],
)
def test_templates_are_known_by_their_names(name: str, template: bool):
    assert is_template(name) is template


def test_an_env_file_matches_a_pattern_and_is_no_template():
    patterns = tuple(Pattern(text) for text in DEFAULT_PATTERNS)
    assert is_env_file(b".env", patterns)
    assert is_env_file(b".env.production", patterns)
    assert not is_env_file(b".env.example", patterns)
    assert not is_env_file(b"app/.env", patterns)
    assert not is_env_file(b".envrc", patterns)
    # A path that is not UTF-8 is matched by its other characters.
    assert is_env_file(b".env.\xff", patterns)
