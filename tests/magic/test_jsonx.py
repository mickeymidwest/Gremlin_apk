"""extract_json survives the shapes weak local models actually emit."""
from gremlin_core.magic._jsonx import extract_json


def test_plain_object():
    assert extract_json('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}


def test_fenced_and_trailing_prose():
    raw = 'Sure, here it is:\n```json\n{"choice": "card", "reason": "niche"}\n```\nLet me know.'
    assert extract_json(raw) == {"choice": "card", "reason": "niche"}


def test_leading_stray_brace_in_prose():
    # a greedy \{.*\} regex would start here and fail
    raw = 'The set {a, b} then the JSON: {"accept": true, "reason": "ok"}'
    assert extract_json(raw) == {"accept": True, "reason": "ok"}


def test_nested_object():
    raw = 'x {"proposals": [{"kind": "new_fact", "text": "y"}], "diagnosis": "d"} z'
    got = extract_json(raw)
    assert got["diagnosis"] == "d" and got["proposals"][0]["text"] == "y"


def test_no_json_or_empty():
    assert extract_json("no json here") == {}
    assert extract_json("") == {}
    assert extract_json(None) == {}


def test_skips_non_dict_json():
    assert extract_json('[1,2,3] but really {"a": 1}') == {"a": 1}
