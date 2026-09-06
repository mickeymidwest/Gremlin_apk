"""Pull one JSON object out of a model's reply.

Weak local models wrap JSON in ```json fences, add a sentence before or
after it, or emit a stray brace in prose first. A single greedy
``\\{.*\\}`` regex breaks on all but the cleanest output. This scans
every ``{`` offset with json.raw_decode and returns the first that
parses to a dict -- fences and trailing prose fall away for free.
"""
from __future__ import annotations

import json

_DECODER = json.JSONDecoder()


def extract_json(text: str) -> dict:
    if not text:
        return {}
    s = str(text)
    start = 0
    while True:
        i = s.find("{", start)
        if i == -1:
            return {}
        try:
            obj, _ = _DECODER.raw_decode(s, i)
        except ValueError:
            start = i + 1
            continue
        if isinstance(obj, dict):
            return obj
        start = i + 1
