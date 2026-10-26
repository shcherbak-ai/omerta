"""The forms of a value: plain, and encoded in URLs, JSON and base64."""

from __future__ import annotations

import base64
import json
import urllib.parse

from hypothesis import given
from hypothesis import strategies as st

from omerta.forms import forms

_KEY = "sk-test-0123456789abcdef"


def test_a_key_of_letters_and_digits_has_its_plain_and_base64_forms():
    found = forms(_KEY, encoded=True, min_chars=8)
    assert found[_KEY] == "plain"
    assert set(found.values()) == {"plain", "base64"}
    # The URL and JSON forms of such a key are the key itself.
    assert urllib.parse.quote(_KEY, safe="") in found


def test_no_encoded_keeps_the_plain_forms_only():
    value = "https://user:p@ss w0rd@db.example.com/"
    assert forms(value, encoded=False, min_chars=8) == {
        value: "plain",
        value[:-1]: "plain",
    }


def test_a_multi_line_value_is_also_searched_with_crlf_line_ends():
    found = forms("-----BEGIN KEY-----\nabc\n-----END KEY-----", encoded=False, min_chars=8)
    assert list(found) == [
        "-----BEGIN KEY-----\nabc\n-----END KEY-----",
        "-----BEGIN KEY-----\r\nabc\r\n-----END KEY-----",
    ]


def test_url_forms_are_percent_encoded_as_a_path_and_a_query_in_both_cases():
    found = forms("p@ss w/rd+é", encoded=True, min_chars=8)
    for form in (
        "p%40ss%20w%2Frd%2B%C3%A9",
        "p%40ss%20w%2frd%2b%c3%a9",
        "p%40ss+w%2Frd%2B%C3%A9",
        "p%40ss+w%2frd%2b%c3%a9",
    ):
        assert found[form] == "url"


def test_json_forms_escape_as_json_does_with_and_without_ascii_and_slashes():
    value = 'pa"ss/wörd'
    found = forms(value, encoded=True, min_chars=8)
    assert found[json.dumps(value)[1:-1]] == "json"  # pa\"ss/wörd
    assert found[json.dumps(value, ensure_ascii=False)[1:-1]] == "json"
    assert found['pa\\"ss\\/w\\u00f6rd'] == "json"
    assert found['pa\\"ss\\/wörd'] == "json"


# A min-chars and a value at least that long. min-chars starts at 2 here: a
# value of one byte has no base64 character of its own at one alignment.
_SHORTEST_AND_VALUE = st.integers(2, 12).flatmap(
    lambda shortest: st.tuples(st.just(shortest), st.text(min_size=shortest, max_size=40))
)


@given(_SHORTEST_AND_VALUE, st.binary(max_size=8), st.binary(max_size=8))
def test_a_base64_form_is_found_inside_any_longer_encoding(
    shortest_and_value: tuple[int, str], before: bytes, after: bytes
):
    shortest, value = shortest_and_value
    found = forms(value, encoded=True, min_chars=shortest)
    data = before + value.encode() + after
    standard = base64.b64encode(data).decode()
    url_safe = base64.urlsafe_b64encode(data).decode()
    assert any(form in standard and kind == "base64" for form, kind in found.items())
    assert any(form in url_safe and kind == "base64" for form, kind in found.items())


def _plain_forms(value: str) -> set[str]:
    """The plain forms of a value, by their definition: the oracle of the test below."""
    plain = {value, value.replace("\n", "\r\n")}
    if "://" in value and value.endswith("/"):
        plain.add(value[:-1])
    return plain


@given(st.text(min_size=8, max_size=40))
def test_every_form_stands_for_the_value_and_decodes_to_a_plain_form(value: str):
    # A form that decoded to anything else would report text that is not the
    # user's value.
    found = forms(value, encoded=True, min_chars=8)
    plain = _plain_forms(value)
    assert {form for form, kind in found.items() if kind == "plain"} == {
        form for form in plain if len(form) >= 8
    }
    for form, kind in found.items():
        if kind == "url":
            assert urllib.parse.unquote_plus(form) in plain
        elif kind == "json":
            assert json.loads(f'"{form}"') in plain


def test_forms_shorter_than_min_chars_are_left_out_and_duplicates_kept_once():
    found = forms("abcdefgh", encoded=True, min_chars=8)
    # Each kind's form of a plain value is the value itself, kept as plain.
    assert found["abcdefgh"] == "plain"
    assert all(len(form) >= 8 for form in found)
    assert forms("abcdefgh", encoded=True, min_chars=40) == {}


def test_a_url_is_also_searched_without_one_trailing_slash():
    found = forms("https://res.example.com//", encoded=False, min_chars=8)
    assert list(found) == ["https://res.example.com//", "https://res.example.com/"]
    assert list(forms("no-url-here/", encoded=False, min_chars=8)) == ["no-url-here/"]
