import pytest

from app.llm.json_utils import parse_json_object


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('{"a": 1}', {"a": 1}),
        ('  {"a": 1}\n', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('```\n{"a": 1}\n```', {"a": 1}),
        ('Sure! Here is the JSON:\n{"a": {"b": [1, 2]}}\nHope that helps.', {"a": {"b": [1, 2]}}),
        ('{"text": "braces } inside { strings"}', {"text": "braces } inside { strings"}),
        ('{"q": "escaped \\" quote }"}', {"q": 'escaped " quote }'}),
        ('noise { not json } then {"ok": true}', {"ok": True}),
    ],
)
def test_parses_messy_llm_output(raw, expected):
    assert parse_json_object(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "no json here", "[1, 2, 3]", '{"unterminated": ', "```json\n```"])
def test_returns_none_for_unusable_output(raw):
    assert parse_json_object(raw) is None
